"""Validate exact installation ownership before removing any owned target."""

from dataclasses import dataclass, replace
import errno
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys

from . import desktop
from .commands import CommandRunner
from .compose import ComposeProject
from .config import AccountInfo, load_config, resolve_config_file
from .desktop import stop_desktop_runtime
from .installer import (InstallLayout, _check_link, _exists, _owned_directory, _verify_release,
                        installation_lock, _write_json, _sync_directory)
from .model import AppConfig
from .manifest import InstallManifest, InstallationRegistry, _read_metadata, validate_version
from .state import RuntimeState


@dataclass(frozen=True)
class UninstallReport:
    removed: tuple[Path, ...]
    retained: tuple[Path, ...]
    warnings: tuple[str, ...]


_LABEL = 'io.github.diborodin.claude-in-docker-linux.'


@dataclass(frozen=True)
class DockerResources:
    containers: tuple[str, ...]
    images: tuple[str, ...]
    networks: tuple[str, ...]
    volumes: tuple[str, ...]


def discover_labeled_resources(uid: int, runner: CommandRunner) -> DockerResources:
    """Require exact managed and host-owner labels, including on inspect."""
    if type(uid) is not int or uid < 0:
        raise ValueError('Resource owner must be a host UID')
    found = {}
    for kind in ('container', 'image', 'network', 'volume'):
        command = ['docker', kind, 'ls', '--quiet']
        if kind == 'container':
            command.append('--all')
        if kind != 'volume':
            command.append('--no-trunc')
        command += ['--filter', f'label={_LABEL}managed=true',
                    '--filter', f'label={_LABEL}owner-uid={uid}']
        output = runner.run(command, capture_output=True).stdout
        identifiers = []
        for identifier in dict.fromkeys(output.splitlines()):
            pattern = r'[A-Za-z0-9][A-Za-z0-9_.-]*' if kind == 'volume' else r'(?:sha256:)?[0-9a-f]{12,64}'
            if not re.fullmatch(pattern, identifier):
                raise ValueError(f'Invalid Docker {kind} identifier')
            try:
                records = json.loads(runner.run(['docker', kind, 'inspect', identifier],
                                                capture_output=True).stdout)
                if not isinstance(records, list) or len(records) != 1:
                    raise ValueError('Expected one inspected resource')
                record = records[0]
                if not isinstance(record, dict):
                    raise ValueError('Expected an inspected resource object')
                if kind in ('container', 'image') and not isinstance(record.get('Config'), dict):
                    raise ValueError(f'Cannot validate Docker {kind} ownership')
                identity = record['Name' if kind == 'volume' else 'Id']
                labels = record['Config'].get('Labels') if kind in ('container', 'image') else record.get('Labels')
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise ValueError(f'Cannot validate Docker {kind} ownership') from error
            if identity != identifier:
                raise ValueError(f'Docker {kind} identity changed during discovery')
            if (isinstance(labels, dict) and labels.get(_LABEL + 'managed') == 'true'
                    and labels.get(_LABEL + 'owner-uid') == str(uid)):
                identifiers.append(identifier)
        found[kind + 's'] = tuple(identifiers)
    return DockerResources(**found)


def _aliases(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(alias for path in paths
                              for alias in (Path(os.path.abspath(path)), path.resolve())))


def protected_user_paths(config: AppConfig) -> tuple[Path, ...]:
    """Retain defaults and every configured source, including disabled mounts."""
    home = Path(os.environ.get('HOME') or pwd.getpwuid(os.getuid()).pw_dir)
    return _aliases((home / '.claude', home / '.claude.json', home / '.config/Claude',
                     config.state_code.source, config.state_code_json.source,
                     config.state_desktop.source, *config.container.mounts))


def _inside_protected(target: Path, protected: tuple[Path, ...] | list[Path]) -> bool:
    # Unlinking an owned symlink removes the link itself, never its final target.
    aliases = (Path(os.path.abspath(target)), target.parent.resolve() / target.name)
    return any(alias == path or path in alias.parents for alias in aliases for path in protected)


@dataclass
class _PurgeProject:
    """Provide Desktop teardown with only inspected container IDs to stop."""
    resources: DockerResources
    runner: CommandRunner

    def stop_service(self, service: str) -> None:
        failures = []
        for identifier in self.resources.containers:
            try:
                self.runner.run(['docker', 'container', 'stop', identifier])
            except (OSError, subprocess.SubprocessError):
                failures.append(f'container stop failed: {identifier}')
        if failures:
            raise ValueError('; '.join(failures))


def _purge_resources(resources: DockerResources, runner: CommandRunner) -> list[str]:
    failures = []
    # Containers must release networks, volumes and image references first.
    for kind in ('container', 'network', 'volume', 'image'):
        for identifier in getattr(resources, kind + 's'):
            try:
                runner.run(['docker', kind, 'rm', identifier])
            except (OSError, subprocess.SubprocessError):
                failures.append(f'{kind} removal failed: {identifier}')
    return failures


def _confirm_purge(resources: DockerResources, files: tuple[Path, ...],
                   protected: tuple[Path, ...], *, yes: bool) -> bool:
    print('Purge exact Docker resources:')
    for name in ('containers', 'images', 'networks', 'volumes'):
        print(f"  {name}: {', '.join(getattr(resources, name)) or '(none)'}")
    for path in files:
        print(f'Purge launcher file: {path}')
    for path in protected:
        print(f'Retain user data: {path}')
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError('Noninteractive purge requires --yes')
    try:
        return input('Remove this exact inventory? [y/N] ').strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def _parents(path: Path) -> None:
    """Check lexical containment without following a final command symlink."""
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError(f'Unsafe installation target: {path}')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f'Installation parent is a symlink: {parent}')
        if parent.exists() and not parent.is_dir():
            raise ValueError(f'Installation parent is not a directory: {parent}')


def _layout(layout: InstallLayout) -> None:
    for target in (layout.root, layout.command_link, layout.registry_file, layout.intent_file,
                   layout.development_manifest_file, layout.releases_dir, layout.removal_file):
        _parents(target)
    if layout.root == Path('/') or layout.root == layout.command_link.parent:
        raise ValueError('Unsafe installation root')
    if layout.mode == 'system':
        prefix = layout.root.parent.parent
        if (prefix == Path('/') or layout.root != prefix / 'share/claude-docker'
                or layout.command_link != prefix / 'bin/claude-docker'
                or layout.current_link != layout.root / 'current' or layout.host_paths is not None):
            raise ValueError('System installation layout does not match its exact prefix')
    elif layout.mode == 'user':
        paths = layout.host_paths
        if (paths is None or layout.root != paths.data_dir or layout.root == paths.home
                or layout.command_link != paths.home / '.local/bin/claude-docker'
                or layout.current_link != layout.root / 'current'):
            raise ValueError('User installation layout does not match its exact roots')
    if _exists(layout.root):
        _owned_directory(layout.root)
    if layout.current_link is not None:
        _parents(layout.current_link)
    if _exists(layout.command_link.parent):
        _owned_directory(layout.command_link.parent)


def validate_manifest_targets(manifest: InstallManifest, layout: InstallLayout) -> None:
    """Validate all recorded paths and actual file inventory, never mutate."""
    _layout(layout)
    if manifest.mode != layout.mode:
        raise ValueError('Manifest mode does not match installation layout')
    if layout.mode == 'dev':
        if manifest != InstallManifest.read(layout.development_manifest_file):
            raise ValueError('Development ownership manifest changed')
    else:
        _parents(layout.manifest_file(manifest.version))
        if manifest != _verify_release(layout, manifest.version):
            raise ValueError('Release ownership manifest changed')


@dataclass(frozen=True)
class _Inventory:
    files: tuple[Path, ...]
    directories: tuple[Path, ...]
    source: Path | None = None


def _inventory(layout: InstallLayout, ownership: dict | None = None) -> _Inventory:
    _layout(layout)
    def exists(path):
        return str(path.relative_to(layout.root)) in ownership if ownership is not None else _exists(path)
    def read(path, kind=None):
        if ownership is None:
            data = _read_metadata(path)
        else:
            key = str(path.relative_to(layout.root))
            if key not in ownership:
                raise ValueError(f'Missing pending removal ownership: {path}')
            data = ownership[key]
            if _exists(path) and _read_metadata(path) != data:
                raise ValueError(f'Removal ownership metadata changed: {path}')
        return kind.from_dict(data) if kind else data
    registry = read(layout.registry_file, InstallationRegistry) if exists(layout.registry_file) else None
    if registry is not None and (registry.mode != layout.mode or registry.command_link != layout.command_link
                                 or registry.current_link != layout.current_link):
        raise ValueError('Installation registry does not match layout')
    files = []
    directories = {layout.root}
    if registry is not None:
        files.append(layout.registry_file)
    if layout.mode == 'dev':
        if registry is None or exists(layout.intent_file) or _exists(layout.releases_dir) or _exists(layout.root / 'current'):
            raise ValueError('Development installation has inconsistent ownership metadata')
        manifest = read(layout.development_manifest_file, InstallManifest)
        if ownership is None:
            validate_manifest_targets(manifest, layout)
        elif manifest.mode != 'dev':
            raise ValueError('Invalid development removal ownership')
        source = registry.source.resolve()
        targets = (layout.root.resolve(), layout.command_link.parent.resolve() / layout.command_link.name)
        if any(target == source or source in target.parents for target in targets):
            raise ValueError('Development command and metadata must remain outside the checkout')
        if manifest.source != registry.source:
            raise ValueError('Development source ownership records disagree')
        _check_link(layout.command_link, registry.source / 'claude-docker')
        files.append(layout.development_manifest_file)
    else:
        if exists(layout.development_manifest_file):
            raise ValueError('Release installation has conflicting development ownership')
        versions = list(registry.releases if registry else ())
        intent = None
        if exists(layout.intent_file):
            intent = read(layout.intent_file)
            if (set(intent) != {'schema', 'mode', 'version', 'release', 'command_link', 'current_link'}
                    or type(intent['schema']) is not int or intent['schema'] != 1 or intent['mode'] != layout.mode):
                raise ValueError('Invalid installation recovery intent')
            version = validate_version(intent['version'])
            if (intent['release'] != str(layout.release_dir(version))
                    or intent['command_link'] != str(layout.command_link)
                    or intent['current_link'] != str(layout.current_link)):
                raise ValueError('Installation recovery intent does not match layout')
            _parents(layout.release_dir(version))
            recorded = ownership is not None and str(layout.manifest_file(version).relative_to(layout.root)) in ownership
            if (_exists(layout.release_dir(version)) or recorded) and version not in versions:
                versions.append(version)
            files.append(layout.intent_file)
        if registry is None and intent is None:
            raise ValueError('No installation ownership registry or recovery intent found')
        if _exists(layout.releases_dir):
            _owned_directory(layout.releases_dir)
        for version in versions:
            _parents(layout.manifest_file(version))
            manifest = read(layout.manifest_file(version), InstallManifest)
            if ownership is None:
                validate_manifest_targets(manifest, layout)
            else:
                _verify_release(layout, version, manifest=manifest, allow_missing=True)
            release = layout.release_dir(version)
            for item in manifest.files:
                path = release / item.path
                files.append(path)
                directories.update(parent for parent in path.parents if parent == release or release in parent.parents)
            files.append(layout.manifest_file(version))
            directories.add(release)
        current = None
        if _exists(layout.current_link):
            if not layout.current_link.is_symlink():
                raise ValueError('Current pointer must remain an owned symlink')
            for version in versions:
                if layout.current_link.readlink() == layout.release_dir(version):
                    current = version
                    break
            if current is None:
                raise ValueError('Current pointer has no validated ownership')
            files.append(layout.current_link)
        if ownership is None and intent is None and registry is not None and current != registry.active_version:
            raise ValueError('Current pointer and ownership registry disagree')
        _check_link(layout.command_link, layout.current_link / 'claude-docker')
        if _exists(layout.releases_dir):
            directories.add(layout.releases_dir)
    if _exists(layout.command_link):
        files.append(layout.command_link)
    if ownership is not None:
        expected = {str(path.relative_to(layout.root)) for path in files
                    if path != layout.command_link and (path.name == 'manifest.json'
                    or path in (layout.registry_file, layout.intent_file, layout.development_manifest_file))}
        if set(ownership) != expected:
            raise ValueError('Invalid removal ownership inventory')
    # Program files and links first, ownership metadata last. The durable
    # removal snapshot remains until even the last metadata unlink commits.
    metadata = {layout.registry_file, layout.intent_file, layout.development_manifest_file}
    files.sort(key=lambda path: path in metadata or path.name == 'manifest.json')
    return _Inventory(tuple(files), tuple(sorted(directories, key=lambda path: (-len(path.parts), str(path)))),
                      registry.source if layout.mode == 'dev' else None)


def _runtime_inventory(layout: InstallLayout) -> tuple[Path, ...]:
    paths = layout.host_paths
    files = []
    # No launcher cache payload currently has an ownership contract. Unknown
    # cache/runtime files are retained; only exact generated entries are owned.
    for directory in (paths.runtime_dir, paths.cache_dir, paths.state_dir):
        _parents(directory)
        if _exists(directory):
            _owned_directory(directory, mode=0o700 if directory == paths.runtime_dir else None)
    for name in ('compose.none.json', 'compose.sysbox.json', 'clipboard.json', 'xephyr.json', 'Xauthority'):
        path = paths.runtime_dir / name
        if _exists(path):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError(f'Runtime target is not an owned regular file: {path}')
            files.append(path)
    for path in (paths.state_dir / 'active-mode', paths.state_dir / 'pending-mode'):
        if _exists(path):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError(f'Runtime state target is not an owned regular file: {path}')
            files.append(path)
    record_path = paths.runtime_dir / 'xephyr.json'
    if _exists(record_path):
        record = _read_metadata(record_path)
        display = record.get('display')
        sockets = desktop._desktop_socket_path()
        if (type(display) is not int or not 20 <= display <= 99
                or record.get('socket_dir') != str(sockets)
                or record.get('xauthority') != str(paths.runtime_dir / 'Xauthority')):
            raise ValueError('Recorded Desktop runtime paths do not match owned targets')
        socket = sockets / f'X{display}'
        _parents(socket)
        files.append(socket)
    state = RuntimeState(paths.state_dir)
    state.read_mode()
    state.read_pending()
    return tuple(files)


def _protected_data(layout: InstallLayout, *, purge: bool = False) -> tuple[Path, ...]:
    paths = layout.host_paths
    uid = os.getuid()
    account = pwd.getpwuid(uid)
    config = load_config(env=os.environ, paths=paths,
                         account=AccountInfo(account.pw_name, uid, os.getgid(), Path(account.pw_dir)))
    targets = (paths.home / '.claude', paths.home / '.claude.json', paths.home / '.config/Claude',
               *protected_user_paths(config))
    if not purge:
        targets += (paths.config_dir, resolve_config_file(env=os.environ, paths=paths))
    return _aliases(targets)


def _purge_inventory(layout: InstallLayout, protected: tuple[Path, ...]) -> _Inventory:
    paths = layout.host_paths
    files = []
    for path in dict.fromkeys((paths.config_file, resolve_config_file(env=os.environ, paths=paths))):
        if not _exists(path) or _inside_protected(path, protected):
            continue
        _parents(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f'Configuration target is not an owned regular file: {path}')
        files.append(path)
    directories = tuple(dict.fromkeys((paths.config_dir, paths.state_dir, paths.cache_dir, paths.runtime_dir)))
    for directory in directories:
        _parents(directory)
        if _exists(directory):
            _owned_directory(directory)
    return _Inventory(tuple(files), tuple(sorted(directories, key=lambda path: (-len(path.parts), str(path)))))


def _remove(inventory: _Inventory, *, removed: list[Path], retained: list[Path],
            protected: tuple[Path, ...] = ()) -> None:
    for path in inventory.files:
        if _inside_protected(path, protected):
            retained.append(path)
            continue
        if _exists(path):
            _parents(path)
            path.unlink()
            removed.append(path)
    for directory in inventory.directories:
        if not _exists(directory):
            continue
        _parents(directory)
        _owned_directory(directory)
        resolved = directory.resolve()
        if any(resolved == path or resolved in path.parents or path in resolved.parents for path in retained):
            retained.append(resolved)
            continue
        try:
            directory.rmdir()
        except OSError as error:
            if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
            retained.append(directory)
        else:
            removed.append(directory)


def _read_removal(layout: InstallLayout) -> dict | None:
    if not _exists(layout.removal_file):
        return None
    plan = _read_metadata(layout.removal_file)
    if (set(plan) != {'schema', 'mode', 'purge', 'cleanup_done', 'cleanup_required', 'ownership', 'protected'}
            or type(plan['schema']) is not int or plan['schema'] != 1
            or plan['mode'] != layout.mode or type(plan['purge']) is not bool
            or type(plan['cleanup_done']) is not bool or type(plan['cleanup_required']) is not bool
            or not isinstance(plan['ownership'], dict) or not plan['ownership']
            or not all(isinstance(key, str) and isinstance(value, dict)
                       for key, value in plan['ownership'].items())
            or not isinstance(plan['protected'], list)
            or not all(isinstance(path, str) and Path(path).is_absolute() and '..' not in Path(path).parts
                       for path in plan['protected'])
            or (layout.mode == 'system' and (plan['purge'] or plan['protected'] or plan['cleanup_required']))):
        raise ValueError('Invalid pending removal plan')
    return plan


def _new_removal(layout: InstallLayout, inventory: _Inventory, *, purge: bool,
                 protected: tuple[Path, ...], cleanup_required: bool) -> dict:
    if _inside_protected(layout.removal_file, protected):
        raise ValueError(f'Removal intent overlaps retained user data: {layout.removal_file}')
    metadata = (layout.registry_file, layout.intent_file, layout.development_manifest_file)
    ownership = {str(path.relative_to(layout.root)): _read_metadata(path)
                 for path in inventory.files if path in metadata or path.name == 'manifest.json'}
    plan = dict(schema=1, mode=layout.mode, purge=purge, cleanup_done=False,
                cleanup_required=cleanup_required, ownership=ownership,
                protected=[str(path) for path in protected])
    _write_json(layout.removal_file, plan)
    return plan


def _finish_removal(layout: InstallLayout, inventory: _Inventory, *, removed: list[Path],
                    retained: list[Path], protected: tuple[Path, ...] = ()) -> None:
    # Keep the root and plan until file and metadata deletion has completed.
    _remove(_Inventory(inventory.files, tuple(path for path in inventory.directories if path != layout.root)),
            removed=removed, retained=retained, protected=protected)
    layout.removal_file.unlink()
    _sync_directory(layout.root)
    removed.append(layout.removal_file)
    _remove(_Inventory((), (layout.root,)), removed=removed, retained=retained, protected=protected)


def uninstall_user(layout: InstallLayout, *, purge: bool = False, yes: bool = False) -> UninstallReport:
    """Serialize validation, consent, runtime cleanup and resumable file removal."""
    if layout.mode == 'system':
        raise ValueError('Shared system installation requires: sudo /usr/local/share/claude-docker/current/install --uninstall --system')
    if layout.host_paths is None:
        raise ValueError('User and development uninstall require explicit host paths')
    with installation_lock(layout):
        layout = public_layout(Path(__file__), layout)
        return _uninstall_user(layout, purge=purge, yes=yes)


def _uninstall_user(layout: InstallLayout, *, purge: bool, yes: bool) -> UninstallReport:
    plan = _read_removal(layout)
    if plan is not None:
        purge = plan['purge']
    inventory = _inventory(layout, plan['ownership']) if plan else _inventory(layout)
    removed, retained, warnings = [], [], []
    generated = _runtime_inventory(layout)
    existing_generated = tuple(path for path in generated if _exists(path))
    paths = layout.host_paths
    protected = _protected_data(layout, purge=purge)
    if plan:
        protected = tuple(dict.fromkeys((*protected, *(Path(path) for path in plan['protected']))))
    directories = tuple(path for path in (paths.runtime_dir, paths.cache_dir) if _exists(path))
    if inventory.source is not None:
        source = inventory.source.resolve()
        protected += _aliases((inventory.source,))
        for directory in directories:
            resolved = directory.resolve()
            if source == resolved or source in resolved.parents or resolved in source.parents:
                raise ValueError(f'Runtime/cache removal overlaps retained development checkout: {directory}')
    retained.extend(protected if purge else (paths.state_dir.resolve(), *protected))
    for targets, protected_paths in ((() if purge else inventory.files, retained), (generated, protected)):
        for target in targets:
            if _inside_protected(target, protected_paths):
                raise ValueError(f'Owned removal target overlaps retained user data: {target}')
    runner = CommandRunner()
    purge_inventory = _purge_inventory(layout, protected) if purge else None
    cleanup_required = plan['cleanup_required'] if plan else bool(generated)
    cleanup_done = plan is not None and plan['cleanup_done']
    if not cleanup_done:
        if purge:
            resources = discover_labeled_resources(os.getuid(), runner)
            files = tuple(path for path in (*inventory.files, *generated, *purge_inventory.files)
                          if _exists(path) and not _inside_protected(path, protected))
            if not _confirm_purge(resources, files, protected, yes=yes):
                return UninstallReport((), tuple(dict.fromkeys((*retained, *files))), ('Purge cancelled.',))
            project = _PurgeProject(resources, runner)
        else:
            project = ComposeProject.for_teardown(paths, runner) if cleanup_required else None
        if plan is None:
            plan = _new_removal(layout, inventory, purge=purge, protected=protected,
                                cleanup_required=cleanup_required)
        failures = []
        if purge or cleanup_required:
            try:
                stop_desktop_runtime(paths, project=project)
            except (ValueError, OSError, subprocess.SubprocessError):
                failures.append('Desktop runtime cleanup failed')
            if purge:
                failures.extend(_purge_resources(resources, runner))
            else:
                try:
                    project.remove_containers()
                except (ValueError, OSError, subprocess.SubprocessError):
                    failures.append('Owned container cleanup failed')
        if failures:
            raise ValueError('; '.join(failures))
        plan['cleanup_done'] = True
        _write_json(layout.removal_file, plan)
    for path in existing_generated:
        if not _exists(path):
            removed.append(path)
        elif path.name in ('Xauthority', 'xephyr.json', 'clipboard.json'):
            # Identity-aware teardown may deliberately retain an orphan. Report
            # retention explicitly rather than suggesting it was cleaned up.
            retained.append(path)
    if purge:
        operational = tuple(paths.runtime_dir / f'compose.{mode}.json' for mode in ('none', 'sysbox'))
        operational += (paths.state_dir / 'active-mode', paths.state_dir / 'pending-mode')
        _remove(_Inventory((*operational, *purge_inventory.files), purge_inventory.directories),
                removed=removed, retained=retained, protected=protected)
    else:
        _remove(_Inventory((), directories), removed=removed, retained=retained)
        warnings.append('Docker images, networks and named volumes were retained.')
    _finish_removal(layout, inventory, removed=removed, retained=retained, protected=protected if purge else ())
    return UninstallReport(tuple(removed), tuple(dict.fromkeys(retained)), tuple(warnings))


def uninstall_system(layout: InstallLayout) -> UninstallReport:
    """Explicit administrator removal never derives user paths or runs Docker."""
    if layout.mode != 'system':
        raise ValueError('System uninstall requires a system installation layout')
    with installation_lock(layout):
        plan = _read_removal(layout)
        inventory = _inventory(layout, plan['ownership']) if plan else _inventory(layout)
        if plan is None:
            _new_removal(layout, inventory, purge=False, protected=(), cleanup_required=False)
        removed, retained = [], []
        _finish_removal(layout, inventory, removed=removed, retained=retained)
        return UninstallReport(tuple(removed), tuple(retained), ())


def print_report(report: UninstallReport) -> None:
    for path in report.removed:
        print(f'Removed: {path}')
    for path in report.retained:
        print(f'Retained: {path}')
    for warning in report.warnings:
        print(warning)


def public_layout(module_file: Path, user_layout: InstallLayout) -> InstallLayout:
    """Select dev ownership from private metadata without following source files."""
    if _exists(user_layout.removal_file):
        plan = _read_metadata(user_layout.removal_file)
        if plan.get('mode') == 'dev':
            return replace(user_layout, mode='dev', current_link=None)
    if _exists(user_layout.registry_file):
        registry = InstallationRegistry.read(user_layout.registry_file)
        if registry.mode == 'dev':
            return replace(user_layout, mode='dev', current_link=None)
    return user_layout


def reject_shared_system(module_file: Path) -> None:
    """Give admin guidance even when the root-owned registry cannot be opened."""
    program = module_file.absolute().parents[2]
    if program.parent.name != 'releases':
        return
    root = program.parent.parent
    if root.name == 'claude-docker' and root.parent.name == 'share':
        prefix = root.parent.parent
        if prefix == Path('/usr/local') or (root.exists() and root.stat().st_uid != os.getuid()):
            raise ValueError(f'Shared system installation requires: sudo {root / "current/install"} --uninstall --system')

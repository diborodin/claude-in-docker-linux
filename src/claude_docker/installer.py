"""Publish verified program bundles or explicit development checkout links."""

import argparse
from contextlib import contextmanager
import fcntl
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from .manifest import InstallManifest, InstallationRegistry, ManifestFile, _read_metadata, validate_version
from .paths import HostPaths, resolve_host_paths


# Deliberately enumerate program inputs: never sweep checkout state or new files
# into the bundle merely because they happen to live under a runtime directory.
BUNDLE_FILES = (
    ".dockerignore", "claude-docker", "install", "VERSION", "README.md", "LICENSE",
    "Dockerfile", "compose.yaml", "compose.sysbox.yaml", "config.example.toml",
    "image/customize.sh", "image/mise.lock", "image/mise.toml",
    "src/claude_docker/__init__.py", "src/claude_docker/__main__.py",
    "src/claude_docker/checks.py", "src/claude_docker/cli.py",
    "src/claude_docker/clipboard.py", "src/claude_docker/commands.py",
    "src/claude_docker/compose.py", "src/claude_docker/config.py",
    "src/claude_docker/desktop.py", "src/claude_docker/environment.py",
    "src/claude_docker/installer.py", "src/claude_docker/manifest.py",
    "src/claude_docker/model.py", "src/claude_docker/mounts.py",
    "src/claude_docker/paths.py", "src/claude_docker/process.py",
    "src/claude_docker/state.py", "src/claude_docker/uninstall.py",
    "src/claude_docker/seccomp.py",
    "security/desktop-seccomp-v0.2.4-linux-amd64.json",
    "security/LICENSE.moby-profiles", "security/NOTICE.moby-profiles",
)


# These are exact historical inventories, not permission to omit individual
# current inputs. Earlier releases had 34 files before the project license,
# 30 before the policy assets, and 29 before uninstall.py.
_PRE_LICENSE_FILES = frozenset(BUNDLE_FILES) - {"LICENSE"}
_PRE_POLICY_FILES = _PRE_LICENSE_FILES - {
    "src/claude_docker/seccomp.py",
    "security/desktop-seccomp-v0.2.4-linux-amd64.json",
    "security/LICENSE.moby-profiles", "security/NOTICE.moby-profiles",
}
_RELEASE_INVENTORIES = (frozenset(BUNDLE_FILES), _PRE_LICENSE_FILES, _PRE_POLICY_FILES,
                        _PRE_POLICY_FILES - {"src/claude_docker/uninstall.py"})


@dataclass(frozen=True)
class InstallLayout:
    mode: str
    root: Path
    command_link: Path
    current_link: Path | None
    host_paths: HostPaths | None = None

    def __post_init__(self):
        if self.mode not in ("user", "dev", "system"):
            raise ValueError("Invalid installation mode")
        if not self.root.is_absolute() or not self.command_link.is_absolute():
            raise ValueError("Installation roots must be absolute")
        if self.current_link is not None and not self.current_link.is_absolute():
            raise ValueError("Current link must be absolute")

    @classmethod
    def user(cls, env: Mapping[str, str]) -> "InstallLayout":
        paths = resolve_host_paths(env, os.getuid(), create_runtime=False)
        return cls("user", paths.data_dir, paths.home / ".local/bin/claude-docker",
                   paths.data_dir / "current", paths)

    @classmethod
    def development(cls, source: Path, bin_dir: Path, *, metadata_root: Path,
                    host_paths: HostPaths) -> "InstallLayout":
        # Checkout files are never installer-owned; metadata has an explicit root.
        if not source.is_absolute():
            raise ValueError("Development checkout must be absolute")
        return cls("dev", metadata_root, bin_dir / "claude-docker", None, host_paths)

    @classmethod
    def system(cls, prefix: Path = Path("/usr/local")) -> "InstallLayout":
        root = prefix / "share/claude-docker"
        return cls("system", root, prefix / "bin/claude-docker", root / "current")

    @property
    def releases_dir(self) -> Path:
        return self.root / "releases"

    @property
    def registry_file(self) -> Path:
        return self.root / "installation.json"

    @property
    def intent_file(self) -> Path:
        return self.root / "installation-intent.json"

    @property
    def removal_file(self) -> Path:
        return self.root / "removal-intent.json"

    def release_dir(self, version: str) -> Path:
        return self.releases_dir / validate_version(version)

    def manifest_file(self, version: str) -> Path:
        return self.release_dir(version) / "manifest.json"

    @property
    def development_manifest_file(self) -> Path:
        return self.root / "development.json"


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, data: dict) -> None:
    """Publish private metadata with a durable sibling-file rename."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _directory(path: Path) -> None:
    """Create program parents without following existing directory symlinks."""
    missing = []
    ancestor = path
    while not ancestor.exists() and not ancestor.is_symlink():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for component in (ancestor, *ancestor.parents):
        if component.is_symlink():
            raise ValueError(f"Installation directory is a symlink: {component}")
        if not component.is_dir():
            raise ValueError(f"Installation parent is not a directory: {component}")
    for component in reversed(missing):
        component.mkdir(mode=0o755)
        component.chmod(0o755)
        _sync_directory(component)
        _sync_directory(component.parent)


@contextmanager
def installation_lock(layout: InstallLayout):
    """Lock a stable parent inode, never a removable installation lockfile."""
    _directory(layout.root.parent)
    descriptor = os.open(layout.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Installation is busy; retry after the other install/uninstall finishes") from error
        yield
    finally:
        os.close(descriptor)


def _source_file(source: Path, name: str) -> Path:
    candidate = source
    for part in ("", *Path(name).parts):
        if part:
            candidate = candidate / part
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Source bundle symlink is not allowed: {candidate}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Source bundle input is not a regular file: {candidate}")
    return candidate


def _check_link(path: Path, target: Path) -> None:
    if path.is_symlink():
        if path.readlink() != target:
            raise ValueError(f"Refusing to replace unrelated command link: {path}")
    elif path.exists():
        raise ValueError(f"Refusing to replace existing path: {path}")


def _replace_link(path: Path, target: Path) -> None:
    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _owned_directory(path: Path, *, mode: int | None = None) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise ValueError(f"Installation directory must be owned and not writable by others: {path}")


def _prepare(layout: InstallLayout, *, releases: bool) -> None:
    _directory(layout.releases_dir if releases else layout.root)
    _directory(layout.command_link.parent)
    for directory in (layout.root, layout.command_link.parent):
        _owned_directory(directory)
    if releases:
        _owned_directory(layout.releases_dir)


def _registry(layout: InstallLayout) -> InstallationRegistry | None:
    if not _exists(layout.registry_file):
        return None
    registry = InstallationRegistry.read(layout.registry_file)
    if registry.mode != layout.mode:
        raise ValueError("Installation mode change requires uninstall first")
    if registry.command_link != layout.command_link or registry.current_link != layout.current_link:
        raise ValueError("Installation registry links do not match layout")
    return registry


def _verify_release(layout: InstallLayout, version: str, *, manifest: InstallManifest | None = None,
                    allow_missing: bool = False) -> InstallManifest:
    release = layout.release_dir(version)
    if _exists(release):
        _owned_directory(release)
    if _exists(release) and stat.S_IMODE(release.lstat().st_mode) not in ((0o700, 0o755) if layout.mode == 'system' else (0o700,)):
        raise ValueError('Invalid release directory mode')
    if manifest is None:
        manifest = InstallManifest.read(layout.manifest_file(version))
    if (manifest.version != version or manifest.mode != layout.mode
            or {str(item.path) for item in manifest.files} not in _RELEASE_INVENTORIES):
        raise ValueError("Release manifest does not match the fixed runtime bundle")
    expected = {item.path for item in manifest.files} | {Path("manifest.json")}
    actual = set()
    for directory, directories, files in os.walk(release, followlinks=False):
        for name in directories:
            _owned_directory(Path(directory) / name, mode=0o755)
        for name in files:
            actual.add((Path(directory) / name).relative_to(release))
    if (not actual <= expected) or (not allow_missing and actual != expected):
        raise ValueError("Release manifest inventory verification failed")
    for item in manifest.files:
        if allow_missing and not _exists(release / item.path):
            continue
        path = _source_file(release, str(item.path))
        info = path.lstat()
        contents = path.read_bytes()
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != item.mode
                or len(contents) != item.size or hashlib.sha256(contents).hexdigest() != item.sha256):
            raise ValueError(f"Release checksum or mode verification failed: {item.path}")
    return manifest


def _current_version(layout: InstallLayout, versions: tuple[str, ...]) -> str | None:
    current = layout.current_link
    if current is None or not _exists(current):
        return None
    if not current.is_symlink():
        raise ValueError("Current installation pointer must be a symlink")
    for version in versions:
        if current.readlink() == layout.release_dir(version):
            _verify_release(layout, version)
            return version
    raise ValueError("Current installation pointer has no validated ownership")


def _recover(layout: InstallLayout, registry: InstallationRegistry | None) -> InstallationRegistry | None:
    """Reconcile a durable rename intent without guessing ownership from names."""
    if not _exists(layout.intent_file):
        if registry is None:
            if layout.current_link is not None and _exists(layout.current_link):
                raise ValueError("Current installation has no ownership registry")
            return None
        actual = _current_version(layout, registry.releases)
        if actual != registry.active_version:
            raise ValueError("Installation pointer and registry disagree without recovery intent")
        return registry
    intent = _read_metadata(layout.intent_file)
    if (set(intent) != {"schema", "mode", "version", "release", "command_link", "current_link"}
            or type(intent["schema"]) is not int or intent["schema"] != 1
            or intent["mode"] != layout.mode):
        raise ValueError("Invalid installation recovery intent")
    version = validate_version(intent["version"])
    if (intent["release"] != str(layout.release_dir(version))
            or intent["command_link"] != str(layout.command_link)
            or intent["current_link"] != str(layout.current_link)):
        raise ValueError("Installation recovery intent does not match layout")
    versions = registry.releases if registry else ()
    if _exists(layout.release_dir(version)):
        _verify_release(layout, version)
        if version not in versions:
            versions += (version,)
    actual = _current_version(layout, versions)
    recovered = InstallationRegistry(layout.mode, actual, versions, layout.command_link, layout.current_link)
    if recovered != registry:
        _write_json(layout.registry_file, recovered.to_dict())
    # The release is now in the durable inventory even if a later update fails.
    layout.intent_file.unlink()
    _sync_directory(layout.root)
    return recovered


def _source_manifest(source: Path, mode: str, version: str) -> tuple[InstallManifest, list[tuple[str, Path]]]:
    inputs = [(name, _source_file(source, name)) for name in BUNDLE_FILES]
    if (source / "VERSION").read_text().strip() != version:
        raise ValueError("Requested version does not match source VERSION")
    files = []
    for name, original in inputs:
        data = original.read_bytes()
        file_mode = 0o755 if original.stat().st_mode & 0o111 else 0o644
        files.append(ManifestFile(Path(name), len(data), hashlib.sha256(data).hexdigest(), file_mode))
    return InstallManifest(version, mode, tuple(files)), inputs


def _intent(layout: InstallLayout, version: str) -> None:
    _write_json(layout.intent_file, {"schema": 1, "mode": layout.mode, "version": version,
                "release": str(layout.release_dir(version)), "command_link": str(layout.command_link),
                "current_link": str(layout.current_link)})


def install_release(source: Path, layout: InstallLayout, *, version: str) -> InstallManifest:
    with installation_lock(layout):
        if _exists(layout.removal_file):
            raise ValueError("Pending removal; retry ./install --uninstall before installing")
        return _install_release(source, layout, version=version)


def _install_release(source: Path, layout: InstallLayout, *, version: str) -> InstallManifest:
    """Publish a complete verified release, keeping pending ownership recoverable."""
    version = validate_version(version)
    if layout.mode not in ("user", "system") or layout.current_link is None:
        raise ValueError("Release installation requires user or system layout")
    source = Path(source).absolute()
    manifest, inputs = _source_manifest(source, layout.mode, version)
    # Check metadata and visible paths before any mutation, including mkdir.
    registry = _registry(layout)
    if _exists(layout.development_manifest_file):
        raise ValueError("Development installation requires uninstall before release installation")
    _check_link(layout.command_link, layout.current_link / "claude-docker")
    _prepare(layout, releases=True)
    registry = _recover(layout, registry)
    if layout.mode == 'system':
        # Only our validated program directories become public; never chmod
        # external prefix parents or private publication stages/metadata.
        for directory in (layout.root, layout.releases_dir):
            directory.chmod(0o755)
            _sync_directory(directory)
    release = layout.release_dir(version)
    if _exists(release):
        if registry is None or version not in registry.releases:
            raise ValueError(f"Existing release has no validated ownership: {release}")
        existing = _verify_release(layout, version)
        if existing != manifest:
            raise ValueError(f"Release version collision: {version}")
        if layout.mode == 'system':
            release.chmod(0o755)
            _sync_directory(release)
        if (registry.active_version == version and layout.command_link.is_symlink()):
            return existing
    else:
        stage = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=layout.releases_dir))
        try:
            stage.chmod(0o700)
            for record, (name, original) in zip(manifest.files, inputs):
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                for parent in destination.parents:
                    if parent == stage:
                        break
                    parent.chmod(0o755)
                data = original.read_bytes()
                with destination.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), record.mode)
                    os.fsync(stream.fileno())
                if (len(data) != record.size or hashlib.sha256(destination.read_bytes()).hexdigest() != record.sha256):
                    raise ValueError(f"Copied bundle verification failed: {name}")
            _write_json(stage / "manifest.json", manifest.to_dict())
            for directory, _, _ in os.walk(stage, topdown=False):
                _sync_directory(Path(directory))
            _intent(layout, version)
            os.replace(stage, release)
            if layout.mode == 'system':
                release.chmod(0o755)
                _sync_directory(release)
            _sync_directory(layout.releases_dir)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    # A retry of an already-owned inactive release needs the same switch journal.
    if not _exists(layout.intent_file):
        _intent(layout, version)
    previous = registry.active_version if registry else None
    versions = registry.releases if registry else ()
    if version not in versions:
        versions += (version,)
    pending = InstallationRegistry(layout.mode, previous, versions, layout.command_link, layout.current_link)
    _write_json(layout.registry_file, pending.to_dict())
    try:
        _replace_link(layout.current_link, release)
        _check_link(layout.command_link, layout.current_link / "claude-docker")
        if not layout.command_link.is_symlink():
            # Never replace a concurrently-created unrelated path.
            layout.command_link.symlink_to(layout.current_link / "claude-docker")
            _sync_directory(layout.command_link.parent)
        active = InstallationRegistry(layout.mode, version, versions, layout.command_link, layout.current_link)
        _write_json(layout.registry_file, active.to_dict())
    except (Exception, KeyboardInterrupt) as error:
        # A rename can succeed before its directory fsync fails. Restore the
        # previous complete release, while retaining the durable recovery intent.
        if previous is not None and layout.current_link.is_symlink() and layout.current_link.readlink() == release:
            try:
                _replace_link(layout.current_link, layout.release_dir(previous))
            except OSError as rollback_error:
                error.add_note(f"Could not restore previous current pointer: {rollback_error}; retry installation for recovery")
        raise
    layout.intent_file.unlink()
    _sync_directory(layout.root)
    return manifest


def install_development(source: Path, layout: InstallLayout) -> InstallManifest:
    with installation_lock(layout):
        if _exists(layout.removal_file):
            raise ValueError("Pending removal; retry ./install --uninstall before installing")
        return _install_development(source, layout)


def _install_development(source: Path, layout: InstallLayout) -> InstallManifest:
    """Link an explicitly validated live checkout; own metadata and the link only."""
    if layout.mode != "dev" or layout.current_link is not None:
        raise ValueError("Development installation requires dev layout")
    source = Path(source).absolute()
    for name in BUNDLE_FILES:
        _source_file(source, name)
    source = source.resolve()
    version = validate_version((source / "VERSION").read_text().strip())
    metadata_root = layout.root.resolve()
    command_path = layout.command_link.parent.resolve() / layout.command_link.name
    if any(path == source or source in path.parents for path in (metadata_root, command_path)):
        raise ValueError("Development metadata and command link must be outside the checkout")
    manifest = InstallManifest(version, "dev", (), source)
    registry = _registry(layout)
    if registry is not None and registry.source != source:
        raise ValueError("Changing development checkout requires uninstall first")
    if (_exists(layout.intent_file) or _exists(layout.root / "current")
            or _exists(layout.releases_dir)):
        raise ValueError("Pending release installation requires uninstall first")
    _check_link(layout.command_link, source / "claude-docker")
    if _exists(layout.development_manifest_file):
        existing = InstallManifest.read(layout.development_manifest_file)
        if existing.mode != "dev" or existing.source != source:
            raise ValueError("Changing development checkout requires uninstall first")
    _prepare(layout, releases=False)
    if not _exists(layout.development_manifest_file) or existing != manifest:
        _write_json(layout.development_manifest_file, manifest.to_dict())
    desired = InstallationRegistry("dev", None, (), layout.command_link, None, source)
    if registry != desired:
        _write_json(layout.registry_file, desired.to_dict())
    if not layout.command_link.is_symlink():
        layout.command_link.symlink_to(source / "claude-docker")
        _sync_directory(layout.command_link.parent)
    return manifest


def main(source: Path, argv: list[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install claude-docker")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dev", action="store_true", help="Link this checkout for development")
    modes.add_argument("--system", action="store_true", help="Install explicitly below /usr/local")
    parser.add_argument('--uninstall', action='store_true', help='Remove owned program files while retaining user data')
    arguments = parser.parse_args(argv)
    # System mode does not inspect HOME, XDG or the caller's PATH.
    environment = {} if arguments.system else dict(os.environ if env is None else env)
    try:
        if arguments.system:
            layout = InstallLayout.system()
        else:
            layout = InstallLayout.user(environment)
            if arguments.dev:
                layout = InstallLayout.development(source.absolute(), layout.command_link.parent,
                                                   metadata_root=layout.root, host_paths=layout.host_paths)
        if arguments.uninstall:
            from .uninstall import uninstall_system, uninstall_user, print_report
            report = uninstall_system(layout) if arguments.system else uninstall_user(layout)
            print_report(report)
            return 0
        if arguments.dev:
            install_development(source, layout)
        else:
            version = (source / "VERSION").read_text().strip()
            install_release(source, layout, version=version)
    except (OSError, ValueError) as error:
        action = '--uninstall --system' if arguments.uninstall else '--system'
        hint = f" Rerun explicitly with sudo: sudo ./install {action}." if arguments.system and isinstance(error, PermissionError) else ""
        parser.exit(1, f"install: {error}{hint}\n")
    print(f"Installed {layout.command_link}")
    if not arguments.system and str(layout.command_link.parent) not in environment.get("PATH", "").split(os.pathsep):
        print(f"Add {layout.command_link.parent} to PATH in your shell configuration.")
    print("Optional next command: claude-docker check")
    return 0

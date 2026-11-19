"""Explicit Docker-only artifact locking with private, exact input snapshots."""
from collections.abc import Mapping
from contextlib import contextmanager
import csv
import hashlib
import io
import os
import re
from pathlib import Path
import sys
import tempfile
import tomllib
import uuid

from .claude_version import read_code_version, update_code_version
from .commands import CommandRunner, docker_environment
from .customize import read_user_customize, user_customize_path
from .desktop_version import select_desktop_version
from .paths import HostPaths
from .tool_config import (MISE_IMAGE, ToolProfile, _regular_bytes, read_user_tools,
                          graph_file_bytes, read_python_graphs,
                          validate_declarations, validate_distinct_tool_paths, validate_lock)

POLICY = b'\n[settings]\nlockfile = true\nlocked_require_checksum = true\n'


def _python_lock_image(program_dir: Path, runner: CommandRunner, env: Mapping[str, str]) -> str:
    """Cache a native Python/uv helper. The host needs neither Python tools nor mise."""
    baseline = tomllib.loads((program_dir / 'image/mise.lock').read_text())
    uv_version = baseline['tools']['uv'][0]['version']
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', uv_version):
        raise ValueError('Invalid baseline uv version')
    dockerfile = f'''FROM {MISE_IMAGE} AS mise
FROM ghcr.io/astral-sh/uv:{uv_version} AS uv
FROM python:3.14-slim
COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
COPY --from=uv /uv /usr/local/bin/uv
ENTRYPOINT ["mise"]
'''
    docker_env = docker_environment(env)
    platform = runner.run(['docker', 'version', '--format', '{{.Server.Os}}/{{.Server.Arch}}'],
                          env=docker_env, capture_output=True).stdout.strip()
    if platform not in ('linux/amd64', 'linux/arm64'):
        raise ValueError('Python tool locking requires a Linux amd64 or arm64 daemon')
    revision = hashlib.sha256((dockerfile + platform).encode()).hexdigest()
    label = 'org.claude-docker.python-lock-helper'
    tag = 'claude-docker-python-lock:' + revision
    inspect = ['docker', 'image', 'inspect', '--format',
               f'{{{{.Os}}}}/{{{{.Architecture}}}} {{{{index .Config.Labels "{label}"}}}} {{{{.Id}}}}', tag]
    found = runner.run(inspect, env=docker_env, check=False, capture_output=True)
    if found.returncode:
        if 'No such image' not in found.stderr:
            found.check_returncode()
        print('claude-docker: Preparing cached Python tool-lock helper.', file=sys.stderr, flush=True)
        with tempfile.TemporaryDirectory(prefix='claude-python-lock-helper-') as temporary:
            (Path(temporary) / 'Dockerfile').write_text(dockerfile + f'LABEL {label}={revision}\n')
            runner.run(['docker', 'build', '--progress', 'plain', '--platform', platform,
                        '--tag', tag, temporary], env=docker_env, stream_stderr=True)
        found = runner.run(inspect, env=docker_env, capture_output=True)
    match = re.fullmatch(re.escape(platform + ' ' + revision) + r' (sha256:[a-f0-9]{64})', found.stdout.strip())
    if match is None:
        raise ValueError('Cannot verify the cached Python tool-lock helper')
    return match[1]


def _publish_graphs(root: Path, graphs: Mapping[Path, bytes]) -> None:
    """Publish immutable graph files first, without following destination links.

    The lock is the commit point. Old content-addressed directories remain usable
    if resolution fails, races another writer or is interrupted before publication.
    """
    for relative, contents in graphs.items():
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            temporary = '.graph-' + uuid.uuid4().hex
            file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=descriptor)
            try:
                with os.fdopen(file, 'wb') as stream:
                    stream.write(contents)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, relative.name, src_dir_fd=descriptor,
                            dst_dir_fd=descriptor, follow_symlinks=False)
                except FileExistsError:
                    if graph_file_bytes(root, relative) != contents:
                        raise ValueError('Existing Python graph differs; previous lock retained')
                os.fsync(descriptor)
            finally:
                os.unlink(temporary, dir_fd=descriptor)
        finally:
            os.close(descriptor)


def _relocate_graphs(locked: bytes, tools: Mapping, path: Path) -> tuple[bytes, dict[Path, bytes]]:
    """Use content-addressed directories so new graphs never overwrite old locks."""
    validate_lock(locked, tools, path)
    # mise can rewrite a reused directory in its private resolver snapshot.
    # Verify the native digest now and compute a new immutable address below.
    original = read_python_graphs(locked, tools, path, verify_address=False)
    graphs = {}
    for directory in sorted({name.parent for name in original}):
        files = {name.name: raw for name, raw in original.items() if name.parent == directory}
        digest = hashlib.sha256(b''.join(name.encode() + b'\0' + files[name] for name in sorted(files))).hexdigest()
        destination = Path('.mise/locks/claude-docker') / digest
        locked = locked.replace(('"' + str(directory) + '"').encode(), ('"' + str(destination) + '"').encode())
        graphs.update({destination / name: raw for name, raw in files.items()})
    return locked, graphs


def _publish_lock(profile: ToolProfile, locked: bytes, graphs: Mapping[Path, bytes] = ()) -> None:
    """Validate and atomically publish a lock only while its inputs still match."""
    validate_lock(locked, profile.tools, profile.lock_path)
    validate_distinct_tool_paths(profile.source, profile.lock_path)
    if _regular_bytes(profile.source) != profile.config_bytes:
        raise ValueError(f'Tool profile changed during resolution: {profile.source}; previous lock retained')
    if _regular_bytes(profile.lock_path, lock=True) != profile.lock_bytes:
        raise ValueError(f'Tool lock changed during resolution: {profile.lock_path}; previous lock retained')
    for relative, raw in profile.graphs.items():
        if graph_file_bytes(profile.lock_path.parent, relative) != raw:
            raise ValueError('Python graph changed during resolution; previous lock retained')
    _publish_graphs(profile.lock_path.parent, dict(graphs))
    read_python_graphs(locked, profile.tools, profile.lock_path)
    if (_regular_bytes(profile.source) != profile.config_bytes
            or _regular_bytes(profile.lock_path, lock=True) != profile.lock_bytes):
        raise ValueError('Tool inputs changed during graph publication; previous lock retained')
    descriptor, output = tempfile.mkstemp(prefix='.mise-lock-', dir=profile.lock_path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(locked)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(output, profile.lock_path)
    finally:
        Path(output).unlink(missing_ok=True)


def lock_image_tools(program_dir: Path, paths: HostPaths, runner: CommandRunner,
                     env: Mapping[str, str], *, base: bool = False, bump: bool = False,
                     report_paths: bool = False) -> None:
    if base:
        if (program_dir / 'manifest.json').exists() or (program_dir / 'manifest.json').is_symlink():
            raise ValueError('tools lock --base requires a source/development checkout; installed bundles are immutable')
        source = program_dir / 'image/mise.toml'
        raw = _regular_bytes(source)
        if raw is None:
            raise ValueError(f'Base tool profile does not exist: {source}')
        tools, packages = validate_declarations(raw, source, base=True)
        lock_path = source.with_suffix('.lock')
        profile = ToolProfile(source, lock_path, raw, _regular_bytes(lock_path, lock=True), tools, packages, True)
    else:
        profile = read_user_tools(paths, env, require_lock=False)
    if profile is None:
        print('claude-docker: tools: baseline only; nothing to lock. Run tools init to create a user profile.', file=sys.stderr)
        return
    validate_distinct_tool_paths(profile.source, profile.lock_path)
    if report_paths:
        print(f'claude-docker: tools: {profile.source}', file=sys.stderr)
    if not profile.tools:
        if base:
            # Base build inputs always require a native lock, even with no artifacts.
            _publish_lock(profile, b'lockfile_version = 4\n\n[tools]\n')
        else:
            print('claude-docker: tools: no artifact requests; apt packages need no artifact lock.', file=sys.stderr)
        return
    python = any(name.startswith('pipx:') for name in profile.tools)
    image = _python_lock_image(program_dir, runner, env) if python else MISE_IMAGE
    # This mount contains snapshots only, never the original user directory.
    with tempfile.TemporaryDirectory(prefix='claude-tools-') as temporary:
        root = Path(temporary)
        config = profile.config_bytes if base else profile.config_bytes + POLICY
        (root / 'mise.toml').write_bytes(config)
        if profile.lock_bytes is not None:
            (root / 'mise.lock').write_bytes(profile.lock_bytes)
        for relative, raw in profile.graphs.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(raw)
        mount = io.StringIO()
        csv.writer(mount, lineterminator='').writerow(['type=bind', 'source=' + str(root), 'target=/workspace'])
        argv = ['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}',
                '--mount', mount.getvalue(), '--workdir', '/workspace',
                '--tmpfs', '/tmp:rw,mode=1777']
        for value in ('HOME=/tmp', 'MISE_CACHE_DIR=/tmp/mise-cache', 'MISE_STATE_DIR=/tmp/mise-state',
                      'MISE_DATA_DIR=/tmp/mise-data', 'MISE_TRUSTED_CONFIG_PATHS=/workspace'):
            argv.extend(['--env', value])
        argv.extend([image, 'lock', '--platform', 'linux-x64,linux-arm64'])
        if bump:
            argv.append('--bump')
        runner.run(argv, env=docker_environment(env), capture_output=not python, stream_stderr=python)
        if {p.name for p in root.iterdir()} - {'mise.toml', 'mise.lock', *({'.mise'} if python else set())}:
            raise ValueError('Unsupported generated tool files or dependency sidecars; previous lock retained')
        if _regular_bytes(root / 'mise.toml') != config:
            raise ValueError('Tool resolver modified declarations; previous lock retained')
        locked = _regular_bytes(root / 'mise.lock', lock=True)
        if locked is None:
            raise ValueError('Tool resolver produced no lock; previous lock retained')
        locked, graphs = _relocate_graphs(locked, profile.tools, root / 'mise.lock')
        # Only referenced graph files (and the exact old snapshots) are admitted.
        allowed = set(read_python_graphs(_regular_bytes(root / 'mise.lock'), profile.tools,
                                         root / 'mise.lock', verify_address=False)) | set(profile.graphs)
        for path in (root / '.mise').rglob('*'):
            if path.is_symlink() or (not path.is_dir() and path.relative_to(root) not in allowed):
                raise ValueError('Unexpected generated Python dependency files; previous lock retained')
        _publish_lock(profile, locked, graphs)


@contextmanager
def image_build_inputs(program_dir: Path, paths: HostPaths, env: Mapping[str, str], *,
                       runner: CommandRunner | None = None, include_desktop: bool = False,
                       update_claude: bool = False, update_desktop: bool = False, report_paths: bool = False):
    """Snapshot only validated, exact image inputs; never send a host directory."""
    profile = read_user_tools(paths, env, require_lock=True)
    customize = read_user_customize(paths)
    baseline = {}
    names = ('Dockerfile', '.dockerignore', 'image/mise.toml', 'image/mise.lock', 'image/customize.sh')
    desktop_apt = 'image/claude-desktop-apt.sh'
    if include_desktop:
        names += (desktop_apt,)
    for name in names:
        raw = _regular_bytes(program_dir / name, lock=name.endswith('.lock'))
        if raw is None:
            raise ValueError(f'Missing image build input: {program_dir / name}')
        baseline[name] = raw
    tools, _ = validate_declarations(baseline['image/mise.toml'], program_dir / 'image/mise.toml', base=True)
    validate_lock(baseline['image/mise.lock'], tools, program_dir / 'image/mise.lock')
    select_version = update_code_version if update_claude else read_code_version
    version = select_version(program_dir, paths)
    baseline['image/claude-code.version'] = f'{version}\n'.encode('ascii')
    print(f'claude-docker: Claude Code version: {version}', file=sys.stderr)
    if include_desktop:
        version = select_desktop_version(program_dir, paths, runner if runner is not None else CommandRunner(),
                                         env, apt_script=baseline[desktop_apt], refresh=update_desktop)
        baseline['image/claude-desktop.version'] = f'{version}\n'.encode('ascii')
        print(f'claude-docker: Claude Desktop version: {version}', file=sys.stderr)
    if profile is not None and report_paths:
        print(f'claude-docker: tools: {profile.source}', file=sys.stderr)
    elif profile is None:
        print('claude-docker: tools: baseline only (no user profile)', file=sys.stderr)
    if customize is not None and report_paths:
        print(f'claude-docker: customize: {user_customize_path(paths)}', file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix='claude-image-inputs-') as temporary:
        root = Path(temporary)
        (root / 'image').mkdir(mode=0o700)
        graph_root = root / 'image/mise-user-graphs'
        graph_root.mkdir()
        if profile is not None and profile.graphs:
            for relative, raw in profile.graphs.items():
                target = graph_root / Path(*relative.parts[2:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
        for name, raw in baseline.items():
            (root / name).write_bytes(raw)
        (root / 'image/customize.sh').chmod(0o755)
        if customize is not None:
            (root / 'image/customize-user.sh').write_bytes(customize)
        if profile is not None:
            (root / 'image/mise-user.toml').write_bytes(profile.config_bytes)
            if profile.tools:
                (root / 'image/mise-user.lock').write_bytes(profile.lock_bytes)
        yield root

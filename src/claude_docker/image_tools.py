"""Explicit Docker-only artifact locking with private, exact input snapshots."""
from collections.abc import Mapping
from contextlib import contextmanager
import csv
import io
import os
from pathlib import Path
import sys
import tempfile

from .claude_version import read_code_version, update_code_version
from .commands import CommandRunner, docker_environment
from .customize import read_user_customize, user_customize_path
from .desktop_version import select_desktop_version
from .paths import HostPaths
from .tool_config import (MISE_IMAGE, ToolProfile, _regular_bytes, read_user_tools,
                          validate_declarations, validate_distinct_tool_paths, validate_lock)

POLICY = b'\n[settings]\nlockfile = true\nlocked_require_checksum = true\n'


def _publish_lock(profile: ToolProfile, locked: bytes) -> None:
    """Validate and atomically publish a lock only while its inputs still match."""
    validate_lock(locked, profile.tools, profile.lock_path)
    validate_distinct_tool_paths(profile.source, profile.lock_path)
    if _regular_bytes(profile.source) != profile.config_bytes:
        raise ValueError(f'Tool profile changed during resolution: {profile.source}; previous lock retained')
    if _regular_bytes(profile.lock_path, lock=True) != profile.lock_bytes:
        raise ValueError(f'Tool lock changed during resolution: {profile.lock_path}; previous lock retained')
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
                     env: Mapping[str, str], *, base: bool = False, bump: bool = False) -> None:
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
    print(f'claude-docker: tools: {profile.source}', file=sys.stderr)
    if not profile.tools:
        if base:
            # Base build inputs always require a native lock, even with no artifacts.
            _publish_lock(profile, b'lockfile_version = 4\n\n[tools]\n')
        else:
            print('claude-docker: tools: no artifact requests; apt packages need no artifact lock.', file=sys.stderr)
        return
    # This mount contains snapshots only, never the original user directory.
    with tempfile.TemporaryDirectory(prefix='claude-tools-') as temporary:
        root = Path(temporary)
        config = profile.config_bytes if base else profile.config_bytes + POLICY
        (root / 'mise.toml').write_bytes(config)
        if profile.lock_bytes is not None:
            (root / 'mise.lock').write_bytes(profile.lock_bytes)
        mount = io.StringIO()
        csv.writer(mount, lineterminator='').writerow(['type=bind', 'source=' + str(root), 'target=/workspace'])
        argv = ['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}',
                '--mount', mount.getvalue(), '--workdir', '/workspace',
                '--tmpfs', '/tmp:rw,mode=1777']
        for value in ('HOME=/tmp', 'MISE_CACHE_DIR=/tmp/mise-cache', 'MISE_STATE_DIR=/tmp/mise-state',
                      'MISE_DATA_DIR=/tmp/mise-data', 'MISE_TRUSTED_CONFIG_PATHS=/workspace'):
            argv.extend(['--env', value])
        argv.extend([MISE_IMAGE, 'lock', '--platform', 'linux-x64,linux-arm64'])
        if bump:
            argv.append('--bump')
        runner.run(argv, env=docker_environment(env), capture_output=True)
        if {p.name for p in root.iterdir()} != {'mise.toml', 'mise.lock'}:
            raise ValueError('Unsupported generated tool files or dependency sidecars; previous lock retained')
        if _regular_bytes(root / 'mise.toml') != config:
            raise ValueError('Tool resolver modified declarations; previous lock retained')
        locked = _regular_bytes(root / 'mise.lock', lock=True)
        if locked is None:
            raise ValueError('Tool resolver produced no lock; previous lock retained')
        _publish_lock(profile, locked)


@contextmanager
def image_build_inputs(program_dir: Path, paths: HostPaths, env: Mapping[str, str], *,
                       runner: CommandRunner | None = None, include_desktop: bool = False,
                       update_claude: bool = False, update_desktop: bool = False):
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
    if profile is not None:
        print(f'claude-docker: tools: {profile.source}', file=sys.stderr)
    else:
        print('claude-docker: tools: baseline only (no user profile)', file=sys.stderr)
    if customize is not None:
        print(f'claude-docker: customize: {user_customize_path(paths)}', file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix='claude-image-inputs-') as temporary:
        root = Path(temporary)
        (root / 'image').mkdir(mode=0o700)
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

"""Persistent, deliberately limited user image declarations (stdlib only)."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
import tomllib
from .paths import HostPaths


MISE_IMAGE = 'jdxcode/mise:2026.11.5'


@dataclass(frozen=True)
class ToolProfile:
    source: Path
    lock_path: Path
    config_bytes: bytes
    lock_bytes: bytes | None
    tools: Mapping
    packages: Mapping
    explicit: bool
    graphs: Mapping[Path, bytes] = field(default_factory=dict)


def validate_distinct_tool_paths(source: Path, lock_path: Path) -> None:
    """Keep a profile and its publication destination distinct, including aliases."""
    collision = source.resolve() == lock_path.resolve()
    if not collision:
        try:
            collision = source.samefile(lock_path)
        except FileNotFoundError:
            pass
    if collision:
        raise ValueError(f'Tool profile and lock must be distinct files: {source}')


def _derive_user_tools_paths(paths: HostPaths, env: Mapping[str, str]) -> tuple[Path, Path, bool]:
    """Derive configured names without filesystem or profile validation."""
    explicit = 'CLAUDE_MISE_CONFIG_FILE' in env
    value = env.get('CLAUDE_MISE_CONFIG_FILE')
    if explicit:
        if value and any(character in value for character in ('\x00', '\r', '\n')):
            raise ValueError('CLAUDE_MISE_CONFIG_FILE must select an absolute or ~/ file without control characters')
        if value and value.startswith('~/'):
            source = paths.home / value[2:]
        elif value and Path(value).is_absolute():
            source = Path(value)
        else:
            raise ValueError('CLAUDE_MISE_CONFIG_FILE must select an absolute or ~/ file')
    else:
        source = paths.config_dir / 'mise.toml'
    return source, source.with_suffix('.lock'), explicit


def resolve_user_tools_paths(paths: HostPaths, env: Mapping[str, str]) -> tuple[Path, Path, bool]:
    source, lock_path, explicit = _derive_user_tools_paths(paths, env)
    validate_distinct_tool_paths(source, lock_path)
    return source, lock_path, explicit


def retained_user_tools_paths(paths: HostPaths, env: Mapping[str, str]) -> tuple[Path, ...]:
    files = list(_derive_user_tools_paths(paths, {})[:2])
    try:
        files.extend(_derive_user_tools_paths(paths, env)[:2])
    except ValueError:
        pass
    files.extend(parent / '.mise/locks' for parent in dict.fromkeys(path.parent for path in files))
    return tuple(dict.fromkeys(files))


def _toml(raw: bytes, path: Path) -> dict:
    try:
        return tomllib.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ValueError(f'Invalid tool TOML: {path}') from None


def _regular_bytes(path: Path, *, lock: bool = False) -> bytes | None:
    if lock and path.is_symlink():
        raise ValueError(f'Tool lock must not be a symlink: {path}')
    try:
        info = path.stat()
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError(f'Tool input is not a regular file: {path}') from None
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Tool input is not a regular file: {path}')
    return path.read_bytes()


def _safe_value(value) -> bool:
    if isinstance(value, str):
        return bool(value) and not any(x in value for x in ('{{', '}}', '\n', '\x00'))
    if isinstance(value, (bool, int)):
        return True
    if isinstance(value, list):
        return all(_safe_value(v) for v in value)
    return False


def validate_declarations(raw: bytes, source: Path, *, base: bool = False) -> tuple[dict, dict]:
    data = _toml(raw, source)
    allowed = {'tools', 'bootstrap'} | ({'settings'} if base else set())
    if set(data) - allowed:
        raise ValueError(f'Unsupported tool profile section: {source}; only tools and bootstrap.packages are supported')
    tools = data.get('tools', {})
    bootstrap = data.get('bootstrap', {})
    if not isinstance(tools, dict) or not isinstance(bootstrap, dict) or set(bootstrap) - {'packages'}:
        raise ValueError(f'Unsupported tool declarations: {source}')
    packages = bootstrap.get('packages', {})
    if not isinstance(packages, dict):
        raise ValueError(f'Unsupported package declarations: {source}')
    for name, value in packages.items():
        if (not re.fullmatch(r'apt:[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?', name)
                or not isinstance(value, str) or not _safe_value(value)):
            raise ValueError(f'Only additive apt package requests are supported: {source}')
    for name, value in tools.items():
        if base and name.startswith('pipx:'):
            raise ValueError(f'Python dependency graphs are supported only in user profiles: {source}')
        if (not re.fullmatch(r'[A-Za-z0-9_./:@+-]+', name)
                or name.split(':', 1)[0] in {'npm', 'pypi', 'pip', 'asdf', 'vfox'}):
            raise ValueError(f'Unsupported tool backend or dependency sidecars: {source}')
        if name.startswith('pipx:') and (not re.fullmatch(r'pipx:[A-Za-z0-9][A-Za-z0-9_.-]*', name)
                                       or not isinstance(value, str)):
            raise ValueError(f'Python tools require a package name and version string: {source}')
        if isinstance(value, str):
            valid = _safe_value(value) and not value.startswith(('path:', 'system', 'ref:'))
        elif isinstance(value, dict):
            valid = (isinstance(value.get('version'), str) and _safe_value(value['version'])
                     and all(isinstance(v, (str, bool, int)) and _safe_value(v) for v in value.values())
                     and not value['version'].startswith(('path:', 'system', 'ref:'))
                     and not set(value) & {'install_env', 'postinstall', 'os', 'depends',
                                           'depends_on', 'lazy', 'lazy_bins', 'auto_update'})
        else:
            valid = False
        if not valid:
            raise ValueError(f'Unsupported tool request: {source}')
    return tools, packages


def validate_lock(raw: bytes, tools: Mapping, path: Path) -> None:
    data = _toml(raw, path)
    if data.get('lockfile_version') != 4 or not isinstance(data.get('tools', {}), dict):
        raise ValueError(f'Unsupported native tool lock: {path}; run tools lock')
    entries = data.get('tools', {})
    if set(entries) != set(tools):
        raise ValueError(f'Stale tool lock: {path}; run tools lock')
    for name, request in tools.items():
        rows = entries[name]
        version = request if isinstance(request, str) else request['version']
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError(f'Unsupported tool lock entries: {path}; run tools lock')
        row = rows[0]
        if (not isinstance(row, dict) or not isinstance(row.get('specifiers'), list)
                or version not in row['specifiers'] or not isinstance(row.get('version'), str)
                or not row['version'] or not isinstance(row.get('backend'), str) or not row['backend']):
            raise ValueError(f'Stale tool lock request: {path}; run tools lock')
        options = {} if isinstance(request, str) else {
            'vars.' + key: (str(value).lower() if isinstance(value, bool) else str(value))
            for key, value in request.items() if key != 'version'
        }
        if row.get('options', {}) != options:
            raise ValueError(f'Stale tool lock options: {path}; run tools lock')
        if any(key in row for key in ('dependencies', 'dependency_graph', 'sidecar', 'aube')):
            raise ValueError(f'Unsupported dependency sidecars: {path}')
        if name.startswith('pipx:'):
            if row['backend'] != name:
                raise ValueError(f'Unexpected Python backend: {path}')
            _python_graph_reference(row, path)
            continue
        if 'uv' in row or 'aube' in row:
            raise ValueError(f'Unsupported dependency sidecar: {path}')
        for platform in ('linux-x64', 'linux-arm64'):
            artifact = row.get('platforms.' + platform, {})
            if (not isinstance(artifact, dict)
                    or not isinstance(artifact.get('url'), str) or not artifact['url']
                    or not isinstance(artifact.get('checksum'), str)
                    or not re.fullmatch(r'(?:sha256:[a-fA-F0-9]{64}|sha512:[a-fA-F0-9]{128})',
                                        artifact['checksum'])):
                raise ValueError(f'Tool lock requires both Linux platforms and checksums: {path}; run tools lock')
        if any(key in row for key in ('dependencies', 'dependency_graph', 'sidecar')):
            raise ValueError(f'Dependency sidecars are unsupported: {path}')


def _python_graph_reference(row: dict, path: Path) -> tuple[Path, str]:
    graph = row.get('uv')
    if not isinstance(graph, dict) or set(graph) != {'path', 'digest'}:
        raise ValueError(f'Python tool requires a uv dependency graph: {path}; run tools lock')
    value, digest = graph['path'], graph['digest']
    if (not isinstance(value, str) or not re.fullmatch(r'\.mise/locks/[A-Za-z0-9_./+-]+', value)
            or '..' in value.split('/') or '.' in value.split('/') or '//' in value
            or value.endswith('/') or len(Path(value).parts) < 4
            or not isinstance(digest, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest)):
        raise ValueError(f'Invalid Python dependency graph reference: {path}')
    return Path(value), digest


def graph_file_bytes(root: Path, relative: Path) -> bytes:
    """Read only below the profile directory; never follow sidecar symlinks."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Python dependency graph file must be regular')
            contents = stream.read(4 * 1024 * 1024 + 1)
            if len(contents) > 4 * 1024 * 1024:
                raise ValueError('Python dependency graph file is too large')
            return contents
    finally:
        os.close(descriptor)


def read_python_graphs(raw: bytes, tools: Mapping, path: Path, *, verify_address: bool = True) -> dict[Path, bytes]:
    entries = _toml(raw, path).get('tools', {})
    result = {}
    for name in tools:
        if not name.startswith('pipx:'):
            continue
        directory, digest = _python_graph_reference(entries[name][0], path)
        try:
            for filename in ('uv.lock', 'pyproject.toml'):
                relative = directory / filename
                result[relative] = graph_file_bytes(path.parent, relative)
                _toml(result[relative], relative)
        except OSError:
            raise ValueError(f'Missing or unsafe Python dependency graph: {path}; run tools lock') from None
        graph = result[directory / 'uv.lock'].replace(b'\r\n', b'\n')
        if 'sha256:' + hashlib.sha256(graph).hexdigest() != digest:
            raise ValueError(f'Python dependency graph digest mismatch: {path}; run tools lock')
        if verify_address and directory.parent == Path('.mise/locks/claude-docker'):
            files = {name.name: raw for name, raw in result.items() if name.parent == directory}
            address = hashlib.sha256(b''.join(name.encode() + b'\0' + files[name] for name in sorted(files))).hexdigest()
            if directory.name != address:
                raise ValueError(f'Python project/lock digest mismatch: {path}; run tools lock')
    return result


def read_user_tools(paths: HostPaths, env: Mapping[str, str], *, require_lock: bool) -> ToolProfile | None:
    source, lock_path, explicit = resolve_user_tools_paths(paths, env)
    raw = _regular_bytes(source)
    if raw is None:
        if explicit:
            raise ValueError(f'Selected tool profile does not exist: {source}')
        return None
    tools, packages = validate_declarations(raw, source)
    python = any(name.startswith('pipx:') for name in tools)
    lock = _regular_bytes(lock_path, lock=True)
    graphs = {}
    if require_lock and tools:
        if lock is None:
            raise ValueError(f'Missing tool lock: {lock_path}; run tools lock')
        validate_lock(lock, tools, lock_path)
        graphs = read_python_graphs(lock, tools, lock_path)
    elif python and lock is not None:
        # Repair a missing/stale graph through explicit tools lock, never build.
        try:
            validate_lock(lock, tools, lock_path)
            graphs = read_python_graphs(lock, tools, lock_path)
        except ValueError:
            pass
    return ToolProfile(source, lock_path, raw, lock, tools, packages, explicit, graphs)


def init_user_tools(paths: HostPaths, env: Mapping[str, str]) -> Path:
    source, _, _ = resolve_user_tools_paths(paths, env)
    source.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(b'''# Optional image tools. Remove entries you do not need.
# Run tools lock, then build. Existing profiles are never overwritten.
[tools]
kubectl = "1.36"
aws-cli = "2"
helm = "latest"
terraform = "latest"
# Required for locked Python tool environments.
python = "3.14"
"pipx:ansible-core" = "latest"

# Optional Ubuntu packages (not artifact-locked):
[bootstrap.packages]
"apt:build-essential" = "latest"
"apt:wget" = "latest"
"apt:openssh-client" = "latest"
"apt:rsync" = "latest"
"apt:postgresql-client" = "latest"
"apt:mysql-client" = "latest"
"apt:redis-tools" = "latest"
# "apt:tree" = "latest"
''')
    return source

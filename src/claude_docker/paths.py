"""Resolve host storage without creating configuration or application state."""

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import stat


@dataclass(frozen=True)
class HostPaths:
    home: Path
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    runtime_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"


def _xdg_home(env: Mapping[str, str], name: str, default: Path) -> Path:
    value = env.get(name, "")
    path = Path(value)
    return path if value and path.is_absolute() else default


def _usable_runtime(path: Path, uid: int) -> bool:
    try:
        info = path.stat()
        return (path.is_absolute() and stat.S_ISDIR(info.st_mode)
                and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o700
                and os.access(path, os.R_OK | os.W_OK | os.X_OK))
    except OSError:
        return False


def _private_runtime(path: Path, uid: int) -> Path:
    """Refuse links and foreign owners before changing runtime permissions."""
    try:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(descriptor).st_uid != uid:
                raise ValueError(f"Private runtime directory has a different owner: {path}")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ValueError(f"Cannot use private runtime directory: {path}") from error
    return path


def resolve_host_paths(env: Mapping[str, str], uid: int, *, create_runtime: bool = True) -> HostPaths:
    home = Path(env.get("HOME") or pwd.getpwuid(uid).pw_dir)
    if not home.is_absolute():
        raise ValueError("HOME must be an absolute path")
    runtime_base = Path(env.get("XDG_RUNTIME_DIR", ""))
    runtime = (runtime_base / "claude-docker" if _usable_runtime(runtime_base, uid)
               else Path("/tmp") / f"claude-docker-{uid}")
    return HostPaths(
        home=home,
        config_dir=_xdg_home(env, "XDG_CONFIG_HOME", home / ".config") / "claude-docker",
        data_dir=_xdg_home(env, "XDG_DATA_HOME", home / ".local/share") / "claude-docker",
        state_dir=_xdg_home(env, "XDG_STATE_HOME", home / ".local/state") / "claude-docker",
        cache_dir=_xdg_home(env, "XDG_CACHE_HOME", home / ".cache") / "claude-docker",
        runtime_dir=_private_runtime(runtime, uid) if create_runtime else runtime,
    )


def resolve_program_dir(executable: Path, env: Mapping[str, str]) -> Path:
    if "CLAUDE_DOCKER_DIR" in env:
        candidate = Path(env["CLAUDE_DOCKER_DIR"])
        if not candidate.is_absolute():
            raise ValueError("CLAUDE_DOCKER_DIR must be an absolute program directory")
        label = "CLAUDE_DOCKER_DIR"
    else:
        candidate = executable.resolve().parent
        label = "Program directory"
    if not (candidate / "src/claude_docker/cli.py").is_file():
        raise ValueError(f"{label} has no launcher package: {candidate}")
    return candidate.resolve()


def desktop_socket_dir(uid: int) -> Path:
    """Expose only our nested socket, on the host X socket filesystem."""
    base = _private_runtime(Path('/tmp') / f'claude-docker-{uid}', uid)
    descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_uid != uid:
            raise ValueError(f'Private socket directory has a different owner: {base}')
        try:
            os.mkdir('x11', mode=0o700, dir_fd=descriptor)
        except FileExistsError:
            pass
        sockets = os.open('x11', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=descriptor)
        try:
            if os.fstat(sockets).st_uid != uid:
                raise ValueError(f'Private socket directory has a different owner: {base / "x11"}')
            os.fchmod(sockets, 0o700)
        finally:
            os.close(sockets)
    finally:
        os.close(descriptor)
    return base / 'x11'

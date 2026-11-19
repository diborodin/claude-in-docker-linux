"""Read and publish exact application version pins with atomic replacement."""

from collections.abc import Callable
import fcntl
import os
from pathlib import Path
import stat
import uuid

from .paths import HostPaths

MAX_VERSION_BYTES = 128


def _file_stamp(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_pin(path: Path, product: str, parse: Callable[[bytes, str], str], *, updating: bool) -> tuple[str, tuple] | None:
    try:
        link = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f'Cannot read {product} version file: {path}') from None

    if updating and stat.S_ISLNK(link.st_mode):
        raise ValueError(f'Cannot update a {product} version pin through a symlink: {path}')
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'{product} version must be a regular file: {path}')
        # Avoid blocking if the file turns into a FIFO after the check.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f'{product} version must be a regular file: {path}')
            raw = stream.read(MAX_VERSION_BYTES + 1)
            if (_file_stamp(info) != _file_stamp(opened)
                    or _file_stamp(opened) != _file_stamp(os.fstat(stream.fileno()))
                    or _file_stamp(link) != _file_stamp(path.lstat())):
                raise ValueError(f'{product} version file changed while reading: {path}')
    except OSError:
        raise ValueError(f'Cannot read {product} version file: {path}') from None
    return parse(raw, str(path)), (_file_stamp(link), _file_stamp(opened))


def _directory_identity(directory: Path) -> tuple[int, int] | None:
    try:
        info = directory.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _protect_program_directory(program_dir: Path, paths: HostPaths, parent: Path, product: str) -> None:
    program = program_dir.resolve()
    protected = [program, Path(os.path.realpath(paths.data_dir / 'releases'))]
    if program.parent.name == 'releases' and (program / 'manifest.json').is_file():
        protected.append(program.parent)
    if any(parent.is_relative_to(root) for root in protected):
        raise ValueError(f'Cannot save a {product} version pin inside the program directory: {parent}')


def select_version_pin(program_dir: Path, paths: HostPaths, destination: Path, *,
                       product: str, parse: Callable[[bytes, str], str],
                       resolve: Callable[[], str], refresh: bool,
                       is_newer: Callable[[str, str], bool] | None = None) -> str:
    parent = destination.parent
    resolved = Path(os.path.realpath(parent))
    identity = _directory_identity(parent)
    if refresh:
        _protect_program_directory(program_dir, paths, resolved, product)
    previous = _read_pin(destination, product, parse, updating=refresh)
    if not refresh:
        if previous is not None:
            return previous[0]
        _protect_program_directory(program_dir, paths, resolved, product)

    latest = resolve()
    changed = f'{product} version changed during update: {destination}'

    def check_selection(directory: int | None = None) -> None:
        if (Path(os.path.realpath(parent)) != resolved
                or (identity is not None and _directory_identity(parent) != identity)
                or _read_pin(destination, product, parse, updating=True) != previous
                or (directory is not None and not os.path.samestat(parent.stat(), os.fstat(directory)))):
            raise ValueError(changed)
        _protect_program_directory(program_dir, paths, resolved, product)

    check_selection()
    if previous is not None:
        current = previous[0]
        if latest == current or (is_newer is not None and not is_newer(latest, current)):
            return current

    try:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # Serialize the recheck and replacement between launcher processes.
            fcntl.flock(directory, fcntl.LOCK_EX)
            check_selection(directory)
            temporary = f'.{destination.stem}-{uuid.uuid4().hex}'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, 'wb') as draft:
                    os.fchmod(draft.fileno(), 0o600)
                    draft.write(f'{latest}\n'.encode('ascii'))
                    draft.flush()
                    os.fsync(draft.fileno())
                check_selection(directory)
                os.replace(temporary, destination.name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory)
    except OSError:
        raise ValueError(f'Cannot publish the {product} version pin: {destination}') from None
    return latest

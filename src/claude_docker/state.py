"""Private Compose transition lock and atomic runtime mode records."""

from contextlib import ExitStack, contextmanager
import fcntl
import os
from os import getuid
from pathlib import Path
from pwd import getpwuid
import stat
import tempfile


def runtime_transition_lock_path() -> Path:
    try:
        home = Path(getpwuid(getuid()).pw_dir)
    except KeyError as error:
        raise ValueError('Cannot locate the account home for the runtime transition lock') from error
    if not home.is_absolute():
        raise ValueError('The account home for the runtime transition lock must be absolute')
    return home / '.claude-docker-locks' / 'runtime-transition.lock'


@contextmanager
def runtime_transition():
    """Serialize lifecycle changes for the host UID's Compose project."""
    uid = getuid()
    # Compose is keyed to the host UID, independently of the XDG paths. Keep
    # the inode outside runtime/state cleanup and leave it in place on release.
    lock_path = runtime_transition_lock_path()
    directory_path = lock_path.parent
    with ExitStack() as stack:
        try:
            # An account's home can be a symlink. Validate the opened target
            # before creating anything inside it and use the descriptor below.
            home_path = directory_path.parent
            home = os.open(home_path, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, home)
            info = os.fstat(home)
            if info.st_uid != uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError(f'Runtime lock account home must be owned and writable only by this user: {home_path}')
            try:
                os.mkdir(directory_path.name, mode=0o700, dir_fd=home)
            except FileExistsError:
                pass
            directory = os.open(directory_path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=home)
            stack.callback(os.close, directory)
            info = os.fstat(directory)
            # A setgid home passes this bit to newly created directories.
            directory_mode = stat.S_IMODE(info.st_mode) & ~stat.S_ISGID
            if info.st_uid != uid or directory_mode != 0o700:
                raise ValueError(f'Runtime lock directory must be owned by this user and private: {directory_path}')
            descriptor = os.open(lock_path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 mode=0o600, dir_fd=directory)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError(f'Runtime lock must be an owned, private regular file: {lock_path}')
        except OSError as error:
            raise ValueError(f'Cannot open the runtime transition lock: {lock_path}') from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another claude-docker runtime transition is in progress. '
                             'Retry after it finishes.') from error
        yield


class RuntimeState:
    def __init__(self, state_dir: Path):
        self.path = state_dir / 'active-mode'
        self.pending_path = state_dir / 'pending-mode'

    @staticmethod
    def _validate(mode: str) -> str:
        if mode not in ('none', 'sysbox'):
            raise ValueError('Runtime mode must be none or sysbox; inspect active-mode and pending-mode state')
        return mode

    def _read(self, path: Path) -> str | None:
        try:
            text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None
        return self._validate(text.strip())

    def _write(self, path: Path, mode: str, *, if_absent: bool = False) -> bool:
        self._validate(mode)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(mode + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            if if_absent:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    return False
            else:
                os.replace(temporary, path)
            return True
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read_mode(self) -> str | None:
        return self._read(self.path)

    def write_mode(self, mode: str) -> None:
        self._write(self.path, mode)

    def write_mode_if_absent(self, mode: str) -> bool:
        """Publish a complete first-mode record without replacing another writer."""
        return self._write(self.path, mode, if_absent=True)

    def read_pending(self) -> str | None:
        return self._read(self.pending_path)

    def require_modes(self, mode: str | None, pending: str | None) -> None:
        """Reject a Desktop launch whose recorded mode or transition changed."""
        if self.read_mode() != mode or self.read_pending() != pending:
            raise ValueError('Docker mode or pending transition changed during Desktop launch. '
                             'Retry after the transition completes.')

    def write_pending(self, mode: str) -> None:
        self._write(self.pending_path, mode)

    def clear_pending(self) -> None:
        self.pending_path.unlink(missing_ok=True)

    def clear_mode(self) -> None:
        self.path.unlink(missing_ok=True)

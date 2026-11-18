"""Select and persist the Claude Code version for image builds."""

import fcntl
from http.client import HTTPException
import os
from pathlib import Path
import re
import stat
import urllib.error
from urllib.parse import urlsplit
import urllib.request
import uuid

from .paths import HostPaths

_PIN_NAME = 'claude-code.version'
_LATEST_URL = 'https://downloads.claude.ai/claude-code-releases/latest'
_MAX_BYTES = 128


def code_version_path(paths: HostPaths) -> Path:
    return paths.config_dir / _PIN_NAME


def _parse_version(raw: bytes, source: str) -> str:
    version = raw.strip()
    if (len(raw) > _MAX_BYTES or len(version) >= _MAX_BYTES
            or not re.fullmatch(rb'[0-9]+\.[0-9]+\.[0-9]+', version)):
        raise ValueError(f'Invalid Claude Code version in {source}; expected numeric X.Y.Z')
    return version.decode('ascii')


def _file_stamp(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_pin(path: Path, *, updating: bool = False) -> tuple[str, tuple] | None:
    try:
        link = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f'Cannot read Claude Code version file: {path}') from None

    if updating and stat.S_ISLNK(link.st_mode):
        raise ValueError(f'Cannot update a Claude Code version pin through a symlink: {path}')
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'Claude Code version must be a regular file: {path}')
        # Avoid blocking if the file turns into a FIFO after the check.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f'Claude Code version must be a regular file: {path}')
            raw = stream.read(_MAX_BYTES + 1)
            if (_file_stamp(info) != _file_stamp(opened)
                    or _file_stamp(opened) != _file_stamp(os.fstat(stream.fileno()))
                    or _file_stamp(link) != _file_stamp(path.lstat())):
                raise ValueError(f'Claude Code version file changed while reading: {path}')
    except OSError:
        raise ValueError(f'Cannot read Claude Code version file: {path}') from None
    return _parse_version(raw, str(path)), (_file_stamp(link), _file_stamp(opened))


class _RejectMetadataRedirects(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Use the default HTTP error path without draining the response body.
        return None

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _latest_version() -> str:
    try:
        opener = urllib.request.build_opener(_RejectMetadataRedirects())
        try:
            response = opener.open(_LATEST_URL, timeout=10)
        except urllib.error.HTTPError as error:
            error.close()
            raise
        with response:
            if urlsplit(response.geturl()).scheme != 'https':
                raise ValueError('Insecure release response')
            raw = response.read(_MAX_BYTES + 1)
    except (OSError, HTTPException, ValueError):
        raise ValueError('Cannot fetch the latest Claude Code version') from None
    return _parse_version(raw, 'latest release metadata')


def _directory_identity(directory: Path) -> tuple[int, int] | None:
    try:
        info = directory.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _protect_program_directory(program_dir: Path, paths: HostPaths, parent: Path) -> None:
    program = program_dir.resolve()
    protected = [program, Path(os.path.realpath(paths.data_dir / 'releases'))]
    if program.parent.name == 'releases' and (program / 'manifest.json').is_file():
        protected.append(program.parent)
    if any(parent.is_relative_to(root) for root in protected):
        raise ValueError(f'Cannot save a Claude Code version pin inside the program directory: {parent}')


def read_code_version(program_dir: Path, paths: HostPaths) -> str:
    return _select_code_version(program_dir, paths, refresh=False)


def update_code_version(program_dir: Path, paths: HostPaths) -> str:
    return _select_code_version(program_dir, paths, refresh=True)


def _select_code_version(program_dir: Path, paths: HostPaths, *, refresh: bool) -> str:
    destination = code_version_path(paths)
    parent = destination.parent
    resolved = Path(os.path.realpath(parent))
    identity = _directory_identity(parent)
    if refresh:
        _protect_program_directory(program_dir, paths, resolved)
    previous = _read_pin(destination, updating=refresh)
    if not refresh:
        if previous is not None:
            return previous[0]
        _protect_program_directory(program_dir, paths, resolved)

    latest = _latest_version()
    changed = f'Claude Code version changed during update: {destination}'

    def check_selection(directory: int | None = None) -> None:
        if (Path(os.path.realpath(parent)) != resolved
                or (identity is not None and _directory_identity(parent) != identity)
                or _read_pin(destination, updating=True) != previous
                or (directory is not None and not os.path.samestat(parent.stat(), os.fstat(directory)))):
            raise ValueError(changed)
        _protect_program_directory(program_dir, paths, resolved)

    check_selection()
    if previous is not None:
        current_numbers = tuple(map(int, previous[0].split('.')))
        latest_numbers = tuple(map(int, latest.split('.')))
        if latest_numbers <= current_numbers:
            return previous[0]

    try:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # Serialize the recheck and replacement between launcher processes.
            fcntl.flock(directory, fcntl.LOCK_EX)
            check_selection(directory)
            temporary = f'.claude-code-{uuid.uuid4().hex}'
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
        raise ValueError(f'Cannot publish the Claude Code version pin: {destination}') from None
    return latest

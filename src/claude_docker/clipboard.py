"""Explicit clipboard transfers and an opt-in, private Desktop sync worker."""

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time

from .desktop import XephyrManager, process_identity
from .model import DesktopRuntime, ProcessIdentity
from .paths import HostPaths, resolve_host_paths

_WARNING = 'WARNING: Clipboard contents can include secrets; enabling bidirectional host/Desktop transfer.\n'


def warn_sync() -> None:
    sys.stderr.write(_WARNING)


def _validated(runtime: DesktopRuntime) -> HostPaths:
    paths = resolve_host_paths(os.environ, os.getuid(), create_runtime=False)
    if XephyrManager(paths).load() != runtime:
        raise ValueError('Clipboard requires a live recorded Desktop runtime')
    if not os.environ.get('DISPLAY'):
        raise ValueError('Clipboard requires a host DISPLAY')
    if shutil.which('xclip') is None:
        raise ValueError('Clipboard requires xclip')
    return paths


@contextmanager
def _directory(paths: HostPaths, *, locked: bool = False,
               runtime: DesktopRuntime | None = None):
    descriptor = XephyrManager._open_private(paths.runtime_dir)
    try:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        if runtime is not None and XephyrManager(paths).load() != runtime:
            raise ValueError('Clipboard requires a live recorded Desktop runtime')
        XephyrManager(paths)._check_directory(paths.runtime_dir, descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _payload(directory: int):
    name = '.clipboard-' + secrets.token_hex(16)
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    with os.fdopen(descriptor, 'w+b') as stream:
        # Remove the directory entry before invoking a subprocess. Even a
        # forced worker exit cannot leave clipboard bytes on the filesystem.
        info = os.fstat(stream.fileno())
        try:
            os.unlink(name, dir_fd=directory)
            os.fchmod(stream.fileno(), 0o600)
            yield stream
        finally:
            try:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not os.path.samestat(current, info):
                    raise ValueError('Clipboard payload identity changed')
                os.unlink(name, dir_fd=directory)


def _xclip(argv: list[str], *, env: dict[str, str], directory: int, **streams) -> None:
    try:
        subprocess.run(['xclip', '-selection', 'clipboard', *argv], env=env,
                       pass_fds=(directory,), stderr=subprocess.DEVNULL,
                       check=True, timeout=5, shell=False, **streams)
    except (OSError, subprocess.SubprocessError):
        pass
    else:
        return
    # Raise outside the handler so no captured bytes survive in __context__.
    raise ValueError('Clipboard xclip operation failed') from None


def _environments(runtime: DesktopRuntime, host_display: str, directory: int):
    if not host_display:
        raise ValueError('Clipboard requires a host DISPLAY')
    host = dict(os.environ, DISPLAY=host_display)
    nested = dict(os.environ, DISPLAY=f':{runtime.display}',
                  XAUTHORITY=f'/proc/self/fd/{directory}/Xauthority')
    return host, nested


def _read_selection(env: dict[str, str], directory: int) -> tuple[str, bytes]:
    with _payload(directory) as stream:
        _xclip(['-out', '-target', 'TARGETS'], env=env, directory=directory, stdout=stream)
        stream.seek(0)
        targets = stream.read().split()
    target = ('image/png' if b'image/png' in targets else
              'UTF8_STRING' if b'UTF8_STRING' in targets else None)
    if target is None:
        raise ValueError('Clipboard has no supported PNG or UTF-8 target')
    with _payload(directory) as stream:
        _xclip(['-out', '-target', target], env=env, directory=directory, stdout=stream)
        stream.seek(0)
        return target, stream.read()


def _write_selection(selection: tuple[str, bytes], env: dict[str, str], directory: int) -> None:
    target, value = selection
    with _payload(directory) as stream:
        stream.write(value)
        stream.seek(0)
        _xclip(['-in', '-target', target], env=env, directory=directory, stdin=stream,
               stdout=subprocess.DEVNULL)


def _transfer(runtime: DesktopRuntime, host_display: str, *, push: bool) -> None:
    paths = _validated(runtime)
    if push:
        sys.stderr.write('WARNING: Host clipboard contents can include secrets; copying into Desktop.\n')
    with _directory(paths, runtime=runtime) as directory:
        host, nested = _environments(runtime, host_display, directory)
        source, destination = (host, nested) if push else (nested, host)
        selection = _read_selection(source, directory)
        _write_selection(selection, destination, directory)


def clipboard_push(runtime: DesktopRuntime, *, host_display: str) -> None:
    _transfer(runtime, host_display, push=True)


def clipboard_pull(runtime: DesktopRuntime, *, host_display: str) -> None:
    _transfer(runtime, host_display, push=False)


def _runtime_key(runtime: DesktopRuntime) -> dict:
    value = asdict(runtime)
    value['socket_dir'] = str(runtime.socket_dir)
    value['xauthority'] = str(runtime.xauthority)
    return value


class ClipboardSync:
    def __init__(self, paths: HostPaths, *, timeout: float = 5.0, poll_interval: float = 0.25):
        self.paths = paths
        self.timeout = timeout
        self.poll_interval = max(0.25, poll_interval)
        self._process = None

    def _read(self, directory: int) -> dict | None:
        try:
            descriptor = os.open('clipboard.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(descriptor, encoding='utf-8') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ValueError('Invalid clipboard worker record')
                record = json.load(stream)
                if (not isinstance(record, dict)
                        or (record.get('record_device'), record.get('record_inode')) != (info.st_dev, info.st_ino)):
                    raise ValueError('Clipboard worker record file changed')
            identity = ProcessIdentity(**record['identity'])
            if (type(identity.pid) is not int or identity.pid <= 0
                    or type(identity.start_ticks) is not int
                    or identity.executable != str(Path(sys.executable).resolve())
                    or not isinstance(record['runtime'], dict)
                    or not isinstance(record['host_display'], str)):
                raise ValueError('Invalid clipboard worker identity')
            return record
        except (TypeError, KeyError, UnicodeError, json.JSONDecodeError):
            raise ValueError('Invalid clipboard worker record') from None

    def _save(self, directory: int, record: dict) -> None:
        name = '.clipboard-record-' + secrets.token_hex(8)
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                info = os.fstat(stream.fileno())
                record = dict(record, record_device=info.st_dev, record_inode=info.st_ino)
                json.dump(record, stream)
                stream.write('\n')
            XephyrManager(self.paths)._check_directory(self.paths.runtime_dir, directory)
            os.replace(name, 'clipboard.json', src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _stop(self, identity: ProcessIdentity, directory: int) -> None:
        record = self._read(directory)
        if record is None:
            return
        if record['identity'] != asdict(identity):
            raise ValueError('Clipboard worker identity no longer matches')
        current = process_identity(identity.pid)
        if current is not None and current != identity:
            raise ValueError('Clipboard worker identity no longer matches')
        if current is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                current = process_identity(identity.pid)
                if current is None:
                    break
                if current != identity:
                    raise ValueError('Clipboard worker identity no longer matches')
                try:
                    os.kill(identity.pid, sig)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + self.timeout
                while True:
                    current = process_identity(identity.pid)
                    if current is None:
                        break
                    if current != identity:
                        raise ValueError('Clipboard worker identity no longer matches')
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(self.poll_interval)
                if current is None:
                    break
            else:
                raise ValueError('Clipboard worker did not terminate; record retained')
        # Only remove our matching record, never glob or remove payload paths.
        if self._read(directory) != record:
            raise ValueError('Clipboard worker identity no longer matches')
        os.unlink('clipboard.json', dir_fd=directory)
        if self._process is not None and self._process.pid == identity.pid:
            self._process.poll()

    def stop(self, identity: ProcessIdentity) -> None:
        try:
            with _directory(self.paths, locked=True) as directory:
                self._stop(identity, directory)
        except FileNotFoundError:
            return

    def stop_recorded(self) -> None:
        try:
            with _directory(self.paths, locked=True) as directory:
                record = self._read(directory)
                if record is not None:
                    self._stop(ProcessIdentity(**record['identity']), directory)
        except FileNotFoundError:
            return

    def start(self, runtime: DesktopRuntime, *, host_display: str) -> ProcessIdentity:
        if _validated(runtime).runtime_dir != self.paths.runtime_dir:
            raise ValueError('Clipboard runtime directory no longer matches')
        with _directory(self.paths, locked=True, runtime=runtime) as directory:
            record = self._read(directory)
            if record is not None:
                identity = ProcessIdentity(**record['identity'])
                current = process_identity(identity.pid)
                if current is not None and current != identity:
                    raise ValueError('Clipboard worker identity no longer matches')
                if (current == identity and record['runtime'] == _runtime_key(runtime)
                        and record['host_display'] == host_display
                        and record.get('host_authority') == os.environ.get('XAUTHORITY')):
                    return identity
                self._stop(identity, directory)
            launcher = Path(__file__).resolve().parents[2] / 'claude-docker'
            process = subprocess.Popen([sys.executable, str(launcher), '_clipboard-worker'],
                                       start_new_session=True, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._process = process
            identity = process_identity(process.pid)
            if (identity is None or identity.executable != str(Path(sys.executable).resolve())):
                # Popen owns this direct child; never signal a PID recovered from a record here.
                process.terminate()
                process.wait(timeout=self.timeout)
                raise ValueError('Cannot verify clipboard worker identity')
            try:
                self._save(directory, dict(identity=asdict(identity), runtime=_runtime_key(runtime),
                                           host_display=host_display,
                                           host_authority=os.environ.get('XAUTHORITY')))
            except BaseException:
                if process_identity(identity.pid) == identity:
                    process.terminate()
                    process.wait(timeout=self.timeout)
                raise
            return identity

    def run(self, runtime: DesktopRuntime, *, host_display: str) -> None:
        """Poll no faster than four times a second; remember only in-memory digests."""
        paths = _validated(runtime)
        last = [None, None]
        while True:
            with _directory(paths) as directory:
                # Revalidate after opening the retained directory, so rotation
                # cannot substitute an unchecked authority between these steps.
                if XephyrManager(paths).load() != runtime:
                    return
                XephyrManager(paths)._check_directory(paths.runtime_dir, directory)
                environments = _environments(runtime, host_display, directory)
                for side in (0, 1):
                    try:
                        selection = _read_selection(environments[side], directory)
                    except ValueError:
                        # An absent/unsupported selection does not clear the other side.
                        continue
                    digest = hashlib.sha256(selection[0].encode() + b'\0' + selection[1]).digest()
                    if digest != last[side]:
                        _write_selection(selection, environments[1 - side], directory)
                        last[:] = [digest, digest]
            time.sleep(self.poll_interval)


def worker_main() -> int:
    """Private launcher entry point; no clipboard bytes or exceptions are printed."""
    def terminate(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        paths = resolve_host_paths(os.environ, os.getuid(), create_runtime=False)
        sync = ClipboardSync(paths)
        identity = process_identity(os.getpid())
        deadline = time.monotonic() + sync.timeout
        while True:
            with _directory(paths) as directory:
                record = sync._read(directory)
            if record is not None and record['identity'] == asdict(identity):
                break
            if time.monotonic() >= deadline:
                return 1
            time.sleep(sync.poll_interval)
        runtime = XephyrManager(paths).load()
        if runtime is None or record['runtime'] != _runtime_key(runtime):
            return 1
        sync.run(runtime, host_display=record['host_display'])
        return 0
    except (ValueError, OSError, subprocess.SubprocessError):
        return 1

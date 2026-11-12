"""Private authenticated Xephyr display and exact process ownership checks."""

from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import asdict
import json
import os
import pwd
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import struct
import sys
import time

from .commands import CommandRunner
from .compose import ComposeProject, validate_desktop_uid
from .environment import select_environment_names
from .model import AppConfig, DesktopRuntime, ProcessIdentity
from .mounts import prepare_mounts
from .process import docker_exec_argv
from .paths import (HostPaths, _private_runtime, desktop_socket_dir, resolve_host_paths,
                    resolve_program_dir)
from .seccomp import resolve_desktop_seccomp

_X11_DIR = Path('/tmp/.X11-unix')
_PROC_ROOT = Path('/proc')


def process_identity(pid: int) -> ProcessIdentity | None:
    """Read field 22 safely even when the command name contains parentheses."""
    if pid <= 0:
        return None
    directory = _PROC_ROOT / str(pid)
    try:
        fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return ProcessIdentity(pid, int(fields[19]), os.readlink(directory / 'exe'))
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (IndexError, ValueError, PermissionError) as error:
        raise ValueError('Cannot verify process identity') from error


class XephyrManager:
    def __init__(self, paths: HostPaths, *, screen: str = '1600x1000',
                 resize: bool = True, timeout: float = 5.0, poll_interval: float = 0.05):
        self.paths = paths
        self.screen = screen
        self.resize = resize
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.record_path = paths.runtime_dir / 'xephyr.json'
        self._process = None

    def select_display(self, *, socket_dir_fd: int | None = None) -> int:
        if socket_dir_fd is None:
            directory = desktop_socket_dir(os.getuid())
            with ExitStack() as stack:
                parent = self._open_private(directory.parent)
                stack.callback(os.close, parent)
                sockets = self._open_private(Path(directory.name), dir_fd=parent)
                stack.callback(os.close, sockets)
                return self.select_display(socket_dir_fd=sockets)
        for number in range(20, 100):
            path = _X11_DIR / f'X{number}'
            if os.path.lexists(path):
                continue
            try:
                os.stat(path.name, dir_fd=socket_dir_fd, follow_symlinks=False)
                continue
            except FileNotFoundError:
                pass
            # A live but not-yet-listening abstract socket is occupied too.
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.bind('\0' + str(path))
                except OSError:
                    continue
            return number
        raise ValueError('No free nested X display in range 20-99')

    def _authority(self, display: int, runtime_fd: int) -> None:
        descriptor = os.open('Xauthority', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=runtime_fd)
        created = os.fstat(descriptor)
        os.close(descriptor)
        cookie = secrets.token_hex(16)
        number = str(display).encode().hex()
        protocol = b'MIT-MAGIC-COOKIE-1'.hex()
        # xauth's cwd and all its auxiliary files stay in the retained directory.
        # FamilyWild crosses container hostnames; stdin keeps cookies out of argv.
        record = f'ffff 0000 {len(str(display)):04x} {number} 0012 {protocol} 0010 {cookie}\n'
        failed = False
        try:
            subprocess.run(['xauth', '-f', 'Xauthority', 'nmerge', '-'], input=record,
                           cwd=f'/proc/self/fd/{runtime_fd}', pass_fds=(runtime_fd,),
                           text=True, capture_output=True, check=True, timeout=self.timeout)
        except BaseException as error:
            try:
                current = os.stat('Xauthority', dir_fd=runtime_fd, follow_symlinks=False)
                if os.path.samestat(created, current):
                    os.unlink('Xauthority', dir_fd=runtime_fd)
            except FileNotFoundError:
                pass
            if not isinstance(error, (OSError, subprocess.SubprocessError)):
                raise
            failed = True
        if failed:
            raise ValueError('Cannot create private Xauthority database') from None
        descriptor = os.open('Xauthority', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=runtime_fd)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _peer_pid(self, path: Path) -> int:
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(min(self.timeout, 0.1))
            peer.connect(str(path))
            pid, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise ValueError('Nested X socket has a different owner')
            return pid

    def _socket_owned(self, path: Path, identity: ProcessIdentity) -> os.stat_result | None:
        """Certify the current endpoint, not the pathname remembered by a socket."""
        if process_identity(identity.pid) != identity:
            return None
        try:
            before = path.lstat()
            if not stat.S_ISSOCK(before.st_mode):
                return None
            directory = _PROC_ROOT / str(identity.pid)
            descriptors = {os.readlink(entry) for entry in (directory / 'fd').iterdir()}
            held = any(len(fields := line.split(maxsplit=7)) == 8
                       and fields[7] == str(path) and f'socket:[{fields[6]}]' in descriptors
                       for line in (directory / 'net/unix').read_text().splitlines()[1:])
            if not held or self._peer_pid(path) != identity.pid:
                return None
            after = path.lstat()
            if os.path.samestat(before, after) and process_identity(identity.pid) == identity:
                return after
        except (OSError, ValueError):
            pass
        return None

    def _save(self, runtime: DesktopRuntime, runtime_fd: int,
              socket_info: os.stat_result | None = None) -> None:
        value = asdict(runtime)
        value['socket_dir'] = str(runtime.socket_dir)
        value['xauthority'] = str(runtime.xauthority)
        value['socket_device'] = socket_info.st_dev if socket_info is not None else None
        value['socket_inode'] = socket_info.st_ino if socket_info is not None else None
        authority = os.stat('Xauthority', dir_fd=runtime_fd, follow_symlinks=False)
        value['authority_device'] = authority.st_dev
        value['authority_inode'] = authority.st_ino
        temporary = '.xephyr-' + secrets.token_hex(8)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=runtime_fd)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                info = os.fstat(stream.fileno())
                value['record_device'] = info.st_dev
                value['record_inode'] = info.st_ino
                json.dump(value, stream)
                stream.write('\n')
            os.replace(temporary, 'xephyr.json', src_dir_fd=runtime_fd, dst_dir_fd=runtime_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=runtime_fd)
            except FileNotFoundError:
                pass

    def _check_directory(self, path: Path, descriptor: int) -> None:
        current = self._open_private(path)
        try:
            if not os.path.samestat(os.fstat(current), os.fstat(descriptor)):
                raise ValueError('Private Desktop directory changed during startup')
        finally:
            os.close(current)

    def start(self) -> DesktopRuntime:
        _private_runtime(self.paths.runtime_dir, os.getuid())
        with ExitStack() as stack:
            runtime_fd = self._open_private(self.paths.runtime_dir)
            stack.callback(os.close, runtime_fd)
            try:
                os.stat('xephyr.json', dir_fd=runtime_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('A recorded Xephyr runtime already exists; stop it first')
            directory = desktop_socket_dir(os.getuid())
            parent_fd = self._open_private(directory.parent)
            stack.callback(os.close, parent_fd)
            sockets_fd = self._open_private(Path(directory.name), dir_fd=parent_fd)
            stack.callback(os.close, sockets_fd)
            source_fd = os.open(_X11_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, source_fd)
            display = self.select_display(socket_dir_fd=sockets_fd)
            authority = self.paths.runtime_dir / 'Xauthority'
            runtime = None
            created_authority = False
            process = None
            authority_info = None
            linked_info = None
            name = f'X{display}'
            try:
                self._authority(display, runtime_fd)
                created_authority = True
                authority_info = os.stat('Xauthority', dir_fd=runtime_fd, follow_symlinks=False)
                self._check_directory(self.paths.runtime_dir, runtime_fd)
                # Inherit the retained directory so Xephyr's authority reads also
                # stay anchored if the lexical runtime path is substituted.
                argv = ['Xephyr', f':{display}', '-auth', f'/proc/self/fd/{runtime_fd}/Xauthority',
                        '-nolisten', 'tcp', '-screen', self.screen]
                if self.resize:
                    argv.append('-resizeable')
                process = subprocess.Popen(argv, start_new_session=True, pass_fds=(runtime_fd,),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._process = process
                identity = process_identity(process.pid)
                expected = shutil.which('Xephyr')
                if (identity is None or expected is None
                        or identity.executable != str(Path(expected).resolve())):
                    raise ValueError('Cannot verify Xephyr process identity')
                runtime = DesktopRuntime(display, directory, authority, identity)
                self._save(runtime, runtime_fd)
                source = _X11_DIR / name
                deadline = time.monotonic() + self.timeout
                while True:
                    if process.poll() is not None:
                        raise ValueError('Xephyr exited before its socket was ready')
                    verified = self._socket_owned(source, identity)
                    if verified is not None:
                        os.link(name, name, src_dir_fd=source_fd, dst_dir_fd=sockets_fd,
                                follow_symlinks=False)
                        linked_info = verified
                        after = os.stat(name, dir_fd=sockets_fd, follow_symlinks=False)
                        reverified = self._socket_owned(source, identity)
                        if (not os.path.samestat(verified, after) or reverified is None
                                or not os.path.samestat(reverified, after)):
                            raise ValueError('Xephyr socket ownership changed during startup')
                        self._save(runtime, runtime_fd, after)
                        self._check_directory(self.paths.runtime_dir, runtime_fd)
                        self._check_directory(directory.parent, parent_fd)
                        self._check_directory(directory, sockets_fd)
                        return runtime
                    if time.monotonic() >= deadline:
                        raise ValueError('Timed out waiting for the authenticated Xephyr socket')
                    time.sleep(self.poll_interval)
            except BaseException:
                if runtime is not None:
                    self._terminate(runtime.xephyr_identity)
                    try:
                        os.unlink('xephyr.json', dir_fd=runtime_fd)
                    except FileNotFoundError:
                        pass
                elif process is not None:
                    # This unreaped direct child was created by this call, even
                    # if interruption prevented capturing its /proc identity.
                    process.terminate()
                    try:
                        process.wait(timeout=self.timeout)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=self.timeout)
                if linked_info is not None:
                    try:
                        current = os.stat(name, dir_fd=sockets_fd, follow_symlinks=False)
                        if os.path.samestat(linked_info, current):
                            os.unlink(name, dir_fd=sockets_fd)
                    except FileNotFoundError:
                        pass
                if created_authority:
                    try:
                        current = os.stat('Xauthority', dir_fd=runtime_fd, follow_symlinks=False)
                        if os.path.samestat(current, authority_info):
                            os.unlink('Xauthority', dir_fd=runtime_fd)
                    except FileNotFoundError:
                        pass
                raise

    def load(self) -> DesktopRuntime | None:
        """Reuse grants only while their private paths and exact endpoint still match."""
        try:
            descriptor = self._open_private(self.paths.runtime_dir)
        except FileNotFoundError:
            return None
        with ExitStack() as stack:
            stack.callback(os.close, descriptor)
            try:
                record = os.open('xephyr.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            try:
                with os.fdopen(record, encoding='utf-8') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o600):
                        raise ValueError('Invalid recorded Desktop runtime')
                    value = json.load(stream)
                    if not self._recorded_file_matches(info, value, 'record'):
                        raise ValueError('Recorded Desktop runtime file changed')
                display = value['display']
                if type(display) is not int or not 20 <= display <= 99:
                    raise ValueError('Invalid recorded Desktop runtime')
                identity = ProcessIdentity(**value['xephyr_identity'])
                expected = shutil.which('Xephyr')
                if (type(identity.pid) is not int or type(identity.start_ticks) is not int
                        or expected is None or identity.executable != str(Path(expected).resolve())
                        or process_identity(identity.pid) != identity):
                    raise ValueError('Invalid or stale recorded Desktop runtime; stop it first')
                directory = desktop_socket_dir(os.getuid())
                authority = self.paths.runtime_dir / 'Xauthority'
                if value['socket_dir'] != str(directory) or value['xauthority'] != str(authority):
                    raise ValueError('Invalid recorded Desktop runtime paths')
                parent = self._open_private(directory.parent)
                stack.callback(os.close, parent)
                sockets = self._open_private(Path(directory.name), dir_fd=parent)
                stack.callback(os.close, sockets)
                cookie = os.open('Xauthority', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
                stack.callback(os.close, cookie)
                info = os.fstat(cookie)
                if not self._recorded_file_matches(info, value, 'authority'):
                    raise ValueError('Invalid recorded Desktop runtime authority')
                linked = os.stat(f'X{display}', dir_fd=sockets, follow_symlinks=False)
                source = self._socket_owned(_X11_DIR / f'X{display}', identity)
                if (source is None or not os.path.samestat(source, linked)
                        or (linked.st_dev, linked.st_ino) != (value['socket_device'], value['socket_inode'])):
                    raise ValueError('Invalid recorded Desktop runtime socket')
                self._check_directory(self.paths.runtime_dir, descriptor)
                self._check_directory(directory.parent, parent)
                self._check_directory(directory, sockets)
                return DesktopRuntime(display, directory, authority, identity)
            except (KeyError, TypeError, json.JSONDecodeError, UnicodeError, OSError):
                raise ValueError('Cannot verify recorded Desktop runtime; stop it first') from None

    def _wait(self, identity: ProcessIdentity) -> bool:
        deadline = time.monotonic() + self.timeout
        while True:
            current = process_identity(identity.pid)
            if current is None:
                if self._process is not None and self._process.pid == identity.pid:
                    self._process.poll()  # Reap our child if it has exited.
                return True
            if current != identity:
                raise ValueError('Xephyr process identity no longer matches')
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_interval)

    def _terminate(self, identity: ProcessIdentity) -> None:
        current = process_identity(identity.pid)
        if current is None:
            if self._process is not None and self._process.pid == identity.pid:
                self._process.poll()
            return
        if current != identity:
            raise ValueError('Xephyr process identity no longer matches')
        for sig in (signal.SIGTERM, signal.SIGKILL):
            current = process_identity(identity.pid)
            if current is None:
                if self._process is not None and self._process.pid == identity.pid:
                    self._process.poll()
                return
            if current != identity:
                raise ValueError('Xephyr process identity no longer matches')
            try:
                os.kill(identity.pid, sig)
            except ProcessLookupError:
                return
            if self._wait(identity):
                return
        raise ValueError('Xephyr did not terminate; private runtime retained')

    @staticmethod
    def _recorded_file_matches(info: os.stat_result, value: object, name: str) -> bool:
        return (isinstance(value, dict) and stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
                and (value.get(name + '_device'), value.get(name + '_inode')) ==
                (info.st_dev, info.st_ino))

    @staticmethod
    def _open_private(path: Path, *, dir_fd: int | None = None) -> int:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=dir_fd)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(descriptor)
            raise ValueError('Private Desktop runtime ownership or permissions changed')
        return descriptor

    def stop(self, runtime: DesktopRuntime) -> None:
        self._stop_recorded(runtime, strict_socket=False)

    def stop_recorded(self) -> None:
        """Teardown needs the record, not a live display or installed GUI tools."""
        self._stop_recorded(None, strict_socket=True)

    def _stop_recorded(self, runtime: DesktopRuntime | None, *, strict_socket: bool) -> None:
        if runtime is not None:
            current = process_identity(runtime.xephyr_identity.pid)
            if current is not None and current != runtime.xephyr_identity:
                raise ValueError('Xephyr process identity no longer matches')
        try:
            descriptor = self._open_private(self.paths.runtime_dir)
        except FileNotFoundError:
            if runtime is not None and process_identity(runtime.xephyr_identity.pid) is not None:
                raise ValueError('Missing recorded Xephyr process identity') from None
            return
        with ExitStack() as stack:
            stack.callback(os.close, descriptor)
            try:
                record = os.open('xephyr.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if runtime is not None and process_identity(runtime.xephyr_identity.pid) is not None:
                    raise ValueError('Missing recorded Xephyr process identity') from None
                return
            try:
                with os.fdopen(record, encoding='utf-8') as stream:
                    record_info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(record_info.st_mode) or record_info.st_uid != os.getuid()
                            or stat.S_IMODE(record_info.st_mode) != 0o600):
                        raise ValueError('Invalid recorded Desktop runtime')
                    value = json.load(stream)
                    if not self._recorded_file_matches(record_info, value, 'record'):
                        raise ValueError('Recorded Desktop runtime file changed')
                identity = ProcessIdentity(**value['xephyr_identity'])
                display = value['display']
                if (type(display) is not int or not 20 <= display <= 99
                        or type(identity.pid) is not int or identity.pid <= 0
                        or type(identity.start_ticks) is not int or identity.start_ticks < 0
                        or not isinstance(identity.executable, str)
                        or not Path(identity.executable).is_absolute()):
                    raise ValueError('Invalid recorded Xephyr identity')
                recorded = DesktopRuntime(display, Path(value['socket_dir']),
                                          Path(value['xauthority']), identity)
                if runtime is not None and runtime != recorded:
                    raise ValueError('Recorded Xephyr runtime identity no longer matches')
                if (recorded.xauthority != self.paths.runtime_dir / 'Xauthority'
                        or (runtime is None and recorded.socket_dir != _desktop_socket_path())):
                    raise ValueError('Recorded Desktop runtime paths no longer match')
            except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
                raise ValueError('Invalid recorded Desktop runtime') from None
            self._check_directory(self.paths.runtime_dir, descriptor)
            self._terminate(identity)
            failures = []
            # Missing endpoints are normal; substituted endpoints are never removed.
            try:
                parent = self._open_private(recorded.socket_dir.parent)
                stack.callback(os.close, parent)
                sockets = self._open_private(Path(recorded.socket_dir.name), dir_fd=parent)
                stack.callback(os.close, sockets)
                try:
                    info = os.stat(f'X{display}', dir_fd=sockets, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if (stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                            and (info.st_dev, info.st_ino) ==
                            (value.get('socket_device'), value.get('socket_inode'))):
                        self._check_directory(recorded.socket_dir.parent, parent)
                        self._check_directory(recorded.socket_dir, sockets)
                        os.unlink(f'X{display}', dir_fd=sockets)
                    elif strict_socket:
                        failures.append('Desktop socket identity changed')
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                failures.append('Desktop socket cleanup failed')
            try:
                info = os.stat('Xauthority', dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if self._recorded_file_matches(info, value, 'authority'):
                    self._check_directory(self.paths.runtime_dir, descriptor)
                    os.unlink('Xauthority', dir_fd=descriptor)
                else:
                    failures.append('Desktop authority identity changed')
            if failures:
                raise ValueError('; '.join(failures))
            current = os.stat('xephyr.json', dir_fd=descriptor, follow_symlinks=False)
            if not os.path.samestat(current, record_info):
                raise ValueError('Recorded Desktop runtime file changed')
            self._check_directory(self.paths.runtime_dir, descriptor)
            os.unlink('xephyr.json', dir_fd=descriptor)


def _desktop_socket_path() -> Path:
    # Unlike desktop_socket_dir(), teardown must never create/chmod directories.
    return Path('/tmp') / f'claude-docker-{os.getuid()}' / 'x11'


def stop_desktop_runtime(paths: HostPaths, *, project: ComposeProject | None = None) -> None:
    """Stop only this user's recorded Desktop resources, retaining real failures."""
    from .clipboard import ClipboardSync
    from .config import AccountInfo, load_config

    failures = []
    try:
        ClipboardSync(paths).stop_recorded()
    except (ValueError, OSError, subprocess.SubprocessError):
        failures.append('Clipboard worker cleanup failed')
    try:
        if project is None:
            uid = os.getuid()
            account = pwd.getpwuid(uid)
            config = load_config(env=os.environ, paths=paths,
                                 account=AccountInfo(account.pw_name, uid, os.getgid(), Path(account.pw_dir)))
            project = ComposeProject(config, paths, (), CommandRunner())
        project.stop_service('claude-desktop')
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        failures.append('Desktop service cleanup failed')
    try:
        XephyrManager(paths).stop_recorded()
    except (ValueError, OSError, subprocess.SubprocessError):
        failures.append('Xephyr runtime cleanup failed')
    if failures:
        raise ValueError('; '.join(failures))


def _desktop_running(project: ComposeProject, container: str) -> bool:
    # The official package's /usr/bin/claude-desktop is a symlink to an ELF
    # executable named claude-desktop, within Linux's 15-byte comm limit.
    argv = docker_exec_argv(container, ['pgrep', '-x', 'claude-desktop'],
                            workdir=project.config.container.home, env_names=(),
                            interactive=False, tty=False)
    result = project.runner.run(argv, check=False, capture_output=True)
    if result.returncode not in (0, 1):
        raise ValueError('Cannot inspect Desktop application processes')
    return result.returncode == 0


def launch_desktop(config: AppConfig, *, restart: bool, clipboard_sync: bool,
                   no_sandbox: bool, app_args: Sequence[str]) -> int:
    """Launch a detached Desktop with fresh process-scoped host credentials."""
    from .clipboard import ClipboardSync, warn_sync

    validate_desktop_uid(config, os.getuid())
    env_names = select_environment_names(config.environment, os.environ)
    options = list(app_args)
    if '--' in options:
        options = options[:options.index('--')]
    app_unsafe = any(arg.split('=', 1)[0] == '--no-sandbox' for arg in options)
    unsafe = not config.desktop.sandbox or no_sandbox or app_unsafe
    program_dir = resolve_program_dir(Path(__file__).resolve().parents[2] / 'claude-docker',
                                      os.environ)
    runner = CommandRunner()
    desktop_seccomp = resolve_desktop_seccomp(config, runner, program_dir=program_dir, unsafe=unsafe)
    mounts = prepare_mounts(config)
    if not os.environ.get('DISPLAY'):
        raise ValueError('Desktop requires a host DISPLAY')
    for tool in ('Xephyr', 'xauth', 'setxkbmap'):
        if shutil.which(tool) is None:
            raise ValueError(f'Desktop requires {tool}')
    paths = resolve_host_paths(os.environ, os.getuid())
    manager = XephyrManager(paths)
    sync = ClipboardSync(paths)
    synchronize = clipboard_sync or config.desktop.clipboard == 'bidirectional'
    if synchronize and shutil.which('xclip') is None:
        raise ValueError('Clipboard synchronization requires xclip')
    runtime = manager.load()
    project = ComposeProject(config, paths, mounts, runner, program_dir=program_dir,
                             desktop_runtime=runtime, desktop_seccomp=desktop_seccomp)
    if runtime is not None:
        container = project.running_container_id('claude-desktop', runtime_mounts=False)
        if (container is not None and _desktop_running(project, container) and not restart
                and project.state.read_mode() == config.docker_mode
                and project.state.read_pending() is None):
            if synchronize:
                warn_sync()
                sync.start(runtime, host_display=os.environ['DISPLAY'])
            print('Claude Desktop is already running; its environment is unchanged. '
                  'Existing per-run sandbox, clipboard and container security choices persist until '
                  'restart or stop. '
                  'Use desktop --restart to apply current host credentials and launch policy.')
            return 0
    if restart:
        sync.stop_recorded()
        project.stop_service('claude-desktop')
        if runtime is not None:
            manager.stop(runtime)
            runtime = None
    created_runtime = False
    completed = False
    try:
        if runtime is None:
            runtime = manager.start()
            created_runtime = True
        project.desktop_runtime = runtime
        if desktop_seccomp is not None:
            sys.stderr.write("WARNING: Desktop chromium seccomp policy permits selected kernel namespace "
                             "creation operations beyond Docker's default.\n")
        project.up('claude-desktop', recreate=restart)
        container = project.container_id('claude-desktop')
        socket_check = docker_exec_argv(container, ['test', '-S', f'/tmp/.X11-unix/X{runtime.display}'],
                                       workdir=config.container.home, env_names=(),
                                       interactive=False, tty=False)
        if project.runner.run(socket_check, check=False, capture_output=True).returncode:
            raise ValueError('Nested Desktop socket is not visible inside claude-desktop')
        result = project.exec('claude-desktop', ['openbox'], workdir=config.container.home,
                              env_names=(), interactive=False, tty=False, detach=True,
                              capture_output=True)
        if result:
            sys.stderr.write(f'Openbox launch failed (exit {result}).\n')
            return result
        command = ['claude-desktop', *app_args]
        if unsafe:
            sys.stderr.write('WARNING: Chromium sandbox is disabled for this Desktop launch.\n')
            if not app_unsafe:
                command.insert(1, '--no-sandbox')
        result = project.exec('claude-desktop', command, workdir=config.container.home,
                              env_names=env_names, interactive=False, tty=False, detach=True,
                              capture_output=True)
        if result:
            sys.stderr.write(f'Claude Desktop launch failed (exit {result}); no retry was attempted.\n')
            return result
        # Docker's detached exec cannot report the eventual child exit status.
        # Confirm a stable initial process window, without retrying the application.
        seen = False
        for _ in range(5):
            time.sleep(0.1)
            running = _desktop_running(project, container)
            if seen and not running:
                break
            seen = seen or running
        if not running:
            sys.stderr.write('Claude Desktop exited during startup; no retry was attempted.\n')
            return 1
        if synchronize:
            warn_sync()
            sync.start(runtime, host_display=os.environ['DISPLAY'])
        completed = True
        return 0
    finally:
        if created_runtime and not completed:
            failures = []
            for name, cleanup in (
                    ('clipboard worker', sync.stop_recorded),
                    ('Desktop service', lambda: project.stop_service('claude-desktop')),
                    ('Xephyr runtime', lambda: manager.stop(runtime))):
                try:
                    cleanup()
                except (ValueError, OSError, subprocess.SubprocessError):
                    failures.append(name)
            if failures:
                sys.stderr.write('Desktop startup cleanup failed: ' + ', '.join(failures) + '.\n')

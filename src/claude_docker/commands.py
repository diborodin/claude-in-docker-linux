"""One shell-free subprocess boundary shared by launcher operations."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
import os
import selectors
import signal
import subprocess
import threading
import time
from typing import TextIO


# Keep Docker client configuration usable while excluding arbitrary application
# credentials and ambient Compose settings. Container credentials use docker exec.
_DOCKER_ENVIRONMENT = (
    'PATH', 'HOME', 'XDG_RUNTIME_DIR', 'DOCKER_HOST', 'DOCKER_CONTEXT',
    'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION',
    'SSH_AUTH_SOCK', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
)


def docker_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Keep Docker transport settings without ambient application credentials."""
    return {key: env[key] for key in _DOCKER_ENVIRONMENT if key in env}


class _ResolutionCancelled(BaseException):
    """Unwind active resolver work before replaying the cancellation signal."""


@contextmanager
def resolver_signals() -> Iterator[Callable[[], None] | None]:
    # Record signals without raising during process creation or bounded cleanup.
    # Active commands check after taking ownership of their child session.
    first_signal = None
    handlers = {}

    def record(signum, _frame):
        nonlocal first_signal
        if first_signal is None:
            first_signal = signum

    def check_cancelled() -> None:
        if first_signal is not None:
            raise _ResolutionCancelled

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous = signal.getsignal(signum)
                # Preserve any caller-installed or ignored dispositions.
                if previous == signal.SIG_DFL or (signum == signal.SIGINT and previous == signal.default_int_handler):
                    handlers[signum] = signal.signal(signum, record)
        yield check_cancelled if handlers else None
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
        if first_signal is not None:
            signal.raise_signal(first_signal)


def _child_has_exited(pid: int) -> bool:
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


@contextmanager
def child_session(arguments: Sequence[str], **kwargs) -> Iterator[subprocess.Popen]:
    """Own the process group until the caller finishes reading and reaps the leader."""
    with subprocess.Popen(arguments, start_new_session=True, **kwargs) as process:
        try:
            yield process
        except BaseException:
            try:
                _child_has_exited(process.pid)
                os.killpg(process.pid, signal.SIGKILL)
            except (ChildProcessError, ProcessLookupError):
                pass
            raise


def _collect_before_reap(process: subprocess.Popen[str], deadline: float,
                         timeout: float, check_cancelled: Callable[[], None] | None, *,
                         extra_output: tuple[int, bytearray, int] | None = None) -> tuple[bytearray, bytearray]:
    # Drain the pipes without communicate(), which can reap the group leader
    # during an interrupt. An exited leader's PID stays reserved until wait().
    output, errors = bytearray(), bytearray()

    def check_auxiliary_exit() -> None:
        if extra_output is not None:
            # A failed hook can leave descendants holding the protocol pipe.
            # Observe its exit without reaping so group cleanup owns the PID.
            status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is not None and (status.si_code != os.CLD_EXITED or status.si_status):
                code = status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
                raise subprocess.CalledProcessError(code, process.args)

    def remaining() -> float:
        if check_cancelled is not None:
            check_cancelled()
        check_auxiliary_exit()
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout,
                                            output=bytes(output) if process.stdout is not None else None,
                                            stderr=bytes(errors) if process.stderr is not None else None)
        return seconds

    with selectors.DefaultSelector() as selector:
        for stream, buffer in ((process.stdout, output), (process.stderr, errors)):
            if stream is not None:
                selector.register(stream, selectors.EVENT_READ, buffer)
        if extra_output is not None:
            fd, buffer, _ = extra_output
            selector.register(fd, selectors.EVENT_READ, buffer)
        while selector.get_map():
            seconds = remaining()
            poll = 0.05 if check_cancelled is not None or extra_output is not None else 3600
            for key, _ in selector.select(min(poll, seconds)):
                chunk = os.read(key.fd, 65536)
                if chunk:
                    if (extra_output is not None and key.fd == extra_output[0]
                            and len(key.data) + len(chunk) > extra_output[2]):
                        raise BufferError('auxiliary subprocess output exceeds its size limit')
                    key.data.extend(chunk)
                else:
                    selector.unregister(key.fileobj)

    while True:
        seconds = remaining()
        if _child_has_exited(process.pid):
            check_auxiliary_exit()
            return output, errors
        time.sleep(min(0.05, seconds))


def _decode_output(stream: TextIO | None, buffer: bytearray) -> str | None:
    if stream is None:
        return None
    return buffer.decode(stream.encoding, stream.errors).replace('\r\n', '\n').replace('\r', '\n')


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        capture_output: bool = False,
        timeout: float | None = None,
        stream_stderr: bool = False,
        check_cancelled: Callable[[], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise TypeError("Command must be an argument sequence, not a command string")
        if not all(isinstance(argument, str) for argument in argv):
            raise TypeError("Every command argument must be a string")
        # In progress mode, keep captured stdout distinct from live diagnostics.
        # A command whose stdout carries progress too can send both to stderr.
        stdout = subprocess.PIPE if capture_output else (2 if stream_stderr else None)
        stderr = subprocess.PIPE if capture_output and not stream_stderr else None
        arguments = list(argv)
        if timeout is None:
            return subprocess.run(arguments, env=env, check=check, stdout=stdout, stderr=stderr, text=True)

        # A bounded command can spawn a transport such as ssh. Own its
        # process group so cancellation cannot strand the transport or hit peers.
        deadline = time.monotonic() + timeout
        with child_session(arguments, env=env, stdout=stdout, stderr=stderr, text=True) as process:
            output, errors = _collect_before_reap(process, deadline, timeout, check_cancelled)
            status = process.wait()
        result = subprocess.CompletedProcess(arguments, status, _decode_output(process.stdout, output),
                                             _decode_output(process.stderr, errors))
        if check:
            result.check_returncode()
        return result

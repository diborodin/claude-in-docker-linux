"""Select and persist the Claude Code version for image builds."""

from http.client import HTTPException, IncompleteRead
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import urllib.error
from urllib.parse import urlsplit
import urllib.request

from .commands import CommandRunner, resolver_signals
from .paths import HostPaths
from .version_pin import MAX_VERSION_BYTES, select_version_pin

_PIN_NAME = 'claude-code.version'
_LATEST_URL = 'https://downloads.claude.ai/claude-code-releases/latest'
_RESOLVE_TIMEOUT = 30
_SOCKET_TIMEOUT = 10
_FETCH_ERROR = 'Cannot fetch the latest Claude Code version'
_METADATA_SOURCE = 'latest release metadata'
_INVALID_VERSION = 'Invalid Claude Code version in {}; expected numeric X.Y.Z'
_METADATA_ERRORS = {
    'certificate': f'{_FETCH_ERROR}: TLS certificate verification failed',
    'tls': f'{_FETCH_ERROR}: TLS connection failed',
    'timeout': f'{_FETCH_ERROR}: request timed out',
    'network': f'{_FETCH_ERROR}: network request failed',
    'incomplete': 'Invalid Claude Code release metadata: incomplete HTTP response',
    'invalid': _INVALID_VERSION.format(_METADATA_SOURCE),
}
_PROXY_ENVIRONMENT = {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
_TRUST_ENVIRONMENT = {'SSL_CERT_FILE', 'SSL_CERT_DIR'}
_WORKER = '''\
import sys
sys.path.insert(0, sys.argv[1])
try:
    from claude_docker.claude_version import _metadata_worker
    status = _metadata_worker()
except Exception:
    # The parent reports unexpected worker failures.
    status = 2
raise SystemExit(status)
'''


def code_version_path(paths: HostPaths) -> Path:
    return paths.config_dir / _PIN_NAME


def _parse_version(raw: bytes, source: str) -> str:
    version = raw.strip()
    if (len(raw) > MAX_VERSION_BYTES or len(version) >= MAX_VERSION_BYTES
            or not re.fullmatch(rb'[0-9]+\.[0-9]+\.[0-9]+', version)):
        raise ValueError(_INVALID_VERSION.format(source))
    return version.decode('ascii')


class _MetadataError(ValueError):
    def __init__(self, category: str, status: int | None = None) -> None:
        self.category = category
        self.status = status
        if status is None:
            message = _METADATA_ERRORS[category]
        else:
            reason = 'redirect refused' if 300 <= status < 400 else f'HTTP status {status}'
            message = f'{_FETCH_ERROR}: {reason}'
        super().__init__(message)


def _valid_failure_status(status: int) -> bool:
    return isinstance(status, int) and 100 <= status <= 599 and status != 200


def _http_metadata_error(status: int) -> _MetadataError:
    if _valid_failure_status(status):
        return _MetadataError('http', status)
    return _MetadataError('network')


def _worker_failure(error: subprocess.CalledProcessError) -> ValueError:
    if error.returncode == 1:
        # A known failure is one short category, with a status only for HTTP.
        match = re.fullmatch(r'([a-z-]{1,16})(?: ([0-9]{3}))?\n', error.output or '')
        if match:
            category, code = match.groups()
            if category == 'http' and code is not None:
                status = int(code)
                if _valid_failure_status(status):
                    return _MetadataError(category, status)
            elif code is None and category in _METADATA_ERRORS:
                return _MetadataError(category)
    return ValueError(_FETCH_ERROR)


class _RejectMetadataRedirects(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Use the default HTTP error path without draining the response body.
        return None

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _progress(message: str) -> None:
    print(f'claude-docker: {message}', file=sys.stderr, flush=True)


def _fetch_latest_version() -> str:
    try:
        opener = urllib.request.build_opener(_RejectMetadataRedirects())
        with opener.open(_LATEST_URL, timeout=_SOCKET_TIMEOUT) as response:
            if urlsplit(response.geturl()).scheme != 'https':
                raise _MetadataError('network')
            if response.status != 200:
                raise _http_metadata_error(response.status)
            _progress('Reading Claude Code release metadata.')
            raw = response.read(MAX_VERSION_BYTES + 1)
            # A bounded HTTPResponse read can return a partial fixed-length
            # body at EOF with bytes still outstanding. The parser rejects
            # oversized bodies regardless of the remaining length.
            if len(raw) <= MAX_VERSION_BYTES and response.length:
                raise IncompleteRead(raw, response.length)
    except urllib.error.HTTPError as error:
        error.close()
        raise _http_metadata_error(error.code) from None
    except _MetadataError:
        raise
    except IncompleteRead:
        raise _MetadataError('incomplete') from None
    except (OSError, HTTPException, ValueError) as error:
        cause = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(cause, ssl.SSLCertVerificationError):
            category = 'certificate'
        elif isinstance(cause, ssl.SSLError):
            category = 'tls'
        elif isinstance(cause, TimeoutError):
            category = 'timeout'
        else:
            category = 'network'
        raise _MetadataError(category) from None
    try:
        return _parse_version(raw, _METADATA_SOURCE)
    except ValueError:
        raise _MetadataError('invalid') from None


def _metadata_worker() -> int:
    try:
        version = _fetch_latest_version()
    except _MetadataError as error:
        print(error.category if error.status is None else f'{error.category} {error.status}')
        return 1
    print(version)
    return 0


def _latest_version() -> str:
    deadline = time.monotonic() + _RESOLVE_TIMEOUT
    with resolver_signals() as check_cancelled:
        _progress('Requesting the latest Claude Code version over HTTPS.')
        source = Path(__file__).resolve().parents[1]
        arguments = [sys.executable, '-I', '-S', '-B', '-c', _WORKER, str(source)]
        env = {key: value for key, value in os.environ.items()
               if key.lower() in _PROXY_ENVIRONMENT or key in _TRUST_ENVIRONMENT}
        if check_cancelled is not None:
            check_cancelled()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, _RESOLVE_TIMEOUT)
            result = CommandRunner().run(arguments, env=env, capture_output=True, stream_stderr=True,
                                         timeout=remaining, check_cancelled=check_cancelled)
            return _parse_version(result.stdout.encode('ascii'), _METADATA_SOURCE)
        except subprocess.TimeoutExpired:
            raise ValueError(f'Claude Code release lookup timed out after {_RESOLVE_TIMEOUT} seconds') from None
        except subprocess.CalledProcessError as error:
            raise _worker_failure(error) from None
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise ValueError(_FETCH_ERROR) from None


def read_code_version(program_dir: Path, paths: HostPaths) -> str:
    return _select_code_version(program_dir, paths, refresh=False)


def update_code_version(program_dir: Path, paths: HostPaths) -> str:
    return _select_code_version(program_dir, paths, refresh=True)


def _select_code_version(program_dir: Path, paths: HostPaths, *, refresh: bool) -> str:
    def is_newer(latest: str, current: str) -> bool:
        return tuple(map(int, latest.split('.'))) > tuple(map(int, current.split('.')))

    return select_version_pin(program_dir, paths, code_version_path(paths), product='Claude Code',
                              parse=_parse_version, resolve=_latest_version, refresh=refresh,
                              is_newer=is_newer)

"""Resolve a Docker daemon's upstream builtin seccomp source on GitHub."""

from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .seccomp_profile import _decode_json, decode_profile


_API = 'https://api.github.com/repos/moby'
_RAW = 'https://raw.githubusercontent.com/moby'
_DEPENDENCY = 'github.com/moby/profiles/seccomp'
_PROFILE_PATH = 'seccomp/default.json'
_MAX_RESPONSE = 2 * 1024 * 1024
_COMMIT = r'[0-9a-f]{40}'
_REPORTED_COMMIT = r'[0-9a-f]{7,40}'
_VERSION = r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
_RELEASE = _VERSION[1:] + r'(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?'
_OFFICIAL_URL = re.compile(
    rf'(?:{re.escape(_API)}/moby/commits/{_REPORTED_COMMIT}'
    rf'|{re.escape(_API)}/moby/git/ref/tags/(?:docker-)?v{_RELEASE}'
    rf'|{re.escape(_API)}/profiles/git/ref/tags/seccomp/{_VERSION}'
    rf'|{re.escape(_API)}/(?:moby|profiles)/git/tags/{_COMMIT}'
    rf'|{re.escape(_RAW)}/moby/{_COMMIT}/(?:go\.mod|vendor\.mod)'
    rf'|{re.escape(_RAW)}/profiles/{_COMMIT}/(?:{re.escape(_PROFILE_PATH)}|LICENSE))'
)
_SUPPORTED_MODULES = {'github.com/docker/docker', 'github.com/moby/moby', 'github.com/moby/moby/v2'}
_BLOCK_DIRECTIVES = {'require', 'replace', 'exclude', 'godebug', 'tool', 'ignore'}
_SINGLETON_DIRECTIVES = {'module', 'go', 'toolchain'}
_DIRECTIVE_ARITY = {
    'module': 1, 'go': 1, 'toolchain': 1, 'require': 2, 'exclude': 2,
    'godebug': 1, 'tool': 1, 'ignore': 1,
}
_MANIFEST_TOKEN = re.compile(r'//.*|[ \t\r]+|[()]|"(?:[^"\\]|\\.)*"|[^\s"`()]+')
_GO_VERSION = r'[1-9][0-9]*\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?(?:[a-z]+[0-9]+)?'
_TOOLCHAIN = r'default|go1(?:\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?(?:[a-z]+[0-9]+)?(?:-\S+)?)?'
Fetch = Callable[[str], bytes]


@dataclass(frozen=True)
class Baseline:
    source_commit: str
    profile_commit: str
    module_version: str
    profile: bytes
    license: bytes
    profile_path: str
    source_ref: str


class _FetchError(ValueError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Raise before urllib parses Location or consumes the redirect body.
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _http_failure(error: HTTPError) -> _FetchError:
    error.close()
    return _FetchError(f'official upstream returned HTTP {error.code}', status=error.code)


def _transport_failure(error: OSError | HTTPException) -> _FetchError:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, TimeoutError):
        return _FetchError('official upstream request timed out')
    return _FetchError('could not read official upstream response')


def _check_size(raw: bytes) -> bytes:
    if not isinstance(raw, bytes) or not raw:
        raise _FetchError('official upstream returned an empty or invalid response')
    if len(raw) > _MAX_RESPONSE:
        raise _FetchError('official upstream response exceeds 2 MiB')
    return raw


def fetch_official(url: str) -> bytes:
    """Fetch a bounded response from an allowed official source URL."""
    if not isinstance(url, str) or not _OFFICIAL_URL.fullmatch(url):
        raise ValueError('Unsupported official upstream source URL')
    request = Request(url, headers={'User-Agent': 'claude-docker',
                                    'Accept': 'application/vnd.github+json' if url.startswith(_API) else '*/*',
                                    'Accept-Encoding': 'identity'})
    try:
        with build_opener(_NoRedirect).open(request, timeout=10) as response:
            if response.status != 200:
                raise _FetchError(f'official upstream returned HTTP {response.status}',
                                  status=response.status)
            length = response.headers.get('Content-Length')
            if length is not None:
                if not re.fullmatch(r'[0-9]+', length):
                    raise _FetchError('official upstream returned an invalid Content-Length')
                digits = length.lstrip('0') or '0'
                if len(digits) > 7 or int(digits) > _MAX_RESPONSE:
                    raise _FetchError('official upstream response exceeds 2 MiB')
            raw = _check_size(response.read(_MAX_RESPONSE + 1))
            if length is not None and len(raw) != int(digits):
                raise _FetchError('official upstream response was incomplete')
            return raw
    except HTTPError as error:
        raise _http_failure(error) from None
    except (OSError, HTTPException) as error:
        raise _transport_failure(error) from None


def _read(fetch: Fetch, url: str, label: str) -> bytes:
    try:
        return _check_size(fetch(url))
    except HTTPError as error:
        failure = _http_failure(error)
    except _FetchError as error:
        failure = error
    except (OSError, HTTPException) as error:
        failure = _transport_failure(error)
    except ValueError:
        failure = _FetchError('could not read official upstream response')
    raise _FetchError(f'Cannot fetch {label}: {failure}', status=failure.status) from None


def _commit(fetch: Fetch, url: str, label: str, *, prefix: str = '') -> str:
    raw = _read(fetch, url, label)
    try:
        payload = _decode_json(raw)
        sha = payload.get('sha') if isinstance(payload, dict) else None
        if isinstance(sha, str) and re.fullmatch(_COMMIT, sha) and sha.startswith(prefix):
            return sha
    except ValueError:
        pass
    raise ValueError(f'Cannot verify {label}: expected a matching full commit SHA')


def _manifest_fields(line: str) -> list[str]:
    fields = []
    position = 0
    for match in _MANIFEST_TOKEN.finditer(line):
        if match.start() != position:
            raise ValueError('unsupported manifest token')
        token = match.group()
        position = match.end()
        if token.isspace():
            continue
        if token.startswith('"'):
            fields.append(token)
        else:
            before_comment, marker, _ = token.partition('//')
            if before_comment:
                fields.append(before_comment)
            if marker:
                return fields
    if position != len(line):
        raise ValueError('unsupported manifest token')
    return fields


def _manifest_directives(raw: bytes):
    block = None
    seen_singletons = set()
    for line in raw.decode('utf-8').split('\n'):
        fields = _manifest_fields(line)
        if not fields:
            continue
        if block is not None:
            if fields == [')']:
                block = None
                continue
            directive = block
        else:
            directive, *fields = fields
            if fields == ['('] and directive in _BLOCK_DIRECTIVES:
                block = directive
                continue
        if '(' in fields or ')' in fields:
            raise ValueError('unexpected manifest block delimiter')
        if directive in _SINGLETON_DIRECTIVES:
            if directive in seen_singletons:
                raise ValueError('repeated manifest directive')
            seen_singletons.add(directive)
        if directive == 'replace':
            if fields.count('=>') != 1:
                raise ValueError('invalid replace directive')
            target = fields.index('=>')
            if target not in (1, 2) or len(fields) - target - 1 not in (1, 2):
                raise ValueError('invalid replace directive target')
        elif len(fields) != _DIRECTIVE_ARITY.get(directive):
            raise ValueError('unsupported manifest directive or field count')
        if directive == 'go' and not re.fullmatch(_GO_VERSION, fields[0]):
            raise ValueError('invalid go version')
        if directive == 'toolchain' and not re.fullmatch(_TOOLCHAIN, fields[0]):
            raise ValueError('invalid toolchain name')
        fields = [_decode_json(field.encode('utf-8')) if field.startswith('"') else field
                  for field in fields]
        yield directive, fields
    if block is not None:
        raise ValueError('unterminated manifest directive block')


def _module_version(raw: bytes, manifest: str) -> str:
    modules = []
    requires = []
    overridden = False
    try:
        for directive, fields in _manifest_directives(raw):
            if directive == 'module':
                modules.append(fields[0])
            elif fields[0] == _DEPENDENCY:
                if directive == 'require':
                    requires.append(fields[1])
                elif directive in ('replace', 'exclude'):
                    overridden = True
    except (UnicodeError, ValueError):
        raise ValueError(f'Unsupported Moby seccomp source: malformed or unsupported {manifest}') from None
    if len(modules) != 1 or modules[0] not in _SUPPORTED_MODULES:
        raise ValueError(f'Unsupported Moby seccomp source: unrecognized module in {manifest}')
    if overridden:
        raise ValueError(f'Unsupported Moby seccomp source: {manifest} replaces or excludes the seccomp dependency')
    if not requires:
        raise ValueError(f'Unsupported Moby seccomp source: {manifest} has no seccomp module dependency. '
                         'Use a daemon with a versioned profiles/seccomp module. Legacy in-tree profiles '
                         'and other source layouts require separate support.')
    if len(requires) != 1 or not re.fullmatch(_VERSION, requires[0]):
        raise ValueError(f'Unsupported Moby seccomp source: expected one exact release for the '
                         f'seccomp dependency in {manifest}')
    return requires[0]


def release_refs(version: str) -> tuple[str, str]:
    if not isinstance(version, str) or len(version) > 128 or not re.fullmatch(_RELEASE, version):
        raise ValueError('Unsupported Docker release version for official Moby tag lookup')
    return f'refs/tags/docker-v{version}', f'refs/tags/v{version}'


def _published_commit(read, repo, ref, label):
    url = f'{_API}/{repo}/git/ref/{ref.removeprefix("refs/")}'
    raw = _read(read, url, label)
    try:
        data = _decode_json(raw)
        if not isinstance(data, dict) or data.get('ref') != ref:
            raise ValueError
        visited = set()
        while True:
            target = data['object']
            sha = target['sha']
            if not isinstance(sha, str) or not re.fullmatch(_COMMIT, sha):
                raise ValueError
            if target['type'] == 'commit':
                return sha
            if target['type'] != 'tag' or sha in visited or len(visited) >= 8:
                raise ValueError
            visited.add(sha)
            data = _decode_json(_read(read, f'{_API}/{repo}/git/tags/{sha}', label))
            if not isinstance(data, dict) or data.get('sha') != sha:
                raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError(f'Cannot verify {label}: invalid published tag object') from None


def resolve_baseline(git_commit: str, daemon_version: str, *, fetch: Fetch | None = None) -> Baseline:
    """Reconstruct the official upstream baseline for the daemon's GitCommit."""
    if not isinstance(git_commit, str) or not re.fullmatch(_REPORTED_COMMIT, git_commit.lower()):
        raise ValueError('Unsupported Docker GitCommit: expected 7 to 40 hexadecimal characters')
    refs = release_refs(daemon_version)
    reported = git_commit.lower()
    read = fetch_official if fetch is None else fetch
    source = _commit(read, f'{_API}/moby/commits/{reported}', 'Moby source commit', prefix=reported)
    for ref in refs:
        try:
            published = _published_commit(read, 'moby', ref, 'official Moby release')
        except _FetchError as error:
            if error.status == 404:
                continue
            raise
        if published != source:
            raise ValueError('Official Moby release differs from reported Docker GitCommit')
        break
    else:
        raise ValueError('No official Moby release tag for reported Docker version')
    manifest = 'go.mod'
    try:
        raw_manifest = _read(read, f'{_RAW}/moby/{source}/{manifest}', manifest)
    except _FetchError as error:
        if error.status != 404:
            raise
        manifest = 'vendor.mod'
        try:
            raw_manifest = _read(read, f'{_RAW}/moby/{source}/{manifest}', manifest)
        except _FetchError as vendor_error:
            if vendor_error.status != 404:
                raise
            raise ValueError('Unsupported Moby seccomp source: no root go.mod or vendor.mod. '
                             'Use a daemon with a versioned profiles/seccomp module.') from None
    version = _module_version(raw_manifest, manifest)
    profile_commit = _published_commit(read, 'profiles', f'refs/tags/seccomp/{version}', 'profile module commit')
    profile = _read(read, f'{_RAW}/profiles/{profile_commit}/{_PROFILE_PATH}', _PROFILE_PATH)
    try:
        decode_profile(profile)
    except ValueError as error:
        raise ValueError(f'Unsupported upstream {_PROFILE_PATH}: {error}') from None
    license_bytes = _read(read, f'{_RAW}/profiles/{profile_commit}/LICENSE', 'profiles LICENSE')
    try:
        license_text = license_bytes.decode('utf-8')
    except UnicodeError:
        license_text = ''
    if not license_text.strip() or '\0' in license_text:
        raise ValueError('Invalid upstream profiles LICENSE: expected nonempty UTF-8 text')
    return Baseline(source, profile_commit, version, profile, license_bytes, _PROFILE_PATH, ref)

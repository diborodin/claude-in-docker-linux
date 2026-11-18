"""Private offline cache of upstream baselines and their exact Desktop derivations."""

from contextlib import ExitStack
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat

from .seccomp_profile import PATCH_REVISION, decode_profile, desktop_profile
from .seccomp_source import Baseline, release_refs, resolve_baseline


class ProfileNotCached(ValueError):
    """No published entry matches this daemon and patch revision."""


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_SOURCE_LIMIT = 2 * 1024 * 1024
_RESOLUTION_VERSION = 2
_PROFILE = 'desktop.json'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _hash(raw):
    return sha256(raw).hexdigest()


def _identity_key(identity):
    if (not isinstance(identity, dict)
            or set(identity) != {'engine', 'version', 'git_commit', 'os', 'arch'}
            or not all(isinstance(v, str) for v in identity.values())
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,199}', identity['engine'])
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', identity['version'])
            or not re.fullmatch(r'[a-f0-9]{7,40}', identity['git_commit'])
            or identity['os'] != 'linux' or identity['arch'] != 'amd64'):
        raise ValueError('Unsupported seccomp daemon identity (expected Linux amd64)')
    return _hash(_json([identity, PATCH_REVISION, _RESOLUTION_VERSION]))


def _check_link(fd, parent, name):
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not os.path.samestat(os.fstat(fd), current):
        raise ValueError('Seccomp cache path changed during resolution')


def _check_ancestor(st):
    # A sticky shared directory protects each trusted owner's child entry.
    # A foreign directory owner could still rename children or change its mode.
    if (st.st_uid not in (0, os.geteuid())
            or (st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX)):
        raise ValueError('Unsafe seccomp cache ancestor ownership or write permissions')


def _open_dir(stack, parent, name, *, create=False, private=True):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, _DIR, dir_fd=parent)
    stack.callback(os.close, fd)
    stack.callback(_check_link, fd, parent, name)
    st = os.fstat(fd)
    if private and (st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700):
        raise ValueError('Seccomp cache directory requires user ownership and mode 0700')
    if not private:
        _check_ancestor(st)
    return fd


def _read(stack, parent, name, limit):
    fd = os.open(name, _READ, dir_fd=parent)
    file = stack.enter_context(os.fdopen(fd, 'rb'))
    stack.callback(_check_link, fd, parent, name)
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1
            or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > limit):
        raise ValueError(f'Unsafe or oversized seccomp cache file: {name}')
    raw = file.read(limit + 1)
    if len(raw) > limit or len(raw) != st.st_size:
        raise ValueError(f'Seccomp cache file size changed: {name}')
    return raw


def _metadata(identity, base, files):
    return {'identity': identity, 'patch_revision': PATCH_REVISION,
            'resolution_version': _RESOLUTION_VERSION, 'source_ref': base.source_ref,
            'source_commit': base.source_commit, 'profile_commit': base.profile_commit,
            'module_version': base.module_version, 'profile_path': base.profile_path,
            'sha256': {name: _hash(raw) for name, raw in files.items()}}


def _generated(raw):
    return _json(desktop_profile(decode_profile(raw)))


def _lookup(stack, root, key, identity):
    try:
        entry = _open_dir(stack, root, key)
    except FileNotFoundError:
        raise ProfileNotCached('Matching Desktop seccomp profile is not cached') from None
    # One immutable digest directory binds the returned path to the full record.
    with os.scandir(entry) as entries:
        first = next(entries, None)
        if first is None or next(entries, None) or not re.fullmatch('[0-9a-f]{64}', first.name):
            raise ValueError('Invalid seccomp cache entry layout')
        digest = first.name
    fd = _open_dir(stack, entry, digest)
    raw = _read(stack, fd, 'metadata.json', 8192)
    if _hash(raw) != digest:
        raise ValueError('Seccomp cache metadata hash mismatch')
    try:
        meta = json.loads(raw)
        source, module, version = (meta[k] for k in ('source_commit', 'profile_commit', 'module_version'))
        valid = (re.fullmatch('[0-9a-f]{40}', source) and source.startswith(identity['git_commit'])
                 and meta['source_ref'] in release_refs(identity['version'])
                 and re.fullmatch('[0-9a-f]{40}', module)
                 and re.fullmatch(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', version))
    except (TypeError, KeyError, ValueError, RecursionError):
        valid = False
    if not valid:
        raise ValueError('Invalid seccomp cache source provenance')
    baseline = _read(stack, fd, 'baseline.json', _SOURCE_LIMIT)
    license_raw = _read(stack, fd, 'LICENSE', _SOURCE_LIMIT)
    profile = _read(stack, fd, _PROFILE, 8 * _SOURCE_LIMIT)
    files = {'baseline.json': baseline, 'LICENSE': license_raw, _PROFILE: profile}
    base = Baseline(source, module, version, baseline, license_raw, 'seccomp/default.json', meta['source_ref'])
    if raw != _json(_metadata(identity, base, files)):
        raise ValueError('Seccomp cache identity, provenance or file hash mismatch')
    if profile != _generated(baseline):
        raise ValueError('Seccomp cache profile does not match its baseline derivation')
    return Path(key, digest, _PROFILE)


def _publish(root, key, identity, base):
    files = {'baseline.json': base.profile, 'LICENSE': base.license, _PROFILE: _generated(base.profile)}
    files['metadata.json'] = _json(_metadata(identity, base, files))
    stage = '.pending-' + secrets.token_hex(16)
    os.mkdir(stage, 0o700, dir_fd=root)
    try:
        with ExitStack() as stack:
            entry = _open_dir(stack, root, stage)
            fd = _open_dir(stack, entry, _hash(files['metadata.json']), create=True)
            for name, raw in files.items():
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                with os.fdopen(os.open(name, flags, 0o600, dir_fd=fd), 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.fsync(fd)
            os.fsync(entry)
        os.rename(stage, key, src_dir_fd=root, dst_dir_fd=root)
    except BaseException:
        try:
            shutil.rmtree(stage, dir_fd=root)
        except FileNotFoundError:
            pass
        raise
    os.fsync(root)


def resolve_cached_profile(cache_dir: Path, identity: dict[str, str], *, create: bool, fetch=None) -> Path:
    """Verify an immutable entry, optionally resolving and atomically publishing a miss."""
    key = _identity_key(identity)
    cache_dir = cache_dir.absolute()
    if '..' in cache_dir.parts or cache_dir == Path('/'):
        raise ValueError('Invalid seccomp cache directory')
    try:
        with ExitStack() as stack:
            fd = os.open('/', _DIR)
            stack.callback(os.close, fd)
            _check_ancestor(os.fstat(fd))
            try:
                for index, name in enumerate(cache_dir.parts[1:], 1):
                    fd = _open_dir(stack, fd, name, create=create, private=index == len(cache_dir.parts) - 1)
                root = _open_dir(stack, fd, 'seccomp', create=create)
            except FileNotFoundError:
                if not create:
                    raise ProfileNotCached('Matching Desktop seccomp profile is not cached') from None
                raise
            if create:
                # Lock the directory inode itself; readers only see complete renamed entries.
                fcntl.flock(root, fcntl.LOCK_EX)
            try:
                relative = _lookup(stack, root, key, identity)
            except ProfileNotCached:
                if not create:
                    raise
                base = resolve_baseline(identity['git_commit'], identity['version'], fetch=fetch)
                _publish(root, key, identity, base)
                relative = _lookup(stack, root, key, identity)
            return cache_dir / 'seccomp' / relative
    except FileNotFoundError as error:
        raise ValueError(f'Incomplete or replaced seccomp cache: {error}') from None
    except OSError as error:
        raise ValueError(f'Cannot safely access seccomp cache: {error}') from None

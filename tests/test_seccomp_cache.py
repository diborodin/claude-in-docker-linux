"""Verify private, atomic publication and read-only reuse of derived profiles."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tests.test_seccomp_source import OfficialSources, PROFILE, LICENSE, REF_API, SOURCE_COMMIT, ref_json
from claude_docker.seccomp_profile import decode_profile, desktop_profile

try:
    from claude_docker import seccomp_cache as cache
except ImportError:
    cache = None


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(cache, 'seccomp cache resolver is missing')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'xdg' / 'claude-docker'
        self.identity = dict(engine='Docker Engine - Community', version='29.10.0',
                             git_commit='06db173', os='linux', arch='amd64')
        self.fetch = OfficialSources()

    def get(self, create=False, **updates):
        return cache.resolve_cached_profile(self.root, self.identity | updates, create=create,
                                            fetch=self.fetch if create else self.forbid_fetch)

    def forbid_fetch(self, url):
        self.fail(f'unexpected request to {url}')

    def test_offline_derivation_and_private_modes(self):
        path = self.get(True)
        self.assertEqual(self.get(), path)
        self.assertEqual(json.loads(path.read_bytes()), desktop_profile(decode_profile(PROFILE)))
        self.assertEqual((path.parent / 'baseline.json').read_bytes(), PROFILE)
        self.assertEqual((path.parent / 'LICENSE').read_bytes(), LICENSE)
        for p in [self.root, *self.root.rglob('*')]:
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700 if p.is_dir() else 0o600)

    def test_readonly_miss_and_identity_invalidation(self):
        with self.assertRaises(cache.ProfileNotCached):
            self.get()
        self.assertFalse(self.root.parent.exists())
        self.get(True)
        for update in [dict(version='29.9.3'), dict(git_commit='06db174'), dict(engine='Other Engine')]:
            with self.assertRaises(cache.ProfileNotCached):
                self.get(**update)
        with self.assertRaises(ValueError):
            self.get(arch='arm64')
        with patch.object(cache, 'PATCH_REVISION', cache.PATCH_REVISION + 1):
            with self.assertRaises(cache.ProfileNotCached):
                self.get()
        self.fetch.responses[REF_API + 'docker-v29.9.3'] = ref_json('docker-v29.9.3', SOURCE_COMMIT)
        old = self.get()
        self.assertNotEqual(self.get(True, version='29.9.3'), old)
        self.assertEqual(self.get(), old)

    def test_warm_check_avoids_writes_and_locks(self):
        path = self.get(True)
        real_open = os.open
        def read_only_open(name, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_TRUNC))
            return real_open(name, flags, *args, **kwargs)
        with ExitStack() as guards:
            for name in ('mkdir', 'rename', 'unlink', 'chmod', 'fchmod', 'fsync'):
                guards.enter_context(patch.object(os, name, side_effect=AssertionError(name)))
            guards.enter_context(patch.object(os, 'open', side_effect=read_only_open))
            guards.enter_context(patch.object(cache.fcntl, 'flock', side_effect=AssertionError('flock')))
            self.assertEqual(self.get(), path)

    def test_failed_publication_is_invisible_and_retryable(self):
        def fail_rename(stage, key, **kwargs):
            with self.assertRaises(cache.ProfileNotCached):
                self.get()
            self.assertTrue(list((self.root / 'seccomp' / stage).rglob('metadata.json')))
            raise OSError('injected rename failure')
        with patch.object(os, 'rename', side_effect=fail_rename), self.assertRaisesRegex(ValueError, 'rename failure'):
            self.get(True)
        self.assertEqual(list((self.root / 'seccomp').iterdir()), [])
        self.assertEqual(self.get(True), self.get())

    def test_invalid_ref_in_matching_digest_directory_fails(self):
        path = self.get(True)
        meta = path.parent / 'metadata.json'
        data = json.loads(meta.read_bytes())
        data['source_ref'] = 'refs/heads/main'
        raw = cache._json(data)
        meta.write_bytes(raw)
        path.parent.rename(path.parent.with_name(hashlib.sha256(raw).hexdigest()))
        with self.assertRaisesRegex(ValueError, 'source provenance'):
            self.get()

    def test_old_cache_key_requires_fresh_resolution(self):
        path = self.get(True)
        old_key = hashlib.sha256(cache._json([self.identity, cache.PATCH_REVISION])).hexdigest()
        path.parent.parent.rename(path.parent.parent.with_name(old_key))
        with self.assertRaises(cache.ProfileNotCached):
            self.get()
        self.fetch.calls.clear()
        self.get(True)
        self.assertTrue(self.fetch.calls)

    def test_cache_corruption_is_rejected(self):
        path = self.get(True)
        for name in ['metadata.json', 'baseline.json', 'LICENSE', path.name]:
            file = path.parent / name
            raw = file.read_bytes()
            file.write_bytes(raw + b' ')
            with self.assertRaises(ValueError):
                self.get()
            file.write_bytes(raw)

    def test_self_consistent_hashes_cannot_bypass_derivation(self):
        path = self.get(True)
        path.write_bytes(PROFILE)
        meta = path.parent / 'metadata.json'
        data = json.loads(meta.read_bytes())
        data['sha256'][path.name] = hashlib.sha256(PROFILE).hexdigest()
        raw = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        meta.write_bytes(raw)
        path.parent.rename(path.parent.with_name(hashlib.sha256(raw).hexdigest()))
        with self.assertRaisesRegex(ValueError, 'deriv'):
            self.get()

    def test_reject_special_files_and_symlinked_ancestor(self):
        path = self.get(True)
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaises(ValueError):
            self.get()
        path.unlink()
        path.symlink_to(path.parent / 'baseline.json')
        with self.assertRaises(ValueError):
            self.get()
        moved = self.root.parent.with_name('moved')
        self.root.parent.rename(moved)
        self.root.parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.get()

    def test_concurrent_build_and_partial_visibility(self):
        original = self.fetch
        def fetch(url):
            with self.assertRaises(cache.ProfileNotCached):
                self.get()
            return original(url)
        self.fetch = fetch
        with ThreadPoolExecutor(4) as pool:
            paths = list(pool.map(lambda _: self.get(True), range(4)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(len(original.calls), 6)

    def test_writable_nonsticky_ancestor_rejected_before_lookup(self):
        self.get(True)
        for mode in (0o770, 0o777):
            with self.subTest(mode=oct(mode)):
                self.root.parent.chmod(mode)
                with self.assertRaisesRegex(ValueError, 'ancestor'):
                    self.get()

    def test_owned_sticky_shared_ancestor_retains_protected_child(self):
        self.root.parent.mkdir()
        self.root.parent.chmod(0o1777)
        path = self.get(True)
        self.assertEqual(self.get(), path)
        self.assertEqual(path.stat().st_uid, os.geteuid())

    def test_ancestor_replaced_during_fetch(self):
        original = self.fetch
        moved = self.root.with_name('moved')
        def fetch(url):
            if not moved.exists():
                self.root.rename(moved)
                self.root.mkdir()
            return original(url)
        self.fetch = fetch
        with self.assertRaises(ValueError):
            self.get(True)
        self.assertEqual(list(self.root.iterdir()), [])

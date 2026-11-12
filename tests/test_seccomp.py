"""Pinned Desktop policy permissions and selected-daemon compatibility."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from claude_docker.config import AccountInfo, load_config
from claude_docker.model import DesktopConfig
from claude_docker.paths import HostPaths
from claude_docker.seccomp import (POLICY_RELATIVE_PATH, chromium_policy_path,
                                  resolve_desktop_seccomp, validate_chromium_daemon)


SERVER = {'Platform': {'Name': 'Docker Engine - Community'}, 'Version': '29.9.2',
          'Os': 'linux', 'Arch': 'amd64'}
INFO = {'ServerVersion': '29.9.2', 'OSType': 'linux', 'Architecture': 'x86_64',
        'SecurityOptions': ['name=seccomp,profile=builtin', 'name=apparmor']}


class DaemonRunner:
    def __init__(self, results=None):
        self.calls = []
        self.results = results if results is not None else [json.dumps(SERVER), json.dumps(INFO)]

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(argv, 0, result, '')


class SeccompTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        paths = HostPaths(*(self.root / name for name in
                            ('home', 'config', 'data', 'state', 'cache', 'runtime')))
        self.config = load_config(env={}, paths=paths,
                                  account=AccountInfo('tester', 1234, 2345, paths.home))
        self.chromium = replace(self.config, desktop=DesktopConfig(True, 'manual', 'chromium'))

    def test_default_policy_does_not_touch_asset_or_daemon(self):
        self.assertEqual(DesktopConfig(True, 'manual').seccomp, 'default')
        runner = DaemonRunner([])
        self.assertIsNone(resolve_desktop_seccomp(self.config, runner, program_dir=self.root / 'missing'))
        self.assertEqual(runner.calls, [])

    def test_policy_preserves_verified_baseline_and_only_four_exact_rules(self):
        contents = (ROOT / POLICY_RELATIVE_PATH).read_bytes()
        self.assertEqual(hashlib.sha256(contents).hexdigest(),
                         'f245d64a3213793f730f394d44c507145b13ffc8d05acb21b5146413aa395a74')
        policy = json.loads(contents)
        expected = [
            {'names': ['clone'], 'action': 'SCMP_ACT_ALLOW', 'args': [{'index': 0, 'value': 268435473, 'op': 'SCMP_CMP_EQ'}]},
            {'names': ['unshare'], 'action': 'SCMP_ACT_ALLOW', 'args': [{'index': 0, 'value': 268435456, 'op': 'SCMP_CMP_EQ'}]},
            {'names': ['clone'], 'action': 'SCMP_ACT_ALLOW', 'args': [{'index': 0, 'value': 1879048209, 'op': 'SCMP_CMP_EQ'}]},
            {'names': ['clone'], 'action': 'SCMP_ACT_ALLOW', 'args': [{'index': 0, 'value': 536870929, 'op': 'SCMP_CMP_EQ'}]},
        ]
        self.assertEqual(policy['syscalls'][-4:], expected)
        base = deepcopy(policy)
        del base['syscalls'][-4:]
        self.assertEqual({key: value for key, value in policy.items() if key != 'syscalls'},
                         {key: value for key, value in base.items() if key != 'syscalls'})
        semantic = json.dumps(base, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(semantic).hexdigest(),
                         '20ef2b6bfd980e7dd56a971d9ec0d71bc0f2d1184415f2d968a19bbcff9ee2b2')

    def test_policy_integrity_and_file_type_are_checked(self):
        policy = self.root / POLICY_RELATIVE_PATH
        policy.parent.mkdir()
        for kind in ('missing', 'corrupt', 'directory', 'symlink'):
            with self.subTest(kind=kind):
                if kind == 'corrupt':
                    policy.write_text('credential-sentinel')
                elif kind == 'directory':
                    policy.mkdir()
                elif kind == 'symlink':
                    policy.symlink_to(ROOT / POLICY_RELATIVE_PATH)
                with self.assertRaises(ValueError) as caught:
                    chromium_policy_path(self.root)
                self.assertNotIn('credential-sentinel', str(caught.exception))
                if policy.is_symlink() or policy.is_file():
                    policy.unlink()
                elif policy.is_dir():
                    policy.rmdir()
        shutil.copyfile(ROOT / POLICY_RELATIVE_PATH, policy)
        self.assertEqual(chromium_policy_path(self.root), policy.absolute())
        policy.unlink()
        policy.parent.rmdir()
        policy.parent.symlink_to(ROOT / 'security', target_is_directory=True)
        with self.assertRaises(ValueError):
            chromium_policy_path(self.root)

    def test_selected_daemon_compatibility_is_exact(self):
        for server_arch, info_arch in [('amd64', 'amd64'), ('x86_64', 'amd64'),
                                       ('amd64', 'x86_64'), ('x86_64', 'x86_64')]:
            validate_chromium_daemon(dict(SERVER, Arch=server_arch), dict(INFO, Architecture=info_arch), docker_mode='none')
        cases = [
            (None, INFO), ([], INFO), ({}, INFO), (SERVER, None), (SERVER, []), (SERVER, {}),
            (dict(SERVER, Platform=None), INFO), (dict(SERVER, Platform={'Name': 'unknown'}), INFO),
            (dict(SERVER, Version='29.9.3'), INFO), (dict(SERVER, Version='29.9.2-custom'), INFO),
            (dict(SERVER, Os='windows'), INFO), (dict(SERVER, Arch='arm64'), INFO),
            (SERVER, dict(INFO, ServerVersion='29.9.3')), (SERVER, dict(INFO, OSType='windows')),
            (SERVER, dict(INFO, Architecture='arm64')),
        ]
        cases.extend((SERVER, dict(INFO, SecurityOptions=value)) for value in
                     [None, [], 'name=seccomp,profile=builtin', [None], [17],
                      ['name=seccomp'], ['name=seccomp,profile=custom'], ['name=seccomp,profile=unconfined'],
                      ['name=seccomp,profile=builtin-extra']])
        for server, info in cases:
            with self.subTest(server=server, info=info), self.assertRaises(ValueError):
                validate_chromium_daemon(server, info, docker_mode='none')
        with self.assertRaises(ValueError):
            validate_chromium_daemon(SERVER, INFO, docker_mode='sysbox')

    def test_resolver_probes_only_selected_daemon_and_returns_packaged_path(self):
        runner = DaemonRunner()
        self.assertEqual(resolve_desktop_seccomp(self.chromium, runner, program_dir=ROOT),
                         ROOT / POLICY_RELATIVE_PATH)
        self.assertEqual(runner.calls, [
            (['docker', 'version', '--format', '{{json .Server}}'], {'capture_output': True}),
            (['docker', 'info', '--format', '{{json .}}'], {'capture_output': True}),
        ])

    def test_daemon_failures_and_malformed_output_are_redacted(self):
        secret = 'credential-sentinel'
        for results in [[secret], ['null'], ['[]'], [json.dumps(SERVER), secret],
                        [json.dumps(dict(SERVER, Version=secret)), json.dumps(INFO)],
                        [OSError(secret)], [subprocess.CalledProcessError(1, 'docker', secret, secret)]]:
            with self.subTest(results=results), self.assertRaises(ValueError) as caught:
                resolve_desktop_seccomp(self.chromium, DaemonRunner(list(results)), program_dir=ROOT)
            self.assertNotIn(secret, str(caught.exception))

    def test_unsafe_or_sysbox_opt_in_fails_before_probes(self):
        for config, unsafe in [(self.chromium, True),
                               (replace(self.chromium, desktop=DesktopConfig(False, 'manual', 'chromium')), False),
                               (replace(self.chromium, docker_mode='sysbox'), False)]:
            runner = DaemonRunner([])
            with self.subTest(config=config, unsafe=unsafe), self.assertRaisesRegex(
                    ValueError, 'internal sandbox' if unsafe or not config.desktop.sandbox else 'docker_mode=none'):
                resolve_desktop_seccomp(config, runner, program_dir=self.root / 'missing', unsafe=unsafe)
            self.assertEqual(runner.calls, [])


if __name__ == '__main__':
    unittest.main()

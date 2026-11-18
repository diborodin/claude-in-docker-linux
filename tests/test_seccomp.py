"""Selected outer daemon identity and dynamic Desktop policy resolution."""

from unittest.mock import patch
from dataclasses import replace
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from claude_docker.config import AccountInfo, load_config
from claude_docker.model import DesktopConfig
from claude_docker.paths import HostPaths
from claude_docker.seccomp import (resolve_desktop_seccomp, validate_chromium_daemon)


SERVER = {'Platform': {'Name': 'Docker Engine - Community'}, 'Version': '29.10.0',
          'GitCommit': '06db173', 'Os': 'linux', 'Arch': 'amd64'}
INFO = {'ServerVersion': '29.10.0', 'OSType': 'linux', 'Architecture': 'x86_64',
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

    def test_selected_daemon_identity_validation(self):
        for server_arch, info_arch in [('amd64', 'amd64'), ('x86_64', 'amd64'),
                                       ('amd64', 'x86_64'), ('x86_64', 'x86_64')]:
            validate_chromium_daemon(dict(SERVER, Arch=server_arch), dict(INFO, Architecture=info_arch), docker_mode='none')
        # Derivatives can report their own name and an upstream source commit.
        alternative = dict(SERVER, Version='28.3.1-custom', Platform={'Name': 'Other Engine'},
                           Components=[{'Name': 'Engine', 'Details': {'GitCommit': '06db1734'}}])
        identity = validate_chromium_daemon(alternative, dict(INFO, ServerVersion='28.3.1-custom'),
                                            docker_mode='sysbox')
        self.assertEqual(identity['git_commit'], '06db1734')
        self.assertEqual(identity['engine'], 'Other Engine')
        cases = [
            (None, INFO), ([], INFO), ({}, INFO), (SERVER, None), (SERVER, []), (SERVER, {}),
            (dict(SERVER, Platform=None), INFO), (dict(SERVER, GitCommit='../abc'), INFO),
            (dict(SERVER, Version='29.9.3'), INFO), (dict(SERVER, Version='29.10.0-custom'), INFO),
            (dict(SERVER, Os='windows'), INFO), (dict(SERVER, Arch='arm64'), INFO),
            (SERVER, dict(INFO, ServerVersion='29.9.3')), (SERVER, dict(INFO, OSType='windows')),
            (SERVER, dict(INFO, Architecture='arm64')),
        ]
        cases.extend((SERVER, dict(INFO, SecurityOptions=value)) for value in
                     [None, [], 'name=seccomp,profile=builtin', [None], [17],
                      ['name=seccomp'], ['name=seccomp,profile=custom'], ['name=seccomp,profile=unconfined'],
                      ['name=seccomp,profile=builtin-extra'], ['name=seccomp,profile=builtin', 'name=seccomp,profile=custom']])
        for server, info in cases:
            with self.subTest(server=server, info=info), self.assertRaises(ValueError):
                validate_chromium_daemon(server, info, docker_mode='none')
        other = dict(SERVER, Components=[{'Name':'Engine','Details':{'GitCommit':'abcdef1'}}])
        with self.assertRaises(ValueError):
            validate_chromium_daemon(other, INFO, docker_mode='sysbox')

    def test_both_modes_use_same_outer_identity_and_transport(self):
        for mode in ('none', 'sysbox'):
            runner = DaemonRunner()
            with patch('claude_docker.seccomp_cache.resolve_cached_profile', return_value=self.root) as cache, patch.dict(os.environ, {'DOCKER_CONTEXT':'selected', 'SECRET':'hidden'}, clear=True):
                selected = replace(self.chromium, docker_mode=mode)
                self.assertEqual(resolve_desktop_seccomp(selected, runner, program_dir=self.root,
                                 cache_dir=self.root, create=False), self.root)
            cache.assert_called_once_with(self.root, dict(engine='Docker Engine - Community',
                version='29.10.0', git_commit='06db173', os='linux', arch='amd64'), create=False)
            self.assertEqual([argv[1] for argv, kwargs in runner.calls], ['version', 'info'])
            for argv, kwargs in runner.calls:
                self.assertEqual(kwargs['env'], {'DOCKER_CONTEXT':'selected'})

    def test_daemon_failures_and_malformed_output_are_redacted(self):
        secret = 'credential-sentinel'
        for results in [[secret], ['null'], ['[]'], [json.dumps(SERVER), secret],
                        [json.dumps(dict(SERVER, Version=secret)), json.dumps(INFO)],
                        [OSError(secret)], [subprocess.CalledProcessError(1, 'docker', secret, secret)]]:
            with self.subTest(results=results), self.assertRaises(ValueError) as caught:
                resolve_desktop_seccomp(self.chromium, DaemonRunner(list(results)), program_dir=ROOT)
            self.assertNotIn(secret, str(caught.exception))

    def test_unsafe_opt_in_fails_before_probes(self):
        for config, unsafe in [(self.chromium, True),
                               (replace(self.chromium, desktop=DesktopConfig(False, 'manual', 'chromium')), False)]:
            runner = DaemonRunner([])
            with self.subTest(config=config, unsafe=unsafe), self.assertRaisesRegex(
                    ValueError, 'internal sandbox'):
                resolve_desktop_seccomp(config, runner, program_dir=self.root / 'missing', unsafe=unsafe)
            self.assertEqual(runner.calls, [])


if __name__ == '__main__':
    unittest.main()

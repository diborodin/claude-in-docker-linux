"""Read-only diagnostics and deliberately scoped maintenance operations."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker import cli
from claude_docker.model import (AppConfig, ContainerConfig, DesktopConfig,
                                 EnvironmentConfig, StateMountConfig)

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'io.github.diborodin.claude-in-docker-linux.'


class DockerRunner:
    """Only the external Docker process is replaced; inspect payloads are real JSON."""
    def __init__(self, *, fail=None, runtimes=None, compose='2.40.0', volume=None):
        self.calls = []
        self.fail = fail
        self.runtimes = {'runc': {'path': 'runc'}, 'sysbox-runc': {'path': 'sysbox-runc'}} if runtimes is None else runtimes
        self.compose = compose
        self.volume = volume

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.fail == 'missing':
            raise FileNotFoundError('untrusted secret process details')
        rc = 7 if self.fail and self.fail in argv else 0
        if argv[1:3] == ['volume', 'inspect']:
            stdout = json.dumps(self.volume)
        elif argv[1:3] == ['compose', 'version']:
            stdout = self.compose
        elif argv[1:2] == ['info']:
            stdout = json.dumps(self.runtimes) if 'Runtimes' in argv[-1] else '29.0.0'
        else:
            stdout = 'Docker version 29.0.0'
        return subprocess.CompletedProcess(argv, rc, stdout, 'private stderr sentinel')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = AppConfig('none', ContainerConfig('tester', 1234, 2345,
                                Path('/home/tester'), ()),
                                StateMountConfig(self.root / 'code', True),
                                StateMountConfig(self.root / 'code.json', True),
                                StateMountConfig(self.root / 'desktop', True),
                                DesktopConfig(True, 'manual'), EnvironmentConfig((), ()))
        self.env = {'HOME': str(self.root / 'home'), 'PATH': '/usr/bin',
                    'XDG_RUNTIME_DIR': str(self.root / 'run')}
        Path(self.env['XDG_RUNTIME_DIR']).mkdir(mode=0o700)

    def invoke(self, args, runner, *, input_text=None, interactive=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        stdin = io.StringIO(input_text or '')
        stdin.isatty = lambda: interactive
        with patch.dict(os.environ, self.env, clear=True), \
                patch('claude_docker.cli.CommandRunner', return_value=runner), \
                patch('sys.stdin', stdin), redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(args)
        return result, stdout.getvalue(), stderr.getvalue()


class CheckTests(Fixture):
    def run_checks(self, *, mode='none', runner=None, smoke=False, distro=None,
                   system='Linux', machine='x86_64', version=(3, 11, 0)):
        # Import at call time lets missing implementation produce an explicit RED.
        from claude_docker.checks import run_checks
        runner = runner or DockerRunner()
        with patch('claude_docker.checks.platform.system', return_value=system), \
                patch('claude_docker.checks.platform.machine', return_value=machine), \
                patch('claude_docker.checks.sys.version_info', version), \
                patch('claude_docker.checks.platform.freedesktop_os_release',
                      return_value=distro or {'ID': 'ubuntu', 'VERSION_ID': '24.04'}):
            results = run_checks(replace(self.config, docker_mode=mode), smoke=smoke, runner=runner)
        return {result.name: result for result in results}, runner

    def test_ordinary_checks_are_read_only_and_do_not_require_sysbox_mise_or_jq(self):
        results, runner = self.run_checks(runner=DockerRunner(runtimes={}))
        for name in ('python', 'linux', 'docker', 'daemon', 'compose', 'architecture'):
            self.assertEqual(results[name].status, 'ok', results[name])
        for argv, _ in runner.calls:
            self.assertEqual(argv[0], 'docker')
            self.assertNotIn('Runtimes', ' '.join(argv))
            self.assertNotIn('run', argv)
        self.assertFalse(any(name.startswith('sysbox') for name in results))
        self.assertEqual(list(Path(self.env['XDG_RUNTIME_DIR']).iterdir()), [])

    def test_prerequisite_failures_are_structured_without_raw_process_output(self):
        cases = [({'version': (3, 10, 9)}, 'python'), ({'system': 'Darwin'}, 'linux'),
                 ({'machine': 's390x'}, 'architecture'),
                 ({'runner': DockerRunner(fail='missing')}, 'docker'),
                 ({'runner': DockerRunner(fail='info')}, 'daemon'),
                 ({'runner': DockerRunner(compose='1.29.2')}, 'compose')]
        for kwargs, name in cases:
            with self.subTest(name=name):
                results, _ = self.run_checks(**kwargs)
                self.assertEqual(results[name].status, 'error')
                self.assertNotIn('private stderr sentinel', str(results))
                self.assertNotIn('untrusted secret', str(results))

    def test_compose_plugin_accepts_supported_successors_and_rejects_malformed_versions(self):
        for version, status in [('2.40.0', 'ok'), ('v2.40.0-desktop.1', 'ok'),
                                ('5.7.0', 'ok'), ('v5.7.0', 'ok'), ('12.0.1+vendor.2', 'ok'),
                                ('1.29.2', 'error'), ('v1.29.2', 'error'), ('', 'error'),
                                ('garbage', 'error'), ('2.', 'error'), ('2.40', 'error'),
                                ('2.40.0 garbage', 'error'), ('2.40.0..', 'error'),
                                ('5.7.0-', 'error'), ('02.40.0', 'error')]:
            with self.subTest(version=version):
                results, _ = self.run_checks(runner=DockerRunner(compose=version))
                self.assertEqual(results['compose'].status, status)
        results, runner = self.run_checks(runner=DockerRunner(compose='5.7.0'), smoke=True)
        self.assertEqual(results['smoke'].status, 'ok')
        self.assertTrue(any(argv[1] == 'run' for argv, _ in runner.calls))

    def test_supported_architectures_include_arm64(self):
        for machine in ('aarch64', 'arm64', 'amd64'):
            with self.subTest(machine=machine):
                results, _ = self.run_checks(machine=machine)
                self.assertEqual(results['architecture'].status, 'ok')

    def test_sysbox_host_classification_warns_without_failing_experimental_hosts(self):
        for distro, status, message in [
                ({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, 'ok', 'reference'),
                ({'ID': 'debian', 'VERSION_ID': '13'}, 'warning', 'experimental'),
                ({'ID': 'fedora', 'VERSION_ID': '43'}, 'warning', 'reference')]:
            with self.subTest(distro=distro):
                results, _ = self.run_checks(mode='sysbox', distro=distro)
                self.assertEqual(results['sysbox-host'].status, status)
                self.assertIn(message, results['sysbox-host'].message.lower())
                self.assertFalse(any(result.status == 'error' for result in results.values()))

    def test_sysbox_runtime_must_be_registered_with_the_daemon(self):
        results, _ = self.run_checks(mode='sysbox', runner=DockerRunner(runtimes={'runc': {}}))
        self.assertEqual(results['sysbox-runtime'].status, 'error')
        self.assertIn('sysbox-runc', results['sysbox-runtime'].message)

    def test_smoke_runs_only_when_explicit_and_prerequisites_pass(self):
        for mode in ('none', 'sysbox'):
            with self.subTest(mode=mode):
                results, runner = self.run_checks(mode=mode, smoke=True)
                runs = [argv for argv, _ in runner.calls if argv[1] == 'run']
                self.assertEqual(len(runs), 1)
                self.assertIn('--rm', runs[0])
                self.assertEqual('sysbox-runc' in runs[0], mode == 'sysbox')
                self.assertEqual(results['smoke'].status, 'ok')
        results, runner = self.run_checks(mode='sysbox', smoke=True, runner=DockerRunner(runtimes={}))
        self.assertFalse(any(argv[1] == 'run' for argv, _ in runner.calls))
        self.assertEqual(results['smoke'].status, 'skip')

    def test_smoke_failure_is_an_error_without_raw_output(self):
        results, _ = self.run_checks(smoke=True, runner=DockerRunner(fail='run'))
        self.assertEqual(results['smoke'].status, 'error')
        self.assertNotIn('private stderr sentinel', str(results))

    def test_environment_diagnostics_report_exact_names_and_presence_only(self):
        sentinel = 'long-SENTINEL-value-with-secret-suffix-7d7a40'
        self.config = replace(self.config, environment=EnvironmentConfig(
            ('REQUIRED_SET', 'REQUIRED_EMPTY', 'REQUIRED_MISSING'), ('OPTIONAL_SET', 'OPTIONAL_EMPTY', 'OPTIONAL_MISSING')))
        with patch.dict(os.environ, {'REQUIRED_SET': sentinel, 'REQUIRED_EMPTY': '',
                                    'OPTIONAL_SET': sentinel, 'OPTIONAL_EMPTY': '', 'UNCONFIGURED': sentinel}, clear=True):
            results, _ = self.run_checks()
        rows = [result for name, result in results.items() if name.startswith('environment:')]
        self.assertEqual([(row.name, row.status, row.message) for row in rows], [
            ('environment:REQUIRED_SET', 'ok', 'set'),
            ('environment:REQUIRED_EMPTY', 'error', 'missing'),
            ('environment:REQUIRED_MISSING', 'error', 'missing'),
            ('environment:OPTIONAL_SET', 'ok', 'set'),
            ('environment:OPTIONAL_EMPTY', 'ok', 'set'),
            ('environment:OPTIONAL_MISSING', 'warning', 'missing')])
        rendered = str(rows)
        for private in (sentinel, sentinel[:12], sentinel[-12:], str(len(sentinel)),
                        hashlib.sha256(sentinel.encode()).hexdigest(), 'UNCONFIGURED'):
            self.assertNotIn(private, rendered)

    def test_cli_check_never_prepares_mounts_or_runtime_state(self):
        runner = DockerRunner()
        rc, stdout, stderr = self.invoke(['check'], runner)
        self.assertEqual(rc, 0, stderr)
        self.assertIn('docker', stdout.lower())
        self.assertFalse(Path(self.env['HOME']).exists())
        self.assertEqual(list(Path(self.env['XDG_RUNTIME_DIR']).iterdir()), [])
        self.assertTrue(all('run' not in argv for argv, _ in runner.calls))

    def test_cli_check_returns_nonzero_only_for_errors(self):
        self.env['CLAUDE_DOCKER_MODE'] = 'sysbox'
        with patch('claude_docker.checks.platform.freedesktop_os_release',
                   return_value={'ID': 'debian', 'VERSION_ID': '13'}):
            rc, stdout, stderr = self.invoke(['check'], DockerRunner())
        self.assertEqual(rc, 0, stderr)
        self.assertIn('experimental', stdout.lower())
        rc, _, _ = self.invoke(['check'], DockerRunner(fail='info'))
        self.assertEqual(rc, 1)


class MaintenanceTests(Fixture):
    def test_lock_uses_only_repository_image_mount_and_invoking_host_identity(self):
        runner = DockerRunner()
        rc, _, stderr = self.invoke(['tools', 'lock'], runner)
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(len(runner.calls), 1)
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv[:3], ['docker', 'run', '--rm'])
        self.assertEqual(argv[argv.index('--user') + 1], f'{os.getuid()}:{os.getgid()}')
        self.assertEqual(argv[argv.index('--workdir') + 1], '/workspace')
        self.assertEqual(argv[-4:], ['jdxcode/mise:2026.11.5', 'lock', '--platform', 'linux-x64,linux-arm64'])
        self.assertEqual(argv.count('--mount'), 1)
        self.assertIn('source=' + str(ROOT / 'image'), argv[argv.index('--mount') + 1])
        self.assertNotIn('readonly', argv[argv.index('--mount') + 1])
        self.assertNotIn('--volume', argv)
        self.assertIn('--tmpfs', argv)
        container_env = [argv[i + 1] for i, argument in enumerate(argv) if argument == '--env']
        self.assertEqual(container_env, ['HOME=/tmp', 'MISE_CACHE_DIR=/tmp/mise-cache',
            'MISE_STATE_DIR=/tmp/mise-state', 'MISE_DATA_DIR=/tmp/mise-data',
            'MISE_TRUSTED_CONFIG_PATHS=/workspace'])
        self.assertNotIn('sh', argv)
        self.assertNotIn('chown', argv)
        self.assertNotIn('shell', kwargs)
        self.assertFalse(Path(self.env['HOME']).exists())

    def volume(self, **labels):
        target = f'claude-docker-{os.getuid()}_inner-docker-data'
        return [{'Name': target, 'Labels': {
            LABEL + 'managed': 'true', LABEL + 'owner-uid': str(os.getuid()),
            'com.docker.compose.project': f'claude-docker-{os.getuid()}',
            'com.docker.compose.volume': 'inner-docker-data', **labels}}]

    def test_cleanup_yes_removes_only_exact_owned_data_volume(self):
        runner = DockerRunner(volume=self.volume())
        rc, stdout, stderr = self.invoke(['cleanup', 'inner-docker', '--yes'], runner)
        self.assertEqual(rc, 0, stderr)
        target = f'claude-docker-{os.getuid()}_inner-docker-data'
        self.assertIn(target, stdout)
        self.assertEqual([argv for argv, _ in runner.calls], [
            ['docker', 'volume', 'inspect', target], ['docker', 'volume', 'rm', target]])
        self.assertFalse(Path(self.env['HOME']).exists())
        self.assertEqual(list(Path(self.env['XDG_RUNTIME_DIR']).iterdir()), [])

    def test_cleanup_requires_confirmation_or_yes_before_deletion(self):
        for interactive, answer in [(False, ''), (True, 'no\n')]:
            with self.subTest(interactive=interactive):
                runner = DockerRunner(volume=self.volume())
                rc, stdout, _ = self.invoke(['cleanup', 'inner-docker'], runner,
                                           interactive=interactive, input_text=answer)
                self.assertIn(f'claude-docker-{os.getuid()}_inner-docker-data', stdout)
                self.assertFalse(any(argv[1:3] == ['volume', 'rm'] for argv, _ in runner.calls))
                self.assertEqual(rc, 1 if not interactive else 0)
        runner = DockerRunner(volume=self.volume())
        rc, _, stderr = self.invoke(['cleanup', 'inner-docker'], runner,
                    interactive=True, input_text=f'claude-docker-{os.getuid()}_inner-docker-data\n')
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(runner.calls[-1][0][1:3], ['volume', 'rm'])

    def test_cleanup_refuses_unowned_mismatched_and_malformed_volumes(self):
        bad = [None, [], [{'Name': 'other', 'Labels': {}}],
               self.volume(**{LABEL + 'managed': 'false'}),
               self.volume(**{LABEL + 'owner-uid': '999999'}),
               self.volume(**{'com.docker.compose.project': 'other'}),
               self.volume(**{'com.docker.compose.volume': 'inner-docker-ca'})]
        for volume in bad:
            with self.subTest(volume=volume):
                runner = DockerRunner(volume=volume)
                rc, _, stderr = self.invoke(['cleanup', 'inner-docker', '--yes'], runner)
                self.assertEqual(rc, 1)
                self.assertNotIn('Traceback', stderr)
                self.assertFalse(any('rm' in argv for argv, _ in runner.calls))

    def test_cleanup_inspection_failure_does_not_delete_any_volume(self):
        class FailingInspect(DockerRunner):
            def run(self, argv, **kwargs):
                self.calls.append((list(argv), kwargs))
                raise subprocess.CalledProcessError(7, argv, 'secret output', 'secret stderr')
        runner = FailingInspect()
        rc, _, stderr = self.invoke(['cleanup', 'inner-docker', '--yes'], runner)
        self.assertEqual(rc, 1)
        self.assertNotIn('secret', stderr)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0][1:3], ['volume', 'inspect'])

    def test_help_has_no_docker_or_filesystem_side_effects(self):
        for args in (['--help'], ['check', '--help'], ['tools', 'lock', '--help'],
                     ['cleanup', 'inner-docker', '--help']):
            with self.subTest(args=args):
                runner = DockerRunner()
                with self.assertRaises(SystemExit) as caught:
                    self.invoke(args, runner)
                self.assertEqual(caught.exception.code, 0)
                self.assertEqual(runner.calls, [])
                self.assertFalse(Path(self.env['HOME']).exists())
                self.assertEqual(list(Path(self.env['XDG_RUNTIME_DIR']).iterdir()), [])

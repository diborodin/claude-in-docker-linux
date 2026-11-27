"""Desktop pin selection and its Docker build boundary."""

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from tests.test_compose import ComposeFixture
from tests.test_desktop_resolver import ResolverDocker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from claude_docker import cli
from claude_docker.paths import resolve_host_paths


class DesktopVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {'HOME': str(self.root), 'PATH': os.defpath,
                    'DOCKER_HOST': 'ssh://selected', 'DOCKER_DEFAULT_PLATFORM': 'linux/arm64',
                    'COMPOSE_FILE': 'untrusted.yaml', 'TOKEN': 'private'}
        self.paths = resolve_host_paths(self.env, os.getuid(), create_runtime=False)
        self.pin = self.paths.config_dir / 'claude-desktop.version'
        self.code_pin = self.paths.config_dir / 'claude-code.version'
        self.pin.parent.mkdir(parents=True)
        self.code_pin.write_bytes(b'2.2.0\n')
        self.builds = []
        self.resolutions = 0
        self.errors = io.StringIO()

    def build(self, arguments, candidate=None, *, code=None, change_pin_after_cli=False,
              server_platform='linux/amd64'):
        docker_env = {'HOME': str(self.root), 'PATH': os.defpath, 'DOCKER_HOST': 'ssh://selected'}
        resolver = ResolverDocker(server_platform)
        resolver.candidate = candidate
        if isinstance(candidate, BaseException):
            resolver.start_error = candidate

        def run(runner, argv, **kwargs):
            if argv[:2] != ['docker', 'compose']:
                self.assertEqual(kwargs['env'], docker_env)
                self.assertIsNotNone(candidate, 'Unexpected Desktop lookup')
                if argv[1] == 'create':
                    self.assertEqual(argv[argv.index('--platform') + 1], server_platform)
                    self.assertEqual(argv[-1], 'resolve')
                if argv[1] == 'start':
                    self.resolutions += 1
                return resolver.run(argv, **kwargs)
            service = argv[-1]
            index = argv.index('build')
            override = json.loads(Path(argv[index - 1]).read_text())
            context = Path(override['services'][service]['build']['context'])
            version_file = context / 'image/claude-desktop.version'
            script = context / 'image/claude-desktop-apt.sh'
            self.assertEqual(script.is_file(), version_file.is_file())
            self.builds.append((service, context,
                                version_file.read_bytes() if version_file.exists() else None,
                                (context / 'image/claude-code.version').read_bytes(),
                                argv[index + 1:-1]))
            if change_pin_after_cli and service == 'claude-cli':
                self.pin.write_bytes(b'3:5.1-1\n')
            return subprocess.CompletedProcess(argv, 0, '', '')

        with patch.dict(os.environ, self.env, clear=True), redirect_stderr(self.errors), \
                patch('claude_docker.claude_version._latest_version', side_effect=
                      lambda: code if code is not None else self.fail('Unexpected Code lookup')) as lookup, \
                patch.object(cli.CommandRunner, 'run', run):
            try:
                result = cli.main(arguments)
            except SystemExit as error:
                result = error.code
        self.assertEqual(lookup.call_count, int(code is not None))
        return result

    def test_first_desktop_build_persists_exact_version_and_reuses_pin_without_lookup(self):
        self.assertEqual(self.build(['build', '--only-desktop'], '1:5.10.0~rc2-1ubuntu2\n'), 0,
                         self.errors.getvalue())
        self.assertTrue(self.pin.is_file(), 'First Desktop build did not persist a version')
        self.assertEqual(self.pin.read_bytes(), b'1:5.10.0~rc2-1ubuntu2\n')
        self.assertEqual(stat.S_IMODE(self.pin.stat().st_mode), 0o600)
        self.assertEqual(self.build(['build', '--only-desktop']), 0, self.errors.getvalue())
        self.assertEqual([entry[2] for entry in self.builds], [b'1:5.10.0~rc2-1ubuntu2\n'] * 2)
        self.assertEqual(self.resolutions, 1)

    def test_resolution_uses_the_selected_remote_arm64_daemon(self):
        self.env['DOCKER_DEFAULT_PLATFORM'] = 'linux/amd64'
        self.assertEqual(self.build(['build', '--only-desktop'], '5.11-1\n',
                                    server_platform='linux/arm64'), 0, self.errors.getvalue())
        self.assertEqual(self.pin.read_bytes(), b'5.11-1\n')

    def test_unsupported_daemon_platform_fails_without_resolving_or_publishing(self):
        self.pin.write_bytes(b'5.10-1\n')
        self.assertEqual(self.build(['update', '--only-desktop'], '5.11-1\n', server_platform='linux/ppc64le'), 1)
        self.assertIn('Unsupported Claude Desktop platform', self.errors.getvalue())
        self.assertEqual(self.pin.read_bytes(), b'5.10-1\n')
        self.assertEqual(self.resolutions, 0)
        self.assertFalse(self.builds)

    def test_cli_build_ignores_even_an_invalid_desktop_pin_and_omits_desktop_inputs(self):
        self.pin.write_bytes(b'junk')
        self.assertEqual(self.build(['update', '--only-cli'], code='2.3.0'), 0,
                         self.errors.getvalue())
        self.assertEqual(self.builds[0][0], 'claude-cli')
        self.assertIsNone(self.builds[0][2])
        self.assertEqual(self.builds[0][3], b'2.3.0\n')
        self.assertEqual(self.pin.read_bytes(), b'junk')

    def test_desktop_update_alone_selects_desktop_and_keeps_code_pin(self):
        self.pin.write_bytes(b'5.10-1\n')
        self.assertEqual(self.build(['update', '--only-desktop', '--rebuild'], '5.10-2\n'), 0,
                         self.errors.getvalue())
        self.assertEqual(self.builds[0][0], 'claude-desktop')
        self.assertEqual(self.builds[0][2:4], (b'5.10-2\n', b'2.2.0\n'))
        self.assertCountEqual(self.builds[0][4], ['--pull', '--no-cache'])
        self.assertEqual(self.pin.read_bytes(), b'5.10-2\n')
        self.assertEqual(self.code_pin.read_bytes(), b'2.2.0\n')

    def test_all_with_both_updates_uses_one_snapshot_even_if_pin_changes_after_cli(self):
        self.pin.write_bytes(b'1:5.10-1\n')
        self.assertEqual(self.build(['update'],
                                    '2:5.10-1\n', code='2.3.0', change_pin_after_cli=True), 0,
                         self.errors.getvalue())
        self.assertEqual([entry[0] for entry in self.builds], ['claude-cli', 'claude-desktop'])
        self.assertEqual({entry[1] for entry in self.builds}, {self.builds[0][1]})
        self.assertFalse(self.builds[0][1].exists())
        self.assertEqual([entry[2:4] for entry in self.builds], [(b'2:5.10-1\n', b'2.3.0\n')] * 2)
        self.assertEqual(self.pin.read_bytes(), b'3:5.1-1\n')
        self.assertEqual(self.resolutions, 1)

    def test_bad_or_failed_resolution_retains_existing_pin_without_build(self):
        self.pin.write_bytes(b' 5.10-1\n')
        previous = self.pin.stat()
        for candidate in ('', 'latest', '5.11\n5.12\n', '5.10;id', '5.10/../evil', '5' * 129,
                          subprocess.CalledProcessError(100, ['docker'], stderr='APT fixture failure')):
            with self.subTest(candidate=candidate):
                self.assertEqual(self.build(['update', '--only-desktop'], candidate), 1)
                self.assertIn('Claude Desktop', self.errors.getvalue())
                self.assertEqual(self.pin.read_bytes(), b' 5.10-1\n')
                current = self.pin.stat()
                self.assertEqual((current.st_ino, current.st_mtime_ns),
                                 (previous.st_ino, previous.st_mtime_ns))
                self.assertFalse(self.builds)

    def test_invalid_pin_fails_before_resolution(self):
        for raw in (b'latest\n', b'5.1-\n', b'5.1:bad-1\n', b'2:bad-1\n', b'5.1-%1\n'):
            with self.subTest(raw=raw):
                self.pin.write_bytes(raw)
                self.assertEqual(self.build(['update', '--only-desktop']), 1)
                self.assertIn(str(self.pin), self.errors.getvalue())
                self.assertEqual(self.pin.read_bytes(), raw)
                self.assertFalse(self.builds)

    def test_concurrent_updates_preserve_the_first_published_selection(self):
        self.assertTrue((ROOT / 'src/claude_docker/desktop_version.py').is_file(),
                        'Desktop pin selection is missing')
        from claude_docker.desktop_version import select_desktop_version

        self.pin.write_bytes(b'5.10-1\n')
        both_resolving = threading.Barrier(2, timeout=5)
        first_published = threading.Event()

        class Resolver(ResolverDocker):
            def __init__(self, version, wait_for_first=False):
                super().__init__()
                self.candidate = version
                self.wait_for_first = wait_for_first

            def run(self, argv, **kwargs):
                if argv[:2] == ['docker', 'start']:
                    both_resolving.wait()
                    if self.wait_for_first and not first_published.wait(5):
                        raise AssertionError('First update did not finish')
                return super().run(argv, **kwargs)

        def update(resolver):
            apt_script = (ROOT / 'image/claude-desktop-apt.sh').read_bytes()
            return select_desktop_version(ROOT, self.paths, resolver, self.env, apt_script=apt_script, refresh=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(update, Resolver('5.10-2\n'))
            second = pool.submit(update, Resolver('5.10-3\n', wait_for_first=True))
            try:
                self.assertEqual(first.result(timeout=5), '5.10-2')
            finally:
                first_published.set()
            with self.assertRaisesRegex(ValueError, 'changed during update'):
                second.result(timeout=5)
        self.assertEqual(self.pin.read_bytes(), b'5.10-2\n')
        self.assertCountEqual(list(self.pin.parent.iterdir()), [self.code_pin, self.pin])


class ComposeDesktopVersionTests(ComposeFixture, unittest.TestCase):
    def test_direct_build_resolves_desktop_inputs_only_for_desktop(self):
        self.seed_code_version(self.paths.config_dir)
        resolver = ResolverDocker()
        resolver.candidate = '1:5.11-2\n'
        contexts = []
        run = self.runner.run

        def inspect(argv, **kwargs):
            if argv[:2] != ['docker', 'compose']:
                run(argv, **kwargs)
                return resolver.run(argv, **kwargs)
            service = argv[-1]
            override = json.loads(Path(argv[argv.index('build') - 1]).read_text())
            context = Path(override['services'][service]['build']['context'])
            version = context / 'image/claude-desktop.version'
            contexts.append(version.read_bytes() if version.exists() else None)
            return run(argv, **kwargs)

        self.runner.run = inspect
        project = self.project()
        project.build('claude-cli', pull=False, no_cache=False)
        project.build('claude-desktop', pull=False, no_cache=False)
        self.assertEqual(contexts, [None, b'1:5.11-2\n'])
        self.assertEqual([argv[1] for argv, _ in self.runner.calls],
                         ['compose', 'version', 'image', 'build', 'image', 'create', 'start', 'rm', 'compose'])

"""Exercise resolver lifecycle and deadlines with a stand-in for the Docker daemon."""

from contextlib import redirect_stderr
import hashlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from claude_docker.desktop_version import select_desktop_version
from claude_docker.paths import resolve_host_paths


FAKE_SIGNAL_DOCKER = '''\
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path.home()
operation, *arguments = sys.argv[1:]
owned = root / 'owned-container'
owned_id = 'b' * 64
release = root / 'release'

def wait_for_release():
    deadline = time.monotonic() + 10
    while root.exists() and not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

if operation == 'version':
    print('linux/amd64')
elif operation == 'image':
    print('linux/amd64', arguments[-1].partition(':')[2], 'sha256:' + 'c' * 64)
elif operation == 'create':
    owned.write_text(owned_id)
    print(owned_id)
elif operation == 'start':
    assert arguments == ['--attach', owned_id]
    if (root / 'hold-start').exists():
        transport = subprocess.Popen([sys.executable, '-B', __file__, 'transport'])
        draft = root / 'starting'
        draft.write_text(f'{os.getpid()} {transport.pid}')
        draft.replace(root / 'start-ready')
        transport.wait()
    print('5.11-1')
elif operation == 'rm':
    assert arguments == ['--force', owned_id]
    if (root / 'hold-rm').exists():
        (root / 'rm-ready').touch()
        wait_for_release()
    assert owned.read_text() == owned_id
    owned.unlink()
elif operation == 'transport':
    wait_for_release()
else:
    raise AssertionError(sys.argv)
'''


class ResolverDocker:
    def __init__(self, platform='linux/amd64'):
        self.platform = platform
        self.candidate = '5.11-1\n'
        self.images = {}
        self.containers = {'existing-user-container': ('a' * 64, 'user')}
        self.calls = []
        self.dockerfiles = []
        self.elapsed = 0.0
        self.create_error = None
        self.create_missing_inspections = 0
        self.start_error = None
        self.create_with_foreign_owner = False

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.elapsed += 2.0

        def result(stdout='', *, status=0, stderr=''):
            return subprocess.CompletedProcess(argv, status, stdout, stderr)

        if argv[1] == 'version':
            return result(f'{self.platform}\n')
        if argv[1:3] == ['image', 'inspect']:
            cached = self.images.get(argv[-1])
            if cached is None:
                return result(status=1, stderr=f'Error: No such image: {argv[-1]}\n')
            return result(cached)
        if argv[1] == 'build':
            tag = argv[argv.index('--tag') + 1]
            platform = argv[argv.index('--platform') + 1]
            dockerfile = (Path(argv[-1]) / 'Dockerfile').read_text()
            self.dockerfiles.append(dockerfile)
            build_label = next(line for line in dockerfile.splitlines() if line.startswith('LABEL '))
            version = build_label.partition('=')[2]
            image_id = 'sha256:' + hashlib.sha256(tag.encode()).hexdigest()
            self.images[tag] = f'{platform} {version} {image_id}\n'
            return result()
        if argv[1] == 'create':
            name = argv[argv.index('--name') + 1]
            label = argv[argv.index('--label') + 1].partition('=')[2]
            container_id = hashlib.sha256(name.encode()).hexdigest()
            self.containers[name] = (container_id, 'foreign' if self.create_with_foreign_owner else label)
            if self.create_error:
                raise self.create_error
            return result(f'{container_id}\n')
        if argv[1] in ('start', 'run'):
            if self.start_error:
                raise self.start_error
            return result(self.candidate)
        if argv[1:3] == ['container', 'inspect']:
            if self.create_missing_inspections:
                self.create_missing_inspections -= 1
                return result(status=1, stderr=f'Error: No such container: {argv[-1]}\n')
            container_id, label = self.containers[argv[-1]]
            return result(f'{container_id} {label}\n')
        if argv[1] == 'rm':
            name = next(name for name, (container_id, _) in self.containers.items() if container_id == argv[-1])
            del self.containers[name]
            return result()
        raise AssertionError(f'Unexpected Docker command: {argv}')


class DesktopResolverTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.env = {'HOME': home.name, 'PATH': os.defpath, 'DOCKER_HOST': 'ssh://selected',
                    'DOCKER_DEFAULT_PLATFORM': 'linux/arm64', 'COMPOSE_FILE': 'other.yaml',
                    'ANTHROPIC_API_KEY': 'host secret'}
        self.paths = resolve_host_paths(self.env, os.getuid(), create_runtime=False)
        self.pin = self.paths.config_dir / 'claude-desktop.version'
        self.script = (ROOT / 'image/claude-desktop-apt.sh').read_bytes()
        self.docker = ResolverDocker()
        self.progress = io.StringIO()

    def resolve(self, *, refresh=True):
        with redirect_stderr(self.progress):
            return select_desktop_version(ROOT, self.paths, self.docker, self.env,
                                          apt_script=self.script, refresh=refresh)

    def seed_pin(self):
        self.pin.parent.mkdir(parents=True)
        self.pin.write_bytes(b' 5.10-1\n')
        self.pin.chmod(0o600)

    def test_cache_reuses_bootstrap_but_checks_the_signed_candidate_again(self):
        self.assertEqual(self.resolve(), '5.11-1')
        self.docker.candidate = '5.12-1\n'
        self.assertEqual(self.resolve(), '5.12-1')
        self.assertEqual([argv[1] for argv, _ in self.docker.calls],
                         ['version', 'image', 'build', 'image', 'create', 'start', 'rm',
                          'version', 'image', 'create', 'start', 'rm'])
        self.assertIn('Preparing', self.progress.getvalue())
        self.assertEqual(self.progress.getvalue().count('signed Anthropic'), 2)
        for _, kwargs in self.docker.calls:
            self.assertEqual(kwargs['env'], {name: self.env[name] for name in ('HOME', 'PATH', 'DOCKER_HOST')})
        builds = [argv for argv, _ in self.docker.calls if argv[1] == 'build']
        self.assertIn('--progress', builds[0])
        starts = [kwargs for argv, kwargs in self.docker.calls if argv[1] == 'start']
        self.assertTrue(all(kwargs['capture_output'] and kwargs['stream_stderr'] for kwargs in starts))
        self.assertEqual(self.docker.containers, {'existing-user-container': ('a' * 64, 'user')})

    def test_bootstrap_content_and_daemon_platform_invalidate_cache(self):
        self.resolve()
        self.script += b'\n# changed helper content\n'
        self.resolve()
        self.docker.platform = 'linux/arm64'
        self.resolve()
        self.assertEqual(len(self.docker.images), 3)
        create = [argv for argv, _ in self.docker.calls if argv[1] == 'create']
        self.assertEqual([argv[argv.index('--platform') + 1] for argv in create],
                         ['linux/amd64', 'linux/amd64', 'linux/arm64'])
        self.assertTrue(all('--pull' in argv and 'never' in argv for argv in create))

    def test_cold_helper_reuses_the_project_ca_bundle_without_running_ubuntu_apt(self):
        self.resolve()
        dockerfile, = self.docker.dockerfiles
        project_mise = (ROOT / 'Dockerfile').read_text().splitlines()[0]
        self.assertIn(project_mise, dockerfile.splitlines())
        self.assertIn('COPY --from=mise /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt',
                      dockerfile.splitlines())
        self.assertFalse(any(line.startswith('RUN ') for line in dockerfile.splitlines()))

    def test_unrecognized_image_at_cache_tag_fails_before_container_creation(self):
        self.resolve()
        tag, = self.docker.images
        self.docker.images[tag] = f'linux/amd64 unrelated sha256:{"b" * 64}\n'
        self.docker.calls.clear()
        with self.assertRaisesRegex(ValueError, 'resolver image'):
            self.resolve()
        self.assertEqual([argv[1] for argv, _ in self.docker.calls], ['version', 'image'])

    def test_all_resolver_phases_share_one_deadline_with_separate_cleanup_allowance(self):
        with patch('time.monotonic', side_effect=lambda: self.docker.elapsed):
            self.resolve()
        self.assertEqual([kwargs.get('timeout') for _, kwargs in self.docker.calls],
                         [300.0, 298.0, 296.0, 294.0, 292.0, 290.0, 10.0])

    def test_timeout_command_failure_and_interrupt_clean_up_owned_container_and_keep_pin(self):
        self.seed_pin()
        before = self.pin.stat()
        for error in (subprocess.TimeoutExpired(['docker'], 1),
                      subprocess.CalledProcessError(100, ['docker']), KeyboardInterrupt()):
            with self.subTest(error=error):
                self.docker.start_error = error
                raised = KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else ValueError
                with self.assertRaises(raised):
                    self.resolve()
                self.assertEqual(self.docker.calls[-1][0][1:3], ['rm', '--force'])
                self.assertEqual(self.docker.containers, {'existing-user-container': ('a' * 64, 'user')})
                self.assertEqual(self.pin.read_bytes(), b' 5.10-1\n')
                after = self.pin.stat()
                self.assertEqual((after.st_ino, after.st_mode), (before.st_ino, before.st_mode))

    def test_interrupted_create_recovers_the_owned_id_but_never_removes_a_foreign_name(self):
        self.seed_pin()
        self.docker.create_error = subprocess.TimeoutExpired(['docker'], 1)
        with self.assertRaises(ValueError):
            self.resolve()
        self.assertEqual(self.docker.containers, {'existing-user-container': ('a' * 64, 'user')})
        self.docker.create_with_foreign_owner = True
        with self.assertRaises(ValueError):
            self.resolve()
        self.assertEqual(len(self.docker.containers), 2)
        self.assertEqual(self.pin.read_bytes(), b' 5.10-1\n')

    def test_failed_create_recovers_a_late_owned_container(self):
        self.seed_pin()
        before = self.pin.stat()
        self.docker.create_error = subprocess.CalledProcessError(1, ['docker', 'create'])
        self.docker.create_missing_inspections = 1
        with patch('time.monotonic', side_effect=lambda: self.docker.elapsed), patch('time.sleep'):
            with self.assertRaisesRegex(ValueError, 'Docker exit 1'):
                self.resolve()
        cleanup = [(argv[1], kwargs['timeout']) for argv, kwargs in self.docker.calls[-3:]]
        self.assertEqual(cleanup, [('container', 10.0), ('container', 8.0), ('rm', 6.0)])
        self.assertEqual(self.docker.containers, {'existing-user-container': ('a' * 64, 'user')})
        after = self.pin.stat()
        self.assertEqual((after.st_ino, after.st_mode), (before.st_ino, before.st_mode))
        self.assertEqual(self.pin.read_bytes(), b' 5.10-1\n')

    def test_failed_create_with_no_confirmation_reports_uncertainty_without_publishing(self):
        self.docker.create_error = subprocess.CalledProcessError(1, ['docker', 'create'])
        self.docker.create_missing_inspections = 10
        with patch('time.monotonic', side_effect=lambda: self.docker.elapsed), patch('time.sleep'):
            with self.assertRaises(ValueError):
                self.resolve()
        cleanup = [(argv[1], kwargs['timeout']) for argv, kwargs in self.docker.calls[-5:]]
        self.assertEqual(cleanup, [('container', 10.0), ('container', 8.0),
                                   ('container', 6.0), ('container', 4.0), ('container', 2.0)])
        self.assertIn('Could not confirm', self.progress.getvalue())
        self.assertFalse(self.pin.exists())

    def test_saved_pin_skips_all_resolver_operations_and_progress(self):
        self.seed_pin()
        self.assertEqual(self.resolve(refresh=False), '5.10-1')
        self.assertFalse(self.docker.calls)
        self.assertEqual(self.progress.getvalue(), '')


class DesktopResolverSignalTests(unittest.TestCase):
    def check_signal(self, operation, cancel_signal):
        launcher = '''\
import os
from pathlib import Path
from claude_docker.commands import CommandRunner
from claude_docker.desktop_version import select_desktop_version
from claude_docker.paths import resolve_host_paths
root = Path.cwd()
paths = resolve_host_paths(os.environ, os.getuid(), create_runtime=False)
select_desktop_version(root, paths, CommandRunner(), os.environ,
                       apt_script=b'fixture', refresh=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            bin_dir = home / 'bin'
            bin_dir.mkdir()
            docker = bin_dir / 'docker'
            docker.write_text(f'#!{sys.executable}\n' + FAKE_SIGNAL_DOCKER)
            docker.chmod(0o755)
            (home / f'hold-{operation}').touch()
            ready = home / f'{operation}-ready'
            release = home / 'release'
            env = {'HOME': directory, 'PATH': f'{bin_dir}{os.pathsep}{os.defpath}',
                   'PYTHONPATH': str(ROOT / 'src')}
            pin = home / '.config/claude-docker/claude-desktop.version'
            pin.parent.mkdir(parents=True)
            pin.write_bytes(b' 5.10-1\n')
            pin.chmod(0o600)
            before = pin.stat()

            with (home / 'stderr').open('w') as stderr, subprocess.Popen(
                    [sys.executable, '-B', '-c', launcher], cwd=ROOT, env=env, stderr=stderr) as process:
                try:
                    deadline = time.monotonic() + 4
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), (home / 'stderr').read_text())
                    process.send_signal(cancel_signal)
                    if operation == 'rm':
                        # The daemon still has its helper until the outstanding rm completes.
                        with self.assertRaises(subprocess.TimeoutExpired):
                            process.wait(timeout=0.25)
                        process.send_signal(signal.SIGTERM)
                        with self.assertRaises(subprocess.TimeoutExpired):
                            process.wait(timeout=0.1)
                        release.touch()
                    self.assertEqual(process.wait(timeout=4), -cancel_signal, (home / 'stderr').read_text())
                    self.assertFalse((home / 'owned-container').exists())
                    if operation == 'start':
                        for pid in ready.read_text().split():
                            try:
                                state = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[-1][0]
                            except FileNotFoundError:
                                continue
                            self.assertEqual(state, 'Z', f'Owned transport {pid} is still running')
                finally:
                    release.touch()
                    if process.poll() is None:
                        process.kill()
            self.assertEqual(pin.read_bytes(), b' 5.10-1\n')
            after = pin.stat()
            self.assertEqual((after.st_ino, after.st_mode), (before.st_ino, before.st_mode))

    def test_first_sigint_during_remove_is_deferred_until_owned_cleanup_finishes(self):
        self.check_signal('rm', signal.SIGINT)

    def test_sigterm_during_attach_cleans_transport_and_preserves_signal_exit(self):
        self.check_signal('start', signal.SIGTERM)

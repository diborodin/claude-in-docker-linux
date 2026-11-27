"""Successful mode and pending recovery records survive partial failures."""

from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_compose import ComposeFixture
from claude_docker.compose import ComposeProject
from claude_docker.state import RuntimeState, runtime_transition, runtime_transition_lock_path


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = RuntimeState(self.root)

    def test_absent_state_reads_none_and_clear_is_idempotent(self):
        self.assertIsNone(self.state.read_mode())
        self.assertIsNone(self.state.read_pending())
        self.state.clear_mode()
        self.state.clear_mode()
        self.state.clear_pending()
        self.state.clear_pending()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_mode_writes_replace_atomically_with_private_permissions(self):
        self.state.write_mode('sysbox')
        original = self.state.path.read_text()
        with patch('claude_docker.state.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                self.state.write_mode('none')
        self.assertEqual(self.state.path.read_text(), original)
        self.assertEqual(list(self.root.iterdir()), [self.state.path])
        self.state.write_mode('none')
        self.assertEqual(self.state.read_mode(), 'none')
        self.assertEqual(stat.S_IMODE(self.state.path.stat().st_mode), 0o600)
        self.state.clear_mode()
        self.assertIsNone(self.state.read_mode())

    def test_pending_is_private_atomic_and_does_not_change_successful_mode(self):
        self.state.write_mode('none')
        self.state.write_pending('sysbox')
        self.assertEqual(self.state.read_mode(), 'none')
        self.assertEqual(self.state.read_pending(), 'sysbox')
        self.assertEqual(stat.S_IMODE(self.state.pending_path.stat().st_mode), 0o600)
        with patch('claude_docker.state.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                self.state.write_pending('none')
        self.assertEqual(self.state.read_pending(), 'sysbox')
        self.assertEqual(set(self.root.iterdir()), {self.state.path, self.state.pending_path})
        self.state.pending_path.write_text('unknown-mode\n')
        with self.assertRaisesRegex(ValueError, 'pending-mode'):
            self.state.read_pending()
        with self.assertRaisesRegex(ValueError, 'mode'):
            self.state.write_pending('unknown-mode')

    def test_invalid_state_is_actionable_and_never_selected_as_a_model(self):
        self.state.path.write_text('unknown-mode\n')
        with self.assertRaisesRegex(ValueError, 'mode'):
            self.state.read_mode()
        with self.assertRaisesRegex(ValueError, 'mode'):
            self.state.write_mode('unknown-mode')


class RuntimeTransitionLockTests(ComposeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.root / '.claude-docker-locks'
        self.path = self.directory / 'runtime-transition.lock'

    def test_lock_inode_stays_private_and_stable_after_an_exception(self):
        with self.assertRaisesRegex(RuntimeError, 'launch failed'):
            with runtime_transition():
                original = self.path.stat()
                raise RuntimeError('launch failed')
        with runtime_transition():
            self.assertTrue(os.path.samestat(self.path.stat(), original))
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_exec_child_cannot_hold_the_lock_open_after_owner_releases_it(self):
        with runtime_transition():
            child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                     stdin=subprocess.PIPE, close_fds=False)
        try:
            self.assertIsNone(child.poll())
            with runtime_transition():
                self.assertIsNone(child.poll())
        finally:
            child.communicate(timeout=10)

    def test_distinct_lock_directories_do_not_contend(self):
        other_root = self.root / 'other'
        other_root.mkdir(mode=0o700)
        with runtime_transition(), patch('claude_docker.state.getpwuid',
                                         return_value=SimpleNamespace(pw_dir=str(other_root))):
            with runtime_transition():
                self.assertTrue((other_root / self.path.relative_to(self.root)).is_file())

    def test_lock_directory_symlink_is_rejected_without_modifying_target(self):
        target = self.root / 'other'
        target.mkdir()
        target.chmod(0o755)
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'runtime transition lock'):
            self.project().up('claude-cli')
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(self.runner.calls, [])

    def test_unsafe_lock_objects_are_rejected_without_modifying_target(self):
        target = self.root / 'other'
        target.write_text('keep')
        target.chmod(0o600)
        self.directory.mkdir(mode=0o700)
        for create_lock in (lambda: self.path.symlink_to(target),
                            lambda: self.path.hardlink_to(target),
                            lambda: os.mkfifo(self.path, 0o600)):
            with self.subTest(create_lock=create_lock):
                create_lock()
                with self.assertRaisesRegex(ValueError, '(?i)runtime .*lock'):
                    self.project().up('claude-cli')
                self.assertEqual(self.runner.calls, [])
                self.assertEqual(target.read_text(), 'keep')
                self.path.unlink()

    def test_foreign_owned_lock_is_rejected(self):
        self.directory.mkdir(mode=0o700)
        self.path.touch(mode=0o600)
        fstat = os.fstat

        def foreign_file_owner(descriptor):
            info = fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        with patch('claude_docker.state.os.fstat', side_effect=foreign_file_owner):
            with self.assertRaisesRegex(ValueError, 'owned'):
                self.project().up('claude-cli')
        self.assertEqual(self.runner.calls, [])


class RuntimeLockAnchorTests(unittest.TestCase):
    def test_environment_overrides_cannot_select_a_different_runtime_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch('claude_docker.state.getpwuid',
                       return_value=SimpleNamespace(pw_dir=str(home))), \
                    patch.dict(os.environ, {'HOME': '/different-home', 'XDG_STATE_HOME': '/different-state',
                                            'XDG_RUNTIME_DIR': '/different-runtime'}, clear=True):
                self.assertEqual(runtime_transition_lock_path(),
                                 home / '.claude-docker-locks' / 'runtime-transition.lock')

    def test_unsafe_home_anchor_is_rejected_before_creating_lock_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            home.chmod(0o770)
            with patch('claude_docker.state.getpwuid',
                       return_value=SimpleNamespace(pw_dir=str(home))):
                with self.assertRaisesRegex(ValueError, '(?i)account home'):
                    with runtime_transition():
                        self.fail('Lock acquired under a group-writable account home')
            self.assertEqual(list(home.iterdir()), [])

    def test_account_home_symlink_can_point_to_an_owned_private_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'real-home'
            home.mkdir(mode=0o700)
            link = root / 'home'
            link.symlink_to(home, target_is_directory=True)
            with patch('claude_docker.state.getpwuid',
                       return_value=SimpleNamespace(pw_dir=str(link))), runtime_transition():
                self.assertTrue((home / '.claude-docker-locks' / 'runtime-transition.lock').is_file())

    def test_setgid_home_preserves_private_lock_and_reuses_its_inode(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            home.chmod(0o2750)
            with patch('claude_docker.state.getpwuid',
                       return_value=SimpleNamespace(pw_dir=str(home))):
                lock = home / '.claude-docker-locks' / 'runtime-transition.lock'
                with runtime_transition():
                    original = lock.stat()
                    self.assertEqual(stat.S_IMODE(lock.parent.stat().st_mode), 0o2700)
                with runtime_transition():
                    self.assertTrue(os.path.samestat(lock.stat(), original))


class ModeTransitionTests(ComposeFixture, unittest.TestCase):
    def test_concurrent_start_cannot_consume_pending_mode_from_live_owner(self):
        project = self.project()
        context = multiprocessing.get_context('fork')
        ready, signal = context.Pipe(duplex=False)
        resume = context.Event()

        def start():
            run = project.runner.run

            def pause_before_up(argv, **kwargs):
                if 'up' in argv:
                    signal.send('starting')
                    if not resume.wait(10):
                        raise TimeoutError('Test did not release the first start')
                return run(argv, **kwargs)

            project.runner.run = pause_before_up
            project.up('claude-cli')

        owner = context.Process(target=start)
        owner.start()
        try:
            self.assertTrue(ready.poll(10), 'First start never reached Docker up')
            self.assertEqual(ready.recv(), 'starting')
            self.assertTrue((self.root / '.claude-docker-locks/runtime-transition.lock').is_file())
            self.assertEqual(project.state.read_pending(), 'none')
            # A caller must leave both Docker and the active start's record alone.
            try:
                self.project().up('claude-cli')
            except ValueError as error:
                self.assertRegex(str(error), '(?i)retry')
            else:
                self.fail(f'Concurrent start reached Docker: {[argv[-1] for argv, _ in self.runner.calls]}')
            self.assertEqual(self.runner.calls, [])
            self.assertEqual(project.state.read_pending(), 'none')
            with self.assertRaisesRegex(ValueError, '(?i)retry'):
                project.down()
            different_paths = replace(self.paths, state_dir=self.root / 'another-state',
                                      runtime_dir=self.root / 'another-runtime')
            same_project = ComposeProject(self.config, different_paths, project.mounts,
                                          self.runner, program_dir=ROOT)
            with self.assertRaisesRegex(ValueError, '(?i)retry'):
                same_project.up('claude-cli')
            self.assertFalse(different_paths.state_dir.exists())
            self.assertFalse(different_paths.runtime_dir.exists())
            self.assertEqual(self.runner.calls, [])
        finally:
            owner.terminate()
            owner.join(timeout=10)
            for connection in (ready, signal):
                connection.close()

        self.assertFalse(owner.is_alive())
        self.project().up('claude-cli')
        self.assertEqual([argv[-1] for argv, _ in self.runner.calls], ['down', 'claude-cli'])
        self.assertIsNone(project.state.read_pending())

    def test_no_prior_mode_and_same_mode_start_without_old_shutdown(self):
        project = self.project()
        state = RuntimeState(self.paths.state_dir)
        project.up('claude-cli')
        self.assertEqual(state.read_mode(), 'none')
        project.up('claude-cli')
        self.assertEqual(sum('down' in argv for argv, _ in self.runner.calls), 0)

    def test_changed_modes_stop_old_model_before_start_and_then_store_new_mode(self):
        for old, new in [('sysbox', 'none'), ('none', 'sysbox')]:
            with self.subTest(old=old, new=new):
                state = RuntimeState(self.paths.state_dir)
                state.write_mode(old)
                self.runner.calls.clear()
                self.project(config=replace(self.config, docker_mode=new)).up('claude-cli')
                shutdown, start = [argv for argv, _ in self.runner.calls]
                self.assertEqual(shutdown[-1], 'down')
                self.assertEqual(start[-6:], ['up', '-d', '--no-build', '--pull', 'never', 'claude-cli'])
                self.assertEqual(str(ROOT / 'compose.sysbox.yaml') in shutdown, old == 'sysbox')
                self.assertEqual(str(ROOT / 'compose.sysbox.yaml') in start, new == 'sysbox')
                self.assertEqual(state.read_mode(), new)

    def test_failed_old_shutdown_keeps_old_mode_and_never_starts_new_model(self):
        state = RuntimeState(self.paths.state_dir)
        state.write_mode('sysbox')
        self.runner.fail_operation = 'down'
        with self.assertRaises(subprocess.CalledProcessError):
            self.project().up('claude-cli')
        self.assertEqual(state.read_mode(), 'sysbox')
        self.assertEqual(len(self.runner.calls), 1)
        self.assertNotIn('up', self.runner.calls[0][0])

    def test_failed_new_start_never_records_new_mode(self):
        state = RuntimeState(self.paths.state_dir)
        state.write_mode('sysbox')
        self.runner.fail_operation = 'up'
        with self.assertRaises(subprocess.CalledProcessError):
            self.project().up('claude-cli')
        self.assertNotEqual(state.read_mode(), 'none')

    def test_pending_write_failure_prevents_mutating_up(self):
        project = self.project()
        with patch('claude_docker.state.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                project.up('claude-cli')
        self.assertEqual(self.runner.calls, [])
        self.assertIsNone(project.state.read_mode())
        self.assertIsNone(project.state.read_pending())

    def test_success_record_write_failure_preserves_attempted_mode_for_stop(self):
        project = self.project(config=replace(self.config, docker_mode='sysbox'))
        original_replace = os.replace

        def replace_file(source, destination):
            if Path(destination).name == 'active-mode':
                raise OSError('success record write failed')
            return original_replace(source, destination)

        with patch('claude_docker.state.os.replace', side_effect=replace_file):
            with self.assertRaises(OSError):
                project.up('claude-cli')
        self.assertEqual(self.runner.calls[-1][0][-6:], ['up', '-d', '--no-build', '--pull', 'never', 'claude-cli'])
        self.assertIsNone(project.state.read_mode())
        self.assertEqual(project.state.read_pending(), 'sysbox')
        self.project().down()
        self.assertIn(str(ROOT / 'compose.sysbox.yaml'), self.runner.calls[-1][0])
        self.assertIsNone(project.state.read_pending())

    def test_partial_failed_start_is_recovered_by_stop_even_after_mode_change(self):
        state = RuntimeState(self.paths.state_dir)
        state.write_mode('none')
        pending = self.paths.state_dir / 'pending-mode'
        resources = {'claude-cli'}
        original_run = self.runner.run

        def run(argv, **kwargs):
            if 'up' in argv:
                # Docker may create the inner daemon before dependency health fails.
                self.assertTrue(pending.exists(), 'pending mode must precede mutating up')
                self.assertEqual(pending.read_text().strip(), 'sysbox')
                resources.add('inner-docker')
            if 'down' in argv:
                resources.discard('claude-cli')
                if str(ROOT / 'compose.sysbox.yaml') in argv:
                    resources.discard('inner-docker')
            return original_run(argv, **kwargs)

        self.runner.run = run
        self.runner.fail_operation = 'up'
        with self.assertRaises(subprocess.CalledProcessError):
            self.project(config=replace(self.config, docker_mode='sysbox')).up('claude-cli')
        self.assertEqual(state.read_mode(), 'none')
        self.assertEqual(resources, {'inner-docker'})
        self.runner.fail_operation = None
        self.project().down()
        self.assertEqual(resources, set())
        self.assertIsNone(state.read_mode())
        self.assertFalse(pending.exists())

    def test_pending_mode_survives_failed_stop_then_can_be_retried(self):
        self.runner.fail_operation = 'up'
        with self.assertRaises(subprocess.CalledProcessError):
            self.project(config=replace(self.config, docker_mode='sysbox')).up('claude-cli')
        pending = self.paths.state_dir / 'pending-mode'
        self.assertTrue(pending.exists(), 'failed up must retain pending recovery state')
        self.assertEqual(pending.read_text().strip(), 'sysbox')
        self.assertIsNone(RuntimeState(self.paths.state_dir).read_mode())
        self.runner.fail_operation = 'down'
        with self.assertRaises(subprocess.CalledProcessError):
            self.project().down()
        self.assertEqual(pending.read_text().strip(), 'sysbox')
        self.runner.fail_operation = None
        self.project().down()
        self.assertIn(str(ROOT / 'compose.sysbox.yaml'), self.runner.calls[-1][0])
        self.assertFalse(pending.exists())

    def test_pending_mode_must_be_cleaned_before_another_start(self):
        pending = self.paths.state_dir / 'pending-mode'
        pending.parent.mkdir(parents=True)
        pending.write_text('sysbox\n')
        self.runner.fail_operation = 'down'
        with self.assertRaises(subprocess.CalledProcessError):
            self.project().up('claude-cli')
        self.assertFalse(any('up' in argv for argv, _ in self.runner.calls))
        self.assertEqual(pending.read_text(), 'sysbox\n')
        self.runner.fail_operation = None
        self.runner.calls.clear()
        self.project().up('claude-cli')
        self.assertIn(str(ROOT / 'compose.sysbox.yaml'), self.runner.calls[0][0])
        self.assertEqual(self.runner.calls[0][0][-1], 'down')
        self.assertEqual(self.runner.calls[1][0][-6:], ['up', '-d', '--no-build', '--pull', 'never', 'claude-cli'])
        self.assertFalse(pending.exists())

    def test_failed_generated_file_cleanup_retains_active_and_pending_for_retry(self):
        project = self.project(config=replace(self.config, docker_mode='sysbox'))
        project.write_override()
        state = RuntimeState(self.paths.state_dir)
        state.write_mode('none')
        state.write_pending('sysbox')
        original_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if Path(path).name.startswith('compose.'):
                raise OSError('generated file cleanup failed')
            return original_unlink(path, *args, **kwargs)

        with patch('claude_docker.compose.os.unlink', side_effect=unlink):
            with self.assertRaises(OSError):
                project.down()
        self.assertEqual(state.read_mode(), 'none')
        self.assertEqual(state.read_pending(), 'sysbox')
        project.down()
        self.assertIsNone(state.read_mode())
        self.assertIsNone(state.read_pending())

    def test_stop_uses_recorded_old_mode_and_clears_it_only_after_success(self):
        state = RuntimeState(self.paths.state_dir)
        state.write_mode('sysbox')
        self.runner.fail_operation = 'down'
        with self.assertRaises(subprocess.CalledProcessError):
            self.project().down()
        self.assertEqual(state.read_mode(), 'sysbox')
        self.runner.fail_operation = None
        self.project().down()
        self.assertIn(str(ROOT / 'compose.sysbox.yaml'), self.runner.calls[-1][0])
        self.assertIsNone(state.read_mode())


ROOT = Path(__file__).resolve().parents[1]

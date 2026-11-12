"""Successful mode and pending recovery records survive partial failures."""

from dataclasses import replace
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.test_compose import ComposeFixture
from claude_docker.state import RuntimeState


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


class ModeTransitionTests(ComposeFixture, unittest.TestCase):
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
                self.assertEqual(start[-3:], ['up', '-d', 'claude-cli'])
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
        self.assertEqual(self.runner.calls[-1][0][-3:], ['up', '-d', 'claude-cli'])
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
        self.assertEqual(self.runner.calls[1][0][-3:], ['up', '-d', 'claude-cli'])
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

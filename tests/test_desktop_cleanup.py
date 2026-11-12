"""Teardown preserves foreign identities and continues independent cleanup."""
from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from tests import test_desktop, test_desktop_cli
from tests.test_compose import ComposeFixture
from tests.test_compose import RecordingRunner
from claude_docker import cli, desktop, clipboard
from claude_docker.model import ProcessIdentity
from claude_docker.state import RuntimeState


class RecordedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_desktop.DesktopTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.manager
        self.paths = self.fixture.paths
        self.runtime = self.fixture.start()
        patch.object(desktop, '_desktop_socket_path', return_value=self.runtime.socket_dir).start()

    def exited(self):
        shutil.rmtree(self.fixture.proc / '123')

    def public_stop_with_broken_configuration(self, text, *, bad_program=False, fail_down=False):
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.config_file.write_text(text)
        state = RuntimeState(self.paths.state_dir)
        state.write_mode('none')
        state.write_pending('sysbox')
        runner = RecordingRunner(fail_operation='down' if fail_down else None)
        worker = ProcessIdentity(234, 567, str(Path(sys.executable).resolve()))
        descriptor = self.manager._open_private(self.paths.runtime_dir)
        try:
            clipboard.ClipboardSync(self.paths)._save(descriptor, dict(
                identity=asdict(worker), runtime={}, host_display=':0'))
        finally:
            os.close(descriptor)
        signals = []
        def worker_identity(pid):
            return worker if not signals else None
        def kill(pid, sig):
            signals.append((pid, sig))
            if pid == 123:
                self.exited()
        environment = {'HOME': str(self.paths.home)}
        if bad_program:
            environment['CLAUDE_DOCKER_DIR'] = str(self.fixture.root / 'absent-program')
        error = io.StringIO()
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(cli, 'resolve_host_paths', return_value=self.paths),
              patch.object(cli, 'CommandRunner', return_value=runner),
              patch.object(clipboard, 'process_identity', side_effect=worker_identity),
              patch.object(desktop.os, 'kill', side_effect=kill),
              redirect_stderr(error), redirect_stdout(io.StringIO())):
            rc = cli.main(['stop'])
        self.assertEqual(rc, 1)
        self.assertFalse(self.manager.record_path.exists())
        self.assertFalse(self.runtime.xauthority.exists())
        self.assertFalse((self.paths.runtime_dir / 'clipboard.json').exists())
        self.assertEqual(signals, [(234, signal.SIGTERM), (123, signal.SIGTERM)])
        commands = [argv for argv, _ in runner.calls]
        downs = [argv for argv in commands if 'down' in argv]
        self.assertEqual(len(downs), 1 if fail_down else 2)
        self.assertTrue(all('--project-name' in argv and
                            argv[argv.index('--project-name') + 1] == f'claude-docker-{os.getuid()}'
                            for argv in commands))
        self.assertTrue(all('compose.none.json' not in ' '.join(argv) and
                            'compose.sysbox.json' not in ' '.join(argv) and '--volumes' not in argv
                            for argv in commands))
        self.assertEqual(state.read_mode(), 'none' if fail_down else None)
        self.assertEqual(state.read_pending(), 'sysbox' if fail_down else None)
        self.assertNotIn('private-sentinel', error.getvalue())
        return error.getvalue()

    def test_stop_ignores_missing_policy_and_incompatible_daemon(self):
        # Use the real recorded clipboard/Xephyr cleanup and public stop boundary.
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.config_file.write_text('[desktop]\nseccomp = "chromium"\n')
        state = RuntimeState(self.paths.state_dir)
        state.write_mode('none')
        state.write_pending('sysbox')
        missing = self.fixture.root / 'program-without-policy'
        (missing / 'src/claude_docker').mkdir(parents=True)
        (missing / 'src/claude_docker/cli.py').touch()
        self.exited()
        runner = RecordingRunner(stdout='unsupported-daemon-secret')
        error = io.StringIO()
        with (patch.dict(os.environ, {'HOME': str(self.paths.home), 'CLAUDE_DOCKER_DIR': str(missing)}, clear=True),
              patch.object(cli, 'resolve_host_paths', return_value=self.paths),
              patch.object(cli, 'CommandRunner', return_value=runner),
              patch('claude_docker.seccomp.chromium_policy_path', side_effect=AssertionError('policy lookup')),
              redirect_stdout(io.StringIO()), redirect_stderr(error)):
            self.assertEqual(cli.main(['stop']), 0, error.getvalue())
        commands = [argv for argv, _ in runner.calls]
        self.assertEqual(sum(argv[-2:] == ['stop', 'claude-desktop'] for argv in commands), 1)
        self.assertEqual(sum('down' in argv for argv in commands), 2)
        self.assertTrue(all(argv[:2] == ['docker', 'compose'] for argv in commands))
        self.assertTrue(all('compose.none.json' not in ' '.join(argv) and
                            'compose.sysbox.json' not in ' '.join(argv) for argv in commands))
        self.assertFalse(self.manager.record_path.exists())
        self.assertFalse(self.runtime.xauthority.exists())
        self.assertIsNone(state.read_mode())
        self.assertIsNone(state.read_pending())

    def test_public_stop_invalid_toml_cleans_recorded_host_resources_and_owned_modes(self):
        self.public_stop_with_broken_configuration('invalid = [ # private-sentinel\n')

    def test_public_stop_invalid_schema_cleans_recorded_host_resources_and_owned_modes(self):
        self.public_stop_with_broken_configuration('unknown = "private-sentinel"\n')

    def test_public_stop_project_construction_failure_still_cleans_host_and_compose(self):
        self.public_stop_with_broken_configuration('[container]\nmounts = []\n', bad_program=True)

    def test_public_stop_reports_configuration_and_compose_errors_after_host_cleanup(self):
        error = self.public_stop_with_broken_configuration('invalid = [\n', fail_down=True)
        self.assertIn('configuration', error.lower())
        self.assertIn('Compose teardown failed', error)

    def test_xauth_failure_never_removes_a_substituted_authority(self):
        self.exited()
        self.manager.stop_recorded()
        directory = self.manager._open_private(self.paths.runtime_dir)
        authority = self.paths.runtime_dir / 'Xauthority'
        def failure(argv, **kwargs):
            authority.rename(self.paths.runtime_dir / 'original-authority')
            authority.write_text('foreign')
            raise subprocess.CalledProcessError(1, argv)
        try:
            with patch.object(desktop.subprocess, 'run', side_effect=failure):
                with self.assertRaises(ValueError):
                    self.manager._authority(20, directory)
        finally:
            os.close(directory)
        self.assertEqual(authority.read_text(), 'foreign')

    def test_interrupted_process_identity_capture_reaps_direct_child(self):
        # Use a fresh manager/start fixture; this exception occurs before recording.
        self.exited()
        self.manager.stop_recorded()
        self.fixture.x11.joinpath('X20').unlink()
        events = []
        fixture = self.fixture
        class Child:
            pid = 123
            def terminate(self):
                events.append('terminate')
            def wait(self, **kwargs):
                events.append('wait')
                return 0
        def spawned(argv, **kwargs):
            return Child()
        with (patch.object(desktop.subprocess, 'run', side_effect=fixture.xauth),
              patch.object(desktop.subprocess, 'Popen', side_effect=spawned),
              patch.object(desktop, 'process_identity', side_effect=KeyboardInterrupt)):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.start()
        self.assertEqual(events, ['terminate', 'wait'])
        self.assertFalse(self.runtime.xauthority.exists())

    def test_exited_process_cleanup_is_idempotent_and_preserves_foreign_files(self):
        self.exited()
        credential = self.paths.runtime_dir / 'credentials.json'
        credential.write_text('preserve')
        foreign = self.paths.runtime_dir / '.clipboard-foreign'
        foreign.write_text('preserve')
        self.manager.stop_recorded()
        self.manager.stop_recorded()
        self.assertFalse(self.runtime.xauthority.exists())
        self.assertFalse(self.manager.record_path.exists())
        self.assertFalse((self.runtime.socket_dir / 'X20').exists())
        self.assertTrue((self.fixture.x11 / 'X20').exists())
        self.assertEqual(credential.read_text(), 'preserve')
        self.assertEqual(foreign.read_text(), 'preserve')
        self.assertEqual(self.fixture.signals, [])

    def test_process_disappearing_before_signal_is_already_absent(self):
        with patch.object(desktop, 'process_identity', side_effect=[self.runtime.xephyr_identity, None]):
            self.manager.stop_recorded()
        self.assertFalse(self.manager.record_path.exists())
        self.assertEqual(self.fixture.signals, [])

    def test_non_object_record_is_reported_without_skipping_service_cleanup(self):
        self.manager.record_path.write_text('[]')
        class Project:
            calls = []
            def stop_service(inner, service):
                inner.calls.append(service)
        project = Project()
        with self.assertRaises(ValueError):
            desktop.stop_desktop_runtime(self.paths, project=project)
        self.assertEqual(project.calls, ['claude-desktop'])
        self.assertEqual(self.fixture.signals, [])

    def test_reused_pid_is_never_signalled_or_deleted(self):
        self.fixture.identity(ticks=999)
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.manager.stop_recorded()
        self.assertEqual(self.fixture.signals, [])
        self.assertTrue(self.manager.record_path.exists())
        self.assertTrue(self.runtime.xauthority.exists())

    def substituted_file(self, target):
        self.exited()
        original = target.with_suffix('.original')
        target.rename(original)
        target.write_text('foreign')
        target.chmod(0o600)
        with self.assertRaises(ValueError):
            self.manager.stop_recorded()
        self.assertEqual(target.read_text(), 'foreign')

    def test_substituted_cookie_is_retained_and_reported(self):
        self.substituted_file(self.runtime.xauthority)

    def test_substituted_socket_is_retained_and_reported(self):
        self.substituted_file(self.runtime.socket_dir / 'X20')

    def test_copied_record_is_rejected_before_signalling(self):
        record = self.manager.record_path
        original = record.with_suffix('.original')
        record.rename(original)
        record.write_bytes(original.read_bytes())
        record.chmod(0o600)
        with self.assertRaises(ValueError):
            self.manager.stop_recorded()
        self.assertEqual(self.fixture.signals, [])
        self.assertTrue(record.exists())

    def test_default_project_config_failure_still_cleans_recorded_display(self):
        self.exited()
        with patch('claude_docker.config.load_config', side_effect=ValueError('private config detail')):
            with self.assertRaises(ValueError) as caught:
                desktop.stop_desktop_runtime(self.paths)
        self.assertNotIn('private config detail', str(caught.exception))
        self.assertFalse(self.manager.record_path.exists())

    def test_symlink_record_or_cookie_does_not_delete_target(self):
        self.exited()
        for path in (self.manager.record_path, self.runtime.xauthority):
            with self.subTest(path=path):
                original = path.with_suffix('.original')
                path.rename(original)
                path.symlink_to(original)
                with self.assertRaises((ValueError, OSError)):
                    self.manager.stop_recorded()
                self.assertTrue(path.is_symlink())
                self.assertTrue(original.exists())
                path.unlink()
                original.rename(path)

    def test_missing_cookie_socket_and_process_are_already_absent(self):
        self.exited()
        self.runtime.xauthority.unlink()
        (self.runtime.socket_dir / 'X20').unlink()
        self.manager.stop_recorded()
        self.assertFalse(self.manager.record_path.exists())

    def test_matching_worker_stops_before_service_and_xephyr(self):
        events = []
        worker = ProcessIdentity(234, 567, str(Path(sys.executable).resolve()))
        record = dict(identity=asdict(worker), runtime={}, host_display=':0')
        directory = desktop.XephyrManager._open_private(self.paths.runtime_dir)
        try:
            clipboard.ClipboardSync(self.paths)._save(directory, record)
        finally:
            os.close(directory)
        def identity(pid):
            return worker if pid == 234 and 'worker' not in events else None
        def kill(pid, sig):
            self.assertEqual((pid, sig), (234, signal.SIGTERM))
            events.append('worker')
        class Project:
            def stop_service(inner, service):
                self.assertEqual(service, 'claude-desktop')
                events.append('service')
                self.assertFalse((self.paths.runtime_dir / 'clipboard.json').exists())
        self.exited()
        original_stop = desktop.XephyrManager.stop_recorded
        def stop(manager):
            events.append('xephyr')
            return original_stop(manager)
        with patch.object(clipboard, 'process_identity', side_effect=identity), \
                patch.object(clipboard.os, 'kill', side_effect=kill), \
                patch.object(desktop.XephyrManager, 'stop_recorded', stop):
            desktop.stop_desktop_runtime(self.paths, project=Project())
        self.assertEqual(events, ['worker', 'service', 'xephyr'])
        self.assertFalse(self.manager.record_path.exists())

    def test_worker_failure_does_not_skip_service_or_display_cleanup(self):
        worker = ProcessIdentity(234, 567, str(Path(sys.executable).resolve()))
        directory = desktop.XephyrManager._open_private(self.paths.runtime_dir)
        try:
            clipboard.ClipboardSync(self.paths)._save(directory, dict(identity=asdict(worker), runtime={}, host_display=':0'))
        finally:
            os.close(directory)
        self.exited()
        class Project:
            calls = []
            def stop_service(inner, service):
                inner.calls.append(service)
                raise subprocess.CalledProcessError(7, ['docker'], stderr='secret')
        project = Project()
        with patch.object(clipboard, 'process_identity', return_value=replace(worker, start_ticks=999)):
            with self.assertRaises(ValueError) as caught:
                desktop.stop_desktop_runtime(self.paths, project=project)
        self.assertNotIn('secret', str(caught.exception))
        self.assertIn('clipboard', str(caught.exception).lower())
        self.assertIn('service', str(caught.exception).lower())
        self.assertEqual(project.calls, ['claude-desktop'])
        self.assertFalse(self.manager.record_path.exists())
        self.assertTrue((self.paths.runtime_dir / 'clipboard.json').exists())
        self.assertEqual(self.fixture.signals, [])


class StopIntegrationTests(ComposeFixture, unittest.TestCase):
    def test_stop_uses_mount_independent_core_recovery_even_after_desktop_failure(self):
        project = self.project()
        project.state.write_mode('none')
        project.state.write_pending('sysbox')
        config = replace(self.config, container=replace(self.config.container, mounts=(self.root / 'absent-workspace',)))
        events = []
        def fail(paths, **kwargs):
            events.append('desktop')
            raise ValueError('Desktop cleanup failed')
        with patch.object(cli, 'resolve_host_paths', return_value=self.paths), \
                patch.object(cli, 'load_config', return_value=config), \
                patch.object(cli, 'CommandRunner', return_value=self.runner), \
                patch.object(cli, 'stop_desktop_runtime', side_effect=fail, create=True), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli.main(['stop'])
        self.assertEqual(rc, 1)
        self.assertEqual(events, ['desktop'])
        commands = [call[0] for call in self.runner.calls]
        self.assertEqual(sum('down' in argv for argv in commands), 2)
        self.assertTrue(all('compose.none.json' not in ' '.join(argv) and 'compose.sysbox.json' not in ' '.join(argv) for argv in commands))
        self.assertFalse((self.root / 'absent-workspace').exists())
        self.assertIsNone(project.state.read_mode())
        self.assertIsNone(project.state.read_pending())


class LaunchCleanupTests(ComposeFixture, unittest.TestCase):
    def setUp(self):
        self.fixture = test_desktop_cli.DesktopLaunchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.events = self.fixture.events
        self.runner = self.fixture.runner

    def launch(self):
        return self.fixture.launch()

    def test_interrupted_partial_launch_stops_service_and_new_display(self):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt
        with patch.object(desktop.ComposeProject, 'up', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.launch()
        self.assertEqual(self.events, ['start', 'stop'])
        self.assertTrue(any(argv[-2:] == ['stop', 'claude-desktop'] for argv, _ in self.runner.calls))

    def test_failed_launch_cleans_new_display_and_service(self):
        self.runner.app_exit = 37
        self.assertEqual(self.launch(), 37)
        self.assertEqual(self.events, ['start', 'stop'])
        self.assertTrue(any(argv[-2:] == ['stop', 'claude-desktop'] for argv, _ in self.runner.calls))

    def test_restart_stops_worker_before_service(self):
        events = []
        original = self.runner.run
        def run(argv, **kwargs):
            if argv[-2:] == ['stop', 'claude-desktop']:
                events.append('service')
            return original(argv, **kwargs)
        with (patch.object(clipboard.ClipboardSync, 'stop_recorded', side_effect=lambda: events.append('worker')),
              patch.object(self.runner, 'run', side_effect=run)):
            self.assertEqual(self.fixture.launch(restart=True), 0)
        self.assertEqual(events, ['worker', 'service'])


class ClipboardCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        self.paths = type('Paths', (), {'runtime_dir': self.directory})()
        self.worker = ProcessIdentity(234, 567, str(Path(sys.executable).resolve()))
        self.sync = clipboard.ClipboardSync(self.paths)
        descriptor = desktop.XephyrManager._open_private(self.directory)
        try:
            self.sync._save(descriptor, dict(identity=asdict(self.worker), runtime={}, host_display=':0'))
        finally:
            os.close(descriptor)

    def test_copied_worker_record_never_signals_or_deletes(self):
        record = self.directory / 'clipboard.json'
        original = self.directory / 'original'
        record.rename(original)
        record.write_bytes(original.read_bytes())
        record.chmod(0o600)
        with (patch.object(clipboard, 'process_identity', return_value=self.worker),
              patch.object(clipboard.os, 'kill') as kill):
            with self.assertRaises(ValueError):
                self.sync.stop_recorded()
        kill.assert_not_called()
        self.assertTrue(record.exists())

    def test_non_object_worker_record_is_reported_as_validation_error(self):
        (self.directory / 'clipboard.json').write_text('[]')
        with self.assertRaises(ValueError):
            self.sync.stop_recorded()

    def test_payload_entry_is_removed_when_interrupted_before_initial_unlink(self):
        real_unlink = os.unlink
        interrupted = False
        def unlink(name, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return real_unlink(name, **kwargs)
        directory = desktop.XephyrManager._open_private(self.directory)
        try:
            with patch.object(clipboard.os, 'unlink', side_effect=unlink):
                with self.assertRaises(KeyboardInterrupt):
                    with clipboard._payload(directory):
                        self.fail('Interrupted payload must never be yielded')
        finally:
            os.close(directory)
        self.assertEqual([path.name for path in self.directory.iterdir()], ['clipboard.json'])

"""Clipboard transfers exercise a fake xclip boundary, never the user's selection."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import signal
import stat
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker import cli, desktop
from claude_docker.model import DesktopRuntime, ProcessIdentity
from tests import test_desktop_cli
from tests.test_compose import ComposeFixture


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        self.runtime = DesktopRuntime(25, self.directory / 'x11', self.directory / 'Xauthority',
                                      ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        from claude_docker import clipboard
        self.clipboard = clipboard
        self.paths = type('Paths', (), {'runtime_dir': self.directory})()
        self.addCleanup(patch.stopall)
        patch.object(clipboard, 'resolve_host_paths', return_value=self.paths).start()
        patch.object(desktop.XephyrManager, 'load', return_value=self.runtime).start()
        patch.object(clipboard.shutil, 'which', return_value='/usr/bin/xclip').start()
        patch.dict(os.environ, {'DISPLAY': ':0', 'XAUTHORITY': '/host/auth'}, clear=True).start()
        self.calls = []
        self.targets = b'UTF8_STRING\nimage/png\n'
        self.payload = b'\x89PNG\r\n\x00\xffprivate'
        self.xclip_failure = False
        self.output = io.StringIO()
        self.error = io.StringIO()

    def run_xclip(self, argv, **kwargs):
        self.calls.append((argv, kwargs['env']))
        self.assertIsInstance(argv, list)
        self.assertFalse(kwargs.get('shell', False))
        self.assertNotIn(self.payload, [value for value in argv if isinstance(value, bytes)])
        if self.xclip_failure:
            raise subprocess.CalledProcessError(1, argv, output=self.payload, stderr=self.payload)
        if '-out' in argv:
            value = self.targets if argv[-1] == 'TARGETS' else self.payload
            stream = kwargs['stdout']
            self.assertEqual(stat.S_IMODE(os.fstat(stream.fileno()).st_mode), 0o600)
            self.assertEqual(Path(os.readlink(f'/proc/self/fd/{stream.fileno()}')).parent, self.directory)
            stream.write(value)
            stream.flush()
        else:
            self.assertEqual(kwargs['stdin'].read(), self.payload)
        return subprocess.CompletedProcess(argv, 0)

    def transfer(self, direction):
        with patch.object(self.clipboard.subprocess, 'run', side_effect=self.run_xclip), \
                redirect_stdout(self.output), redirect_stderr(self.error):
            getattr(self.clipboard, 'clipboard_' + direction)(self.runtime, host_display=':0')

    def test_png_push_prefers_binary_and_warns_before_host_read(self):
        original = self.run_xclip
        def check(argv, **kwargs):
            self.assertIn('WARNING', self.error.getvalue())
            return original(argv, **kwargs)
        with patch.object(self, 'run_xclip', side_effect=check):
            self.transfer('push')
        self.assertEqual(self.calls[0][1]['DISPLAY'], ':0')
        self.assertEqual(self.calls[0][1]['XAUTHORITY'], '/host/auth')
        self.assertEqual(self.calls[-1][1]['DISPLAY'], ':25')
        self.assertTrue(self.calls[-1][1]['XAUTHORITY'].startswith('/proc/self/fd/'))
        self.assertTrue(self.calls[-1][1]['XAUTHORITY'].endswith('/Xauthority'))
        self.assertEqual(self.calls[1][0][-1], 'image/png')
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertNotIn('private', self.output.getvalue() + self.error.getvalue())

    def test_pull_preserves_utf8_and_empty_selection(self):
        self.targets = b'UTF8_STRING\n'
        for payload in ('привет 🔐'.encode(), b''):
            with self.subTest(payload=payload):
                self.payload = payload
                self.calls.clear()
                self.transfer('pull')
                self.assertEqual(self.calls[0][1]['DISPLAY'], ':25')
                self.assertEqual(self.calls[-1][1]['DISPLAY'], ':0')
                self.assertEqual(self.calls[1][0][-1], 'UTF8_STRING')
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_unavailable_target_is_not_an_empty_selection(self):
        self.targets = b'TIMESTAMP\n'
        with self.assertRaisesRegex(ValueError, 'target'):
            self.transfer('pull')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_xclip_failure_is_redacted_and_payload_removed(self):
        self.xclip_failure = True
        with self.assertRaises(ValueError) as caught:
            self.transfer('push')
        self.assertNotIn('private', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_stale_runtime_and_missing_xclip_fail_before_selection_access(self):
        for loaded in (None, replace(self.runtime, display=26)):
            with patch.object(desktop.XephyrManager, 'load', return_value=loaded), \
                    self.assertRaisesRegex(ValueError, 'runtime'):
                self.transfer('push')
        with patch.object(self.clipboard.shutil, 'which', return_value=None), \
                self.assertRaisesRegex(ValueError, 'xclip'):
            self.transfer('pull')
        self.assertEqual(self.calls, [])

    def test_runtime_rotation_between_validation_and_transfer_blocks_access(self):
        with patch.object(desktop.XephyrManager, 'load', side_effect=[self.runtime, None]), \
                self.assertRaisesRegex(ValueError, 'runtime'):
            self.transfer('pull')
        self.assertEqual(self.calls, [])

    def test_runtime_rotation_before_worker_spawn_blocks_start(self):
        with patch.object(desktop.XephyrManager, 'load', side_effect=[self.runtime, None]), \
                patch.object(self.clipboard.subprocess, 'Popen', side_effect=AssertionError('worker spawned after runtime rotation')) as spawn, \
                self.assertRaisesRegex(ValueError, 'runtime'):
            self.clipboard.ClipboardSync(self.paths).start(self.runtime, host_display=':0')
        spawn.assert_not_called()

    def test_payload_cleanup_on_worker_exit(self):
        def exit_after_read(argv, **kwargs):
            self.run_xclip(argv, **kwargs)
            raise SystemExit(0)
        with patch.object(self.clipboard.subprocess, 'run', side_effect=exit_after_read), \
                self.assertRaises(SystemExit):
            self.clipboard.clipboard_pull(self.runtime, host_display=':0')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_private_directory_substitution_is_rejected(self):
        foreign = self.directory / 'foreign'
        foreign.mkdir(mode=0o700)
        link = self.directory / 'link'
        link.symlink_to(foreign, target_is_directory=True)
        self.paths.runtime_dir = link
        with self.assertRaises((ValueError, OSError)):
            self.transfer('pull')
        self.assertEqual(list(foreign.iterdir()), [])

    def test_worker_start_reuses_record_and_stale_identity_cannot_signal_or_delete(self):
        sync = self.clipboard.ClipboardSync(self.paths)
        identity = ProcessIdentity(4567, 888, str(Path(sys.executable).resolve()))
        process = type('Process', (), {'pid': identity.pid, 'poll': lambda self: None})()
        with patch.object(self.clipboard.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(self.clipboard, 'process_identity', return_value=identity), \
                redirect_stderr(self.error):
            self.assertEqual(sync.start(self.runtime, host_display=':0'), identity)
            self.assertEqual(sync.start(self.runtime, host_display=':0'), identity)
        self.assertEqual(spawn.call_count, 1)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(stat.S_IMODE((self.directory / 'clipboard.json').stat().st_mode), 0o600)
        record = (self.directory / 'clipboard.json').read_bytes()
        with patch.object(self.clipboard, 'process_identity', return_value=replace(identity, start_ticks=999)), \
                patch.object(self.clipboard.os, 'kill') as kill, self.assertRaisesRegex(ValueError, 'identity'):
            sync.stop(identity)
        kill.assert_not_called()
        self.assertEqual((self.directory / 'clipboard.json').read_bytes(), record)

    def test_stop_exact_worker_removes_only_its_record(self):
        sync = self.clipboard.ClipboardSync(self.paths)
        identity = ProcessIdentity(4567, 888, str(Path(sys.executable).resolve()))
        process = type('Process', (), {'pid': identity.pid, 'poll': lambda self: None})()
        with patch.object(self.clipboard.subprocess, 'Popen', return_value=process), \
                patch.object(self.clipboard, 'process_identity', return_value=identity), redirect_stderr(self.error):
            sync.start(self.runtime, host_display=':0')
        foreign = self.directory / 'foreign-payload'
        foreign.write_text('keep')
        with patch.object(self.clipboard, 'process_identity', side_effect=[identity, identity, None]), \
                patch.object(self.clipboard.os, 'kill') as kill:
            sync.stop(identity)
        kill.assert_called_once_with(identity.pid, signal.SIGTERM)
        self.assertEqual([p.name for p in self.directory.iterdir()], ['foreign-payload'])

    def test_payload_permissions_are_0600_even_with_restrictive_umask(self):
        previous = os.umask(0o777)
        try:
            self.transfer('pull')
        finally:
            os.umask(previous)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_private_worker_bootstraps_without_pythonpath_and_no_runtime(self):
        environment = dict(os.environ, XDG_RUNTIME_DIR=str(self.directory), PYTHONDONTWRITEBYTECODE='1')
        environment.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'claude-docker'),
                                 '_clipboard-worker'], env=environment, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout + result.stderr, b'')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_worker_launcher_uses_absolute_bootstrap_without_pythonpath(self):
        sync = self.clipboard.ClipboardSync(self.paths)
        identity = ProcessIdentity(4567, 888, str(Path(sys.executable).resolve()))
        process = type('Process', (), {'pid': identity.pid, 'poll': lambda self: None})()
        with patch.object(self.clipboard.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(self.clipboard, 'process_identity', return_value=identity):
            sync.start(self.runtime, host_display=':0')
        argv = spawn.call_args.args[0]
        self.assertEqual(argv, [sys.executable, str(Path(__file__).resolve().parents[1] / 'claude-docker'),
                                '_clipboard-worker'])
        self.assertNotIn('shell', spawn.call_args.kwargs)

    def test_worker_only_copies_changes_and_exits_when_runtime_rotates(self):
        self.targets = b'UTF8_STRING\n'
        selections = {':0': b'host-secret', ':25': b'nested-original'}
        writes = []
        sleeps = []
        def xclip(argv, **kwargs):
            display = kwargs['env']['DISPLAY']
            if '-out' in argv:
                value = self.targets if argv[-1] == 'TARGETS' else selections[display]
                kwargs['stdout'].write(value)
                kwargs['stdout'].flush()
            else:
                value = kwargs['stdin'].read()
                selections[display] = value
                writes.append((display, value))
            return subprocess.CompletedProcess(argv, 0)
        def tick(interval):
            sleeps.append(interval)
            if len(sleeps) == 2:
                selections[':25'] = b'changed-nested'
        # One validation, three polling iterations, then an authority rotation.
        with patch.object(desktop.XephyrManager, 'load', side_effect=[self.runtime] * 4 + [None]), \
                patch.object(self.clipboard.subprocess, 'run', side_effect=xclip), \
                patch.object(self.clipboard.time, 'sleep', side_effect=tick), \
                redirect_stdout(self.output), redirect_stderr(self.error):
            self.clipboard.ClipboardSync(self.paths).run(self.runtime, host_display=':0')
        self.assertEqual(writes, [(':25', b'host-secret'), (':0', b'changed-nested')])
        self.assertEqual(sleeps, [0.25, 0.25, 0.25])
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.output.getvalue() + self.error.getvalue(), '')

    def test_cli_push_and_pull_transfer_without_configuration_or_docker(self):
        for direction in ('push', 'pull'):
            self.calls.clear()
            with patch.object(cli, 'resolve_host_paths', return_value=self.paths), \
                    patch.object(cli, 'load_config') as config, \
                    patch.object(cli, 'CommandRunner') as runner, \
                    patch.object(self.clipboard.subprocess, 'run', side_effect=self.run_xclip), \
                    redirect_stdout(self.output), redirect_stderr(self.error):
                self.assertEqual(cli.main(['clipboard', direction]), 0)
            config.assert_not_called()
            runner.assert_not_called()
            self.assertEqual(len(self.calls), 3)

    def test_foreign_or_linked_worker_record_is_never_removed(self):
        foreign = self.directory / 'foreign'
        foreign.write_text('private foreign data')
        (self.directory / 'clipboard.json').symlink_to(foreign)
        identity = ProcessIdentity(4567, 888, str(Path(sys.executable).resolve()))
        with patch.object(self.clipboard.os, 'kill') as kill, self.assertRaises(OSError):
            self.clipboard.ClipboardSync(self.paths).stop(identity)
        kill.assert_not_called()
        self.assertEqual(foreign.read_text(), 'private foreign data')
        self.assertTrue((self.directory / 'clipboard.json').is_symlink())

    def test_public_cli_requires_live_runtime_without_loading_configuration(self):
        with patch.object(cli, 'resolve_host_paths', return_value=self.paths), \
                patch.object(cli, 'load_config') as config, \
                patch.object(desktop.XephyrManager, 'load', return_value=None), redirect_stderr(self.error):
            self.assertEqual(cli.main(['clipboard', 'push']), 1)
        config.assert_not_called()
        self.assertIn('runtime', self.error.getvalue())


class ClipboardModeTests(ComposeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, container=replace(self.config.container, uid=os.getuid()))
        self.runner = test_desktop_cli.DesktopRunner()
        self.paths.runtime_dir.mkdir(mode=0o700)
        sockets = self.root / 'x11'
        sockets.mkdir(mode=0o700)
        authority = self.paths.runtime_dir / 'Xauthority'
        authority.touch(mode=0o600)
        self.runtime = DesktopRuntime(25, sockets, authority, ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        self.events = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DISPLAY': ':0', 'SECRET': 'do-not-leak-credential'}, clear=True).start()
        patch.object(desktop, 'resolve_host_paths', return_value=self.paths).start()
        patch.object(desktop, 'CommandRunner', return_value=self.runner).start()
        patch.object(desktop.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name).start()
        patch.object(desktop.XephyrManager, 'load', return_value=None).start()
        patch.object(desktop.XephyrManager, 'start', side_effect=lambda: self.events.append('start') or self.runtime).start()
        patch.object(desktop.XephyrManager, 'stop', side_effect=lambda runtime: self.events.append('stop')).start()
        patch.object(desktop.time, 'sleep').start()
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    launch = test_desktop_cli.DesktopLaunchTests.launch
    app_commands = test_desktop_cli.DesktopLaunchTests.app_commands
    def test_manual_starts_no_worker(self):
        from claude_docker import clipboard
        with patch.object(clipboard.ClipboardSync, 'start') as start:
            self.assertEqual(self.launch(), 0)
        start.assert_not_called()

    def test_config_and_flag_start_once_after_warning(self):
        from claude_docker import clipboard
        for config, options in ((replace(self.config, desktop=replace(self.config.desktop, clipboard='bidirectional')), {}),
                                (self.config, {'clipboard_sync': True})):
            def start(*args, **kwargs):
                self.assertIn('secrets', self.stderr.getvalue())
            self.stderr.seek(0)
            self.stderr.truncate()
            with patch.object(clipboard.ClipboardSync, 'start', side_effect=start) as worker:
                self.assertEqual(self.launch(config, **options), 0)
            self.assertEqual(worker.call_count, 1)

    def test_existing_instance_can_enable_sync(self):
        from claude_docker import clipboard
        self.project().state.write_mode('none')
        self.runner.running = True
        with patch.object(desktop.XephyrManager, 'load', return_value=self.runtime), \
                patch.object(clipboard.ClipboardSync, 'start') as worker:
            self.assertEqual(self.launch(clipboard_sync=True), 0)
        self.assertEqual(worker.call_count, 1)

    def test_restart_stops_worker_before_rotating_authority(self):
        from claude_docker import clipboard
        with patch.object(desktop.XephyrManager, 'load', return_value=self.runtime), \
                patch.object(clipboard.ClipboardSync, 'stop_recorded', side_effect=lambda: self.events.append('clipboard-stop')):
            self.assertEqual(self.launch(restart=True), 0)
        self.assertEqual(self.events, ['clipboard-stop', 'stop', 'start'])

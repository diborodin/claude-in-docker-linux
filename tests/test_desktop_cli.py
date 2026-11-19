"""Desktop policy and orchestration at the external Docker/X server boundaries."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests.test_compose import ComposeFixture, ROOT
from tests.test_seccomp import SERVER, INFO
from tests.test_seccomp_source import OfficialSources, PROFILE_URL, REF_API, SOURCE_COMMIT, ref_json
from claude_docker import cli, desktop
from claude_docker.model import DesktopRuntime, ProcessIdentity


class DesktopRunner:
    def __init__(self):
        self.calls = []
        self.container_running = False
        self.running = False
        self.app_exit = 0
        self.openbox_exit = 0
        self.disappear = False
        self.socket_exit = 0
        self.pgrep_exit = None
        self.security_options = None
        self.server = dict(SERVER)
        self.info = dict(INFO)

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        code, output = 0, ''
        if argv[1:] == ['version', '--format', '{{json .Server}}']:
            output = json.dumps(self.server)
        if argv[1:] == ['info', '--format', '{{json .}}']:
            output = json.dumps(self.info)
        if argv[1] == 'inspect':
            output = json.dumps(self.security_options)
        if 'up' in argv and argv[-1] == 'claude-desktop':
            self.container_running = True
        if 'ps' in argv and (self.container_running or self.running):
            output = 'desktop-one\n'
        if 'pgrep' in argv:
            code = self.pgrep_exit if self.pgrep_exit is not None else (0 if self.running else 1)
        if 'openbox' in argv:
            code = self.openbox_exit
        if 'test' in argv and '-S' in argv:
            code = self.socket_exit
        if argv[-2:] == ['claude', '--version']:
            output = '2.1.328 (Claude Code)\n'
        if 'dpkg-query' in argv:
            output = '5.11492.5'
        if 'claude-desktop' in argv and argv[:2] == ['docker', 'exec'] and '-d' in argv:
            code = self.app_exit
            self.running = not code and not self.disappear
        return subprocess.CompletedProcess(argv, code, output, '')


class DesktopLaunchTests(ComposeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, container=replace(self.config.container, uid=os.getuid()))
        self.runner = DesktopRunner()
        self.paths.runtime_dir.mkdir(mode=0o700)
        sockets = self.root / 'x11'
        sockets.mkdir(mode=0o700)
        authority = self.paths.runtime_dir / 'Xauthority'
        authority.touch(mode=0o600)
        self.runtime = DesktopRuntime(25, sockets, authority, ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        self.events = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'DISPLAY': ':0', 'SECRET': 'do-not-leak-credential'}, clear=True).start()
        patch.object(desktop, 'resolve_host_paths', return_value=self.paths, create=True).start()
        patch.object(desktop, 'CommandRunner', return_value=self.runner, create=True).start()
        patch.object(desktop.shutil, 'which', side_effect=lambda name, **kwargs: '/usr/bin/' + name).start()
        patch.object(desktop.XephyrManager, 'load', return_value=None, create=True).start()
        self.start_xephyr = patch.object(
            desktop.XephyrManager, 'start', side_effect=lambda **kwargs: self.events.append('start') or self.runtime
        ).start()
        patch.object(desktop.XephyrManager, 'stop', side_effect=lambda runtime: self.events.append('stop')).start()
        patch.object(desktop.XephyrManager, 'stop_recorded', side_effect=lambda **kwargs: self.events.append('stop')).start()
        patch.object(desktop.time, 'sleep').start()
        patch('claude_docker.seccomp.resolve_host_paths', return_value=self.paths).start()
        self.sources = OfficialSources()
        patch('claude_docker.seccomp_source.fetch_official', side_effect=self.sources).start()
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def launch(self, config=None, **flags):
        options = dict(restart=False, clipboard_sync=False, no_sandbox=False, app_args=())
        options.update(flags)
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return desktop.launch_desktop(config or self.config, **options)

    def app_commands(self):
        return [argv for argv, _ in self.runner.calls if argv[:2] == ['docker', 'exec']
                and '-d' in argv and 'claude-desktop' in argv]

    def chromium_config(self):
        return replace(self.config, desktop=replace(self.config.desktop, seccomp='chromium'))

    def test_opt_in_launch_applies_cached_profile_and_warns(self):
        self.assertEqual(self.launch(self.chromium_config()), 0)
        services = json.loads((self.paths.runtime_dir / 'compose.none.json').read_text())['services']
        self.assertEqual(services['claude-desktop']['security_opt'],
                         ['seccomp=' + str(next(self.paths.cache_dir.rglob('desktop.json')))])
        self.assertNotIn('security_opt', services['claude-cli'])
        self.assertNotIn('--no-sandbox', self.app_commands()[0])
        warning = self.stderr.getvalue()
        self.assertIn('WARNING', warning)
        self.assertIn('kernel namespace', warning)
        self.assertIn("Docker's default", warning)

    def test_incompatible_policy_fails_before_state_or_runtime_creation(self):
        # Real resolver errors must precede mount preparation and runtime mkdir.
        self.runtime.xauthority.unlink()
        self.paths.runtime_dir.rmdir()
        for server in (dict(SERVER, Version='29.9.3'), dict(SERVER, Arch='arm64')):
            self.runner.server = server
            self.runner.calls.clear()
            with self.subTest(server=server), \
                    patch.object(desktop, 'prepare_mounts', side_effect=AssertionError('mount mutation')):
                with self.assertRaisesRegex(ValueError, 'seccomp policy'):
                    self.launch(self.chromium_config(), restart=True)
            self.assertFalse(self.config.state_code.source.exists())
            self.assertFalse(self.config.state_desktop.source.exists())
            self.assertFalse(self.paths.runtime_dir.exists())
            self.assertEqual(self.events, [])
            self.assertFalse(any('compose' in argv for argv, _ in self.runner.calls))

    def test_profile_rejects_all_recognized_no_sandbox_requests(self):
        selected = self.chromium_config()
        for config, flags in ((replace(selected, desktop=replace(selected.desktop, sandbox=False)), {}),
                              (selected, {'no_sandbox': True}),
                              (selected, {'app_args': ('--no-sandbox',)}),
                              (selected, {'app_args': ('--no-sandbox=true',)}),
                              (selected, {'app_args': ('--no-sandbox', '--', 'operand')})):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, 'internal sandbox'):
                self.launch(config, **flags)
            self.assertFalse(self.config.state_code.source.exists())
            self.assertEqual(self.runner.calls, [])
            self.assertEqual(self.events, [])
        self.assertEqual(self.launch(selected, app_args=('--', '--no-sandbox')), 0)
        self.assertEqual(self.app_commands()[0][-3:], ['claude-desktop', '--', '--no-sandbox'])
        self.assertNotIn('sandbox is disabled', self.stderr.getvalue())

    def test_running_desktop_policy_change_requires_restart(self):
        self.project().state.write_mode('none')
        patch.object(desktop.XephyrManager, 'load', return_value=self.runtime).start()
        self.runner.running = True
        selected = self.chromium_config()
        with self.assertRaisesRegex(ValueError, '--restart'):
            self.launch(selected)
        self.assertEqual(self.events, [])
        self.assertFalse(any('up' in argv for argv, _ in self.runner.calls))
        self.assertFalse((self.paths.runtime_dir / 'compose.none.json').exists())
        policy = next(self.paths.cache_dir.rglob('desktop.json'))
        self.runner.security_options = ['seccomp=' + policy.read_text()]
        self.assertEqual(self.launch(selected), 0)
        self.assertIn('already running', self.stdout.getvalue())
        self.runner.calls.clear()
        self.assertEqual(self.launch(selected, restart=True), 0)
        self.assertEqual(self.events, ['stop', 'start'])
        commands = [argv for argv, _ in self.runner.calls]
        up = next(argv for argv in commands if 'up' in argv)
        self.assertEqual(up[-1], 'claude-desktop')
        self.assertIn('--force-recreate', up)
        self.assertIn('--no-deps', up)
        self.assertFalse(any('down' in argv or 'claude-cli' in argv or 'inner-docker' in argv for argv in commands))
        services = json.loads((self.paths.runtime_dir / 'compose.none.json').read_text())['services']
        self.assertEqual(services['claude-desktop']['security_opt'],
                         ['seccomp=' + str(next(self.paths.cache_dir.rglob('desktop.json')))])

    def test_docker_update_keeps_running_policy_until_restart(self):
        selected = self.chromium_config()
        self.launch(selected)
        old = next(self.paths.cache_dir.rglob('desktop.json'))
        self.runner.security_options = ['seccomp=' + old.read_text()]
        self.runner.calls.clear()
        self.runner.server['Version'] = self.runner.info['ServerVersion'] = '29.10.1'
        self.sources.responses[REF_API + 'docker-v29.10.1'] = ref_json('docker-v29.10.1', SOURCE_COMMIT)
        self.sources.responses[PROFILE_URL] = self.sources.responses[PROFILE_URL].replace(b'read', b'write')
        with patch.object(desktop.XephyrManager, 'load', return_value=self.runtime):
            with self.assertRaisesRegex(ValueError, '--restart'):
                self.launch(selected)
        self.assertEqual(len(list(self.paths.cache_dir.rglob('desktop.json'))), 2)
        self.assertFalse(any('up' in argv or 'down' in argv for argv, _ in self.runner.calls))

    def test_policy_change_requires_restart_after_app_exits(self):
        self.runner.container_running = True
        self.runner.pgrep_exit = 1
        self.runner.security_options = ['seccomp={"defaultAction":"SCMP_ACT_ERRNO"}']
        with self.assertRaisesRegex(ValueError, '--restart'):
            self.launch()
        self.assertFalse(any('up' in argv for argv, _ in self.runner.calls))

    def test_running_policy_change_requires_restart_even_without_display_record(self):
        self.runner.running = True
        self.runner.security_options = ['seccomp={"defaultAction":"SCMP_ACT_ERRNO"}']
        with self.assertRaisesRegex(ValueError, '--restart'):
            self.launch()
        self.assertEqual(self.events, [])
        self.assertFalse(any('up' in argv for argv, _ in self.runner.calls))

    def test_default_starts_only_desktop_then_detached_openbox_and_sandboxed_app(self):
        self.assertEqual(self.launch(app_args=('--help', 'literal; $(false)')), 0)
        self.start_xephyr.assert_called_once_with(keyboard=self.config.desktop)
        ups = [argv for argv, _ in self.runner.calls if 'up' in argv]
        self.assertEqual(ups[0][-6:], ['up', '-d', '--no-build', '--pull', 'never', 'claude-desktop'])
        detached = [argv for argv, _ in self.runner.calls if argv[:2] == ['docker', 'exec'] and '-d' in argv]
        self.assertEqual(len(detached), 2)
        self.assertEqual(detached[0][-1], 'openbox')
        self.assertEqual(detached[1][-3:], ['claude-desktop', '--help', 'literal; $(false)'])
        self.assertNotIn('--no-sandbox', detached[1])
        self.assertEqual(detached[1][detached[1].index('--workdir') + 1], '/home/tester')
        self.assertEqual(detached[1][detached[1].index('--env') + 1], 'SECRET')
        self.assertIn('Claude Desktop version:', self.stderr.getvalue())
        self.assertNotIn('WARNING:', self.stderr.getvalue())
        checks = [argv for argv, _ in self.runner.calls if 'test' in argv]
        self.assertEqual(checks[0][-3:], ['test', '-S', '/tmp/.X11-unix/X25'])

    def test_config_and_flag_warn_before_every_unsafe_launch(self):
        for config, flags in ((replace(self.config, desktop=replace(self.config.desktop, sandbox=False)), {}),
                              (self.config, {'no_sandbox': True}),
                              (self.config, {'app_args': ('--no-sandbox',)}),
                              (self.config, {'app_args': ('--no-sandbox=true',)})):
            with self.subTest(flags=flags):
                self.runner.running = False
                before = self.stderr.getvalue().count('WARNING')
                self.assertEqual(self.launch(config, **flags), 0)
                self.assertTrue(any(arg.startswith('--no-sandbox') for arg in self.app_commands()[-1]))
                self.assertEqual(self.stderr.getvalue().count('WARNING'), before + 1)

    def test_explicit_unsafe_flag_precedes_literal_app_separator(self):
        self.assertEqual(self.launch(no_sandbox=True, app_args=('--', 'document.txt')), 0)
        self.assertEqual(self.app_commands()[0][-4:], ['claude-desktop', '--no-sandbox', '--', 'document.txt'])

    def test_unsafe_flag_and_config_insert_option_before_literal_same_named_operand(self):
        for config, flags in ((self.config, {'no_sandbox': True}),
                              (replace(self.config, desktop=replace(self.config.desktop, sandbox=False)), {})):
            with self.subTest(flags=flags):
                self.assertEqual(self.launch(config, app_args=('--', '--no-sandbox'), **flags), 0)
                command = self.app_commands()[-1]
                self.assertEqual(command[command.index('claude-desktop'):],
                                 ['claude-desktop', '--no-sandbox', '--', '--no-sandbox'])

    def test_literal_no_sandbox_operand_does_not_warn_or_change_options(self):
        self.assertEqual(self.launch(app_args=('--', '--no-sandbox')), 0)
        command = self.app_commands()[-1]
        self.assertEqual(command[command.index('claude-desktop'):],
                         ['claude-desktop', '--', '--no-sandbox'])
        self.assertIn('Claude Desktop version:', self.stderr.getvalue())
        self.assertNotIn('WARNING:', self.stderr.getvalue())

    def test_display_starts_before_any_runtime_override_is_written(self):
        def start(*, keyboard):
            self.assertFalse((self.paths.runtime_dir / 'compose.none.json').exists())
            return self.runtime
        with patch.object(desktop.XephyrManager, 'start', side_effect=start):
            self.assertEqual(self.launch(), 0)

    def test_keyboard_setup_failure_precedes_compose_mutation(self):
        self.start_xephyr.side_effect = ValueError('Cannot configure the nested Desktop keyboard')
        with self.assertRaisesRegex(ValueError, 'keyboard'):
            self.launch()
        self.start_xephyr.assert_called_once_with(keyboard=self.config.desktop)
        self.assertEqual(self.events, [])
        self.assertFalse(any(token in argv for argv, _ in self.runner.calls
                             for token in ('up', 'stop', 'openbox')))

    def test_reuse_keeps_active_keyboard_and_restart_applies_changed_settings(self):
        self.project().state.write_mode('none')
        self.runner.running = True
        changed = replace(self.config, desktop=replace(self.config.desktop, keyboard_layout='de,fr',
                                                       keyboard_options=''))
        with patch.object(desktop.XephyrManager, 'load', return_value=self.runtime):
            self.assertEqual(self.launch(changed), 0)
        self.start_xephyr.assert_not_called()
        self.assertEqual(self.launch(changed, restart=True), 0)
        self.assertEqual(self.events, ['stop', 'start'])
        self.start_xephyr.assert_called_once_with(keyboard=changed.desktop)

    def test_sandbox_launch_failure_propagates_without_retry_or_downgrade(self):
        self.runner.app_exit = 37
        self.assertEqual(self.launch(), 37)
        self.assertEqual(len(self.app_commands()), 1)
        self.assertNotIn('--no-sandbox', self.app_commands()[0])

    def test_openbox_detach_failure_is_returned_without_starting_app(self):
        self.runner.openbox_exit = 29
        self.assertEqual(self.launch(), 29)
        self.assertEqual(self.app_commands(), [])

    def test_successful_detach_followed_by_immediate_exit_reports_failure(self):
        self.runner.disappear = True
        self.assertNotEqual(self.launch(), 0)
        self.assertEqual(len(self.app_commands()), 1)
        self.assertIn('exited', self.stderr.getvalue().lower())

    def test_existing_instance_keeps_environment_and_does_not_start_another_display(self):
        self.project().state.write_mode('none')
        patch.object(desktop.XephyrManager, 'load', return_value=self.runtime).start()
        self.runner.running = True
        self.assertEqual(self.launch(), 0)
        self.assertEqual(self.events, [])
        self.assertEqual(self.app_commands(), [])
        self.assertIn('environment is unchanged', self.stdout.getvalue())
        self.assertIn('per-run sandbox, clipboard and container security choices persist until restart or stop',
                      self.stdout.getvalue())
        pgrep = [argv for argv, _ in self.runner.calls if 'pgrep' in argv]
        self.assertEqual(pgrep[0][-3:], ['pgrep', '-x', 'claude-desktop'])
        self.assertFalse(any('up' in argv for argv, _ in self.runner.calls))

    def test_sysbox_reuse_ensures_inner_docker_before_clipboard_sync(self):
        from claude_docker.clipboard import ClipboardSync

        config = replace(self.config, docker_mode='sysbox')
        self.project(config=config).state.write_mode('sysbox')
        self.runner.running = True

        def start_sync(runtime, *, host_display):
            self.assertEqual(runtime, self.runtime)
            self.assertEqual(host_display, ':0')
            self.assertEqual([argv[-1] for argv, _ in self.runner.calls if 'up' in argv],
                             ['inner-docker'])

        with patch.object(desktop.XephyrManager, 'load', return_value=self.runtime), \
                patch.object(ClipboardSync, 'start', side_effect=start_sync) as start:
            self.assertEqual(self.launch(config, clipboard_sync=True), 0)
        start.assert_called_once()
        self.assertIn('already running', self.stdout.getvalue())
        self.assertEqual(self.events, [])
        self.assertEqual(self.app_commands(), [])
        self.assertFalse(any('stop' in argv or 'down' in argv for argv, _ in self.runner.calls))

    def test_sysbox_start_targets_desktop_without_implicitly_recreating_inner_docker(self):
        config = replace(self.config, docker_mode='sysbox')
        state = self.project(config=config).state
        state.write_mode('sysbox')
        self.assertEqual(self.launch(config), 0)
        ups = [argv for argv, _ in self.runner.calls if 'up' in argv]
        self.assertEqual([argv[-1] for argv in ups], ['inner-docker', 'claude-desktop'])
        self.assertIn('--no-recreate', ups[0])
        self.assertIn('--wait', ups[0])
        self.assertIn('--no-deps', ups[1])
        self.assertNotIn('--force-recreate', ups[1])
        self.assertEqual(state.read_mode(), 'sysbox')
        self.assertIsNone(state.read_pending())

    def test_sysbox_dependency_failure_blocks_start_or_reuse_without_mode_writes(self):
        from claude_docker.clipboard import ClipboardSync

        config = replace(self.config, docker_mode='sysbox')
        state = self.project(config=config).state
        state.write_mode('sysbox')
        mode = state.path.read_bytes(), state.path.stat().st_ino
        original = self.runner.run

        def run(argv, **kwargs):
            result = original(argv, **kwargs)
            if 'up' in argv and argv[-1] == 'inner-docker':
                raise subprocess.CalledProcessError(7, argv)
            return result

        with patch.object(self.runner, 'run', side_effect=run), \
                patch.object(ClipboardSync, 'start') as sync, \
                patch.object(ClipboardSync, 'stop_recorded') as stop_sync:
            for reuse in (False, True):
                with self.subTest(reuse=reuse), patch.object(
                        desktop.XephyrManager, 'load', return_value=self.runtime if reuse else None):
                    self.runner.calls.clear()
                    self.runner.container_running = self.runner.running = reuse
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.launch(config, clipboard_sync=True)
                    self.assertEqual(self.events, [])
                    self.assertEqual(self.app_commands(), [])
                    self.assertEqual(self.runner.container_running, reuse)
                    changes = [argv[-1] for argv, _ in self.runner.calls
                               if any(operation in argv for operation in ('up', 'down', 'stop'))]
                    self.assertEqual(changes, ['inner-docker'])
                    self.assertEqual((state.path.read_bytes(), state.path.stat().st_ino), mode)
                    self.assertIsNone(state.read_pending())
        sync.assert_not_called()
        stop_sync.assert_not_called()
        self.assertNotIn('already running', self.stdout.getvalue())

    def test_sysbox_policy_mismatch_rejects_reuse_before_dependency_start(self):
        config = replace(self.config, docker_mode='sysbox')
        self.project(config=config).state.write_mode('sysbox')
        self.runner.running = True
        self.runner.security_options = ['seccomp={"defaultAction":"SCMP_ACT_ERRNO"}']

        with patch.object(desktop.XephyrManager, 'load', return_value=self.runtime):
            with self.assertRaisesRegex(ValueError, '--restart'):
                self.launch(config)
        self.assertEqual(self.events, [])
        self.assertFalse(any('up' in argv for argv, _ in self.runner.calls))

    def test_sysbox_invalid_mode_record_rejects_launch_before_dependency_start(self):
        config = replace(self.config, docker_mode='sysbox')
        state = self.project(config=config).state
        state.write_mode('sysbox')
        state.path.write_text('invalid\n')

        for restart in (False, True):
            with self.subTest(restart=restart), self.assertRaisesRegex(ValueError, 'Runtime mode'):
                self.launch(config, restart=restart)
        self.assertEqual(self.events, [])
        self.assertFalse(any('up' in argv or 'stop' in argv for argv, _ in self.runner.calls))
        self.assertEqual(state.path.read_text(), 'invalid\n')
        self.assertIsNone(state.read_pending())

    def test_sysbox_launch_paths_abort_if_mode_changes_while_waiting_for_inner_docker(self):
        from claude_docker.clipboard import ClipboardSync

        config = replace(self.config, docker_mode='sysbox')
        state = self.project(config=config).state
        original = self.runner.run

        for action in ('start', 'restart', 'reuse'):
            for changed_record in ('mode', 'pending'):
                with self.subTest(action=action, changed_record=changed_record):
                    state.write_mode('sysbox')
                    state.clear_pending()
                    self.runner.calls.clear()
                    self.runner.container_running = self.runner.running = action != 'start'
                    self.events.clear()

                    def run(argv, **kwargs):
                        if 'up' in argv and argv[-1] == 'inner-docker':
                            writer = state.write_mode if changed_record == 'mode' else state.write_pending
                            writer('none')
                        return original(argv, **kwargs)

                    with patch.object(self.runner, 'run', side_effect=run), \
                            patch.object(ClipboardSync, 'start') as start_sync, \
                            patch.object(ClipboardSync, 'stop_recorded') as stop_sync, \
                            patch.object(desktop.XephyrManager, 'load',
                                         return_value=self.runtime if action == 'reuse' else None):
                        with self.assertRaisesRegex(ValueError, 'mode.*changed'):
                            self.launch(config, restart=action == 'restart', clipboard_sync=True)
                    start_sync.assert_not_called()
                    stop_sync.assert_not_called()
                    self.assertEqual(self.events, [])
                    self.assertEqual(self.app_commands(), [])
                    changes = [argv[-1] for argv, _ in self.runner.calls
                               if any(operation in argv for operation in ('up', 'down', 'stop'))]
                    self.assertEqual(changes, ['inner-docker'])
                    self.assertEqual(state.read_mode(), 'none' if changed_record == 'mode' else 'sysbox')
                    self.assertEqual(state.read_pending(), 'none' if changed_record == 'pending' else None)

    def test_default_checks_after_per_run_overrides_only_report_requested_policy(self):
        from claude_docker.checks import run_checks
        from claude_docker.clipboard import ClipboardSync
        from tests.test_checks import DockerRunner
        with patch.object(ClipboardSync, 'start'):
            self.assertEqual(self.launch(no_sandbox=True, clipboard_sync=True), 0)
        rows = {row.name: row for row in run_checks(self.config, smoke=False,
                                                   runner=DockerRunner(), desktop=True)}
        for name in ('clipboard-mode', 'sandbox-mode'):
            self.assertIn('Requested policy for next Desktop start:', rows[name].message)
        self.assertIn('not verified', rows['desktop-lifetime'].message)
        self.assertNotIn('sandbox is enabled', rows['sandbox-mode'].message)
        self.assertNotIn('Manual clipboard transfers only.', rows['clipboard-mode'].message)

    def test_existing_instance_does_not_skip_requested_mode_transition(self):
        patch.object(desktop.XephyrManager, 'load', return_value=self.runtime).start()
        for active, desired in (('none', 'sysbox'), ('sysbox', 'none')):
            with self.subTest(active=active, desired=desired):
                self.runner.calls.clear()
                self.runner.running = True
                config = replace(self.config, docker_mode=desired)
                project = self.project(config=config)
                project.state.write_mode(active)
                self.assertEqual(self.launch(config), 0)
                commands = [argv for argv, _ in self.runner.calls]
                lifecycle = [argv for argv in commands if 'down' in argv or 'up' in argv]
                self.assertEqual([argv[-1] for argv in lifecycle],
                                 ['down', 'inner-docker', 'claude-desktop'] if desired == 'sysbox'
                                 else ['down', 'claude-desktop'])
                if desired == 'sysbox':
                    self.assertIn('--no-recreate', lifecycle[1])
                    self.assertIn('--no-deps', lifecycle[2])
                self.assertEqual(len(self.app_commands()), 1)
                self.assertEqual(project.state.read_mode(), desired)
                self.assertIsNone(project.state.read_pending())
                self.assertNotIn('environment is unchanged', self.stdout.getvalue())

    def test_existing_instance_does_not_skip_pending_mode_recovery(self):
        patch.object(desktop.XephyrManager, 'load', return_value=self.runtime).start()
        for pending in ('none', 'sysbox'):
            with self.subTest(pending=pending):
                self.runner.calls.clear()
                self.runner.running = True
                project = self.project()
                project.state.write_mode('none')
                project.state.write_pending(pending)
                self.assertEqual(self.launch(), 0)
                commands = [argv for argv, _ in self.runner.calls]
                downs = [argv for argv in commands if 'down' in argv]
                self.assertEqual(len(downs), 1)
                down = downs[0]
                self.assertEqual('compose.sysbox.yaml' in ' '.join(down), pending == 'sysbox')
                self.assertEqual(sum('up' in argv for argv in commands), 1)
                self.assertEqual(len(self.app_commands()), 1)
                self.assertEqual(project.state.read_mode(), 'none')
                self.assertIsNone(project.state.read_pending())
                self.assertNotIn('environment is unchanged', self.stdout.getvalue())

    def test_restart_refreshes_environment_and_only_recreates_desktop(self):
        patch.object(desktop.XephyrManager, 'load', return_value=self.runtime).start()
        self.runner.running = True
        self.project().state.write_mode('none')
        self.config = replace(self.config, environment=replace(self.config.environment, required=('ROTATED',)))
        with patch.dict(os.environ, {'ROTATED': 'fresh-credential'}):
            self.assertEqual(self.launch(restart=True), 0)
        self.assertEqual(self.events, ['stop', 'start'])
        commands = [argv for argv, _ in self.runner.calls]
        self.assertTrue(any(argv[-2:] == ['stop', 'claude-desktop'] for argv in commands))
        up = next(argv for argv in commands if 'up' in argv)
        self.assertIn('--force-recreate', up)
        self.assertIn('--no-deps', up)
        self.assertEqual(up[-1], 'claude-desktop')
        self.assertFalse(any('down' in argv or 'claude-cli' in argv or 'inner-docker' in argv for argv in commands))
        self.assertIn('ROTATED', self.app_commands()[0])
        self.assertNotIn('SECRET', self.app_commands()[0])

    def test_restart_rejects_mode_transition_without_stopping_services(self):
        self.project().state.write_mode('none')
        config = replace(self.config, docker_mode='sysbox')
        with self.assertRaisesRegex(ValueError, 'mode'):
            self.launch(config, restart=True)
        commands = [argv for argv, _ in self.runner.calls]
        self.assertFalse(any('down' in argv or 'stop' in argv or 'up' in argv for argv in commands))
        self.assertEqual(self.project().state.read_mode(), 'none')
        self.assertEqual(self.events, [])

    def test_restart_cleans_record_without_loading_stale_runtime_in_both_modes(self):
        from claude_docker.clipboard import ClipboardSync

        for mode in ('none', 'sysbox'):
            with self.subTest(mode=mode):
                self.events.clear()
                self.runner.calls.clear()
                config = replace(self.config, docker_mode=mode)
                self.project(config=config).state.write_mode(mode)
                dependency_targets = ['inner-docker'] if mode == 'sysbox' else []

                def stop_sync():
                    self.assertEqual([argv[-1] for argv, _ in self.runner.calls], dependency_targets)
                    self.assertEqual(self.events, [])

                with patch.object(desktop.XephyrManager, 'load', side_effect=ValueError('stale')) as load, \
                        patch.object(ClipboardSync, 'stop_recorded', side_effect=stop_sync) as stop:
                    self.assertEqual(self.launch(config, restart=True), 0)
                load.assert_not_called()
                stop.assert_called_once()
                self.assertEqual(self.events, ['stop', 'start'])
                commands = [argv for argv, _ in self.runner.calls]
                ups = [argv for argv in commands if 'up' in argv]
                self.assertEqual([argv[-1] for argv in ups], dependency_targets + ['claude-desktop'])
                self.assertIn('--no-deps', ups[-1])
                self.assertIn('--force-recreate', ups[-1])
                self.assertFalse(any('down' in argv or 'claude-cli' in argv for argv in commands))

    def test_restart_rejects_pending_modes_without_cleanup(self):
        for old, pending, desired in (('none', 'none', 'none'), ('sysbox', 'sysbox', 'sysbox')):
            with self.subTest(old=old, pending=pending, desired=desired):
                state = self.project().state
                state.clear_mode()
                state.clear_pending()
                if old is not None:
                    state.write_mode(old)
                if pending is not None:
                    state.write_pending(pending)
                self.runner.calls.clear()
                config = replace(self.config, docker_mode=desired)
                with self.assertRaisesRegex(ValueError, 'mode'):
                    self.launch(config, restart=True)
                self.assertFalse(any('down' in argv or 'stop' in argv or 'up' in argv
                                     for argv, _ in self.runner.calls))
                self.assertEqual(self.events, [])

    def test_restart_establishes_missing_mode_then_normal_launch_reuses_desktop(self):
        for mode in ('none', 'sysbox'):
            with self.subTest(mode=mode):
                state = self.project().state
                state.clear_mode()
                config = replace(self.config, docker_mode=mode)
                self.assertEqual(self.launch(config, restart=True), 0)
                self.assertEqual(state.read_mode(), mode)
                self.assertIsNone(state.read_pending())
                self.runner.calls.clear()
                with patch.object(desktop.XephyrManager, 'load', return_value=self.runtime):
                    self.assertEqual(self.launch(config), 0)
                self.assertEqual(self.app_commands(), [])
                self.assertEqual([argv[-1] for argv, _ in self.runner.calls if 'up' in argv],
                                 ['inner-docker'] if mode == 'sysbox' else [])

    def test_config_free_stop_recovers_inner_docker_after_failed_first_sysbox_restart(self):
        config = replace(self.config, docker_mode='sysbox')
        state = self.project(config=config).state
        labels = {
            'io.github.diborodin.claude-in-docker-linux.managed': 'true',
            'io.github.diborodin.claude-in-docker-linux.owner-uid': str(os.getuid()),
            'com.docker.compose.project': f'claude-docker-{os.getuid()}',
            'com.docker.compose.service': 'inner-docker',
        }
        foreign = {
            'b' * 64: {**labels, 'io.github.diborodin.claude-in-docker-linux.managed': 'false'},
            'c' * 64: {**labels, 'io.github.diborodin.claude-in-docker-linux.owner-uid': str(os.getuid() + 1)},
            'd' * 64: {**labels, 'com.docker.compose.project': 'other-project'},
            'e' * 64: {**labels, 'com.docker.compose.service': 'other-service'},
        }
        inner = 'a' * 64
        original_run = self.runner.run

        for failure in ('inner-docker', 'claude-desktop'):
            with self.subTest(failure=failure):
                containers = foreign.copy()
                stopped = []

                def run(argv, **kwargs):
                    argv = list(argv)
                    self.assertEqual(kwargs['env']['DOCKER_HOST'], 'ssh://selected')
                    self.assertNotIn('SECRET', kwargs['env'])
                    result = original_run(argv, **kwargs)
                    if 'up' in argv:
                        if argv[-1] == 'inner-docker':
                            containers[inner] = labels
                        if argv[-1] == failure:
                            raise subprocess.CalledProcessError(7, argv)
                    if argv[:3] == ['docker', 'container', 'ls']:
                        self.assertTrue({'--all', '--quiet', '--no-trunc'}.issubset(argv))
                        filters = dict(argv[i + 1].removeprefix('label=').split('=', 1)
                                       for i, arg in enumerate(argv) if arg == '--filter')
                        result.stdout = '\n'.join(identifier for identifier, actual in containers.items()
                                                 if all(actual.get(key) == value
                                                        for key, value in filters.items()))
                    if argv[:3] == ['docker', 'container', 'stop']:
                        stopped.extend(argv[3:])
                    if argv[:3] == ['docker', 'container', 'rm']:
                        for identifier in argv[3:]:
                            del containers[identifier]
                    return result

                with (patch.dict(os.environ, {'DOCKER_HOST': 'ssh://selected'}),
                      patch.object(self.runner, 'run', side_effect=run),
                      patch.object(cli, 'resolve_host_paths', return_value=self.paths),
                      patch.object(cli, 'CommandRunner', return_value=self.runner),
                      patch.object(cli, 'load_config', side_effect=ValueError('invalid configuration')),
                      redirect_stderr(self.stderr)):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.launch(config, restart=True)
                    self.assertIn(inner, containers)
                    self.assertIsNone(state.read_mode())
                    self.assertIsNone(state.read_pending())

                    self.assertEqual(cli.main(['stop']), 1)  # The config error remains visible.

                self.assertEqual(containers, foreign)
                self.assertEqual(stopped, [inner])
                self.assertNotIn('Docker Compose teardown failed', self.stderr.getvalue())

    def test_first_restart_preserves_mode_published_concurrently(self):
        state = self.project().state
        original = self.runner.run

        def run(argv, **kwargs):
            if 'up' in argv:
                state.write_mode('sysbox')
            return original(argv, **kwargs)

        with patch.object(self.runner, 'run', side_effect=run):
            self.assertEqual(self.launch(restart=True), 0)
        self.assertEqual(state.read_mode(), 'sysbox')
        self.assertIsNone(state.read_pending())

    def test_restart_does_not_consume_concurrent_core_transition(self):
        config = replace(self.config, docker_mode='sysbox')
        state = self.project(config=config).state
        state.write_mode('sysbox')
        original = self.runner.run

        def run(argv, **kwargs):
            if argv[-2:] == ['stop', 'claude-desktop']:
                state.write_pending('none')
            return original(argv, **kwargs)

        with patch.object(self.runner, 'run', side_effect=run):
            self.assertEqual(self.launch(config, restart=True), 0)
        self.assertEqual(state.read_mode(), 'sysbox')
        self.assertEqual(state.read_pending(), 'none')
        commands = [argv for argv, _ in self.runner.calls]
        self.assertFalse(any('down' in argv for argv in commands))
        ups = [argv for argv in commands if 'up' in argv]
        self.assertEqual([argv[-1] for argv in ups], ['inner-docker', 'claude-desktop'])
        self.assertIn('--no-deps', ups[1])
        self.assertIn('--force-recreate', ups[1])

    def test_failed_restart_does_not_poison_core_mode_state(self):
        state = self.project().state
        state.write_mode('none')
        original = self.runner.run

        def run(argv, **kwargs):
            if 'up' in argv:
                raise subprocess.CalledProcessError(7, argv)
            return original(argv, **kwargs)

        with patch.object(self.runner, 'run', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                self.launch(restart=True)
        self.assertEqual(state.read_mode(), 'none')
        self.assertIsNone(state.read_pending())
        self.assertFalse(any('down' in argv for argv, _ in self.runner.calls))

    def test_generated_commands_and_override_do_not_expand_privileges_or_leak_secrets(self):
        self.assertEqual(self.launch(), 0)
        model = json.loads((self.paths.runtime_dir / 'compose.none.json').read_text())
        combined = json.dumps([self.runner.calls, model])
        for forbidden in ('--privileged', 'cap_add', '--cap-add', 'unconfined', '/var/run/docker.sock',
                          'do-not-leak-credential'):
            self.assertNotIn(forbidden, combined)
        self.assertNotIn('/tmp/.X11-unix', [v['source'] for v in model['services']['claude-desktop']['volumes']])
        self.assertNotIn('DISPLAY', model['services']['claude-cli']['environment'])

    def test_missing_environment_or_mount_fails_before_container_or_display_changes(self):
        configs = (replace(self.config, environment=replace(self.config.environment, required=('MISSING',))),
                   replace(self.config, container=replace(self.config.container, mounts=(self.root / 'missing',))))
        for config in configs:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.launch(config)
            self.assertTrue(all('ps' in argv for argv, _ in self.runner.calls))
            self.assertEqual(self.events, [])

    def test_pre_launch_hook_runs_for_start_and_restart_but_not_reuse(self):
        from claude_docker.model import HooksConfig

        script = self.root / 'pre-launch.sh'
        marker = self.root / 'prepared'
        script.write_text(f'printf "%s\\n" called >> "{marker}"\nexport TOKEN=fresh\n')
        config = replace(self.config, hooks=HooksConfig(script),
                         environment=replace(self.config.environment, required=('TOKEN',)))
        self.assertEqual(self.launch(config), 0)
        self.assertEqual(marker.read_text(), 'called\n')
        app = self.app_commands()[-1]
        self.assertIn('TOKEN', app)
        self.assertNotIn('fresh', app)
        for argv, kwargs in self.runner.calls:
            if argv == app:
                self.assertEqual(kwargs['env']['TOKEN'], 'fresh')
            else:
                self.assertNotIn('TOKEN', kwargs.get('env') or {})
        self.assertNotIn('TOKEN', os.environ)

        script.unlink()
        with patch.object(desktop.XephyrManager, 'load', return_value=self.runtime):
            self.assertEqual(self.launch(config), 0)
        self.assertIn('already running', self.stdout.getvalue())
        self.assertEqual(len(self.app_commands()), 1)
        self.assertEqual(marker.read_text(), 'called\n')

        script.write_text('exit 13\n')
        self.runner.calls.clear()
        self.events.clear()
        with self.assertRaisesRegex(ValueError, r'hooks\.pre_launch'):
            self.launch(config, restart=True)
        self.assertEqual(self.events, [])
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(self.runner.running)

        script.write_text(f'printf "%s\\n" restarted >> "{marker}"\nexport TOKEN=refreshed\n')
        self.assertEqual(self.launch(config, restart=True), 0)
        self.assertEqual(marker.read_text(), 'called\nrestarted\n')
        [app] = self.app_commands()
        kwargs = next(kwargs for argv, kwargs in self.runner.calls if argv == app)
        self.assertEqual(kwargs['env']['TOKEN'], 'refreshed')

    def test_different_uid_rejected_before_state_preparation_display_or_docker(self):
        config = replace(self.config, container=replace(self.config.container, uid=os.getuid() + 1))
        with self.assertRaisesRegex(ValueError, 'container.uid.*host UID'):
            self.launch(config)
        self.assertFalse(config.state_code.source.exists())
        self.assertFalse(config.state_desktop.source.exists())
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.events, [])

    def test_missing_desktop_prerequisites_fail_before_docker(self):
        for missing in ('DISPLAY', 'Xephyr', 'xauth', 'setxkbmap'):
            with self.subTest(missing=missing):
                with patch.dict(os.environ, {'DISPLAY': '' if missing == 'DISPLAY' else ':0'}), \
                        patch.object(desktop.shutil, 'which', side_effect=lambda name: None if name == missing else '/bin/' + name):
                    with self.assertRaisesRegex(ValueError, missing):
                        self.launch()
                self.assertEqual(self.runner.calls, [])

    def test_manual_mode_does_not_require_xclip(self):
        with patch.object(desktop.shutil, 'which', side_effect=lambda name: None if name == 'xclip' else '/bin/' + name):
            self.assertEqual(self.launch(), 0)

    def test_missing_nested_socket_prevents_openbox_and_app(self):
        self.runner.socket_exit = 1
        with self.assertRaisesRegex(ValueError, 'socket'):
            self.launch()
        self.assertEqual(self.app_commands(), [])
        self.assertFalse(any('openbox' in argv for argv, _ in self.runner.calls))

    def test_process_probe_error_is_not_treated_as_no_instance(self):
        patch.object(desktop.XephyrManager, 'load', return_value=self.runtime).start()
        self.project().state.write_mode('none')
        self.runner.container_running = True
        self.runner.pgrep_exit = 2
        with self.assertRaisesRegex(ValueError, 'process'):
            self.launch()
        self.assertEqual(self.app_commands(), [])


class DesktopParserTests(unittest.TestCase):
    def test_global_debug_preserves_literal_desktop_arguments(self):
        with patch.object(cli, 'launch_desktop', return_value=0) as launch, \
                patch.object(cli, 'load_config', return_value=object()), \
                patch.object(cli, '_report_build_sources'), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['-d', 'desktop', '--restart', '--', '-d', '--help']), 0)
        self.assertTrue(launch.call_args.kwargs['restart'])
        self.assertEqual(launch.call_args.kwargs['app_args'], ['-d', '--help'])

    def test_desktop_flags_and_literal_app_arguments_are_routed(self):
        with patch.object(cli, 'launch_desktop', return_value=23, create=True) as launch, \
                patch.object(cli, 'load_config', return_value=object()), \
                patch.object(cli, 'resolve_host_paths'), \
                patch.object(cli, '_report_build_sources'):
            try:
                result = cli.main(['desktop', '--restart', '--clipboard-sync', '--no-sandbox', '--', '--help', 'literal; $(false)'])
            except SystemExit as error:
                self.fail(f'Desktop command must be routed: {error}')
        self.assertEqual(result, 23)
        self.assertEqual(launch.call_args.kwargs, dict(restart=True, clipboard_sync=True, no_sandbox=True,
                                                     app_args=['--help', 'literal; $(false)']))

    def test_no_sandbox_after_separator_remains_app_argument(self):
        with patch.object(cli, 'launch_desktop', return_value=0, create=True) as launch, \
                patch.object(cli, 'load_config', return_value=object()), \
                patch.object(cli, 'resolve_host_paths'), \
                patch.object(cli, '_report_build_sources'):
            self.assertEqual(cli.main(['desktop', '--', '--no-sandbox']), 0)
        self.assertFalse(launch.call_args.kwargs['no_sandbox'])
        self.assertEqual(launch.call_args.kwargs['app_args'], ['--no-sandbox'])

    def test_desktop_help_does_not_initialize_docker(self):
        with patch.object(cli, 'CommandRunner') as runner, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main(['desktop', '--help'])
        self.assertEqual(caught.exception.code, 0)
        runner.assert_not_called()

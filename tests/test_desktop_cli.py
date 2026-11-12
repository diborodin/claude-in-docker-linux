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
from claude_docker.seccomp import POLICY_RELATIVE_PATH
from claude_docker import cli, desktop
from claude_docker.model import DesktopRuntime, ProcessIdentity


class DesktopRunner:
    def __init__(self):
        self.calls = []
        self.running = False
        self.app_exit = 0
        self.openbox_exit = 0
        self.disappear = False
        self.socket_exit = 0
        self.pgrep_exit = None
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
        if 'ps' in argv:
            output = 'desktop-one\n'
        if 'pgrep' in argv:
            code = self.pgrep_exit if self.pgrep_exit is not None else (0 if self.running else 1)
        if 'openbox' in argv:
            code = self.openbox_exit
        if 'test' in argv and '-S' in argv:
            code = self.socket_exit
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
        patch.object(desktop.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name).start()
        patch.object(desktop.XephyrManager, 'load', return_value=None, create=True).start()
        patch.object(desktop.XephyrManager, 'start', side_effect=lambda: self.events.append('start') or self.runtime).start()
        patch.object(desktop.XephyrManager, 'stop', side_effect=lambda runtime: self.events.append('stop')).start()
        patch.object(desktop.time, 'sleep').start()
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

    def test_opt_in_launch_applies_packaged_profile_and_warns(self):
        self.assertEqual(self.launch(self.chromium_config()), 0)
        services = json.loads((self.paths.runtime_dir / 'compose.none.json').read_text())['services']
        self.assertEqual(services['claude-desktop']['security_opt'],
                         ['seccomp=' + str(ROOT / POLICY_RELATIVE_PATH)])
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
        missing = self.root / 'program'
        (missing / 'src/claude_docker').mkdir(parents=True)
        (missing / 'src/claude_docker/cli.py').touch()
        corrupt = self.root / 'corrupt-program'
        (corrupt / 'src/claude_docker').mkdir(parents=True)
        (corrupt / 'src/claude_docker/cli.py').touch()
        (corrupt / POLICY_RELATIVE_PATH).parent.mkdir()
        (corrupt / POLICY_RELATIVE_PATH).write_text('corrupted policy')
        for program, server in ((ROOT, dict(SERVER, Version='29.9.3')),
                                (missing, SERVER), (corrupt, SERVER)):
            self.runner.server = server
            self.runner.calls.clear()
            with self.subTest(program=program), patch.dict(os.environ, {'CLAUDE_DOCKER_DIR': str(program)}), \
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
        self.assertEqual(self.launch(selected), 0)
        self.assertEqual(self.events, [])
        self.assertFalse(any('up' in argv for argv, _ in self.runner.calls))
        self.assertFalse((self.paths.runtime_dir / 'compose.none.json').exists())
        self.assertIn('container security', self.stdout.getvalue())
        self.assertIn('persist until restart or stop', self.stdout.getvalue())
        self.assertEqual(self.stderr.getvalue(), '')
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
                         ['seccomp=' + str(ROOT / POLICY_RELATIVE_PATH)])

    def test_default_starts_only_desktop_then_detached_openbox_and_sandboxed_app(self):
        self.assertEqual(self.launch(app_args=('--help', 'literal; $(false)')), 0)
        ups = [argv for argv, _ in self.runner.calls if 'up' in argv]
        self.assertEqual(ups[0][-3:], ['up', '-d', 'claude-desktop'])
        detached = [argv for argv, _ in self.runner.calls if argv[:2] == ['docker', 'exec'] and '-d' in argv]
        self.assertEqual(len(detached), 2)
        self.assertEqual(detached[0][-1], 'openbox')
        self.assertEqual(detached[1][-3:], ['claude-desktop', '--help', 'literal; $(false)'])
        self.assertNotIn('--no-sandbox', detached[1])
        self.assertEqual(detached[1][detached[1].index('--workdir') + 1], '/home/tester')
        self.assertEqual(detached[1][detached[1].index('--env') + 1], 'SECRET')
        self.assertEqual(self.stderr.getvalue(), '')
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
        self.assertEqual(self.stderr.getvalue(), '')

    def test_display_starts_before_any_runtime_override_is_written(self):
        def start():
            self.assertFalse((self.paths.runtime_dir / 'compose.none.json').exists())
            return self.runtime
        with patch.object(desktop.XephyrManager, 'start', side_effect=start):
            self.assertEqual(self.launch(), 0)

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
                self.assertEqual(sum('down' in argv for argv in commands), 1)
                self.assertEqual(sum('up' in argv for argv in commands), 1)
                self.assertLess(next(i for i, argv in enumerate(commands) if 'down' in argv),
                                next(i for i, argv in enumerate(commands) if 'up' in argv))
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

    def test_restart_mode_transition_retains_core_recovery_and_starts_dependencies(self):
        self.project().state.write_mode('none')
        config = replace(self.config, docker_mode='sysbox')
        self.assertEqual(self.launch(config, restart=True), 0)
        commands = [argv for argv, _ in self.runner.calls]
        downs = [argv for argv in commands if 'down' in argv]
        self.assertEqual(len(downs), 1)
        self.assertNotIn(str(self.paths.runtime_dir / 'compose.none.json'), downs[0])
        up = next(argv for argv in commands if 'up' in argv)
        self.assertIn('--force-recreate', up)
        self.assertNotIn('--no-deps', up)
        self.assertIn('compose.sysbox.yaml', ' '.join(up))
        project = self.project(config=config)
        self.assertEqual(project.state.read_mode(), 'sysbox')
        self.assertIsNone(project.state.read_pending())

    def test_generated_commands_and_override_do_not_expand_privileges_or_leak_secrets(self):
        self.assertEqual(self.launch(), 0)
        model = json.loads((self.paths.runtime_dir / 'compose.none.json').read_text())
        combined = json.dumps([self.runner.calls, model])
        for forbidden in ('--privileged', 'cap_add', '--cap-add', 'unconfined', '/var/run/docker.sock',
                          'do-not-leak-credential'):
            self.assertNotIn(forbidden, combined)
        self.assertNotIn('/tmp/.X11-unix', [v['source'] for v in model['services']['claude-desktop']['volumes']])
        self.assertNotIn('DISPLAY', model['services']['claude-cli']['environment'])

    def test_missing_environment_or_mount_fails_before_docker_or_display(self):
        configs = (replace(self.config, environment=replace(self.config.environment, required=('MISSING',))),
                   replace(self.config, container=replace(self.config.container, mounts=(self.root / 'missing',))))
        for config in configs:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.launch(config)
            self.assertEqual(self.runner.calls, [])
            self.assertEqual(self.events, [])

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
        self.runner.pgrep_exit = 2
        with self.assertRaisesRegex(ValueError, 'process'):
            self.launch()
        self.assertEqual(self.app_commands(), [])


class DesktopParserTests(unittest.TestCase):
    def test_desktop_flags_and_literal_app_arguments_are_routed(self):
        with patch.object(cli, 'launch_desktop', return_value=23, create=True) as launch, \
                patch.object(cli, 'load_config', return_value=object()), \
                patch.object(cli, 'resolve_host_paths'):
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
                patch.object(cli, 'resolve_host_paths'):
            self.assertEqual(cli.main(['desktop', '--', '--no-sandbox']), 0)
        self.assertFalse(launch.call_args.kwargs['no_sandbox'])
        self.assertEqual(launch.call_args.kwargs['app_args'], ['--no-sandbox'])

    def test_desktop_help_does_not_initialize_docker(self):
        with patch.object(cli, 'CommandRunner') as runner, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main(['desktop', '--help'])
        self.assertEqual(caught.exception.code, 0)
        runner.assert_not_called()

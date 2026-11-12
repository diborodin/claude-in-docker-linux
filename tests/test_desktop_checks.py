"""Feature requests add read-only Desktop prerequisites and effective policy."""
from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.test_checks import Fixture, DockerRunner
from claude_docker.checks import run_checks
from tests.test_seccomp import SERVER, INFO

ROOT = Path(__file__).resolve().parents[1]


class PolicyCheckRunner(DockerRunner):
    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        if argv[1:] == ["version", "--format", "{{json .Server}}"]:
            result.stdout = json.dumps(SERVER)
        elif argv[1:] == ["info", "--format", "{{json .}}"]:
            result.stdout = json.dumps(INFO)
        return result


class DesktopChecksTests(Fixture):
    def test_check_reports_requested_policy_without_claiming_active_policy(self):
        for policy in ('default', 'chromium'):
            config = replace(self.config, desktop=replace(self.config.desktop, seccomp=policy))
            rows = {row.name: row for row in run_checks(config, smoke=False, runner=PolicyCheckRunner(),
                                                       desktop=True, program_dir=ROOT)}
            self.assertEqual(rows['desktop-seccomp'].status, 'ok')
            self.assertIn('Requested policy for next Desktop start: ' + policy, rows['desktop-seccomp'].message)
            self.assertIn('not verified', rows['desktop-lifetime'].message)
            self.assertIn('container security', rows['desktop-lifetime'].message)
            self.assertNotIn('active', rows['desktop-seccomp'].message)

    def test_opt_in_check_is_read_only(self):
        selected = replace(self.config, desktop=replace(self.config.desktop, seccomp='chromium'))
        before = {path: path.stat().st_mode for path in self.root.rglob('*')}
        runner = PolicyCheckRunner()
        with patch('os.mkdir', side_effect=AssertionError('check created directory')), \
                patch('os.chmod', side_effect=AssertionError('check chmod')), \
                patch('os.fchmod', side_effect=AssertionError('check fchmod')):
            rows = {row.name: row for row in run_checks(selected, smoke=False, runner=runner,
                                                       desktop=True, program_dir=ROOT)}
        self.assertEqual(rows['desktop-seccomp'].status, 'ok')
        self.assertEqual(before, {path: path.stat().st_mode for path in self.root.rglob('*')})
        self.assertTrue(all(argv[1] in ('--version', 'version', 'info', 'compose') for argv, _ in runner.calls))
        self.assertTrue(all('run' not in argv and 'up' not in argv for argv, _ in runner.calls))

    def test_policy_errors_are_reported_and_plain_check_does_not_resolve_policy(self):
        selected = replace(self.config, desktop=replace(self.config.desktop, seccomp='chromium'))
        for config, flags, error in ((selected, {}, 'integrity'),
                                     (selected, {'no_sandbox': True}, 'internal sandbox'),
                                     (replace(selected, docker_mode='sysbox'), {}, 'docker_mode=none')):
            rows = {row.name: row for row in run_checks(config, smoke=False, runner=PolicyCheckRunner(),
                                                       desktop=True, program_dir=self.root, **flags)}
            self.assertEqual(rows['desktop-seccomp'].status, 'error')
            self.assertIn(error, rows['desktop-seccomp'].message)
        runner = PolicyCheckRunner()
        rows = {row.name: row for row in run_checks(selected, smoke=False, runner=runner,
                                                   program_dir=self.root)}
        self.assertNotIn('desktop-seccomp', rows)
        self.assertFalse(any('{{json .Server}}' in argv or '{{json .}}' in argv for argv, _ in runner.calls))
        rows = {row.name: row for row in run_checks(self.config, smoke=False, runner=PolicyCheckRunner(),
                                                   desktop=True, program_dir=self.root)}
        self.assertEqual(rows['desktop-seccomp'].status, 'ok')

    def test_different_uid_is_actionable_only_for_desktop_features(self):
        self.config = replace(self.config, container=replace(self.config.container, uid=os.getuid() + 1))
        self.assertNotIn('desktop-uid', self.check())
        with patch('shutil.which', return_value='/bin/tool'):
            rows = self.check(desktop=True)
        self.assertIn('desktop-uid', rows)
        self.assertEqual(rows['desktop-uid'].status, 'error')
        self.assertIn('container.uid', rows['desktop-uid'].message)
        self.assertIn(str(os.getuid()), rows['desktop-uid'].message)
        self.assertIn('private', rows['desktop-uid'].message)

    def check(self, **features):
        runner = DockerRunner()
        results = run_checks(self.config, smoke=False, runner=runner, **features)
        self.assertTrue(all(argv[0] == 'docker' and 'run' not in argv for argv, _ in runner.calls))
        self.assertFalse(self.config.state_code.source.exists())
        return {row.name: row for row in results}

    def test_cli_checks_never_probe_gui_even_with_unsafe_desktop_config(self):
        self.config = replace(self.config, desktop=replace(self.config.desktop, sandbox=False, clipboard='bidirectional'))
        with patch('shutil.which', side_effect=AssertionError('GUI probe')):
            rows = self.check()
        self.assertNotIn('display', rows)
        self.assertNotIn('clipboard-mode', rows)

    def test_desktop_checks_require_display_and_three_tools_without_xclip(self):
        with patch.dict(os.environ, {}, clear=True), patch('shutil.which', return_value=None):
            rows = self.check(desktop=True)
        for name in ('display', 'Xephyr', 'xauth', 'setxkbmap'):
            self.assertEqual(rows[name].status, 'error')
        self.assertNotIn('xclip', rows)
        self.assertEqual(rows['clipboard-mode'].status, 'ok')
        self.assertIn('manual', rows['clipboard-mode'].message.lower())
        self.assertIn('enabled', rows['sandbox-mode'].message)

    def test_clipboard_and_sync_require_xclip_and_report_effective_unsafe_modes(self):
        for features in ({'clipboard': True}, {'desktop': True, 'clipboard_sync': True}):
            with self.subTest(features=features), patch.dict(os.environ, {'DISPLAY': ':0'}), \
                    patch('shutil.which', side_effect=lambda tool: None if tool == 'xclip' else '/bin/' + tool):
                rows = self.check(**features)
                self.assertEqual(rows['xclip'].status, 'error')
                self.assertEqual(rows['clipboard-mode'].status, 'warning' if features.get('clipboard_sync') else 'ok')
        self.config = replace(self.config, desktop=replace(self.config.desktop, clipboard='bidirectional', sandbox=False))
        with patch.dict(os.environ, {'DISPLAY': ':0'}), patch('shutil.which', return_value='/bin/tool'):
            rows = self.check(desktop=True)
        self.assertEqual(rows['clipboard-mode'].status, 'warning')
        self.assertEqual(rows['sandbox-mode'].status, 'warning')
        self.assertEqual(rows['xclip'].status, 'ok')

    def test_no_sandbox_override_warns_and_cli_routes_feature_flags(self):
        with patch('shutil.which', return_value='/bin/tool'):
            rows = self.check(desktop=True, no_sandbox=True)
        self.assertEqual(rows['sandbox-mode'].status, 'warning')
        self.env['DISPLAY'] = ':0'
        with patch('shutil.which', return_value='/bin/tool'):
            rc, output, error = self.invoke(['check', '--desktop', '--clipboard-sync', '--no-sandbox'], DockerRunner())
        self.assertEqual(rc, 0, error)
        self.assertIn('warning: clipboard-mode:', output)
        self.assertIn('warning: sandbox-mode:', output)

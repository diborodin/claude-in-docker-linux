"""Report the selected configuration without leaking values or changing stdout."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker import cli


class ConfigReportingTests(unittest.TestCase):
    def test_bad_config_override_with_debug_still_shows_help_and_other_paths(self):
        result, stdout, stderr = self.invoke(['-d'], dict(self.env, CLAUDE_CONFIG_FILE='relative'))
        self.assertEqual(result, 0)
        self.assertIn('usage:', stdout)
        self.assertIn('launcher version:', stderr)
        self.assertIn('image/mise.toml', stderr)
        self.assertIn('config path unavailable', stderr)

    def test_paths_are_opt_in(self):
        self.write_config(self.default_file)
        for command in ('claude', 'desktop', 'check', 'stop'):
            with self.subTest(command=command):
                status, _, quiet = self.invoke([command])
                self.assertEqual(status, 0, quiet)
                self.assertNotIn(str(self.default_file), quiet)
                self.assertNotIn('build input', quiet)
                status, _, verbose = self.invoke(['-d', command])
                self.assertEqual(status, 0, verbose)
                self.assertIn(str(self.default_file), verbose)
                self.assertIn('user mise (build input)', verbose)
                self.assertEqual(verbose.count('launcher version:'), 1)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / 'xdg')}
        self.default_file = self.home / 'xdg/claude-docker/config.toml'

    def write_config(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[container]\nmounts = []\n'
                        '[state.code]\nmount = false\n'
                        '[state.code_json]\nmount = false\n'
                        '[state.desktop]\nmount = false\n')

    def test_no_arguments_show_only_help_without_resolving_paths_or_creating_state(self):
        self.default_file.parent.mkdir(parents=True)
        self.default_file.write_text('broken TOML [ secret-sentinel')
        with (patch.object(cli, 'load_config', side_effect=AssertionError('parsed config')),
              patch.object(cli, 'resolve_host_paths', side_effect=AssertionError('resolved paths'))):
            result, stdout, stderr = self.invoke([])
        self.assertEqual(result, 0)
        self.assertIn('usage:', stdout)
        self.assertEqual(stderr, '')
        self.assertNotIn('secret-sentinel', stdout)
        self.assertFalse((self.home / '.local').exists())

    def test_inactive_tool_profile_collision_does_not_block_a_container_launch(self):
        self.write_config(self.default_file)
        for selected in (str(self.home / 'custom.lock'), 'invalid-relative'):
            result, _, stderr = self.invoke(['claude'], dict(self.env, CLAUDE_MISE_CONFIG_FILE=selected))
            self.assertEqual(result, 0, stderr)

    def invoke(self, arguments, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        account = SimpleNamespace(pw_name='tester', pw_dir=str(self.home))
        with (patch.dict(os.environ, self.env if env is None else env, clear=True),
              patch.object(cli.pwd, 'getpwuid', return_value=account),
              patch.object(cli, 'launch_desktop', return_value=0),
              patch.object(cli, 'ComposeProject') as project,
              patch.object(cli, 'CommandRunner'),
              patch.object(cli, 'run_checks', return_value=[]),
              patch.object(cli, 'lock_tools'), patch.object(cli, 'cleanup_inner_docker'),
              patch.object(cli, 'stop_desktop_runtime'),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            project.return_value.exec.return_value = 0
            project.return_value.program_dir = Path(__file__).resolve().parents[1]
            result = cli.main(arguments)
            if not arguments:
                project.assert_not_called()
        return result, stdout.getvalue(), stderr.getvalue()

    def test_commands_that_load_config_report_the_xdg_source_once(self):
        self.write_config(self.default_file)
        self.default_file.with_name('claude-code.version').write_text('2.1.326\n')
        for args in (['claude'], ['desktop'], ['build'], ['rebuild'], ['pull'],
                     ['check'], ['tools', 'lock'], ['cleanup', 'inner-docker'], ['stop']):
            with self.subTest(command=args):
                result, stdout, stderr = self.invoke(['-d', *args])
                self.assertEqual(result, 0, stderr)
                self.assertEqual(stderr.count(f'claude-docker: config: {self.default_file}\n'), 1)
                if args[0] in ('claude', 'desktop', 'build', 'rebuild'):
                    for label in ('launcher version:', 'base mise (build input):',
                                  'user mise (build input):', 'user customize (build input):'):
                        self.assertIn(label, stderr)
                if args[0] in ('build', 'rebuild'):
                    self.assertIn('Claude Code version: 2.1.326', stderr)
                self.assertNotIn(str(self.default_file), stdout)

    def test_explicit_override_is_expanded_and_reported_instead_of_default(self):
        selected = self.home / 'selected config.toml'
        self.write_config(selected)
        result, stdout, stderr = self.invoke(['-d', 'desktop'], dict(self.env, CLAUDE_CONFIG_FILE='~/selected config.toml'))
        self.assertEqual(result, 0, stderr)
        self.assertEqual(stdout, '')
        self.assertIn(f'claude-docker: config: {selected}\n', stderr)

    def test_missing_default_reports_defaults_without_creating_the_file(self):
        result, stdout, stderr = self.invoke(['-d', 'desktop'])
        self.assertEqual(result, 0, stderr)
        self.assertEqual(stdout, '')
        self.assertIn(str(self.default_file), stderr)
        self.assertIn('not found; using defaults and environment overrides', stderr)
        self.assertFalse(self.default_file.exists())

    def test_missing_explicit_file_is_an_error_without_defaults_claim(self):
        selected = self.home / 'missing.toml'
        result, stdout, stderr = self.invoke(['-d', 'desktop'], dict(self.env, CLAUDE_CONFIG_FILE=str(selected)))
        self.assertEqual(result, 1)
        self.assertIn(str(selected), stderr)
        self.assertNotIn('using defaults', stderr)
        self.assertEqual(stdout, '')

    def test_invalid_config_reports_source_without_printing_contents_or_environment(self):
        self.default_file.parent.mkdir(parents=True)
        secret = 'credential-sentinel-never-print'
        self.default_file.write_text(f'docker_mode = "{secret}"\n')
        result, stdout, stderr = self.invoke(['-d', 'desktop'], dict(self.env, AWS_SECRET_ACCESS_KEY=secret))
        self.assertEqual(result, 1)
        self.assertIn(f'claude-docker: config: {self.default_file}\n', stderr)
        self.assertNotIn(secret, stdout + stderr)


if __name__ == '__main__':
    unittest.main()

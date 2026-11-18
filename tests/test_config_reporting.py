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
        return result, stdout.getvalue(), stderr.getvalue()

    def test_commands_that_load_config_report_the_xdg_source_once(self):
        self.write_config(self.default_file)
        self.default_file.with_name('claude-code.version').write_text('2.1.326\n')
        for args in (['claude'], ['desktop'], ['build'], ['rebuild'], ['pull'],
                     ['check'], ['tools', 'lock'], ['cleanup', 'inner-docker'], ['stop']):
            with self.subTest(command=args):
                result, stdout, stderr = self.invoke(args)
                self.assertEqual(result, 0, stderr)
                self.assertEqual(stderr, f'claude-docker: config: {self.default_file}\n' +
                                 ('claude-docker: Claude Code version: 2.1.326\n'
                                  'claude-docker: tools: baseline only (no user profile)\n'
                                  if args[0] in ('build', 'rebuild') else ''))
                self.assertNotIn(str(self.default_file), stdout)

    def test_explicit_override_is_expanded_and_reported_instead_of_default(self):
        selected = self.home / 'selected config.toml'
        self.write_config(selected)
        result, stdout, stderr = self.invoke(['desktop'], dict(self.env, CLAUDE_CONFIG_FILE='~/selected config.toml'))
        self.assertEqual(result, 0, stderr)
        self.assertEqual(stdout, '')
        self.assertEqual(stderr, f'claude-docker: config: {selected}\n')

    def test_missing_default_reports_defaults_without_creating_the_file(self):
        result, stdout, stderr = self.invoke(['desktop'])
        self.assertEqual(result, 0, stderr)
        self.assertEqual(stdout, '')
        self.assertIn(str(self.default_file), stderr)
        self.assertIn('not found; using defaults and environment overrides', stderr)
        self.assertFalse(self.default_file.exists())

    def test_missing_explicit_file_is_an_error_without_defaults_claim(self):
        selected = self.home / 'missing.toml'
        result, stdout, stderr = self.invoke(['desktop'], dict(self.env, CLAUDE_CONFIG_FILE=str(selected)))
        self.assertEqual(result, 1)
        self.assertIn(str(selected), stderr)
        self.assertNotIn('using defaults', stderr)
        self.assertEqual(stdout, '')

    def test_invalid_config_reports_source_without_printing_contents_or_environment(self):
        self.default_file.parent.mkdir(parents=True)
        secret = 'credential-sentinel-never-print'
        self.default_file.write_text(f'docker_mode = "{secret}"\n')
        result, stdout, stderr = self.invoke(['desktop'], dict(self.env, AWS_SECRET_ACCESS_KEY=secret))
        self.assertEqual(result, 1)
        self.assertIn(f'claude-docker: config: {self.default_file}\n', stderr)
        self.assertNotIn(secret, stdout + stderr)


if __name__ == '__main__':
    unittest.main()

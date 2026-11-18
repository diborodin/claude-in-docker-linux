"""Discover command flags without configuration, files, or Docker access."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker import cli


class HelpTests(unittest.TestCase):
    def help(self, *arguments):
        output = io.StringIO()
        with (redirect_stdout(output),
              patch.object(cli, 'resolve_host_paths', side_effect=AssertionError('host paths in help')),
              patch.object(cli, 'load_config', side_effect=AssertionError('configuration in help')),
              patch.object(cli, 'CommandRunner', side_effect=AssertionError('Docker in help')),
              patch.object(cli, 'launch_desktop', side_effect=AssertionError('Desktop in help'))):
            with self.assertRaises(SystemExit) as result:
                cli.main([*arguments, '--help'])
        self.assertEqual(result.exception.code, 0)
        return output.getvalue()

    def test_root_help_exposes_subcommand_flags_and_nested_commands(self):
        text = self.help()
        for command in ('check', 'build', 'rebuild', 'desktop', 'uninstall'):
            with self.subTest(command=command):
                detailed = self.help(command)
                usage = detailed.split('\n\n', 1)[0]
                for flag in ('--smoke', '--desktop', '--clipboard', '--clipboard-sync',
                             '--no-sandbox', '--all', '--restart', '--purge', '--yes', '--update-claude'):
                    if flag in usage:
                        self.assertIn(flag, text)
        for command in ('config init', 'tools init', 'tools lock', 'customize init', 'cleanup inner-docker',
                        'clipboard push', 'clipboard pull'):
            self.assertIn('claude-docker ' + command, text)
        self.assertIn('claude-docker check --desktop', text)
        self.assertIn('claude-docker check --help', text)

    def test_subcommand_help_has_descriptions_and_examples_without_side_effects(self):
        for command in (('config',), ('config', 'init'), ('check',), ('tools',), ('tools', 'init'), ('tools', 'lock'),
                        ('customize',), ('customize', 'init'),
                        ('cleanup',), ('cleanup', 'inner-docker'), ('build',), ('rebuild',),
                        ('pull',), ('stop',), ('desktop',), ('clipboard',),
                        ('clipboard', 'push'), ('clipboard', 'pull'), ('uninstall',)):
            with self.subTest(command=command):
                text = self.help(*command)
                self.assertIn('Examples:', text)
                self.assertIn('claude-docker ' + ' '.join(command), text)

    def test_user_tool_help_explains_lock_scope_and_persistence_without_config(self):
        text = self.help('tools', 'init') + self.help('tools', 'lock')
        for term in ('sibling .lock', 'apt-only', 'updates', 'purge', 'Build never'):
            self.assertIn(term, text)

    def test_customize_init_help_explains_activation_and_persistence(self):
        text = self.help('customize', 'init')
        for term in ('customize.sh', 'XDG_CONFIG_HOME', 'checkbashisms', 'root', 'purge'):
            self.assertIn(term, text)

    def test_help_explains_defaults_and_application_argument_boundaries(self):
        root = self.help()
        self.assertIn('claude --help', root)
        self.assertIn('Claude Code', root)
        self.assertIn('CLI image', self.help('build'))
        self.assertIn('CLI image', self.help('rebuild'))
        for command in ('build', 'rebuild'):
            text = self.help(command)
            self.assertIn('--update-claude', text)
            self.assertIn('latest Claude Code release', text)
            self.assertIn('before Claude Code is copied', text)
            self.assertIn('The first build saves the latest Claude Code version', text)
        self.assertIn('--smoke', self.help('check'))
        self.assertIn('temporary container', self.help('check'))
        self.assertIn('next start', self.help('check'))
        self.assertIn('desktop -- --help', self.help('desktop'))
        self.assertIn('before application arguments', self.help('desktop'))
        self.assertIn('permanent', self.help('cleanup', 'inner-docker'))
        self.assertIn('--purge', self.help('uninstall'))


if __name__ == '__main__':
    unittest.main()

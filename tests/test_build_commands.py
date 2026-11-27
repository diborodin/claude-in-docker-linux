"""Build and update select products independently of cache policy."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker import cli


class BuildCommandTests(unittest.TestCase):
    def test_product_update_and_rebuild_matrix(self):
        for command in ('build', 'update'):
            for selection, services in (([], ('claude-cli', 'claude-desktop')),
                                        (['--only-cli'], ('claude-cli',)),
                                        (['--only-desktop'], ('claude-desktop',))):
                for rebuild in (False, True):
                    with self.subTest(command=command, selection=selection, rebuild=rebuild):
                        project = MagicMock()
                        project.program_dir = Path('/fixture/program')
                        context = Path('/fixture/context')
                        with patch.object(cli, 'resolve_host_paths'), patch.object(cli, 'load_config'), \
                                patch.object(cli, 'ComposeProject', return_value=project), \
                                patch.object(cli, 'image_build_inputs') as inputs, \
                                redirect_stderr(io.StringIO()):
                            inputs.return_value.__enter__.return_value = context
                            result = cli.main([command, *selection, *(['--rebuild'] if rebuild else [])])
                        self.assertEqual(result, 0)
                        self.assertEqual([call.args[0] for call in project.build.call_args_list], list(services))
                        for call in project.build.call_args_list:
                            self.assertEqual(call.kwargs, dict(pull=rebuild, no_cache=rebuild, build_context=context))
                        options = inputs.call_args.kwargs
                        self.assertEqual(options['include_desktop'], 'claude-desktop' in services)
                        self.assertEqual(options['update_claude'], command == 'update' and 'claude-cli' in services)
                        self.assertEqual(options['update_desktop'], command == 'update' and 'claude-desktop' in services)
                        project.up.assert_not_called()
                        project.exec.assert_not_called()

    def test_old_syntax_and_conflicting_selectors_fail_before_side_effects(self):
        for arguments in (['rebuild'], ['build', '--desktop'], ['build', '--all'],
                          ['build', '--update-claude'], ['build', '--update-desktop'],
                          ['update', '--update-claude'], ['update', '--update-desktop'],
                          ['build', '--only-cli', '--only-desktop'],
                          ['update', '--only-cli', '--only-desktop']):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), \
                    patch.object(cli, 'load_config') as config, patch.object(cli, 'CommandRunner') as runner:
                with self.assertRaises(SystemExit) as caught:
                    cli.main(arguments)
                self.assertEqual(caught.exception.code, 2)
                config.assert_not_called()
                runner.assert_not_called()

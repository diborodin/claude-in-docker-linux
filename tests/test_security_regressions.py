"""Fast release security gates; no daemon, display, or real user deletion."""

import ast
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from tests.test_compose import ComposeFixture
from claude_docker import cli
from claude_docker.commands import CommandRunner
from claude_docker.config import AccountInfo, load_config
from claude_docker.desktop import XephyrManager
from claude_docker.environment import select_environment_names
from claude_docker.installer import InstallLayout
from claude_docker.model import EnvironmentConfig, ProcessIdentity
from claude_docker.paths import resolve_host_paths
from claude_docker.process import docker_exec_argv
from claude_docker.uninstall import uninstall_user


class RuntimeSourceSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = [(path, ast.parse(path.read_text(), filename=str(path))) for path in
                       [ROOT / 'claude-docker', ROOT / 'install',
                        *sorted((ROOT / 'src/claude_docker').glob('*.py'))]]

    def test_subprocess_calls_cannot_enable_shell_or_broad_process_kills(self):
        # Inspect calls, not warning/doc strings or the supported image Bash hook.
        for path, tree in self.sources:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                with self.subTest(path=path.name, line=node.lineno):
                    for keyword in node.keywords:
                        if keyword.arg == 'shell':
                            self.assertIsInstance(keyword.value, ast.Constant)
                            self.assertIs(keyword.value.value, False)
                    name = ast.unparse(node.func)
                    self.assertNotIn(name, ('os.system', 'os.popen', 'subprocess.getoutput',
                                            'subprocess.getstatusoutput', 'os.killpg'))
                    if name == 'os.kill' and node.args and isinstance(node.args[0], (ast.Constant, ast.UnaryOp)):
                        self.assertGreater(ast.literal_eval(node.args[0]), 0)

    def test_runtime_argument_fragments_cannot_add_unsafe_grants(self):
        forbidden = {'--privileged', '--env-file', '--env-host', '-ac',
                     'pkill', 'killall', 'prune', '--cap-add'}
        xephyr_commands = 0
        for path, tree in self.sources:
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)):
                    items = node.elts
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in ('append', 'extend', 'insert')):
                    items = node.args
                else:
                    continue
                words = [item.value for item in items
                         if isinstance(item, ast.Constant) and isinstance(item.value, str)]
                with self.subTest(path=path.name, line=node.lineno):
                    self.assertFalse(forbidden.intersection(word.split('=', 1)[0] for word in words), words)
                    for word in words:
                        self.assertNotIn('unconfined', word)
                        self.assertNotIn('docker.sock', word)
                        self.assertNotRegex(word, r'(?:^|:)2375(?:$|/)')
                    parent = parents.get(node)
                    if (isinstance(node, ast.List) and words[:1] == ['Xephyr']
                            and (isinstance(parent, ast.Call) or
                                 isinstance(parent, ast.Assign) and any(
                                     isinstance(target, ast.Name) and target.id in ('argv', 'command')
                                     for target in parent.targets))):
                        xephyr_commands += 1
                        self.assertIn('-auth', words)
                        self.assertIn('-nolisten', words)
                        self.assertIn('tcp', words)
        self.assertGreater(xephyr_commands, 0, 'Authenticated Xephyr launch was not inspected')


class DesktopPolicyBoundaryTests(ComposeFixture, unittest.TestCase):
    def test_cli_only_start_ignores_incompatible_desktop_policy_and_has_no_security_override(self):
        config = replace(self.config, docker_mode='sysbox',
                         desktop=replace(self.config.desktop, seccomp='chromium', sandbox=False))
        with (patch.dict(os.environ, {'SECRET': 'credential-sentinel'}, clear=True),
              patch.object(cli, 'resolve_host_paths', return_value=self.paths),
              patch.object(cli, 'load_config', return_value=config),
              patch.object(cli, 'CommandRunner', return_value=self.runner),
              patch('claude_docker.seccomp.chromium_policy_path', side_effect=AssertionError('policy lookup')),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(cli.main(['claude', '--help']), 0)
        model = json.loads((self.paths.runtime_dir / 'compose.sysbox.json').read_text())
        for service in model['services'].values():
            for forbidden in ('security_opt', 'privileged', 'cap_add', 'devices', 'network_mode'):
                self.assertNotIn(forbidden, service)
        commands = [argv for argv, _ in self.runner.calls]
        self.assertTrue(any(argv[-1] == 'claude-cli' and 'up' in argv for argv in commands))
        self.assertTrue(all(argv[:2] in (['docker', 'compose'], ['docker', 'exec']) for argv in commands))
        self.assertNotIn('credential-sentinel', json.dumps(model))


class TemporaryBoundarySecurityTests(unittest.TestCase):
    def test_command_runner_keeps_metacharacters_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'shell-executed'
            argument = f'$(touch {marker}); touch {marker}'
            result = CommandRunner().run(
                [sys.executable, '-c', 'import sys; print(sys.argv[1])', argument],
                capture_output=True)
            self.assertEqual(result.stdout, argument + '\n')
            self.assertFalse(marker.exists())
            with self.assertRaises(TypeError):
                CommandRunner().run(f'touch {marker}')
            self.assertFalse(marker.exists())

    def test_exec_forwards_only_selected_names_and_never_secret_values(self):
        environment = {'TOKEN': 'secret-sentinel', 'REGION': '', 'UNRELATED': 'private-sentinel'}
        names = select_environment_names(EnvironmentConfig(('TOKEN',), ('REGION', 'ABSENT')), environment)
        argv = docker_exec_argv('owned-container', ['claude', 'literal; $(false)'],
                                workdir=Path('/work'), env_names=names, interactive=False, tty=False)
        self.assertEqual(argv, ['docker', 'exec', '--workdir', '/work', '--env', 'TOKEN',
                                '--env', 'REGION', 'owned-container', 'claude', 'literal; $(false)'])
        self.assertNotIn('secret-sentinel', repr(argv))
        self.assertNotIn('private-sentinel', repr(argv))

    def test_pass_all_environment_configs_are_rejected_without_leaking_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = resolve_host_paths({'HOME': directory}, os.getuid(), create_runtime=False)
            account = AccountInfo('tester', 12345, 12345, root)
            selected = root / 'settings.toml'
            for fragment in ('all = true', 'required = ["*"]', 'optional = ["TOKEN=secret-sentinel"]'):
                with self.subTest(fragment=fragment):
                    selected.write_text('[environment]\n' + fragment + '\n')
                    with self.assertRaises(ValueError) as caught:
                        load_config(env={}, paths=paths, account=account, config_file=selected)
                    self.assertNotIn('secret-sentinel', str(caught.exception))
            self.assertEqual(sorted(path.name for path in root.iterdir()), ['settings.toml'])

    def test_uninstall_rejects_root_home_and_symlink_roots_before_external_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            sentinel = home / 'user-data'
            sentinel.write_text('retain')
            layout = InstallLayout.user({'HOME': str(home)})
            alias = root / 'alias'
            alias.symlink_to(home, target_is_directory=True)
            unsafe = (
                replace(layout, root=Path('/'), current_link=Path('/current')),
                replace(layout, root=home, current_link=home / 'current',
                        host_paths=replace(layout.host_paths, data_dir=home)),
                replace(layout, root=alias / 'installation', current_link=alias / 'installation/current',
                        host_paths=replace(layout.host_paths, data_dir=alias / 'installation')),
            )
            with patch('claude_docker.commands.CommandRunner.run',
                       side_effect=AssertionError('unsafe layout reached external command')):
                for attack in unsafe:
                    with self.subTest(root=attack.root), self.assertRaises(ValueError):
                        uninstall_user(attack, purge=True, yes=True)
                    self.assertEqual(sentinel.read_text(), 'retain')
                    self.assertFalse((home / '.local').exists())
            self.assertTrue(alias.is_symlink())

    def test_reused_pid_is_never_signaled(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_host_paths({'HOME': directory}, os.getuid(), create_runtime=False)
            original = ProcessIdentity(12345, 100, '/usr/bin/Xephyr')
            replacement = replace(original, start_ticks=101)
            with patch('claude_docker.desktop.process_identity', return_value=replacement), \
                    patch('claude_docker.desktop.os.kill',
                          side_effect=AssertionError('reused PID was signaled')):
                with self.assertRaisesRegex(ValueError, 'identity'):
                    XephyrManager(paths)._terminate(original)


if __name__ == '__main__':
    unittest.main()

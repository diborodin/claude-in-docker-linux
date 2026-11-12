"""Purge exercises temporary HOME/XDG files and a strict fake Docker boundary."""

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from claude_docker import __version__, cli, uninstall
from claude_docker.config import AccountInfo, load_config
from claude_docker.installer import InstallLayout, install_release, install_development

MANAGED = 'io.github.diborodin.claude-in-docker-linux.managed'
OWNER = 'io.github.diborodin.claude-in-docker-linux.owner-uid'
CONTAINER, IMAGE, NETWORK = 'a' * 64, 'sha256:' + 'b' * 64, 'c' * 64
VOLUME = 'owned-data'


class DockerFixture:
    def __init__(self, uid):
        self.calls = []
        self.fail = set()
        self.resources = {}
        for kind, identifier in (('container', CONTAINER), ('image', IMAGE),
                                 ('network', NETWORK), ('volume', VOLUME)):
            for value, labels in ((identifier, {MANAGED: 'true', OWNER: str(uid)}),
                                  ('d' * 64 if kind != 'volume' else 'other-data',
                                   {MANAGED: 'true', OWNER: str(uid + 1)}),
                                  ('e' * 64 if kind != 'volume' else 'unrelated',
                                   {OWNER: str(uid)}),
                                  ('f' * 64 if kind != 'volume' else 'false-managed',
                                   {MANAGED: 'false', OWNER: str(uid)}),
                                  ('1' * 64 if kind != 'volume' else 'prefix-uid',
                                   {MANAGED: 'true', OWNER: str(uid) + '0'})):
                self.resources[kind, value] = {'Name' if kind == 'volume' else 'Id': value,
                    **({'Config': {'Labels': labels}} if kind in ('container', 'image') else {'Labels': labels})}

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        assert argv[0] == 'docker', argv
        kind, action = argv[1:3]
        if (kind, action) in self.fail:
            raise subprocess.CalledProcessError(2, argv)
        if action == 'ls':
            assert argv.count('--filter') == 2, argv
            assert 'label=' + MANAGED + '=true' in argv, argv
            assert any(arg.startswith('label=' + OWNER + '=') for arg in argv), argv
            # Return overinclusive results to prove the consumer verifies labels.
            output = '\n'.join(identifier for resource, identifier in self.resources if resource == kind)
        elif action == 'inspect':
            assert len(argv) == 4, argv
            output = json.dumps([self.resources[kind, argv[3]]])
        else:
            assert action in ('stop', 'rm'), argv
            assert argv[-1] in (CONTAINER, IMAGE, NETWORK, VOLUME), argv
            assert '--force' not in argv and '-f' not in argv, argv
            output = ''
        return subprocess.CompletedProcess(argv, 0, output, '')

    @property
    def mutations(self):
        return [call for call in self.calls if call[2] in ('rm', 'stop')]


class PurgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        runtime = self.root / 'runtime'
        runtime.mkdir(mode=0o700)
        env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.root / 'config'),
               'XDG_DATA_HOME': str(self.root / 'data'), 'XDG_STATE_HOME': str(self.root / 'state'),
               'XDG_CACHE_HOME': str(self.root / 'cache'), 'XDG_RUNTIME_DIR': str(runtime),
               'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.layout = InstallLayout.user(env)
        install_release(ROOT, self.layout, version=__version__)
        self.docker = DockerFixture(os.getuid())
        fake = self.docker
        boundary = patch('claude_docker.commands.CommandRunner.run', lambda runner, argv, **kw: fake.run(argv, **kw))
        boundary.start()
        self.addCleanup(boundary.stop)
        self.output = io.StringIO()

    def write(self, path, value='keep'):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(value)
        for directory in (self.layout.host_paths.config_dir, self.layout.host_paths.state_dir,
                          self.layout.host_paths.cache_dir, self.layout.host_paths.runtime_dir):
            if directory.exists():
                directory.chmod(0o700)
        return path

    def purge(self, **kwargs):
        with redirect_stdout(self.output):
            return uninstall.uninstall_user(self.layout, purge=True, **kwargs)

    def assert_intact(self):
        self.assertTrue(self.layout.command_link.exists())
        self.assertTrue(self.layout.registry_file.exists())
        self.assertEqual(self.docker.mutations, [])

    def test_interactive_decline_has_exact_inventory_before_prompt_and_no_mutation(self):
        def decline(prompt):
            for identifier in (CONTAINER, IMAGE, NETWORK, VOLUME):
                self.assertIn(identifier, self.output.getvalue())
            self.assert_intact()
            return 'no'
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', decline):
            report = self.purge()
        self.assert_intact()
        self.assertTrue(report.warnings)

    def test_noninteractive_requires_yes_before_service_or_file_mutation(self):
        with patch('sys.stdin.isatty', return_value=False), self.assertRaisesRegex(ValueError, '--yes'):
            self.purge()
        self.assert_intact()

    def test_interactive_accept_and_noninteractive_yes_remove_only_exact_resources(self):
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='yes'):
            self.purge()
        self.assertFalse(self.layout.command_link.is_symlink())
        self.assertEqual(self.docker.mutations, [
            ['docker', 'container', 'stop', CONTAINER], ['docker', 'container', 'rm', CONTAINER],
            ['docker', 'network', 'rm', NETWORK], ['docker', 'volume', 'rm', VOLUME],
            ['docker', 'image', 'rm', IMAGE]])

    def test_public_yes_uses_host_uid_despite_container_uid_override(self):
        with patch.dict(os.environ, {'CLAUDE_CONTAINER_UID': '43210'}), \
                patch('sys.stdin.isatty', return_value=False), redirect_stdout(self.output):
            self.assertEqual(cli.main(['uninstall', '--purge', '--yes']), 0)
        self.assertFalse(self.layout.command_link.is_symlink())
        for call in self.docker.calls:
            if call[2] == 'ls':
                self.assertIn('label=' + OWNER + '=' + str(os.getuid()), call)
        self.assertEqual(len(self.docker.mutations), 5)

    def test_public_discovery_failure_returns_clean_error_before_mutation(self):
        self.docker.fail.add(('network', 'ls'))
        errors = io.StringIO()
        with redirect_stdout(self.output), redirect_stderr(errors):
            self.assertEqual(cli.main(['uninstall', '--purge', '--yes']), 1)
        self.assertIn('Docker resource discovery failed', errors.getvalue())
        self.assertNotIn('Traceback', errors.getvalue())
        self.assert_intact()

    def test_empty_inventory_still_confirms_files_and_never_calls_resource_mutation(self):
        self.docker.resources.clear()
        config = self.write(self.layout.host_paths.config_file, '')
        def accept(prompt):
            self.assertTrue(config.exists())
            self.assertIn(str(config), self.output.getvalue())
            self.assertIn('containers: (none)', self.output.getvalue())
            self.assert_intact()
            return 'y'
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', accept):
            self.purge()
        self.assertFalse(config.exists())
        self.assertEqual(self.docker.mutations, [])

    def test_discovery_returns_only_both_exact_labels_for_every_resource_class(self):
        self.assertTrue(hasattr(uninstall, 'discover_labeled_resources'), 'Docker discovery is missing')
        found = uninstall.discover_labeled_resources(os.getuid(), self.docker)
        self.assertEqual(found.containers, (CONTAINER,))
        self.assertEqual(found.images, (IMAGE,))
        self.assertEqual(found.networks, (NETWORK,))
        self.assertEqual(found.volumes, (VOLUME,))
        self.assertEqual(self.docker.mutations, [])

    def test_malformed_inspect_ownership_refuses_before_any_mutation(self):
        self.docker.resources['container', CONTAINER]['Config'] = []
        with self.assertRaises(ValueError):
            self.purge(yes=True)
        self.assert_intact()

    def test_changed_inspected_identity_refuses_before_any_mutation(self):
        self.docker.resources['network', NETWORK]['Id'] = '2' * 64
        with self.assertRaises(ValueError):
            self.purge(yes=True)
        self.assert_intact()

    def test_orphaned_desktop_authority_and_unknown_state_are_retained(self):
        paths = self.layout.host_paths
        authority = self.write(paths.runtime_dir / 'Xauthority', 'orphaned authority')
        keep = self.write(paths.state_dir / 'unknown')
        generated = self.write(paths.runtime_dir / 'compose.none.json', '{}')
        active = self.write(paths.state_dir / 'active-mode', 'none\n')
        pending = self.write(paths.state_dir / 'pending-mode', 'sysbox\n')
        self.purge(yes=True)
        self.assertTrue(authority.exists())
        self.assertTrue(keep.exists())
        self.assertFalse(generated.exists())
        self.assertFalse(active.exists())
        self.assertFalse(pending.exists())

    def test_discovery_failure_refuses_before_any_mutation(self):
        self.docker.fail.add(('network', 'ls'))
        with self.assertRaises((ValueError, subprocess.SubprocessError)):
            self.purge(yes=True)
        self.assert_intact()

    def test_exact_removal_failures_aggregate_without_broadening_or_deleting_ownership(self):
        self.docker.fail.update({('container', 'rm'), ('volume', 'rm')})
        with self.assertRaisesRegex(ValueError, 'container.*volume'):
            self.purge(yes=True)
        self.assertTrue(self.layout.command_link.exists())
        self.assertTrue(self.layout.registry_file.exists())
        self.assertEqual(len([call for call in self.docker.mutations if call[2] == 'rm']), 4)
        self.assertFalse(any('prune' in call or 'compose' in call for call in self.docker.calls))

    def test_purge_retains_defaults_disabled_custom_sources_workspaces_and_unknown_files(self):
        paths = self.layout.host_paths
        custom = (paths.config_dir / 'claude', paths.state_dir / 'claude.json', paths.cache_dir / 'claude')
        workspace = paths.data_dir / 'workspace'
        keep = [self.write(custom[0] / 'token'), self.write(custom[1]), self.write(custom[2] / 'settings'),
                self.write(workspace / 'code'), self.write(self.home / '.claude/token'),
                self.write(self.home / '.claude.json'), self.write(self.home / '.config/Claude/settings')]
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir, paths.data_dir, paths.runtime_dir):
            keep.append(self.write(directory / 'unknown'))
        paths.runtime_dir.chmod(0o700)
        config = self.write(paths.config_file, f'[container]\nmounts = ["{workspace}"]\n' + '\n'.join(
            f'[state.{name}]\nsource = "{target}"\nmount = false\n' for name, target in
            zip(('code', 'code_json', 'desktop'), custom)))
        report = self.purge(yes=True)
        self.assertFalse(config.exists())
        self.assertTrue(all(path.exists() for path in keep))
        self.assertFalse(self.layout.registry_file.exists())
        for path in (*custom, workspace):
            self.assertIn(path, report.retained)
        self.assertTrue(paths.config_dir.is_dir())
        self.assertTrue(paths.state_dir.is_dir())

    def test_protected_config_and_program_file_survive_without_recursive_parent_removal(self):
        target = self.layout.release_dir(__version__) / 'README.md'
        config = self.write(self.layout.host_paths.config_file,
            f'[state.code]\nsource = "{self.layout.host_paths.config_dir}"\nmount = false\n'
            f'[state.code_json]\nsource = "{target}"\nmount = false\n')
        self.purge(yes=True)
        self.assertTrue(config.exists())
        self.assertTrue(target.exists())
        self.assertFalse(self.layout.command_link.is_symlink())

    def test_lexical_symlink_and_resolved_aliases_are_both_protected(self):
        paths = self.layout.host_paths
        target = self.write(paths.state_dir / 'data.json')
        alias = paths.config_dir / 'alias.json'
        alias.parent.mkdir(mode=0o700, parents=True)
        alias.symlink_to(target)
        self.write(paths.config_file, f'[state.code_json]\nsource = "{alias}"\nmount = false\n')
        account = AccountInfo('tester', 12345, 12345, self.home)
        config = load_config(env=os.environ, paths=paths, account=account)
        self.assertTrue(hasattr(uninstall, 'protected_user_paths'), 'protected path inventory is missing')
        inventory = uninstall.protected_user_paths(config)
        self.assertIn(alias, inventory)
        self.assertIn(target, inventory)
        self.purge(yes=True)
        self.assertTrue(alias.is_symlink())
        self.assertTrue(target.exists())

    def test_protected_operational_targets_reject_before_any_mutation_even_via_alias(self):
        paths = self.layout.host_paths
        for directory, name, value in ((paths.state_dir, 'active-mode', 'none\n'),
                                        (paths.state_dir, 'pending-mode', 'sysbox\n'),
                                        (paths.runtime_dir, 'compose.none.json', '{}'),
                                        (paths.runtime_dir, 'clipboard.json', '{}'),
                                        (paths.runtime_dir, 'Xauthority', 'authority')):
            with self.subTest(name=name):
                target = self.write(directory / name, value)
                if directory == paths.runtime_dir:
                    directory.chmod(0o700)
                alias = self.home / 'alias'
                alias.symlink_to(target)
                self.write(paths.config_file, f'[state.code_json]\nsource = "{alias}"\nmount = false\n')
                with self.assertRaisesRegex(ValueError, 'retained user data'):
                    self.purge(yes=True)
                self.assert_intact()
                self.assertEqual(target.read_text(), value)
                alias.unlink()

    def test_development_checkout_is_protected_inside_config_storage(self):
        self.purge(yes=True)
        self.docker.calls.clear()
        source = self.layout.host_paths.config_dir / 'checkout'
        source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        layout = InstallLayout.development(source, self.layout.command_link.parent,
                     metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        install_development(source, layout)
        self.layout = layout
        sentinel = self.write(source / 'unknown')
        self.purge(yes=True)
        self.assertTrue(sentinel.exists())
        self.assertTrue((source / 'claude-docker').exists())
        self.assertFalse(layout.command_link.is_symlink())


if __name__ == '__main__':
    unittest.main()

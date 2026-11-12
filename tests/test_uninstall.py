"""Normal uninstall uses temporary ownership fixtures and a fake Docker boundary."""

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
from claude_docker import cli, installer
from claude_docker.installer import InstallLayout, install_release, install_development
from claude_docker.seccomp import POLICY_RELATIVE_PATH


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        runtime = self.root / 'runtime'
        runtime.mkdir(mode=0o700)
        self.env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.root / 'config'),
                    'XDG_DATA_HOME': str(self.root / 'data'), 'XDG_STATE_HOME': str(self.root / 'state'),
                    'XDG_CACHE_HOME': str(self.root / 'cache'), 'XDG_RUNTIME_DIR': str(runtime),
                    'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.layout = InstallLayout.user(self.env)
        # These ownership/removal cases intentionally retain a historical release.
        self.source = self.root / 'historical-source'
        shutil.copytree(ROOT, self.source,
                        ignore=shutil.ignore_patterns('.git', '__pycache__'))
        (self.source / 'VERSION').write_text('0.1.0\n')
        (self.source / 'src/claude_docker/__init__.py').write_text('__version__ = "0.1.0"\n')
        self.calls = []
        def run(runner, argv, **kwargs):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.external = patch('claude_docker.commands.CommandRunner.run', run)
        self.external.start()
        self.addCleanup(self.external.stop)

    def api(self):
        # Missing behavior fails as an assertion before importing the new module.
        self.assertTrue((ROOT / 'src/claude_docker/uninstall.py').is_file(), 'normal uninstall is not implemented')
        from claude_docker import uninstall
        return uninstall

    def install(self):
        return install_release(self.source, self.layout, version='0.1.0')

    def write(self, path, text='retained'):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def recorded_runtime(self):
        paths = self.layout.host_paths
        authority = self.write(paths.runtime_dir / 'Xauthority', 'authority')
        paths.runtime_dir.chmod(0o700)
        authority.chmod(0o600)
        socket_dir = self.root / 'desktop-sockets/x11'
        def record(path, value):
            self.write(path, '')
            path.chmod(0o600)
            info = path.stat()
            path.write_text(json.dumps(dict(value, record_device=info.st_dev, record_inode=info.st_ino)))
        info = authority.stat()
        record(paths.runtime_dir / 'xephyr.json', {
            'display': 20, 'socket_dir': str(socket_dir), 'xauthority': str(authority),
            'xephyr_identity': {'pid': 7654321, 'start_ticks': 1, 'executable': '/usr/bin/Xephyr'},
            'authority_device': info.st_dev, 'authority_inode': info.st_ino,
            'socket_device': None, 'socket_inode': None})
        record(paths.runtime_dir / 'clipboard.json', {
            'identity': {'pid': 7654322, 'start_ticks': 1, 'executable': str(Path(sys.executable).resolve())},
            'runtime': {}, 'host_display': ':0'})
        for mode in ('none', 'sysbox'):
            self.write(paths.runtime_dir / f'compose.{mode}.json', '{}')
        self.write(paths.state_dir / 'active-mode', 'none\n')
        self.write(paths.state_dir / 'pending-mode', 'sysbox\n')
        for target, value in (('claude_docker.desktop._desktop_socket_path', socket_dir),
                              ('claude_docker.desktop.process_identity', None),
                              ('claude_docker.clipboard.process_identity', None)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return paths.runtime_dir / 'clipboard.json', authority

    def assert_rejected_untouched(self, action):
        command = self.layout.command_link.readlink()
        executable = self.layout.release_dir('0.1.0') / 'claude-docker'
        with self.assertRaises((ValueError, OSError)):
            action()
        self.assertEqual(self.layout.command_link.readlink(), command)
        self.assertTrue(executable.exists())
        self.assertEqual(self.calls, [])

    def test_dev_uninstall_with_missing_policy_bypasses_launch_preflight(self):
        api = self.api()
        source = self.root / 'checkout'
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        layout = InstallLayout.development(source, self.layout.command_link.parent,
                         metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        install_development(source, layout)
        (source / POLICY_RELATIVE_PATH).unlink()
        config = self.write(layout.host_paths.config_file,
                            '[desktop]\nseccomp = "chromium"\n')
        sentinel = self.write(source / 'untracked-file')
        user_data = self.write(self.home / '.claude/token')
        worker, authority = self.recorded_runtime()
        before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob('*') if path.is_file()}
        with patch('claude_docker.seccomp.chromium_policy_path', side_effect=AssertionError('policy lookup')):
            api.uninstall_user(layout)
        self.assertTrue(all(path.exists() for path in (sentinel, user_data, config)))
        self.assertEqual(before, {str(path.relative_to(source)): path.read_bytes() for path in source.rglob('*') if path.is_file()})
        self.assertFalse(layout.command_link.is_symlink())
        self.assertFalse(worker.exists())
        self.assertFalse(authority.exists())
        self.assertEqual(sum('rm' in argv for argv in self.calls), 2)
        self.assertTrue(all(argv[:2] == ['docker', 'compose'] for argv in self.calls))

    def test_managed_bundle_missing_policy_retains_integrity_refusal(self):
        api = self.api()
        self.install()
        self.write(self.layout.host_paths.config_file, '[desktop]\nseccomp = "chromium"\n')
        policy = self.layout.release_dir('0.1.0') / POLICY_RELATIVE_PATH
        policy.unlink()
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        self.assertTrue(self.layout.registry_file.exists())
        self.assertTrue(self.layout.current_link.is_symlink())
        self.assertFalse(self.layout.removal_file.exists())

    def test_manifest_attacks_reject_before_any_service_or_file_removal(self):
        api = self.api()
        self.install()
        metadata = self.layout.manifest_file('0.1.0')
        original = metadata.read_text()
        for target in ('/outside', '/', str(self.home), '../escape', 'src/../../escape'):
            with self.subTest(target=target):
                data = json.loads(original)
                data['files'][0]['path'] = target
                metadata.write_text(json.dumps(data))
                self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        for attack in ('duplicate', 'schema', 'unknown-runtime'):
            with self.subTest(attack=attack):
                data = json.loads(original)
                if attack == 'duplicate':
                    data['files'].append(data['files'][0])
                elif attack == 'schema':
                    data['schema'] = 2
                else:
                    data['files'][0]['path'] = 'private-secret'
                metadata.write_text(json.dumps(data))
                self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        metadata.write_text(original)
        (self.layout.release_dir('0.1.0') / 'README.md').write_text('tampered')
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))

    def test_changed_command_and_symlinked_release_parent_reject_before_mutation(self):
        api = self.api()
        self.install()
        self.layout.command_link.unlink()
        self.layout.command_link.symlink_to(self.home / 'other-command')
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        self.layout.command_link.unlink()
        self.layout.command_link.symlink_to(self.layout.current_link / 'claude-docker')
        original = self.layout.release_dir('0.1.0') / 'image'
        outside = self.root / 'outside'
        original.rename(outside)
        original.symlink_to(outside, target_is_directory=True)
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        self.assertTrue((outside / 'customize.sh').exists())

    def test_registry_intent_and_every_inactive_release_validate_before_removal(self):
        api = self.api()
        self.install()
        metadata = self.layout.registry_file
        original = metadata.read_text()
        for key, value in (('command_link', str(self.home)), ('current_link', '/'),
                           ('releases', ['0.1.0', '0.1.0']), ('schema', 3)):
            with self.subTest(key=key):
                data = json.loads(original)
                data[key] = value
                metadata.write_text(json.dumps(data))
                self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        metadata.write_text(original)
        installer._intent(self.layout, '0.2.0')
        intent = json.loads(self.layout.intent_file.read_text())
        intent['release'] = str(self.home)
        self.layout.intent_file.write_text(json.dumps(intent))
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        self.layout.intent_file.unlink()
        source = self.root / 'checkout'
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        (source / 'VERSION').write_text('0.2.0\n')
        install_release(source, self.layout, version='0.2.0')
        (self.layout.release_dir('0.1.0') / 'README.md').write_text('changed inactive release')
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))

    def test_normal_uninstall_retains_all_user_data_and_removes_owned_files_only(self):
        api = self.api()
        self.install()
        paths = self.layout.host_paths
        custom = (self.root / 'custom-code', self.root / 'custom.json', self.root / 'custom-desktop')
        workspace = self.root / 'workspace'
        config = self.write(paths.config_file, '[container]\nmounts = ["' + str(workspace) + '"]\n'
                            + '\n'.join('[state.' + name + ']\nsource = "' + str(path) + '"\n'
                                        for name, path in zip(('code', 'code_json', 'desktop'), custom)))
        keep = [config, self.write(paths.state_dir / 'persistent'),
                self.write(self.home / '.claude/token'), self.write(self.home / '.claude.json'),
                self.write(self.home / '.config/Claude/settings'), self.write(workspace / 'code'),
                self.write(custom[0] / 'token'), self.write(custom[1]), self.write(custom[2] / 'settings'),
                self.write(paths.cache_dir / 'unknown-user-file'), self.write(paths.runtime_dir / 'unknown-user-file'),
                self.write(self.layout.root / 'unknown-user-file'),
                self.write(self.layout.releases_dir / '.unrecorded-stage/file')]
        paths.runtime_dir.chmod(0o700)
        for mode in ('none', 'sysbox'):
            self.write(paths.runtime_dir / f'compose.{mode}.json', '{}')
        self.write(paths.state_dir / 'active-mode', 'none\n')
        self.write(paths.state_dir / 'pending-mode', 'sysbox\n')
        report = api.uninstall_user(self.layout)
        self.assertFalse(self.layout.command_link.is_symlink())
        self.assertFalse(self.layout.current_link.is_symlink())
        self.assertFalse(self.layout.registry_file.exists())
        self.assertFalse(self.layout.release_dir('0.1.0').exists())
        self.assertFalse((paths.runtime_dir / 'compose.none.json').exists())
        self.assertFalse((paths.runtime_dir / 'compose.sysbox.json').exists())
        self.assertTrue(all(path.exists() for path in keep))
        for path in (*custom, workspace, paths.config_dir, paths.state_dir,
                     self.home / '.claude', self.home / '.claude.json', self.home / '.config/Claude'):
            self.assertIn(path.resolve(), report.retained)
        docker = [call for call in self.calls if 'rm' in call]
        self.assertEqual(len(docker), 2)
        self.assertTrue(any('compose.sysbox.yaml' in arg for call in docker for arg in call))
        for call in docker:
            self.assertEqual(call[-3:], ['rm', '--stop', '--force'])
            self.assertFalse(any('compose.none.json' in arg or 'compose.sysbox.json' in arg for arg in call))
        self.assertFalse(any('down' in call or '--volumes' in call or '-v' in call for call in self.calls))
        self.assertTrue(any('images' in warning and 'volumes' in warning and 'networks' in warning for warning in report.warnings))

    def test_protected_custom_state_inside_bundle_prevents_removal(self):
        api = self.api()
        self.install()
        target = self.layout.release_dir('0.1.0') / 'README.md'
        self.write(self.layout.host_paths.config_file, f'[state.code_json]\nsource = "{target}"\n')
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))

    def test_empty_cache_aliased_by_custom_claude_state_is_retained(self):
        api = self.api()
        self.install()
        paths = self.layout.host_paths
        paths.cache_dir.mkdir(mode=0o700, parents=True)
        self.write(paths.config_file, f'[state.code]\nsource = "{paths.cache_dir}"\nmount = false\n')
        report = api.uninstall_user(self.layout)
        self.assertTrue(paths.cache_dir.is_dir())
        self.assertIn(paths.cache_dir, report.retained)

    def test_development_tampered_metadata_cannot_claim_checkout_files(self):
        api = self.api()
        source = self.root / 'checkout'
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        layout = InstallLayout.development(source, self.layout.command_link.parent, metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        install_development(source, layout)
        # Even mutually-consistent private metadata cannot turn the checkout into
        # an owned metadata root; its source relationship must be checked too.
        for path in (layout.registry_file, layout.development_manifest_file):
            record = json.loads(path.read_text())
            record['source'] = str(layout.root)
            path.write_text(json.dumps(record))
        layout.command_link.unlink()
        layout.command_link.symlink_to(layout.root / 'claude-docker')
        with self.assertRaises(ValueError):
            api.uninstall_user(layout)
        self.assertTrue(layout.registry_file.exists())
        self.assertTrue(layout.development_manifest_file.exists())
        self.assertTrue(layout.command_link.is_symlink())
        self.assertEqual(self.calls, [])

    def test_runtime_parent_escape_and_changed_runtime_file_reject_before_removal(self):
        api = self.api()
        self.install()
        paths = self.layout.host_paths
        outside = self.root / 'outside'
        outside.mkdir()
        paths.runtime_dir.symlink_to(outside, target_is_directory=True)
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        paths.runtime_dir.unlink()
        paths.runtime_dir.mkdir(mode=0o700)
        (paths.runtime_dir / 'compose.none.json').symlink_to(self.home / '.claude.json')
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))

    def test_removal_intent_cannot_be_written_inside_configured_protected_data(self):
        api = self.api()
        self.install()
        self.write(self.layout.host_paths.config_file,
                   f'[state.code_json]\nsource = "{self.layout.removal_file}"\nmount = false\n')
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        self.assertFalse(self.layout.removal_file.exists())

    def test_removal_retries_intent_before_release_rename_without_claiming_absent_release(self):
        api = self.api()
        installer._prepare(self.layout, releases=True)
        installer._intent(self.layout, '0.1.0')
        original = Path.unlink
        def fail(path, *args, **kwargs):
            if path == self.layout.intent_file:
                raise PermissionError('interrupted intent removal')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail), self.assertRaises(PermissionError):
            api.uninstall_user(self.layout)
        self.assertTrue(self.layout.removal_file.exists())
        api.uninstall_user(self.layout)
        self.assertFalse(self.layout.root.exists())

    def test_uninstall_pending_release_without_registry_does_not_orphan_program(self):
        api = self.api()
        original = installer._write_json
        def write(path, data):
            if path == self.layout.registry_file:
                raise OSError('registry failure')
            return original(path, data)
        with patch('claude_docker.installer._write_json', write), self.assertRaises(OSError):
            self.install()
        report = api.uninstall_user(self.layout)
        self.assertFalse(self.layout.release_dir('0.1.0').exists())
        self.assertFalse(self.layout.intent_file.exists())
        self.assertIn(self.layout.intent_file, report.removed)

    def test_development_uninstall_cleans_recorded_user_runtime_and_preserves_checkout(self):
        api = self.api()
        source = self.root / 'checkout'
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        layout = InstallLayout.development(source, self.layout.command_link.parent,
                         metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        sentinel = self.write(source / 'untracked-file')
        before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob('*') if path.is_file()}
        for entry in ('library', 'cli', 'installer'):
            with self.subTest(entry=entry):
                install_development(source, layout)
                worker, authority = self.recorded_runtime()
                observations, calls = [], []
                def run(runner, argv, **kwargs):
                    calls.append(list(argv))
                    if 'stop' in argv:
                        observations.append((worker.exists(), authority.exists()))
                    return subprocess.CompletedProcess(argv, 0, '', '')
                output = io.StringIO()
                with patch('claude_docker.commands.CommandRunner.run', run), redirect_stdout(output):
                    if entry == 'library':
                        report = api.uninstall_user(layout)
                    elif entry == 'cli':
                        self.assertEqual(cli.main(['uninstall']), 0)
                    else:
                        self.assertEqual(installer.main(source, ['--uninstall', '--dev'], env=self.env), 0)
                self.assertEqual(observations, [(False, True)], 'worker must stop before Desktop and Xephyr')
                self.assertEqual(sum('rm' in call for call in calls), 2)
                self.assertFalse(any('down' in call or '--volumes' in call for call in calls))
                self.assertFalse(worker.exists())
                self.assertFalse(authority.exists())
                self.assertFalse((layout.host_paths.runtime_dir / 'xephyr.json').exists())
                self.assertFalse((layout.host_paths.state_dir / 'active-mode').exists())
                self.assertFalse((layout.host_paths.state_dir / 'pending-mode').exists())
                self.assertFalse(layout.command_link.is_symlink())
                self.assertFalse(layout.registry_file.exists())
                self.assertFalse(layout.development_manifest_file.exists())
                if entry == 'library':
                    self.assertIn(self.home / '.claude.json', report.retained)
                    self.assertIn(source.resolve(), report.retained)
                    self.assertNotIn(self.root / 'desktop-sockets/x11/X20', report.removed)
                    self.assertTrue(any('images' in warning and 'volumes' in warning for warning in report.warnings))
                else:
                    self.assertIn(str(self.home / '.claude.json'), output.getvalue())
                    self.assertIn('images', output.getvalue())
                self.assertTrue(sentinel.exists())
                self.assertEqual(before, {str(path.relative_to(source)): path.read_bytes() for path in source.rglob('*') if path.is_file()})

    def test_development_checkout_overlapping_state_runtime_or_cache_is_protected(self):
        api = self.api()
        for storage in ('state', 'runtime', 'cache'):
            with self.subTest(storage=storage):
                root = self.root / storage
                runtime = root / 'runtime'
                runtime.mkdir(mode=0o700, parents=True)
                env = dict(self.env, HOME=str(root / 'home'), XDG_DATA_HOME=str(root / 'data'),
                           XDG_STATE_HOME=str(root / 'state'), XDG_CACHE_HOME=str(root / 'cache'),
                           XDG_RUNTIME_DIR=str(runtime))
                user = InstallLayout.user(env)
                paths = user.host_paths
                source = getattr(paths, storage + '_dir')
                source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
                source.chmod(0o700)
                if storage == 'state':
                    self.write(source / 'active-mode', 'none\n')
                elif storage == 'runtime':
                    self.write(source / 'compose.none.json', '{}')
                sentinel = self.write(source / 'checkout-sentinel', 'must survive')
                layout = InstallLayout.development(source, user.command_link.parent,
                             metadata_root=user.root, host_paths=paths)
                install_development(source, layout)
                before = {str(path.relative_to(source)): path.read_bytes()
                          for path in source.rglob('*') if path.is_file()}
                self.calls.clear()
                with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                    api.uninstall_user(layout)
                self.assertTrue(layout.command_link.exists())
                self.assertTrue(layout.registry_file.exists())
                self.assertTrue(layout.development_manifest_file.exists())
                self.assertEqual(self.calls, [])
                self.assertTrue(sentinel.exists())
                self.assertEqual(before, {str(path.relative_to(source)): path.read_bytes()
                                          for path in source.rglob('*') if path.is_file()})

    def test_development_uninstall_requires_explicit_host_context_without_inferring_home(self):
        api = self.api()
        source = self.root / 'checkout'
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        layout = InstallLayout.development(source, self.layout.command_link.parent,
                         metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        install_development(source, layout)
        with patch('claude_docker.uninstall._inventory', side_effect=AssertionError('ownership read')), \
                patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'explicit host paths'):
            api.uninstall_user(replace(layout, host_paths=None))
        self.assertTrue(layout.command_link.exists())
        self.assertTrue(layout.registry_file.exists())
        self.assertEqual(self.calls, [])

    def test_configured_state_and_workspace_aliases_protect_both_mode_records(self):
        api = self.api()
        paths = self.layout.host_paths
        for name in ('active-mode', 'pending-mode'):
            for protection in ('state', 'workspace'):
                for alias in (False, True):
                    with self.subTest(name=name, protection=protection, alias=alias):
                        self.install()
                        self.calls.clear()
                        record = self.write(paths.state_dir / name, 'none\n')
                        target = record if protection == 'state' else paths.state_dir
                        if alias:
                            source = target
                            target = self.home / f'{name}-{protection}-alias'
                            target.symlink_to(source, target_is_directory=protection == 'workspace')
                        config = (f'[state.code_json]\nsource = "{target}"\nmount = false\n'
                                  if protection == 'state' else f'[container]\nmounts = ["{target}"]\n')
                        self.write(paths.config_file, config)
                        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
                        self.assertEqual(record.read_text(), 'none\n')
                        if alias:
                            target.unlink()

    def test_recorded_desktop_socket_target_is_protected_before_worker_or_docker_cleanup(self):
        api = self.api()
        self.install()
        worker, authority = self.recorded_runtime()
        target = self.root / 'desktop-sockets/x11/X20'
        self.write(self.layout.host_paths.config_file, f'[state.code_json]\nsource = "{target}"\nmount = false\n')
        self.assert_rejected_untouched(lambda: api.uninstall_user(self.layout))
        self.assertTrue(worker.exists())
        self.assertTrue(authority.exists())

    def test_system_uninstall_never_reads_user_environment_or_calls_docker(self):
        api = self.api()
        layout = InstallLayout.system(self.root / 'prefix')
        install_release(self.source, layout, version='0.1.0')
        keep = self.write(layout.root / 'unknown-file')
        class NoEnvironment(dict):
            def get(self, *args):
                raise AssertionError('system uninstall read environment')
            def __iter__(self):
                raise AssertionError('system uninstall iterated environment')
        with patch('os.environ', NoEnvironment()), patch('claude_docker.paths.resolve_host_paths', side_effect=AssertionError('HOME read')):
            report = api.uninstall_system(layout)
        self.assertFalse(layout.command_link.is_symlink())
        self.assertFalse(layout.release_dir('0.1.0').exists())
        self.assertTrue(keep.exists())
        self.assertEqual(self.calls, [])
        self.assertIn(layout.registry_file, report.removed)

    def test_system_bootstrap_uninstall_uses_injected_layout_only(self):
        self.api()
        layout = InstallLayout.system(self.root / 'prefix')
        install_release(self.source, layout, version='0.1.0')
        output = io.StringIO()
        with patch.object(InstallLayout, 'system', return_value=layout), \
                patch.object(InstallLayout, 'user', side_effect=AssertionError('user layout read')), redirect_stdout(output):
            self.assertEqual(installer.main(ROOT, ['--uninstall', '--system'], env={}), 0)
        self.assertFalse(layout.registry_file.exists())
        self.assertEqual(self.calls, [])

    def test_system_uninstall_permission_error_preserves_uninstall_in_admin_instruction(self):
        self.api()
        layout = InstallLayout.system(self.root / 'prefix')
        stderr = io.StringIO()
        with patch.object(InstallLayout, 'system', return_value=layout), \
                patch('claude_docker.uninstall.uninstall_system', side_effect=PermissionError('admin required')), \
                redirect_stderr(stderr), self.assertRaises(SystemExit):
            installer.main(ROOT, ['--uninstall', '--system'], env={})
        self.assertIn('sudo ./install --uninstall --system', stderr.getvalue())

    def test_cleanup_failure_keeps_program_ownership_and_retryable_runtime_state(self):
        api = self.api()
        self.install()
        paths = self.layout.host_paths
        self.write(paths.state_dir / 'active-mode', 'none\n')
        self.write(paths.state_dir / 'pending-mode', 'sysbox\n')
        generated = self.write(paths.runtime_dir / 'compose.none.json', '{}')
        paths.runtime_dir.chmod(0o700)
        def fail(runner, argv, **kwargs):
            if 'rm' in argv:
                raise subprocess.CalledProcessError(2, argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch('claude_docker.commands.CommandRunner.run', fail), self.assertRaises(ValueError):
            api.uninstall_user(self.layout)
        self.assertTrue(self.layout.command_link.exists())
        self.assertTrue(self.layout.registry_file.exists())
        self.assertTrue(self.layout.manifest_file('0.1.0').exists())
        self.assertTrue(generated.exists())
        self.assertEqual((paths.state_dir / 'active-mode').read_text(), 'none\n')
        self.assertEqual((paths.state_dir / 'pending-mode').read_text(), 'sysbox\n')

    def test_public_cli_runs_from_installed_bundle_after_own_files_are_removed(self):
        self.api()
        self.install()
        fakebin = self.root / 'fakebin'
        fakebin.mkdir()
        docker = self.write(fakebin / 'docker', '#!/bin/sh\nexit 0\n')
        docker.chmod(0o755)
        result = subprocess.run([str(self.layout.command_link), 'uninstall'],
                                env=dict(self.env, PATH=str(fakebin) + ':' + os.defpath),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.layout.command_link.is_symlink())
        self.assertFalse(self.layout.release_dir('0.1.0').exists())
        self.assertIn(str(self.home / '.claude'), result.stdout)
        self.assertIn('images', result.stdout)
        self.assertIn('volumes', result.stdout)

    def test_public_system_command_gives_admin_instruction_before_private_registry_read(self):
        self.api()
        stderr = io.StringIO()
        with patch.object(cli, '__file__', '/usr/local/share/claude-docker/releases/0.1.0/src/claude_docker/cli.py'), \
                patch.object(InstallLayout, 'user', side_effect=AssertionError('user layout read')), \
                patch('claude_docker.manifest._read_metadata', side_effect=PermissionError('private root registry')), \
                redirect_stderr(stderr):
            self.assertEqual(cli.main(['uninstall']), 1)
        self.assertIn('sudo /usr/local/share/claude-docker/current/install --uninstall --system', stderr.getvalue())
        self.assertEqual(self.calls, [])



if __name__ == '__main__':
    unittest.main()

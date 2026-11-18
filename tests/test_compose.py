"""Generated models and commands exercise the Docker subprocess boundary."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker import cli
from claude_docker.compose import ComposeProject
from claude_docker.model import (AppConfig, ContainerConfig, DesktopConfig, DesktopRuntime,
                                 EnvironmentConfig, ProcessIdentity, StateMountConfig)
from claude_docker.mounts import prepare_mounts
from claude_docker.paths import HostPaths

ROOT = Path(__file__).resolve().parents[1]


class RecordingRunner:
    def __init__(self, stdout='container-one\n', fail_operation=None):
        self.calls = []
        self.stdout = stdout
        self.fail_operation = fail_operation

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.fail_operation and self.fail_operation in argv:
            raise subprocess.CalledProcessError(7, argv)
        return subprocess.CompletedProcess(argv, 0, self.stdout, '')


class ComposeFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'host'
        self.workspace = self.root / 'work space "quote" : colon \\ backslash $TOKEN'
        self.workspace.mkdir()
        self.paths = HostPaths(self.home, self.root / 'config', self.root / 'data',
                               self.root / 'state', self.root / 'cache', self.root / 'runtime')
        self.config = AppConfig('none', ContainerConfig('tester', 1234, 2345,
                                Path('/home/tester'), (self.workspace,)),
                                StateMountConfig(self.root / 'code', True),
                                StateMountConfig(self.root / 'code.json', True),
                                StateMountConfig(self.root / 'desktop', True),
                                DesktopConfig(True, 'manual'), EnvironmentConfig(('SECRET',), ()))
        self.config.state_code_json.source.write_text('{}')
        self.runner = RecordingRunner()


    def seed_code_version(self, config_dir):
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / 'claude-code.version').write_text('2.1.326\n')

    def project(self, **kwargs):
        config = kwargs.pop('config', self.config)
        return ComposeProject(config, self.paths, prepare_mounts(config), self.runner,
                              program_dir=ROOT, **kwargs)


class ComposeTests(ComposeFixture, unittest.TestCase):
    def test_inner_docker_readiness_is_independent_of_runtime_mounts_and_mode_records(self):
        runtime = DesktopRuntime(25, self.root / 'x11', self.root / 'Xauthority',
                                 ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        with patch.dict(os.environ, {'DOCKER_HOST': 'ssh://selected',
                                     'DOCKER_CONFIG': '/outer/docker-config',
                                     'COMPOSE_FILE': 'untrusted.yaml', 'SECRET': 'private'},
                        clear=True):
            project = self.project(config=replace(self.config, docker_mode='sysbox'),
                                   desktop_runtime=runtime)
        project.state.write_mode('none')
        project.state.write_pending('none')
        records = [project.state.path, project.state.pending_path]
        before = [(path.read_bytes(), path.stat().st_ino) for path in records]
        self.workspace.rmdir()

        expected = ['docker', 'compose', '--project-name', f'claude-docker-{os.getuid()}',
                    '-f', str(ROOT / 'compose.yaml'), '-f', str(ROOT / 'compose.sysbox.yaml'),
                    'up', '-d', '--no-build', '--pull', 'never', '--no-recreate',
                    '--no-deps', '--wait', '--wait-timeout', '120', 'inner-docker']
        for stage in ('missing', 'stopped', 'healthy'):
            with self.subTest(stage=stage):
                # Docker selects create/start/reuse from one idempotent command.
                self.runner.calls.clear()
                project.ensure_inner_docker()
                self.assertEqual(self.runner.calls, [(expected, {
                    'capture_output': False,
                    'env': {'DOCKER_HOST': 'ssh://selected', 'DOCKER_CONFIG': '/outer/docker-config',
                            'COMPOSE_DISABLE_ENV_FILE': '1', 'CLAUDE_USER': 'tester',
                            'CLAUDE_UID': '1234', 'CLAUDE_GID': '2345',
                            'CLAUDE_HOME': '/home/tester', 'CLAUDE_HOST_UID': str(os.getuid())},
                })])
                self.assertFalse(self.paths.runtime_dir.exists())
                self.assertEqual([(path.read_bytes(), path.stat().st_ino) for path in records], before)

    def test_inner_docker_readiness_in_none_mode_does_not_run_docker_or_write_files(self):
        self.project().ensure_inner_docker()
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.paths.runtime_dir.exists())
        self.assertFalse(self.paths.state_dir.exists())

    def test_inner_docker_readiness_propagates_compose_failure_with_pull_guidance(self):
        project = self.project(config=replace(self.config, docker_mode='sysbox'))
        self.runner.fail_operation = 'inner-docker'
        stderr = io.StringIO()
        with self.assertRaises(subprocess.CalledProcessError) as error, redirect_stderr(stderr):
            project.ensure_inner_docker()
        self.assertEqual(error.exception.returncode, 7)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.runner.calls[0][0][-4:], ['--wait', '--wait-timeout', '120', 'inner-docker'])
        self.assertIn('inner-docker', stderr.getvalue())
        self.assertIn('claude-docker pull', stderr.getvalue())
        self.assertFalse(self.paths.runtime_dir.exists())
        self.assertFalse(self.paths.state_dir.exists())

    def test_failed_initial_sysbox_desktop_start_records_inner_docker_for_teardown(self):
        for old_mode, old_pending in ((None, None), ('none', None), ('sysbox', 'sysbox')):
            for failure_point in ('inner-docker', 'display'):
                with self.subTest(old_mode=old_mode, old_pending=old_pending, failure=failure_point):
                    project = self.project(config=replace(self.config, docker_mode='sysbox'))
                    project.state.clear_mode()
                    project.state.clear_pending()
                    if old_mode is not None:
                        project.state.write_mode(old_mode)
                    if old_pending is not None:
                        project.state.write_pending(old_pending)
                    self.runner.calls.clear()
                    self.runner.fail_operation = failure_point

                    def prepare_display():
                        self.assertEqual(failure_point, 'display')
                        raise ValueError('display setup failed')

                    with self.assertRaises((subprocess.CalledProcessError, ValueError)):
                        project.up('claude-desktop', before_start=prepare_display)
                    ups = [argv[-1] for argv, _ in self.runner.calls if 'up' in argv]
                    self.assertEqual(ups, ['inner-docker'])
                    self.assertEqual(project.state.read_mode(), old_mode)
                    self.assertEqual(project.state.read_pending(), 'sysbox')

                    # Explicit recovery must still select the Sysbox model even
                    # when there is no launch configuration or the old mode is none.
                    self.runner.calls.clear()
                    ComposeProject.for_teardown(self.paths, self.runner).down()
                    downs = [argv for argv, _ in self.runner.calls]
                    self.assertEqual(len(downs), 2 if old_mode == 'none' else 1)
                    self.assertEqual(
                        sum('down' in argv and str(ROOT / 'compose.sysbox.yaml') in argv
                            for argv in downs), 1)
                    self.assertIsNone(project.state.read_pending())
                    self.assertIsNone(project.state.read_mode())

    def test_build_uses_private_literal_context_override_without_runtime_mutation(self):
        project = self.project()
        context = self.root / 'selected $TOKEN, space'
        context.mkdir()
        seen = []
        original = self.runner.run
        def run(argv, **kwargs):
            override = Path(argv[argv.index('-f', argv.index('-f') + 1) + 1])
            seen.append(override)
            model = json.loads(override.read_text())
            self.assertEqual(model['services']['claude-cli']['build']['context'], str(context).replace('$', '$$'))
            self.assertEqual(model['services']['claude-cli']['build']['args']['CLAUDE_UID'], '1234')
            self.assertEqual(override.stat().st_mode & 0o777, 0o600)
            return original(argv, **kwargs)
        self.runner.run = run
        project.build('claude-cli', pull=True, no_cache=True, build_context=context)
        self.assertFalse(seen[0].exists())
        self.assertFalse(project.override_path.exists())
        self.assertFalse(self.paths.runtime_dir.exists())
        self.assertIn('--pull', self.runner.calls[-1][0])
        self.assertIn('--no-cache', self.runner.calls[-1][0])

    def test_all_builds_share_immutable_snapshot_and_clean_failure(self):
        profile = self.home / '.config/claude-docker/mise.toml'
        profile.parent.mkdir(parents=True)
        profile.write_text('[bootstrap.packages]\n"apt:ripgrep"="latest"')
        self.seed_code_version(profile.parent)
        contexts = []
        def run(argv, **kwargs):
            override = Path(argv[argv.index('-f', argv.index('-f') + 1) + 1])
            build = json.loads(override.read_text())['services'][argv[-1]]['build']
            context = Path(build['context'])
            contexts.append(context)
            self.assertEqual((context / 'image/mise-user.toml').read_text(), '[bootstrap.packages]\n"apt:ripgrep"="latest"')
            profile.write_text('[tools]\nkubectl="changed-without-lock"')
            if len(contexts) == 2:
                raise subprocess.CalledProcessError(7, argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.runner.run = run
        with patch.dict(os.environ, {'HOME': str(self.home)}, clear=True), \
                patch('claude_docker.cli.CommandRunner', return_value=self.runner), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['build', '--all']), 1)
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[0], contexts[1])
        self.assertFalse(contexts[0].exists())

    def test_missing_image_startup_has_build_guidance_and_no_pull(self):
        self.runner.fail_operation = 'up'
        stderr = io.StringIO()
        with self.assertRaises(subprocess.CalledProcessError), redirect_stderr(stderr):
            self.project().up('claude-cli')
        self.assertIn('claude-docker build', stderr.getvalue())
        argv = self.runner.calls[-1][0]
        self.assertIn('--no-build', argv)
        self.assertEqual(argv[argv.index('--pull') + 1], 'never')

    def test_launch_stop_and_help_do_not_read_user_tool_profiles(self):
        with patch('claude_docker.image_tools.read_user_tools', side_effect=AssertionError('profile read')):
            project = self.project()
            project.up('claude-cli')
            project.down()
            with self.assertRaises(SystemExit) as result, redirect_stdout(io.StringIO()):
                cli.main(['build', '--help'])
            self.assertEqual(result.exception.code, 0)

    def test_start_disables_implicit_builds(self):
        project = self.project()
        project.up('claude-cli')
        self.assertIn('--no-build', self.runner.calls[-1][0])

    def test_teardown_factory_ignores_launch_invalid_identity_and_rejects_launch_and_build(self):
        factory = getattr(ComposeProject, 'for_teardown', None)
        self.assertIsNotNone(factory)
        with patch('claude_docker.compose.os.getuid', return_value=0), \
                patch.dict(os.environ, {'CLAUDE_DOCKER_DIR': '/missing-program'}, clear=True):
            project = factory(self.paths, self.runner)
            self.assertFalse(self.paths.runtime_dir.exists())
            self.assertFalse(self.paths.state_dir.exists())
            for action in (lambda: project.up('claude-cli'),
                           lambda: project.build('claude-cli', pull=False, no_cache=False)):
                with self.assertRaisesRegex(ValueError, 'teardown'):
                    action()
            self.assertEqual(self.runner.calls, [])
            self.runner.stdout = ''  # No unrecorded dependency on the selected daemon.
            project.down()
            argv = self.runner.calls[-1][0]
            self.assertEqual(argv[argv.index('--project-name') + 1], 'claude-docker-0')
            self.assertEqual(self.runner.calls[-1][1]['env']['CLAUDE_HOST_UID'], '0')
        self.assertFalse(self.paths.runtime_dir.exists())
        self.assertFalse(self.paths.state_dir.exists())

    def test_config_free_teardown_rejects_mode_published_during_discovery(self):
        project = ComposeProject.for_teardown(self.paths, self.runner)
        original_run = self.runner.run

        def run(argv, **kwargs):
            result = original_run(argv, **kwargs)
            if argv[:3] == ['docker', 'container', 'ls']:
                project.state.write_mode('sysbox')
                result.stdout = 'a' * 64 + '\n'
            return result

        with patch.object(self.runner, 'run', side_effect=run), self.assertRaisesRegex(ValueError, 'changed'):
            project.down()

        self.assertEqual(project.state.read_mode(), 'sysbox')
        self.assertEqual([argv[:3] for argv, _ in self.runner.calls], [['docker', 'container', 'ls']])

    def test_config_free_teardown_rejects_non_id_discovery_output(self):
        self.runner.stdout = 'a' * 64 + '\nother-container\n'
        project = ComposeProject.for_teardown(self.paths, self.runner)

        with self.assertRaisesRegex(ValueError, 'Invalid inner-docker container ID'):
            project.down()

        self.assertEqual([argv[:3] for argv, _ in self.runner.calls], [['docker', 'container', 'ls']])

    def test_normal_removal_uses_both_recorded_models_without_mounts_or_volume_flags(self):
        project = ComposeProject.for_teardown(self.paths, self.runner)
        project.state.write_mode('none')
        project.state.write_pending('sysbox')
        remove = getattr(project, 'remove_containers', None)
        self.assertIsNotNone(remove, 'container-only uninstall teardown is missing')
        remove()
        self.assertEqual(len(self.runner.calls), 2)
        for argv, kwargs in self.runner.calls:
            self.assertEqual(argv[-3:], ['rm', '--stop', '--force'])
            self.assertIn('--profile', argv)
            self.assertNotIn(str(self.paths.runtime_dir / 'compose.none.json'), argv)
            self.assertNotIn(str(self.paths.runtime_dir / 'compose.sysbox.json'), argv)
            self.assertNotIn('--volumes', argv)
            self.assertNotIn('-v', argv)
            self.assertNotIn('down', argv)
        self.assertTrue(any(str(ROOT / 'compose.sysbox.yaml') in argv for argv, _ in self.runner.calls))
        self.assertIsNone(project.state.read_mode())
        self.assertIsNone(project.state.read_pending())
        self.assertFalse(self.paths.runtime_dir.exists())

    def test_opt_in_profile_is_desktop_only(self):
        config = replace(self.config, container=replace(self.config.container, uid=os.getuid()))
        runtime = DesktopRuntime(25, self.root / 'x11', self.root / 'Xauthority',
                                 ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        # A constructor carries an already-validated path without reading it.
        policy = self.root / 'missing-policy.json'
        for active, selected in ((None, None), (runtime, None), (None, policy), (runtime, policy)):
            with self.subTest(active=active is not None, selected=selected is not None):
                project = self.project(config=config, desktop_runtime=active, desktop_seccomp=selected)
                for model in (project, project._model('sysbox')):
                    services = json.loads(model.write_override().read_text())['services']
                    self.assertNotIn('security_opt', services['claude-cli'])
                    if active is not None and selected is not None:
                        self.assertEqual(services['claude-desktop']['security_opt'],
                                         ['seccomp=' + str(policy)])
                    else:
                        self.assertNotIn('security_opt', services['claude-desktop'])

    def test_override_uses_long_form_exact_path_mounts_and_service_specific_state(self):
        project = self.project()
        override = json.loads(project.write_override().read_text())
        services = override['services']
        # Compose unescapes $$ to $ during interpolation; source spellings survive.
        workspace = {'type': 'bind', 'source': str(self.workspace).replace('$', '$$'),
                     'target': str(self.workspace).replace('$', '$$'), 'read_only': False,
                     'bind': {'create_host_path': False}}
        code = {'type': 'bind', 'source': str(self.root / 'code'),
                'target': '/home/tester/.claude', 'read_only': False,
                'bind': {'create_host_path': False}}
        code_json = {'type': 'bind', 'source': str(self.root / 'code.json'),
                     'target': '/home/tester/.claude.json', 'read_only': False,
                     'bind': {'create_host_path': False}}
        desktop = {'type': 'bind', 'source': str(self.root / 'desktop'),
                   'target': '/home/tester/.config/Claude', 'read_only': False,
                   'bind': {'create_host_path': False}}
        self.assertEqual(services['claude-cli']['volumes'], [workspace, code, code_json])
        self.assertEqual(services['claude-desktop']['volumes'], [workspace, code, desktop, code_json])
        args = {'CLAUDE_USER': 'tester', 'CLAUDE_UID': '1234', 'CLAUDE_GID': '2345',
                'CLAUDE_HOME': '/home/tester', 'CLAUDE_HOST_UID': str(os.getuid())}
        labels = {'io.github.diborodin.claude-in-docker-linux.managed': 'true',
                  'io.github.diborodin.claude-in-docker-linux.owner-uid': str(os.getuid())}
        for service in services.values():
            self.assertEqual(service['build']['args'], args)
            self.assertEqual(service['labels'], labels)
            self.assertEqual(service['environment']['HOME'], '/home/tester')

    def test_private_override_contains_no_forwarded_secret_values(self):
        self.seed_code_version(self.paths.config_dir)
        with patch.dict(os.environ, {'SECRET': 'never-persist-this-value', 'UNRELATED': 'also-private'}):
            project = self.project()
            output = project.write_override()
            project.build('claude-cli', pull=False, no_cache=False)
        self.assertEqual(output.parent, self.paths.runtime_dir)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        self.assertNotIn('never-persist-this-value', output.read_text())
        self.assertNotIn('SECRET', self.runner.calls[0][1]['env'])
        self.assertNotIn('UNRELATED', self.runner.calls[0][1]['env'])
        self.assertEqual(self.runner.calls[0][1]['env']['CLAUDE_UID'], '1234')

    def test_missing_or_wrong_type_sources_fail_before_override_or_docker(self):
        project = self.project()
        self.config.state_code_json.source.unlink()
        with self.assertRaisesRegex(ValueError, 'source'):
            project.up('claude-cli')
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.paths.runtime_dir.exists())

    def test_runtime_symlink_is_refused_without_touching_target(self):
        other = self.root / 'other'
        other.mkdir(mode=0o755)
        self.paths.runtime_dir.symlink_to(other)
        with self.assertRaises((ValueError, OSError)):
            self.project().write_override()
        self.assertEqual(list(other.iterdir()), [])
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o755)

    def test_disabled_and_missing_optional_json_mounts_are_omitted(self):
        self.config.state_code_json.source.unlink()
        config = replace(self.config, state_desktop=replace(self.config.state_desktop, mount=False))
        override = json.loads(self.project(config=config).write_override().read_text())
        for service in override['services'].values():
            self.assertEqual(len(service['volumes']), 2)
        self.assertFalse(config.state_desktop.source.exists())

    def test_file_selection_has_uid_project_and_none_never_loads_sysbox(self):
        for mode, filenames in [('none', ['compose.yaml', 'compose.none.json']),
                                ('sysbox', ['compose.yaml', 'compose.sysbox.yaml', 'compose.sysbox.json'])]:
            with self.subTest(mode=mode):
                project = self.project(config=replace(self.config, docker_mode=mode))
                command = project.command('ps', '-q', 'claude-cli')
                self.assertEqual(command[:4], ['docker', 'compose', '--project-name',
                                              'claude-docker-' + str(os.getuid())])
                files = [Path(command[i + 1]).name for i, arg in enumerate(command) if arg == '-f']
                self.assertEqual(files, filenames)
                self.assertNotIn('--profile', command)
                self.assertEqual(command[-3:], ['ps', '-q', 'claude-cli'])

    def test_desktop_profile_is_requested_only_for_desktop_operations(self):
        project = self.project()
        self.assertNotIn('--profile', project.command('ps', '-q', 'claude-cli'))
        project.up('claude-desktop')
        up = self.runner.calls[-1][0]
        self.assertEqual(up[up.index('--profile') + 1], 'desktop')
        project.container_id('claude-desktop')
        self.assertIn('--profile', self.runner.calls[-1][0])

    def test_operations_reuse_file_list_and_build_flags(self):
        self.seed_code_version(self.paths.config_dir)
        project = self.project(config=replace(self.config, docker_mode='sysbox'))
        project.up('claude-cli')
        self.assertEqual(project.container_id('claude-cli'), 'container-one')
        project.build('claude-cli', pull=True, no_cache=True)
        project.pull()
        project.down()
        commands = [call[0] for call in self.runner.calls]
        for command in commands:
            runtime_mounts = any(operation in command for operation in ('up', 'ps'))
            prefix = project.command(runtime_mounts=runtime_mounts)
            without_profile = command[:]
            if '--profile' in without_profile:
                index = without_profile.index('--profile')
                del without_profile[index:index + 2]
            self.assertEqual(without_profile[:len(prefix)], prefix)
        self.assertEqual(commands[0][-6:], ['up', '-d', '--no-build', '--pull', 'never', 'claude-cli'])
        self.assertEqual(commands[1][-3:], ['ps', '-q', 'claude-cli'])
        self.assertEqual(commands[2][-4:], ['build', '--pull', '--no-cache', 'claude-cli'])
        self.assertEqual(commands[3][-2:], ['pull', 'inner-docker'])
        self.assertIn('down', commands[4])
        self.assertNotIn('--volumes', commands[4])

    def test_container_lookup_rejects_zero_or_multiple_ids(self):
        for stdout in ['', '\n', 'one\ntwo\n']:
            with self.subTest(stdout=stdout):
                self.runner.stdout = stdout
                with self.assertRaisesRegex(ValueError, 'exactly one.*claude-cli'):
                    self.project().container_id('claude-cli')

    def test_stop_removes_generated_files_but_preserves_state_sources(self):
        project = self.project()
        project.up('claude-cli')
        stale = self.paths.runtime_dir / 'compose.sysbox.json'
        stale.write_text('{}')
        unrelated = self.paths.runtime_dir / 'other.json'
        unrelated.write_text('keep')
        project.down()
        self.assertNotIn('--volumes', self.runner.calls[-1][0])
        self.assertIn('--profile', self.runner.calls[-1][0])
        self.assertFalse(stale.exists())
        self.assertFalse((self.paths.runtime_dir / 'compose.none.json').exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.config.state_code.source.is_dir())
        self.assertTrue(self.config.state_desktop.source.is_dir())
        self.assertTrue(self.config.state_code_json.source.is_file())

    def test_down_does_not_validate_disappeared_or_replaced_mount_sources(self):
        project = self.project(config=replace(self.config, docker_mode='sysbox'))
        project.up('claude-cli')
        self.workspace.rmdir()
        self.workspace.touch()
        self.config.state_code.source.rmdir()
        self.config.state_code.source.touch()
        self.config.state_code_json.source.unlink()
        try:
            project.down()
        except ValueError as error:
            self.fail(f'Teardown must not validate current mounts: {error}')
        self.assertEqual(self.runner.calls[-1][0][-1], 'down')
        self.assertIn(str(ROOT / 'compose.sysbox.yaml'), self.runner.calls[-1][0])
        self.assertFalse(project.state.path.exists())
        self.assertTrue(self.workspace.is_file())
        self.assertTrue(self.config.state_code.source.is_file())
        self.assertFalse(self.config.state_code_json.source.exists())

    def test_down_refuses_runtime_symlink_without_removing_external_files(self):
        project = self.project()
        other = self.root / 'other-runtime'
        other.mkdir()
        external = other / 'compose.none.json'
        external.write_text('keep')
        self.paths.runtime_dir.symlink_to(other)
        with self.assertRaises((ValueError, OSError)):
            project.down()
        self.assertEqual(external.read_text(), 'keep')
        self.assertEqual(self.runner.calls[-1][0][-1], 'down')

    def test_nonruntime_cli_commands_ignore_unavailable_sources_and_do_not_create_state(self):
        self.seed_code_version(self.root / 'profile/claude-docker')
        runtime_base = self.root / 'run-base'
        runtime_base.mkdir(mode=0o700)
        config_file = self.root / 'settings.toml'
        workspace = self.root / 'vanished-workspace'
        invalid_state = self.root / 'state-is-a-file'
        invalid_state.touch()
        for kind in ('missing', 'file'):
            if kind == 'file':
                workspace.touch()
            config_file.write_text('[container]\nmounts = [' + json.dumps(str(workspace)) + ']\n'
                                   '[state.code]\nsource = ' + json.dumps(str(invalid_state)) + '\n')
            for operation in ('stop', 'build', 'rebuild', 'pull'):
                with self.subTest(kind=kind, operation=operation):
                    self.runner.calls.clear()
                    env = {'HOME': str(self.home), 'CLAUDE_CONFIG_FILE': str(config_file),
                           'XDG_RUNTIME_DIR': str(runtime_base),
                           'XDG_CONFIG_HOME': str(self.paths.config_dir.parent / 'profile')}
                    stderr = io.StringIO()
                    with patch.dict(os.environ, env, clear=True), \
                            patch('claude_docker.cli.CommandRunner', return_value=self.runner), \
                            redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                        rc = cli.main([operation])
                    self.assertEqual(rc, 0, stderr.getvalue())
                    self.assertFalse(self.home.exists())
                    self.assertEqual(list(runtime_base.iterdir()), [])
                    self.assertTrue(invalid_state.is_file())
                    for argv, _ in self.runner.calls:
                        files = [Path(argv[i + 1]).name for i, arg in enumerate(argv) if arg == '-f']
                        self.assertEqual(files, ['compose.yaml', 'build.json'] if operation in ('build', 'rebuild') else ['compose.yaml'])
                        self.assertNotIn('--volumes', argv)

    def test_cli_build_rebuild_pull_and_stop_from_unrelated_directory(self):
        self.seed_code_version(self.root / '.config/claude-docker')
        binary = self.root / 'bin'
        binary.mkdir()
        log = self.root / 'commands.jsonl'
        docker = binary / 'docker'
        docker.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                          'with open(os.environ["HOME"] + "/commands.jsonl", "a") as f:\n'
                          '    f.write(json.dumps(sys.argv[1:]) + "\\n")\n')
        docker.chmod(0o755)
        config_file = self.root / 'settings.toml'
        config_file.write_text('[container]\nmounts = []\n')
        env = {'PATH': str(binary), 'HOME': str(self.root), 'CLAUDE_CONFIG_FILE': str(config_file),
               'XDG_STATE_HOME': str(self.root / 'xdg-state'),
               'XDG_RUNTIME_DIR': str(self.root / 'run-base'), 'PYTHONDONTWRITEBYTECODE': '1'}
        Path(env['XDG_RUNTIME_DIR']).mkdir(mode=0o700)
        for operation in ('build', 'rebuild', 'pull', 'stop'):
            result = subprocess.run([sys.executable, str(ROOT / 'claude-docker'), operation],
                                    cwd=self.root, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if operation == 'pull':
                self.assertIn('no external images', result.stdout.lower())
        commands = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum('build' in cmd for cmd in commands), 2)
        self.assertTrue(all(cmd[-1] == 'claude-cli' and '--profile' not in cmd
                            for cmd in commands if 'build' in cmd))
        rebuilds = [cmd for cmd in commands if '--no-cache' in cmd]
        self.assertEqual(len(rebuilds), 1)
        self.assertTrue(all('--pull' in cmd for cmd in rebuilds))
        self.assertEqual(commands[-1][-1], 'down')
        self.assertTrue(all(str(ROOT / 'compose.yaml') in cmd for cmd in commands))

    def test_cli_build_targets_require_explicit_desktop_or_all_selection(self):
        self.seed_code_version(self.home / '.config/claude-docker')
        for operation in ('build', 'rebuild'):
            for options, expected in (([], ['claude-cli']),
                                      (['--desktop'], ['claude-desktop']),
                                      (['--all'], ['claude-cli', 'claude-desktop'])):
                with self.subTest(operation=operation, options=options):
                    self.runner.calls.clear()
                    with patch.dict(os.environ, {'HOME': str(self.home)}, clear=True), \
                            patch('claude_docker.cli.CommandRunner', return_value=self.runner):
                        try:
                            rc = cli.main([operation, *options])
                        except SystemExit as error:
                            self.fail(f'Build target options must be accepted: {error}')
                    self.assertEqual(rc, 0)
                    commands = [argv for argv, _ in self.runner.calls]
                    self.assertEqual([argv[-1] for argv in commands], expected)
                    for argv in commands:
                        self.assertEqual('--profile' in argv, argv[-1] == 'claude-desktop')
                        self.assertEqual('--pull' in argv, operation == 'rebuild')
                        self.assertEqual('--no-cache' in argv, operation == 'rebuild')
                        self.assertNotIn('up', argv)

    def test_cli_build_targets_are_mutually_exclusive(self):
        with patch('claude_docker.cli.CommandRunner', return_value=self.runner), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main(['build', '--desktop', '--all'])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.runner.calls, [])

    def test_cli_invalid_workspace_fails_before_docker(self):
        config_file = self.root / 'invalid.toml'
        config_file.write_text('[container]\nmounts = ["/definitely-missing-workspace"]\n')
        env = {'PATH': '', 'HOME': str(self.root), 'CLAUDE_CONFIG_FILE': str(config_file)}
        result = subprocess.run([sys.executable, str(ROOT / 'claude-docker'), 'claude'],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Workspace root', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.root / '.claude').exists())

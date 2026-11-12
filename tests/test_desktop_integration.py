"""Explicitly opted-in, isolated smoke test of the real Desktop runtime."""

import base64
from dataclasses import replace
import json
import os
import pwd
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from textwrap import dedent
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from claude_docker import cli, clipboard, desktop
from claude_docker.config import AccountInfo, load_config
from claude_docker.commands import CommandRunner
from claude_docker.compose import ComposeProject
from claude_docker.model import (AppConfig, ContainerConfig, DesktopConfig,
                                 EnvironmentConfig, StateMountConfig)
from claude_docker.mounts import prepare_mounts
from claude_docker.paths import resolve_host_paths


class DesktopIntegrationTests(unittest.TestCase):
    def setUp(self):
        if (self._testMethodName == 'test_chromium_seccomp_desktop'
                and os.environ.get('CLAUDE_DOCKER_CHROMIUM_SECCOMP_TEST') != '1'):
            self.skipTest('set both Desktop and chromium seccomp live flags to exactly 1')
        # Do not even probe the host before this exact opt-in.
        if os.environ.get('CLAUDE_DOCKER_DESKTOP_TEST') != '1':
            self.skipTest('set CLAUDE_DOCKER_DESKTOP_TEST=1 on a prepared graphical Docker host')
        if not os.environ.get('DISPLAY'):
            self.skipTest('Desktop smoke requires a graphical DISPLAY')
        tools = ('docker', 'Xephyr', 'xauth', 'setxkbmap', 'xclip', 'xdpyinfo')
        if self._testMethodName == 'test_chromium_seccomp_desktop':
            tools += ('xwininfo',)
        missing = [name for name in tools
                   if shutil.which(name) is None]
        if missing:
            self.skipTest('Desktop smoke requires host commands: ' + ', '.join(missing))
        try:
            result = subprocess.run(['docker', 'info', '--format', '{{.OSType}}'],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.skipTest(f'Docker is not reachable: {error}')
        if result.returncode:
            self.skipTest('Docker is not reachable: ' + result.stderr.strip())
        self.assertEqual(result.stdout.strip(), 'linux', 'Desktop smoke requires Linux containers')
        for command, reason in ((['docker', 'compose', 'version'], 'Docker Compose is unavailable'),
                                (['xdpyinfo'], 'Host DISPLAY is not reachable')):
            try:
                probe = subprocess.run(command, capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired) as error:
                self.skipTest(f'{reason}: {error}')
            if probe.returncode:
                self.skipTest(reason + ': ' + probe.stderr.strip())

    def test_chromium_seccomp_desktop(self):
        account = pwd.getpwuid(os.getuid())
        project_name = 'claude-seccomp-smoke-' + str(os.getuid()) + '-' + uuid.uuid4().hex[:12]
        build = dict(project=project_name, image=project_name+':test', user=account.pw_name,
                     uid=os.getuid(), gid=os.getgid(), home=account.pw_dir)
        artifacts = Path(os.environ.get('CLAUDE_DOCKER_TEST_ARTIFACTS') or
                         tempfile.mkdtemp(prefix='claude-desktop-artifacts-')).expanduser()
        artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
        report = {'build': build, 'commands': [], 'artifacts': str(artifacts.resolve())}
        # Keep the real detached child's Popen alive until production stop.
        # This observes ownership only; start/stop and policy code stay real.
        managers = []
        manager_class = desktop.XephyrManager

        def observed_manager(*args, **kwargs):
            manager = manager_class(*args, **kwargs)
            managers.append(manager)
            return manager

        def command(*argv, environment=None, timeout=30, errors="strict"):
            return subprocess.run(argv, capture_output=True, text=True, errors=errors, timeout=timeout,
                                  check=True, env=environment).stdout.strip()

        class ObservedRunner(CommandRunner):
            def run(self, argv, **kwargs):
                report['commands'].append(list(argv))
                return super().run(argv, **kwargs)

        def project_resources(kind):
            args = ['docker', 'ps', '-aq'] if kind == 'containers' else ['docker', 'network', 'ls', '-q']
            return command(*args, '--filter', 'label=com.docker.compose.project=' + project_name)

        def emergency_project_cleanup():
            for identifier in project_resources('containers').split():
                command('docker', 'rm', '--force', identifier)
            for identifier in project_resources('networks').split():
                command('docker', 'network', 'rm', identifier)

        snapshot_script = dedent(r'''
        import json
        from pathlib import Path
        items=[]
        for entry in Path('/proc').iterdir():
         if not entry.name.isdigit(): continue
         try:
          cmd=(entry/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
          if (entry/'comm').read_text().strip() != 'claude-desktop': continue
          item={'pid':int(entry.name),'command':cmd}
          for name in ['status','uid_map','gid_map','attr/current']:
           try:
            value=(entry/name).read_text()
            if name=='status': value='\n'.join(line for line in value.splitlines() if line.startswith(('Name:','PPid:','Uid:','Gid:','NSpid:','Cap','Seccomp','NoNewPrivs')))
            item[name]=value
           except OSError as error: item[name]=str(error)
          items.append(item)
         except OSError: pass
        print(json.dumps(items))
        ''')

        original_cwd = Path.cwd()
        temporary_path = None
        cleanup_errors = []
        try:
            with tempfile.TemporaryDirectory(prefix='claude-xephyr-launcher-', dir='/tmp') as temporary:
                temporary_path = Path(temporary)
                root = temporary_path
                for name in ['config', 'data', 'state', 'cache', 'runtime', 'x11', 'workspace']:
                    (root / name).mkdir(mode=0o700)
                (root / 'workspace/marker').write_text('isolated-workspace\n')
                (root / 'code.json').write_text('{}\n')
                environment = {k:v for k,v in os.environ.items() if k in (
                    'PATH','DISPLAY','XAUTHORITY','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG',
                    'DOCKER_TLS_VERIFY','DOCKER_CERT_PATH','SSH_AUTH_SOCK','SSL_CERT_FILE','SSL_CERT_DIR')}
                if 'XAUTHORITY' not in environment and (Path(account.pw_dir)/'.Xauthority').is_file():
                    environment['XAUTHORITY'] = str(Path(account.pw_dir)/'.Xauthority')
                environment.update(HOME=account.pw_dir, CLAUDE_DOCKER_DIR=str(ROOT),
                    XDG_CONFIG_HOME=str(root/'config'), XDG_DATA_HOME=str(root/'data'),
                    XDG_STATE_HOME=str(root/'state'), XDG_CACHE_HOME=str(root/'cache'),
                    XDG_RUNTIME_DIR=str(root/'runtime'), PYTHONDONTWRITEBYTECODE='1')
                config_file = root / 'config.toml'
                config_file.write_text('\n'.join([
                    'docker_mode = "none"', '[container]',
                    'mounts = ['+json.dumps(str(root/'workspace'))+']',
                    '[state.code]', 'source = '+json.dumps(str(root/'code')), 'mount = true',
                    '[state.code_json]', 'source = '+json.dumps(str(root/'code.json')), 'mount = true',
                    '[state.desktop]', 'source = '+json.dumps(str(root/'desktop')), 'mount = true',
                    '[desktop]', 'sandbox = true', 'clipboard = "manual"', 'seccomp = "chromium"',
                    '[environment]', 'required = []', 'optional = []', '']))
                environment['CLAUDE_CONFIG_FILE'] = str(config_file)
                override = root / 'diagnostic.json'
                override.write_text(json.dumps({'services':{
                    'claude-cli':{'image':project_name+'-unused-cli:test'},
                    'claude-desktop':{'image':build['image'],
                        'labels':{'claude-docker.diagnostic':project_name}}}}))

                class TestProject(ComposeProject):
                    def __init__(self, *args, **kwargs):
                        kwargs['program_dir'] = ROOT
                        super().__init__(*args, **kwargs)
                        self.project_name = project_name
                        self.files.insert(-1, override)

                    def _model(self, mode):
                        return TestProject(replace(self.config, docker_mode=mode), self.paths,
                            self.mounts, self.runner, desktop_runtime=self.desktop_runtime)

                with (patch.dict(os.environ, environment, clear=True),
                      patch.object(cli, 'ComposeProject', TestProject),
                      patch.object(desktop, 'ComposeProject', TestProject),
                      patch.object(desktop, 'XephyrManager', observed_manager),
                      patch.object(cli, 'CommandRunner', ObservedRunner),
                      patch.object(desktop, 'CommandRunner', ObservedRunner),
                      patch.object(desktop, 'desktop_socket_dir', return_value=root/'x11'),
                      patch.object(desktop, '_desktop_socket_path', return_value=root/'x11')):
                    paths = resolve_host_paths(environment, os.getuid())
                    config = load_config(env=environment, paths=paths,
                        account=AccountInfo(account.pw_name, os.getuid(), os.getgid(), Path(account.pw_dir)))
                    assert config.container.home == Path(account.pw_dir)
                    assert config.container.user == account.pw_name
                    project = TestProject(config, paths, prepare_mounts(config), ObservedRunner())
                    runtime = None
                    try:
                        args = ['docker', 'build', '--target', 'desktop', '--tag', build['image']]
                        for name, value in project.scalars.items():
                            args.extend(['--build-arg', f'{name}={value}'])
                        report['daemon'] = json.loads(command('docker', 'info', '--format', '{{json .}}'))
                        report['build_output'] = self.command(*args, '.', env=project.environment, timeout=7200)
                        os.chdir(root/'workspace')
                        report['launch_result'] = cli.main(['desktop'])
                        assert report['launch_result'] == 0, 'Real launcher failed'
                        runtime = desktop.XephyrManager(paths).load()
                        assert runtime is not None
                        assert stat.S_IMODE(runtime.xauthority.stat().st_mode) == 0o600
                        nested = dict(environment, DISPLAY=f':{runtime.display}', XAUTHORITY=str(runtime.xauthority))
                        command('xdpyinfo', environment=nested)
                        empty = root/'no-cookie'
                        empty.touch(mode=0o600)
                        rejected = subprocess.run(['xdpyinfo'], env=dict(nested,XAUTHORITY=str(empty)),
                            capture_output=True,text=True,timeout=10)
                        assert rejected.returncode != 0, 'Nested X accepted missing cookie'
                        report['x11_authentication_verified'] = True
                        project.desktop_runtime = runtime
                        container = project.container_id('claude-desktop')
                        details = json.loads(command('docker','inspect',container))[0]
                        host = details['HostConfig']
                        assert details['Config']['Image'] == build['image']
                        assert details['Config']['User'] == account.pw_name
                        assert not host['Privileged'] and not host.get('CapAdd')
                        assert host.get('Runtime') != 'sysbox-runc'
                        assert not any('unconfined' in option for option in host.get('SecurityOpt') or [])
                        assert len(command('docker','ps','-q','--filter','label=com.docker.compose.project='+project_name).split()) == 1
                        report['container_details'] = details
                        expected_mounts = {
                            (str(root/'workspace'), str(root/'workspace'), True),
                            (str(root/'code'), account.pw_dir+'/.claude', True),
                            (str(root/'code.json'), account.pw_dir+'/.claude.json', True),
                            (str(root/'desktop'), account.pw_dir+'/.config/Claude', True),
                            (str(root/'x11'), '/tmp/.X11-unix', False),
                            (str(runtime.xauthority), '/run/claude-docker/Xauthority', False)}
                        assert {(m['Source'], m['Destination'], m['RW']) for m in details['Mounts']} == expected_mounts
                        assert all(m['Type'] == 'bind' for m in details['Mounts'])
                        policies = [o for o in host['SecurityOpt'] if o.startswith('seccomp=')]
                        assert len(policies) == 1
                        assert json.loads(policies[0].split('=', 1)[1]) == json.loads(
                            (ROOT/'security/desktop-seccomp-v0.2.4-linux-amd64.json').read_text())
                        report['mounted_sources'] = [m['Source'] for m in details['Mounts']]
                        report['identity'] = command('docker','exec',container,'sh','-ec',
                            'id -u; id -g; printf "%s\\n" "$HOME"')
                        assert report['identity'].splitlines() == [str(os.getuid()),str(os.getgid()),account.pw_dir]
                        assert command('docker','exec','--workdir',str(root/'workspace'),container,'cat','marker') == 'isolated-workspace'
                        def snapshot():
                            result = subprocess.run(['docker','exec','-i',container,'python3','-'],
                                input=snapshot_script,capture_output=True,text=True,check=True,timeout=15)
                            return json.loads(result.stdout)
                        time.sleep(3)
                        first = snapshot()
                        deadline = time.monotonic()+20
                        while time.monotonic()<deadline:
                            time.sleep(max(0,min(0.5,deadline-time.monotonic())))
                        last = snapshot()
                        report['snapshots'] = [first,last]
                        main = lambda ps: {p['pid'] for p in ps if '--type=' not in p['command']}
                        assert main(first) and main(first) == main(last), 'Main process did not remain stable'
                        renderers = [p for p in last if '--type=renderer' in p['command']]
                        assert renderers, 'No renderer remained alive'
                        first_gpu = {p['pid'] for p in first if '--type=gpu-process' in p['command']}
                        last_gpu = {p['pid'] for p in last if '--type=gpu-process' in p['command']}
                        report['gpu_pids'] = [sorted(first_gpu),sorted(last_gpu)]
                        assert first_gpu and first_gpu == last_gpu, 'GPU process did not remain stable'
                        for proc in renderers:
                            status = dict(line.split(':',1) for line in proc['status'].splitlines())
                            assert status['CapEff'].strip() == '0000000000000000'
                            assert status['CapPrm'].strip() == '0000000000000000'
                            assert status['NoNewPrivs'].strip() == '1' and status['Seccomp'].strip() == '2'
                            assert int(status['Seccomp_filters']) == 2 and len(status['NSpid'].split()) >= 3
                            assert proc['uid_map'].split() == [str(os.getuid()),str(os.getuid()),'1']
                            assert proc['attr/current'].strip() == 'docker-default (enforce)'
                        assert all('--no-sandbox' not in p['command'] and '--disable-setuid-sandbox' not in p['command'] for p in first + last)
                        report['window_tree'] = command('xwininfo','-root','-tree',environment=nested)
                        assert 'Claude' in report['window_tree'], 'No Claude window in nested display'
                        assert not (paths.runtime_dir/'clipboard.json').exists()
                        report['manual_clipboard_verified'] = True
                        report['observed_seconds'] = 20
                        report['startup_logs'] = command('docker', 'logs', container)
                        # The app uses detached exec, so container logs may be empty.
                        # Capture profile log files too, before sending any stop signal.
                        report['profile_logs'] = command('docker', 'exec', container, 'sh', '-c',
                            r'find "$HOME/.config/Claude" -type f -name "*.log" -exec tail -c 16000 {} \;', errors='replace')
                        report['stop_result'] = cli.main(['stop'])
                        assert report['stop_result'] == 0
                        assert not project_resources('containers') and not project_resources('networks')
                        assert desktop.process_identity(runtime.xephyr_identity.pid) is None
                        for filename in ['Xauthority','xephyr.json','clipboard.json','compose.none.json']:
                            assert not (paths.runtime_dir/filename).exists(),filename
                        assert not list((root/'x11').iterdir())
                        report['production_stop_verified_before_fallback'] = True
                    finally:
                        os.chdir(original_cwd)
                        for label, cleanup in [
                            ('desktop',lambda:desktop.stop_desktop_runtime(paths,project=project)),
                            ('compose',project.down), ('exact project fallback',emergency_project_cleanup)]:
                            try: cleanup()
                            except Exception as error: cleanup_errors.append(label+': '+str(error))
                        for manager in managers:
                            if manager._process is not None:
                                manager._process.wait(timeout=10)
                        if runtime is not None:
                            report['xephyr_gone'] = desktop.process_identity(runtime.xephyr_identity.pid) is None
            report['temporary_tree_removed'] = not temporary_path.exists()
            assert not cleanup_errors,cleanup_errors
            report['status'] = 'PASS'
        except BaseException as error:
            report.update(status='FAIL', error=repr(error))
            raise
        finally:
            report['cleanup_errors'] = cleanup_errors
            report['final_project_containers'] = project_resources('containers')
            report['final_project_networks'] = project_resources('networks')
            if temporary_path is not None:
                report['temporary_tree_removed'] = not temporary_path.exists()
            self.remove_image(build['image'])
            (artifacts/'desktop-seccomp-result.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({key:report.get(key) for key in ['status','artifacts','identity','launch_result',
                'stop_result','observed_seconds','xephyr_gone','temporary_tree_removed','cleanup_errors']},indent=2),flush=True)


    def command(self, *argv, env=None, timeout=120):
        result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True,
                                text=True, timeout=timeout)
        self.assertEqual(result.returncode, 0,
                         f'Command failed: {argv!r}\n{result.stdout[-8000:]}\n{result.stderr[-8000:]}')
        return result.stdout.strip()

    def test_authenticated_sandboxed_desktop(self):
        token = f'{os.getuid()}-{os.getpid()}-{uuid.uuid4().hex[:10]}'
        project_name = 'claude-desktop-smoke-' + token
        image = project_name + ':test'
        # Never forward credentials or ambient Compose settings. Xauthority is
        # used only for the parent Xephyr connection, never mounted in Docker.
        environment = {key: value for key, value in os.environ.items()
                       if key in ('PATH', 'DISPLAY', 'XAUTHORITY', 'DOCKER_HOST', 'DOCKER_CONTEXT',
                                  'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH',
                                  'SSH_AUTH_SOCK', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
        if 'XAUTHORITY' not in environment:
            authority = Path.home() / '.Xauthority'
            if authority.is_file():
                environment['XAUTHORITY'] = str(authority)
        # TemporaryDirectory is on /tmp, like the real X socket: hard links
        # cannot cross filesystems. No production socket directory is touched.
        with tempfile.TemporaryDirectory(prefix='claude-desktop-smoke-', dir='/tmp') as temporary:
            root = Path(temporary)
            for name in ('home', 'config', 'data', 'state', 'cache', 'runtime', 'x11'):
                (root / name).mkdir(mode=0o700)
            environment.update(HOME=str(root / 'home'), XDG_CONFIG_HOME=str(root / 'config'),
                               XDG_DATA_HOME=str(root / 'data'), XDG_STATE_HOME=str(root / 'state'),
                               XDG_CACHE_HOME=str(root / 'cache'), XDG_RUNTIME_DIR=str(root / 'runtime'),
                               CLAUDE_DOCKER_DIR=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
            paths = resolve_host_paths(environment, os.getuid())
            config = AppConfig('none', ContainerConfig('tester', os.getuid(), os.getgid(),
                                Path('/home/tester'), ()),
                               StateMountConfig(root / 'code', True),
                               StateMountConfig(root / 'code.json', True),
                               StateMountConfig(root / 'desktop', True),
                               DesktopConfig(True, 'manual'), EnvironmentConfig((), ()))
            config.state_code_json.source.write_text('{}\n')
            image_override = root / 'test-image.json'
            image_override.write_text(json.dumps({'services': {
                service: {'image': image if service == 'claude-desktop' else project_name + '-cli:test'}
                for service in ('claude-cli', 'claude-desktop')}}))

            class TestProject(ComposeProject):
                """Change naming only; retain the production model and lifecycle."""
                def __init__(self, *args, **kwargs):
                    kwargs['program_dir'] = ROOT
                    super().__init__(*args, **kwargs)
                    self.project_name = project_name
                    self.files.insert(-1, image_override)

                def _model(self, mode):
                    return TestProject(replace(self.config, docker_mode=mode), self.paths,
                                       self.mounts, self.runner, desktop_runtime=self.desktop_runtime)

            with (patch.dict(os.environ, environment, clear=True),
                  patch.object(desktop, 'ComposeProject', TestProject),
                  patch.object(desktop, 'desktop_socket_dir', return_value=root / 'x11'),
                  patch.object(desktop, '_desktop_socket_path', return_value=root / 'x11')):
                manager = desktop.XephyrManager(paths)
                project = TestProject(config, paths, prepare_mounts(config), CommandRunner())
                runtime = None
                try:
                    args = ['docker', 'build', '--target', 'desktop', '--tag', image]
                    for name, value in project.scalars.items():
                        args.extend(['--build-arg', f'{name}={value}'])
                    self.command(*args, '.', env=project.environment, timeout=3600)
                    runtime = manager.start()
                    project.desktop_runtime = runtime
                    self.assertEqual(stat.S_IMODE(runtime.xauthority.stat().st_mode), 0o600)
                    nested = dict(environment, DISPLAY=f':{runtime.display}',
                                  XAUTHORITY=str(runtime.xauthority))
                    self.command('xdpyinfo', env=nested)
                    empty_authority = root / 'no-cookie'
                    empty_authority.touch(mode=0o600)
                    denied = subprocess.run(['xdpyinfo'], env=dict(nested, XAUTHORITY=str(empty_authority)),
                                            capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(denied.returncode, 0, 'Xephyr accepted a client without its cookie')
                    self.check_nested_clipboard(nested, paths, root)
                    self.assertEqual(desktop.launch_desktop(config, restart=False, clipboard_sync=False,
                                                           no_sandbox=False, app_args=()), 0,
                                     'Sandboxed Desktop launch failed; unsafe fallback is forbidden')
                    container = project.container_id('claude-desktop')
                    # Validate the actual process after the launcher's initial window.
                    time.sleep(1)
                    processes = self.command('docker', 'exec', container, 'pgrep', '-a', '-x', 'claude-desktop')
                    self.assertTrue(processes)
                    self.assertNotIn('--no-sandbox', processes)
                    self.assertFalse((paths.runtime_dir / 'clipboard.json').exists(),
                                     'Manual clipboard mode unexpectedly started a worker')
                    model = json.loads(self.command(*project.command('config', '--format', 'json'),
                                                    env=project.environment))
                    self.assertEqual(model['services']['claude-desktop']['image'], image)
                    self.assertNotIn('inner-docker', model['services'])
                    details = json.loads(self.command('docker', 'inspect', container))[0]
                    host = details['HostConfig']
                    self.assertFalse(host['Privileged'])
                    self.assertFalse(host.get('CapAdd'))
                    self.assertFalse(any('unconfined' in option for option in host.get('SecurityOpt') or []))
                    self.assertNotEqual(host.get('Runtime'), 'sysbox-runc')
                    self.assertEqual(details['Config']['User'], 'tester')
                    self.assertEqual(self.command('docker', 'exec', container, 'id', '-u'), str(os.getuid()))
                finally:
                    # Independent cleanup continues even when an earlier step fails.
                    errors = []
                    for name, action in (
                            ('Desktop runtime', lambda: desktop.stop_desktop_runtime(paths, project=project)),
                            ('Compose resources', project.down),
                            ('test image', lambda: self.remove_image(image))):
                        try:
                            action()
                        except Exception as error:
                            errors.append(f'{name}: {error}')
                    if manager._process is not None:
                        manager._process.wait(timeout=10)
                    self.assertEqual(errors, [], 'Smoke cleanup failed')
                    self.assertFalse((paths.runtime_dir / 'Xauthority').exists())
                    self.assertFalse((paths.runtime_dir / 'xephyr.json').exists())
                    self.assertFalse((paths.runtime_dir / 'clipboard.json').exists())
                    self.assertEqual(list((root / 'x11').iterdir()), [])
                    self.assertFalse(list(paths.runtime_dir.glob('.clipboard-*')))
                    if runtime is not None:
                        self.assertIsNone(desktop.process_identity(runtime.xephyr_identity.pid))
                    self.assertEqual(self.command('docker', 'ps', '-aq', '--filter',
                                                  f'label=com.docker.compose.project={project_name}'), '')
                    self.assertEqual(self.command('docker', 'network', 'ls', '-q', '--filter',
                                                  f'label=com.docker.compose.project={project_name}'), '')
        self.assertFalse(root.exists(), 'Temporary smoke state was retained')

    def check_nested_clipboard(self, nested, paths, root):
        # Only generated fixtures on our nested display; never read or write the
        # host selection. Foreground xclip owners are exact, unreaped children.
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC'
                               'AAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
        for target, value in (('UTF8_STRING', 'smoke λ привет'.encode()),
                              ('UTF8_STRING', b''), ('image/png', png)):
            fixture = root / 'clipboard-fixture'
            fixture.write_bytes(value)
            owner = subprocess.Popen(['xclip', '-quiet', '-selection', 'clipboard',
                                      '-in', '-target', target, str(fixture)], env=nested,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                with clipboard._directory(paths) as directory:
                    while True:
                        try:
                            selection = clipboard._read_selection(nested, directory)
                        except ValueError:
                            selection = None
                        if selection == (target, value):
                            break
                        if owner.poll() is not None or time.monotonic() >= deadline:
                            self.fail('Nested clipboard fixture did not round-trip exactly')
                        time.sleep(0.05)
            finally:
                if owner.poll() is None:
                    owner.terminate()
                try:
                    owner.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    owner.kill()
                    owner.wait(timeout=10)
                fixture.unlink(missing_ok=True)

    def remove_image(self, image):
        # A failed build may never have created its tag. Do not touch any other image.
        found = self.command('docker', 'image', 'ls', '-q', '--filter', f'reference={image}')
        if found:
            self.command('docker', 'image', 'rm', image)
        self.assertEqual(self.command('docker', 'image', 'ls', '-q', '--filter',
                                      f'reference={image}'), '')


class DesktopGateTests(unittest.TestCase):
    def test_chromium_requires_both_exact_flags_before_host_probes(self):
        for baseline, chromium in (('', ''), ('1', ''), ('', '1'), ('true', '1'), ('1', 'true')):
            with self.subTest(baseline=baseline, chromium=chromium):
                environment = {'CLAUDE_DOCKER_DESKTOP_TEST': baseline,
                               'CLAUDE_DOCKER_CHROMIUM_SECCOMP_TEST': chromium,
                               'DISPLAY': ':0'}
                case = DesktopIntegrationTests('test_chromium_seccomp_desktop')
                result = unittest.TestResult()
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(shutil, 'which', side_effect=AssertionError('host probe')):
                    case.run(result)
                self.assertEqual(len(result.skipped), 1)
                self.assertEqual(result.errors + result.failures, [])


if __name__ == '__main__':
    unittest.main()

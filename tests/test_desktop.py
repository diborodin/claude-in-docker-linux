"""Authenticated display ownership and Desktop-only grants."""

import json
from dataclasses import replace
import os
from pathlib import Path
import signal
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker import desktop
from claude_docker import paths as host_paths
from claude_docker.model import DesktopRuntime, ProcessIdentity
from tests.test_compose import ComposeFixture


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(mode=0o700)
        from claude_docker.paths import HostPaths
        self.paths = HostPaths(self.root, self.root, self.root, self.root, self.root, self.runtime)
        self.x11 = self.root / 'X11'
        self.x11.mkdir()
        self.exposure = self.root / 'private' / 'x11'
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(desktop, '_X11_DIR', self.x11).start()
        patch.object(desktop, '_PROC_ROOT', self.proc).start()
        def prepare_socket_directory(uid):
            self.exposure.parent.mkdir(mode=0o700, exist_ok=True)
            self.exposure.mkdir(mode=0o700, exist_ok=True)
            self.exposure.parent.chmod(0o700)
            self.exposure.chmod(0o700)
            return self.exposure
        patch.object(desktop, 'desktop_socket_dir', side_effect=prepare_socket_directory).start()
        patch.object(desktop.shutil, 'which', return_value='/usr/bin/Xephyr').start()
        patch.object(desktop.XephyrManager, '_peer_pid', return_value=123).start()
        self.signals = []
        patch.object(desktop.os, 'kill', side_effect=lambda pid, sig: self.signals.append((pid, sig))).start()
        self.manager = desktop.XephyrManager(self.paths, screen='1024x768', resize=True,
                                             timeout=0.02, poll_interval=0.001)

    def identity(self, pid=123, ticks=456, exe='/usr/bin/Xephyr'):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        # /proc field 22 is index 19 after the closing command parenthesis.
        (directory / 'stat').write_text(f'{pid} (Xephyr with ) parentheses) S ' +
                                      '0 ' * 18 + f'{ticks} 0\n')
        (directory / 'exe').unlink(missing_ok=True)
        (directory / 'exe').symlink_to(exe)
        (directory / 'fd').mkdir(exist_ok=True)
        (directory / 'fd' / '3').unlink(missing_ok=True)
        (directory / 'fd' / '3').symlink_to('socket:[789]')
        (directory / 'net').mkdir(exist_ok=True)
        (directory / 'net' / 'unix').write_text('header\n0: 0 0 0 0001 01 789 ' + str(self.x11 / 'X20') + '\n')
        return ProcessIdentity(pid, ticks, exe)

    def bind_socket(self, path):
        connection = socket.socket(socket.AF_UNIX)
        connection.bind(str(path))
        self.addCleanup(connection.close)
        return connection

    def fake_xephyr(self, argv, **kwargs):
        self.argv = argv
        self.popen_kwargs = kwargs
        self.identity()
        self.bind_socket(self.x11 / argv[1].replace(':', 'X'))
        class Process:
            pid = 123
            def poll(self):
                return None
        return Process()

    def xauth(self, argv, **kwargs):
        self.xauth_argv = argv
        self.xauth_input = kwargs['input']
        # The real xauth boundary consumes stdin, writing only the private file.
        (Path(kwargs['cwd']) / argv[2]).write_bytes(b'authority')
        return subprocess.CompletedProcess(argv, 0, '', '')

    def start(self):
        with patch.object(desktop.subprocess, 'run', side_effect=self.xauth), \
                patch.object(desktop.subprocess, 'Popen', side_effect=self.fake_xephyr):
            return self.manager.start()

    def test_selects_first_display_free_of_filesystem_and_abstract_sockets(self):
        self.bind_socket(self.x11 / 'X20')
        abstract = socket.socket(socket.AF_UNIX)
        abstract.bind('\0' + str(self.x11 / 'X21'))
        self.addCleanup(abstract.close)
        self.assertEqual(self.manager.select_display(), 22)
        self.assertTrue((self.x11 / 'X20').exists())

    def test_stale_project_link_is_not_unlinked_or_selected(self):
        self.exposure.mkdir(parents=True)
        stale = self.exposure / 'X20'
        stale.write_text('foreign')
        self.assertEqual(self.manager.select_display(), 21)
        self.assertEqual(stale.read_text(), 'foreign')

    def test_all_displays_occupied_fails_without_removing_them(self):
        for number in range(20, 100):
            (self.x11 / f'X{number}').touch()
        with self.assertRaisesRegex(ValueError, 'free.*display'):
            self.manager.select_display()
        self.assertEqual(len(list(self.x11.iterdir())), 80)

    def test_start_authenticates_without_tcp_or_access_control_bypass(self):
        runtime = self.start()
        self.assertEqual(runtime.display, 20)
        self.assertEqual(runtime.xephyr_identity, ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        authority_fd = self.popen_kwargs['pass_fds'][0]
        self.assertEqual(self.argv, ['Xephyr', ':20', '-auth', f'/proc/self/fd/{authority_fd}/Xauthority',
                                    '-nolisten', 'tcp', '-screen', '1024x768', '-resizeable'])
        self.assertTrue(self.popen_kwargs['start_new_session'])
        self.assertNotIn('-ac', self.argv)
        self.assertEqual(stat.S_IMODE(runtime.xauthority.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(runtime.socket_dir.stat().st_mode), 0o700)
        self.assertEqual((runtime.socket_dir / 'X20').stat().st_ino,
                         (self.x11 / 'X20').stat().st_ino)
        record = self.xauth_input.split()
        self.assertEqual(record[0], 'ffff')
        self.assertEqual(record[-2], '0010')
        self.assertEqual(len(bytes.fromhex(record[-1])), 16)
        self.assertNotIn(record[-1], ' '.join(self.xauth_argv + self.argv))
        saved = json.loads((self.runtime / 'xephyr.json').read_text())
        self.assertEqual(saved['xephyr_identity']['start_ticks'], 456)
        self.assertNotIn(record[-1], json.dumps(saved))

    def test_load_reuses_only_authenticated_matching_runtime(self):
        self.assertIsNone(self.manager.load())
        runtime = self.start()
        self.assertEqual(self.manager.load(), runtime)
        self.assertEqual(self.signals, [])

    def test_load_rejects_changed_process_cookie_or_socket_without_cleanup(self):
        runtime = self.start()
        for change in ('identity', 'authority', 'socket', 'display'):
            with self.subTest(change=change):
                saved = self.manager.record_path.read_text()
                value = json.loads(saved)
                if change == 'identity':
                    value['xephyr_identity']['start_ticks'] = 999
                elif change == 'authority':
                    value['xauthority'] = str(self.root / 'other-cookie')
                elif change == 'socket':
                    value['socket_inode'] += 1
                else:
                    value['display'] = '../foreign'
                self.manager.record_path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'runtime'):
                    self.manager.load()
                self.manager.record_path.write_text(saved)
                self.assertTrue(runtime.xauthority.exists())
                self.assertEqual(self.signals, [])

    def test_load_refuses_record_and_authority_symlinks(self):
        runtime = self.start()
        for path in (self.manager.record_path, runtime.xauthority):
            with self.subTest(path=path):
                original = path.with_suffix('.original')
                path.rename(original)
                path.symlink_to(original)
                with self.assertRaises((ValueError, OSError)):
                    self.manager.load()
                path.unlink()
                original.rename(path)
                self.assertEqual(self.signals, [])

    def test_load_refuses_regular_file_substitutions_without_cleanup(self):
        runtime = self.start()
        for path in (self.manager.record_path, runtime.xauthority):
            with self.subTest(path=path):
                original = path.with_suffix('.original')
                path.rename(original)
                content = original.read_bytes()
                path.write_bytes(content)
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    self.manager.load()
                self.assertEqual(path.read_bytes(), content)
                self.assertTrue(original.exists())
                self.assertEqual(self.signals, [])
                path.unlink()
                original.rename(path)

    def test_cookie_uses_cryptographic_random_source(self):
        with patch.object(desktop.secrets, 'token_hex', return_value='ab' * 16) as random:
            self.start()
        random.assert_any_call(16)
        self.assertIn('ab' * 16, self.xauth_input)

    def test_stop_refuses_pid_reuse_or_wrong_executable(self):
        for ticks, exe in ((999, '/usr/bin/Xephyr'), (456, '/usr/bin/other')):
            identity = self.identity(ticks=ticks, exe=exe)
            runtime = DesktopRuntime(20, self.exposure, self.runtime / 'Xauthority',
                                     ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
            self.exposure.mkdir(parents=True, exist_ok=True)
            link = self.exposure / 'X20'
            link.write_text('foreign')
            runtime.xauthority.write_text('keep')
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.manager.stop(runtime)
            self.assertEqual(self.signals, [])
            self.assertEqual(link.read_text(), 'foreign')
            link.unlink()
            self.assertTrue(runtime.xauthority.exists())

    def test_stop_matching_process_cleans_only_owned_link_and_private_files(self):
        runtime = self.start()
        foreign = self.exposure / 'X21'
        foreign.write_text('keep')
        def terminate(pid, sig):
            self.signals.append((pid, sig))
            (self.proc / str(pid) / 'stat').unlink()
        with patch.object(desktop.os, 'kill', side_effect=terminate):
            self.manager.stop(runtime)
            self.manager.stop(runtime)
        self.assertEqual(self.signals, [(123, signal.SIGTERM)])
        self.assertFalse((self.exposure / 'X20').exists())
        self.assertFalse(runtime.xauthority.exists())
        self.assertFalse((self.runtime / 'xephyr.json').exists())
        self.assertTrue((self.x11 / 'X20').exists())
        self.assertEqual(foreign.read_text(), 'keep')

    @unittest.skipUnless(shutil.which('xauth'), 'xauth is not installed')
    def test_authority_is_a_real_familywild_xauth_database(self):
        authority = self.runtime / 'Xauthority'
        descriptor = self.manager._open_private(self.runtime)
        try:
            self.manager._authority(27, descriptor)
        finally:
            os.close(descriptor)
        result = subprocess.run(['xauth', '-f', str(authority), 'nlist'],
                                text=True, capture_output=True, check=True)
        fields = result.stdout.split()
        self.assertEqual(fields[:4], ['ffff', '0000', '0002', '3237'])
        self.assertEqual(bytes.fromhex(fields[5]), b'MIT-MAGIC-COOKIE-1')
        self.assertEqual(len(bytes.fromhex(fields[7])), 16)
        self.assertEqual(stat.S_IMODE(authority.stat().st_mode), 0o600)

    def test_stop_refuses_substituted_private_runtime_directory(self):
        runtime = self.start()
        elsewhere = self.root / 'elsewhere'
        self.runtime.rename(elsewhere)
        self.runtime.symlink_to(elsewhere, target_is_directory=True)
        def terminate(pid, sig):
            (self.proc / str(pid) / 'stat').unlink(missing_ok=True)
        with patch.object(desktop.os, 'kill', side_effect=terminate):
            with self.assertRaises((ValueError, OSError)):
                self.manager.stop(runtime)
        self.assertTrue((elsewhere / 'Xauthority').exists())
        self.assertTrue((elsewhere / 'xephyr.json').exists())

    def test_stop_cleans_recorded_link_after_xephyr_removes_source_socket(self):
        runtime = self.start()
        (self.x11 / 'X20').unlink()
        (self.proc / '123' / 'stat').unlink()
        self.manager.stop(runtime)
        self.assertFalse((runtime.socket_dir / 'X20').exists())
        self.assertFalse(runtime.xauthority.exists())

    def test_stop_escalates_only_while_identity_still_matches(self):
        runtime = self.start()
        def signal_process(pid, sig):
            self.signals.append((pid, sig))
            if sig == signal.SIGKILL:
                (self.proc / str(pid) / 'stat').unlink()
        with patch.object(desktop.os, 'kill', side_effect=signal_process):
            self.manager.stop(runtime)
        self.assertEqual(self.signals, [(123, signal.SIGTERM), (123, signal.SIGKILL)])

    def test_stop_preserves_replaced_socket_link(self):
        runtime = self.start()
        link = runtime.socket_dir / 'X20'
        link.unlink()
        link.write_text('foreign')
        def terminate(pid, sig):
            (self.proc / str(pid) / 'stat').unlink()
        with patch.object(desktop.os, 'kill', side_effect=terminate):
            self.manager.stop(runtime)
        self.assertEqual(link.read_text(), 'foreign')

    def test_start_refuses_socket_not_held_by_recorded_process(self):
        def foreign_socket(argv, **kwargs):
            process = self.fake_xephyr(argv, **kwargs)
            (self.proc / '123' / 'fd' / '3').unlink()
            return process
        def terminate(pid, sig):
            (self.proc / str(pid) / 'stat').unlink(missing_ok=True)
        with patch.object(desktop.subprocess, 'run', side_effect=self.xauth), \
                patch.object(desktop.subprocess, 'Popen', side_effect=foreign_socket), \
                patch.object(desktop.os, 'kill', side_effect=terminate):
            with self.assertRaisesRegex(ValueError, 'socket'):
                self.manager.start()
        self.assertEqual(list(self.exposure.iterdir()), [])
        self.assertTrue((self.x11 / 'X20').exists())

    def test_xauth_errors_do_not_disclose_cookie(self):
        def failure(argv, **kwargs):
            raise subprocess.CalledProcessError(1, argv, output=kwargs['input'], stderr=kwargs['input'])
        with patch.object(desktop.secrets, 'token_hex', return_value='cd' * 16), \
                patch.object(desktop.subprocess, 'run', side_effect=failure):
            with self.assertRaises(ValueError) as caught:
                self.manager.start()
        self.assertNotIn('cd' * 16, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertFalse((self.runtime / 'Xauthority').exists())

    def test_start_records_identity_before_waiting_for_socket(self):
        owned = self.manager._socket_owned
        def check_record(path, identity):
            self.assertTrue((self.runtime / 'xephyr.json').exists(), 'identity must be durable before polling')
            saved = json.loads((self.runtime / 'xephyr.json').read_text())
            self.assertEqual(saved['xephyr_identity']['pid'], 123)
            self.assertEqual(saved['xephyr_identity']['start_ticks'], 456)
            return owned(path, identity)
        def terminate(pid, sig):
            (self.proc / str(pid) / 'stat').unlink(missing_ok=True)
        with patch.object(self.manager, '_socket_owned', side_effect=check_record), \
                patch.object(desktop.os, 'kill', side_effect=terminate):
            self.start()

    def test_stop_does_not_signal_when_metadata_identity_is_inconsistent(self):
        runtime = self.start()
        path = self.runtime / 'xephyr.json'
        saved = json.loads(path.read_text())
        saved['xephyr_identity']['start_ticks'] = 999
        path.write_text(json.dumps(saved))
        def terminate(pid, sig):
            self.signals.append((pid, sig))
            (self.proc / str(pid) / 'stat').unlink(missing_ok=True)
        with patch.object(desktop.os, 'kill', side_effect=terminate):
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.manager.stop(runtime)
        self.assertEqual(self.signals, [])
        self.assertTrue(runtime.xauthority.exists())

    def test_socket_directory_creation_refuses_substituted_parent(self):
        base = self.root / f'claude-docker-{os.getuid()}'
        original = self.root / 'original-base'
        foreign = self.root / 'foreign'
        foreign.mkdir(mode=0o700)
        private = host_paths._private_runtime
        def substitute(path, uid):
            result = private(path, uid)
            if path == base:
                base.rename(original)
                base.symlink_to(foreign, target_is_directory=True)
            return result
        with patch.object(host_paths, 'Path', return_value=self.root), \
                patch.object(host_paths, '_private_runtime', side_effect=substitute):
            with self.assertRaises((ValueError, OSError)):
                host_paths.desktop_socket_dir(os.getuid())
        self.assertEqual(list(foreign.iterdir()), [])

    def test_start_does_not_write_through_substituted_authority_directory(self):
        foreign = self.root / 'foreign'
        foreign.mkdir(mode=0o700)
        (foreign / 'keep').write_text('untouched')
        original = self.root / 'original-runtime'
        authority = self.manager._authority
        def substitute(*args, **kwargs):
            self.runtime.rename(original)
            self.runtime.symlink_to(foreign, target_is_directory=True)
            return authority(*args, **kwargs)
        def terminate(pid, sig):
            (self.proc / str(pid) / 'stat').unlink(missing_ok=True)
        with patch.object(self.manager, '_authority', side_effect=substitute), \
                patch.object(desktop.os, 'kill', side_effect=terminate):
            with self.assertRaises((ValueError, OSError)):
                self.start()
        self.assertEqual({item.name for item in foreign.iterdir()}, {'keep'})
        self.assertEqual(list(original.iterdir()), [])

    def test_start_metadata_and_rollback_stay_in_original_runtime(self):
        foreign = self.root / 'foreign'
        foreign.mkdir(mode=0o700)
        (foreign / 'Xauthority').write_text('foreign authority')
        (foreign / 'xephyr.json').write_text('foreign metadata')
        original = self.root / 'original-runtime'
        def substitute(argv, **kwargs):
            process = self.fake_xephyr(argv, **kwargs)
            self.runtime.rename(original)
            self.runtime.symlink_to(foreign, target_is_directory=True)
            return process
        def terminate(pid, sig):
            (self.proc / str(pid) / 'stat').unlink(missing_ok=True)
        with patch.object(desktop.subprocess, 'run', side_effect=self.xauth), \
                patch.object(desktop.subprocess, 'Popen', side_effect=substitute), \
                patch.object(desktop.os, 'kill', side_effect=terminate):
            with self.assertRaises((ValueError, OSError)):
                self.manager.start()
        self.assertEqual((foreign / 'Xauthority').read_text(), 'foreign authority')
        self.assertEqual((foreign / 'xephyr.json').read_text(), 'foreign metadata')
        self.assertEqual(list(original.iterdir()), [])
        self.assertEqual(list(self.exposure.iterdir()), [])

    def test_start_link_and_rollback_stay_in_original_exposure_directory(self):
        foreign = self.root / 'foreign'
        foreign.mkdir(mode=0o700)
        (foreign / 'keep').write_text('untouched')
        original = self.root / 'original-exposure'
        link = os.link
        def substitute(*args, **kwargs):
            self.exposure.rename(original)
            self.exposure.symlink_to(foreign, target_is_directory=True)
            return link(*args, **kwargs)
        def terminate(pid, sig):
            (self.proc / str(pid) / 'stat').unlink(missing_ok=True)
        with patch.object(desktop.os, 'link', side_effect=substitute), \
                patch.object(desktop.os, 'kill', side_effect=terminate):
            with self.assertRaises((ValueError, OSError)):
                self.start()
        self.assertEqual({item.name for item in foreign.iterdir()}, {'keep'})
        self.assertEqual(list(original.iterdir()), [])
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_start_timeout_is_bounded_and_cleans_private_authority(self):
        class Process:
            pid = 123
            def poll(self):
                return None
        self.identity()
        def terminate(pid, sig):
            self.signals.append((pid, sig))
            (self.proc / str(pid) / 'stat').unlink(missing_ok=True)
        with patch.object(desktop.subprocess, 'run', side_effect=self.xauth), \
                patch.object(desktop.subprocess, 'Popen', return_value=Process()), \
                patch.object(desktop.os, 'kill', side_effect=terminate):
            with self.assertRaisesRegex(ValueError, 'socket'):
                self.manager.start()
        self.assertFalse((self.runtime / 'Xauthority').exists())
        self.assertEqual(self.signals, [(123, signal.SIGTERM)])


class EndpointOwnershipTests(unittest.TestCase):
    def listener(self, path):
        code = ('import socket,sys\n'
                'server=socket.socket(socket.AF_UNIX)\n'
                'server.bind(sys.argv[1]); server.listen(16)\n'
                'print("ready",flush=True)\n'
                'sys.stdin.buffer.read(1)\n')
        process = subprocess.Popen([sys.executable, '-c', code, str(path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        def cleanup():
            process.communicate(b'x', timeout=5)
        self.addCleanup(cleanup)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'listener readiness timed out')
        self.assertEqual(process.stdout.readline(), b'ready\n')
        return process

    def test_unlinked_owned_socket_does_not_certify_foreign_rebound_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            endpoint = root / 'X20'
            original = self.listener(endpoint)
            from claude_docker.paths import HostPaths
            paths = HostPaths(root, root, root, root, root, root)
            manager = desktop.XephyrManager(paths)
            identity = desktop.process_identity(original.pid)
            self.assertIsNotNone(identity)
            self.assertTrue(manager._socket_owned(endpoint, identity))
            endpoint.unlink()
            self.listener(endpoint)
            replacement = endpoint.lstat()
            self.assertFalse(manager._socket_owned(endpoint, identity))
            self.assertEqual(endpoint.lstat().st_ino, replacement.st_ino)


class DesktopComposeTests(ComposeFixture, unittest.TestCase):
    def test_different_uid_cannot_receive_grants_but_can_build_and_teardown(self):
        config = replace(self.config, container=replace(self.config.container, uid=os.getuid() + 1))
        runtime = DesktopRuntime(23, self.root / 'x11', self.root / 'Xauthority',
                                 ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        project = self.project(config=config, desktop_runtime=runtime)
        with self.assertRaisesRegex(ValueError, 'container.uid.*host UID'):
            project.write_override()
        self.assertFalse(self.paths.runtime_dir.exists())
        self.assertEqual(self.runner.calls, [])
        project.state.write_mode('none')
        project.state.write_pending('sysbox')
        project.build('claude-desktop', pull=False, no_cache=False)
        project.stop_service('claude-desktop')
        project.down()
        self.assertEqual([argv[-1] for argv, _ in self.runner.calls],
                         ['claude-desktop', 'claude-desktop', 'down', 'down'])
        self.assertFalse(self.paths.runtime_dir.exists())
        self.assertIsNone(project.state.read_mode())
        self.assertIsNone(project.state.read_pending())

    def test_desktop_grants_are_isolated_to_dedicated_runtime(self):
        self.config = replace(self.config, container=replace(self.config.container, uid=os.getuid()))
        dedicated = self.root / 'x11'
        dedicated.mkdir()
        authority = self.root / 'Xauthority'
        authority.touch(mode=0o600)
        runtime = DesktopRuntime(23, dedicated, authority, ProcessIdentity(123, 456, '/usr/bin/Xephyr'))
        for selected in (None, runtime):
            with self.subTest(runtime=selected):
                project = self.project(desktop_runtime=selected)
                services = json.loads(project.write_override().read_text())['services']
                for name, service in services.items():
                    targets = {item['target']: item for item in service['volumes']}
                    if name == 'claude-desktop' and selected:
                        self.assertEqual(service['environment']['DISPLAY'], ':23')
                        self.assertEqual(service['environment']['XAUTHORITY'], '/run/claude-docker/Xauthority')
                        for source, target in ((dedicated, '/tmp/.X11-unix'),
                                               (authority, '/run/claude-docker/Xauthority')):
                            self.assertEqual(targets[target]['source'], str(source))
                            self.assertTrue(targets[target]['read_only'])
                    else:
                        self.assertNotIn('DISPLAY', service['environment'])
                        self.assertNotIn('XAUTHORITY', service['environment'])
                        self.assertNotIn('/tmp/.X11-unix', targets)
                    self.assertNotIn('/tmp/.X11-unix', [item['source'] for item in service['volumes']])
                    self.assertNotIn(str(Path.home() / '.Xauthority'),
                                     [item['source'] for item in service['volumes']])

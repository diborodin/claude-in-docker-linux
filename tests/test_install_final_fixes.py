"""Public lifecycle regressions; all processes and external boundaries are temporary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_install_lifecycle as lifecycle

ROOT = lifecycle.ROOT

# Fault/pause hooks live in this subprocess harness, never the product API.
HARNESS = r'''
import runpy, sys
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode = True
bootstrap, prefix, hook, target, *arguments = sys.argv[1:]
sys.path.insert(0, str(Path(bootstrap).parent / 'src'))
from claude_docker import installer, uninstall
from contextlib import ExitStack
with ExitStack() as stack:
    if prefix != '-':
        layout = installer.InstallLayout.system(Path(prefix))
        stack.enter_context(patch.object(installer.InstallLayout, 'system', return_value=layout))
        stack.enter_context(patch.object(installer.InstallLayout, 'user', side_effect=AssertionError('user paths')))
    if hook == 'fail':
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if str(path) == target:
                raise PermissionError('injected deletion interruption')
            return original(path, *args, **kwargs)
        stack.enter_context(patch.object(Path, 'unlink', unlink))
    if hook == 'pause':
        module = installer if target == '_registry' else uninstall
        original = getattr(module, target)
        def pause(*args, **kwargs):
            value = original(*args, **kwargs)
            print('PAUSED', flush=True)
            sys.stdin.readline()
            return value
        stack.enter_context(patch.object(module, target, pause))
    if arguments[:1] == ['runtime']:
        bootstrap = str(Path(bootstrap).parent / 'claude-docker')
        arguments = arguments[1:]
    sys.argv = [bootstrap, *arguments]
    runpy.run_path(bootstrap, run_name='__main__')
'''


class FinalInstallFixTests(unittest.TestCase):
    def setUp(self):
        lifecycle.InstallLifecycleTests.setUp(self)
        docker = self.bin_dir / 'docker'
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(0o755)

    run_command = lifecycle.InstallLifecycleTests.run_command
    write = lifecycle.InstallLifecycleTests.write
    snapshot = lifecycle.InstallLifecycleTests.snapshot

    def invoke(self, *arguments, hook='none', target='-', prefix='-', popen=False):
        command = [sys.executable, '-c', HARNESS, str(self.source / 'install'), str(prefix), hook, str(target), *arguments]
        if popen:
            process = subprocess.Popen(command, env=self.env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def cleanup():
                if process.poll() is None:
                    process.kill()
                process.communicate()
            self.addCleanup(cleanup)
            return process
        return subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=20)

    def test_never_used_user_and_dev_public_uninstall_need_no_docker(self):
        (self.bin_dir / 'docker').unlink()
        python = self.bin_dir / 'python3'
        python.symlink_to(sys.executable)
        self.env['PATH'] = str(self.bin_dir)
        for mode in ((), ('--dev',)):
            with self.subTest(mode=mode):
                self.run_command(self.source / 'install', *mode)
                self.run_command(self.command, 'uninstall')
                self.assertFalse(self.program.exists())
                self.assertFalse(self.command.is_symlink())

    def test_install_writers_refuse_busy_before_registry_read_across_modes(self):
        for mode in ('user', 'dev', 'system'):
            with self.subTest(mode=mode):
                prefix = self.root / 'prefix' if mode == 'system' else '-'
                arguments = ('--' + mode,) if mode != 'user' else ()
                paused = self.invoke(*arguments, hook='pause', target='_registry', prefix=prefix, popen=True)
                self.assertEqual(paused.stdout.readline().strip(), 'PAUSED')
                second = self.invoke(*arguments, prefix=prefix)
                self.assertNotEqual(second.returncode, 0, second.stdout)
                self.assertIn('busy', second.stderr.lower())
                paused.stdin.write('\n'); paused.stdin.flush()
                stdout, stderr = paused.communicate(timeout=20)
                self.assertEqual(paused.returncode, 0, stderr)
                result = self.invoke('--uninstall', *arguments, prefix=prefix)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstall_holds_lock_until_root_is_deleted_and_can_be_recreated(self):
        self.run_command(self.source / 'install')
        paused = self.invoke('--uninstall', hook='pause', target='_inventory', popen=True)
        self.assertEqual(paused.stdout.readline().strip(), 'PAUSED')
        (self.source / 'VERSION').write_text('0.2.0\n')
        second = self.invoke()
        self.assertNotEqual(second.returncode, 0, second.stdout)
        self.assertIn('busy', second.stderr.lower())
        paused.stdin.write('\n'); paused.stdin.flush()
        stdout, stderr = paused.communicate(timeout=20)
        self.assertEqual(paused.returncode, 0, stderr)
        self.assertFalse(self.program.exists())
        self.run_command(self.source / 'install')
        self.run_command(self.source / 'install', '--uninstall')
        self.assertFalse(self.program.exists())

    def test_lock_remains_busy_after_uninstall_has_deleted_the_root(self):
        self.run_command(self.source / 'install')
        paused = self.invoke('--uninstall', hook='pause', target='_finish_removal', popen=True)
        # Removal reports are printed only after the lock is released.
        self.assertEqual(paused.stdout.readline().strip(), 'PAUSED')
        self.assertFalse(self.program.exists())
        second = self.invoke()
        self.assertNotEqual(second.returncode, 0, second.stdout)
        self.assertIn('busy', second.stderr.lower())
        self.assertFalse(self.program.exists())
        paused.stdin.write('\n'); paused.stdin.flush()
        stdout, stderr = paused.communicate(timeout=20)
        self.assertEqual(paused.returncode, 0, stderr)
        self.run_command(self.source / 'install')
        self.run_command(self.source / 'install', '--uninstall')

    def test_purge_holds_writer_lock_during_consent(self):
        self.run_command(self.source / 'install')
        paused = self.invoke('runtime', 'uninstall', '--purge', '--yes', hook='pause',
                             target='_confirm_purge', popen=True)
        while True:
            line = paused.stdout.readline()
            self.assertTrue(line, 'purge exited before consent boundary')
            if line.strip() == 'PAUSED':
                break
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('busy', result.stderr.lower())
        paused.stdin.write('\n'); paused.stdin.flush()
        stdout, stderr = paused.communicate(timeout=20)
        self.assertEqual(paused.returncode, 0, stderr)
        self.assertFalse(self.program.exists())

    def test_source_bootstrap_resumes_before_first_and_mid_removal_all_modes(self):
        for mode in ('user', 'dev', 'system'):
            for phase in ('first', 'middle', 'metadata'):
                with self.subTest(mode=mode, phase=phase):
                    prefix = self.root / 'prefix' if mode == 'system' else '-'
                    program = self.root / 'prefix/share/claude-docker' if mode == 'system' else self.program
                    command = self.root / 'prefix/bin/claude-docker' if mode == 'system' else self.command
                    arguments = ('--' + mode,) if mode != 'user' else ()
                    result = self.invoke(*arguments, prefix=prefix)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if mode == 'dev':
                        target = command if phase == 'first' else program / ('development.json' if phase == 'middle' else 'installation.json')
                    else:
                        target = program / 'releases/0.1.0' / ('.dockerignore' if phase == 'first' else 'src/claude_docker/installer.py' if phase == 'middle' else 'manifest.json')
                    failed = self.invoke('--uninstall', *arguments, prefix=prefix, hook='fail', target=target)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn('injected deletion interruption', failed.stderr)
                    self.assertTrue((program / 'removal-intent.json').exists())
                    # Reinstall must not reinterpret a partially deleted bundle.
                    blocked = self.invoke(*arguments, prefix=prefix)
                    self.assertNotEqual(blocked.returncode, 0)
                    retried = self.invoke('--uninstall', *arguments, prefix=prefix)
                    self.assertEqual(retried.returncode, 0, retried.stderr)
                    self.assertFalse(program.exists())
                    self.assertFalse(command.is_symlink())
                    self.assertTrue((self.source / 'src/claude_docker/installer.py').exists())

    def test_pending_removal_revalidates_survivors_and_rejects_malformed_plans(self):
        self.run_command(self.source / 'install')
        target = self.program / 'releases/0.1.0/src/claude_docker/installer.py'
        failed = self.invoke('--uninstall', hook='fail', target=target)
        self.assertNotEqual(failed.returncode, 0)
        plan = self.program / 'removal-intent.json'
        self.assertTrue(plan.exists())
        original = plan.read_bytes()
        survivor = self.program / 'releases/0.1.0/src/claude_docker/uninstall.py'
        contents = survivor.read_bytes()
        survivor.write_bytes(contents + b'\n# changed\n')
        result = self.invoke('--uninstall')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.command.is_symlink())
        survivor.write_bytes(contents)
        survivor.chmod(0o755)
        self.assertNotEqual(self.invoke('--uninstall').returncode, 0)
        survivor.chmod(0o644)
        survivor.unlink()
        survivor.symlink_to(self.source / 'src/claude_docker/uninstall.py')
        self.assertNotEqual(self.invoke('--uninstall').returncode, 0)
        survivor.unlink()
        survivor.write_bytes(contents)
        survivor.chmod(0o644)
        unknown = survivor.parent / 'unknown.py'
        unknown.write_text('unowned')
        self.assertNotEqual(self.invoke('--uninstall').returncode, 0)
        self.assertTrue(unknown.exists())
        unknown.unlink()
        # Missing program files are never acceptable without the durable plan.
        plan.unlink()
        self.assertNotEqual(self.invoke('--uninstall').returncode, 0)
        plan.write_bytes(original)
        plan.chmod(0o644)
        self.assertNotEqual(self.invoke('--uninstall').returncode, 0)
        plan.chmod(0o600)
        for data in ({}, {'schema': 1, 'files': ['/tmp/unowned']}, []):
            plan.write_text(json.dumps(data))
            result = self.invoke('--uninstall')
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(survivor.exists())
        damaged = json.loads(original)
        damaged['ownership']['../outside.json'] = {}
        plan.write_text(json.dumps(damaged))
        self.assertNotEqual(self.invoke('--uninstall').returncode, 0)
        damaged = json.loads(original)
        manifest = damaged['ownership']['releases/0.1.0/manifest.json']
        manifest['files'][0]['path'] = '../outside'
        plan.write_text(json.dumps(damaged))
        self.assertNotEqual(self.invoke('--uninstall').returncode, 0)
        self.assertTrue(survivor.exists())
        plan.write_bytes(original)
        result = self.invoke('--uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_purge_retry_keeps_original_protection_after_configuration_is_removed(self):
        self.run_command(self.source / 'install')
        protected = self.program / 'releases/0.1.0/README.md'
        original = protected.read_bytes()
        config = self.root / 'config/claude-docker/config.toml'
        self.write(config, f'[state.code_json]\nsource = "{protected}"\nmount = false\n')
        target = self.program / 'releases/0.1.0/src/claude_docker/installer.py'
        failed = self.invoke('runtime', 'uninstall', '--purge', '--yes', hook='fail', target=target)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('injected deletion interruption', failed.stderr)
        self.assertFalse(config.exists())
        self.assertEqual(protected.read_bytes(), original)
        # Cleanup committed already. A trusted source retry is local even for
        # pending purge, and cannot rediscover protection from the removed TOML.
        (self.bin_dir / 'docker').unlink()
        retried = self.invoke('--uninstall')
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(protected.read_bytes(), original)
        self.assertFalse((self.program / 'removal-intent.json').exists())
        self.assertFalse(self.command.is_symlink())

    def test_recorded_launch_without_docker_stays_fail_closed_until_retry(self):
        self.run_command(self.source / 'install')
        self.write(self.root / 'state/claude-docker/active-mode', 'none\n')
        docker = self.bin_dir / 'docker'
        docker.unlink()
        python = self.bin_dir / 'python3'
        python.symlink_to(sys.executable)
        self.env['PATH'] = str(self.bin_dir)
        for attempt in range(2):
            result = self.invoke('--uninstall')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('cleanup failed', result.stderr)
            self.assertTrue(self.command.is_symlink())
            self.assertTrue((self.program / 'installation.json').exists())
        docker.write_text('#!' + sys.executable + '\n')
        docker.chmod(0o755)
        result = self.invoke('--uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.program.exists())

    def test_tools_lock_source_and_dev_remain_writable(self):
        log = self.root / 'lock-runs'
        docker = self.bin_dir / 'docker'
        docker.write_text('#!' + sys.executable + '\nfrom pathlib import Path\np = Path(' + repr(str(log)) + ')\np.write_text(p.read_text() + "run\\n" if p.exists() else "run\\n")\n')
        docker.chmod(0o755)
        self.run_command(self.source / 'claude-docker', 'tools', 'lock')
        self.run_command(self.source / 'install', '--dev')
        self.run_command(self.command, 'tools', 'lock')
        self.assertEqual(log.read_text().splitlines(), ['run', 'run'])

    def test_installed_user_and_system_tools_lock_refuses_before_docker(self):
        log = self.root / 'docker-called'
        docker = self.bin_dir / 'docker'
        docker.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(' + repr(str(log)) + ').touch()\n')
        docker.chmod(0o755)
        for mode in ('user', 'system'):
            with self.subTest(mode=mode):
                prefix = self.root / 'prefix' if mode == 'system' else '-'
                arguments = ('--system',) if mode == 'system' else ()
                self.assertEqual(self.invoke(*arguments, prefix=prefix).returncode, 0)
                command = self.root / 'prefix/bin/claude-docker' if mode == 'system' else self.command
                result = subprocess.run([str(command), 'tools', 'lock'], env=self.env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('checkout', result.stderr.lower())
                self.assertFalse(log.exists())
                result = self.invoke('--uninstall', *arguments, prefix=prefix)
                self.assertEqual(result.returncode, 0, result.stderr)

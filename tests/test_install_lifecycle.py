"""Real install/command/uninstall subprocesses, confined to temporary roots."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]

# Keep the prefix seam in the test process, never in a production environment
# override. runpy still executes the actual bootstrap, including self-removal.
SYSTEM_BOOTSTRAP = """
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

bootstrap, prefix, *arguments = sys.argv[1:]
# The harness imports the installer to inject its existing layout factory;
# those imports precede the bootstrap's own bytecode guard.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(bootstrap).resolve().parent / 'src'))
from claude_docker.installer import InstallLayout
layout = InstallLayout.system(Path(prefix))
sys.argv = [bootstrap, *arguments]
with patch.object(InstallLayout, 'system', return_value=layout), \
     patch.object(InstallLayout, 'user', side_effect=AssertionError('user paths in system mode')), \
     patch('claude_docker.installer.resolve_host_paths', side_effect=AssertionError('XDG in system mode')), \
     patch('claude_docker.commands.CommandRunner.run', side_effect=AssertionError('Docker in system mode')):
    runpy.run_path(bootstrap, run_name='__main__')
"""


class InstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        runtime = self.root / 'runtime'
        runtime.mkdir(mode=0o700)
        self.bin_dir = self.root / 'bin'
        self.bin_dir.mkdir()
        # Do not inherit live-test gates, CLAUDE_* overrides, Docker context,
        # PYTHONPATH, or the parent test runner's bytecode suppression.
        self.env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.root / 'config'),
                    'XDG_DATA_HOME': str(self.root / 'data'),
                    'XDG_STATE_HOME': str(self.root / 'state'),
                    'XDG_CACHE_HOME': str(self.root / 'cache'),
                    'XDG_RUNTIME_DIR': str(runtime), 'TMPDIR': str(self.root),
                    'PATH': str(self.bin_dir) + os.pathsep + os.defpath}
        self.program = self.root / 'data/claude-docker'
        self.command = self.home / '.local/bin/claude-docker'
        self.source = self.root / 'checkout'
        shutil.copytree(ROOT, self.source,
                        ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
        (self.source / 'VERSION').write_text('0.1.0\n')
        (self.source / 'src/claude_docker/__init__.py').write_text('__version__ = "0.1.0"\n')
        self.docker_log = self.root / 'docker.jsonl'

    def run_command(self, *arguments, expected_stderr=''):
        result = subprocess.run([str(arg) for arg in arguments], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0,
                         f'{arguments!r}\nstdout: {result.stdout}\nstderr: {result.stderr}')
        lines = result.stderr.splitlines(keepends=True)
        details = [line for line in lines if ' (build input): ' in line or line.startswith('claude-docker: launcher version: ')]
        if details:
            self.assertEqual(len(details), 8)
        self.assertEqual(''.join(line for line in lines if line not in details), expected_stderr)
        return result.stdout

    def snapshot(self, root):
        """Detect changed bytes, targets, metadata, additions and removals."""
        return {str(path.relative_to(root)): (
                    path.lstat().st_ino, path.lstat().st_mode, path.lstat().st_mtime_ns,
                    str(path.readlink()) if path.is_symlink()
                    else path.read_bytes() if path.is_file() else None)
                for path in (root, *root.rglob('*'))}

    def write(self, path, contents):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(contents)

    def fake_docker(self, program):
        # Compose is the only external boundary needed for normal cleanup.
        # Accept exact mount-free service stop/container removal; unexpected
        # discovery, down, volume flags, or other Docker operations fail closed.
        script = f'''\
            #!{sys.executable}
            import json
            from pathlib import Path
            import sys
            arguments = sys.argv[1:]
            with Path({str(self.docker_log)!r}).open('a') as log:
                log.write(json.dumps(arguments) + '\\n')
            base = ['compose', '--project-name', 'claude-docker-{os.getuid()}',
                    '-f', {str(program / 'compose.yaml')!r}, '--profile', 'desktop']
            allowed = [base + ['stop', 'claude-desktop'],
                       base + ['rm', '--stop', '--force']]
            if arguments not in allowed:
                sys.stderr.write('Unexpected Docker operation: ' + repr(arguments))
                sys.exit(91)
            '''
        docker = self.bin_dir / 'docker'
        docker.write_text(textwrap.dedent(script))
        docker.chmod(0o755)

    def assert_cleanup_commands(self, program):
        commands = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        base = ['compose', '--project-name', f'claude-docker-{os.getuid()}',
                '-f', str(program / 'compose.yaml'), '--profile', 'desktop']
        self.assertEqual(commands, [base + ['stop', 'claude-desktop'],
                                    base + ['rm', '--stop', '--force']])

    def test_user_bootstrap_update_and_normal_uninstall_retain_all_user_data(self):
        # Breaks if an update keeps old code active, or uninstall leaves either
        # release behind or deletes default/custom/unknown user-owned files.
        self.fake_docker(self.program / 'releases/0.2.0')
        self.run_command(self.source / 'install')
        self.assertEqual(self.run_command(self.command, '--version'), 'claude-docker 0.1.0\n')
        self.assertIn('uninstall', self.run_command(self.command, '--help'))
        self.assertEqual(self.command.resolve(), self.program / 'releases/0.1.0/claude-docker')
        self.assertFalse(self.docker_log.exists())
        for name in ('config', 'state', 'cache'):
            self.assertFalse((self.root / name).exists())
        self.assertEqual(list((self.root / 'runtime').iterdir()), [])

        retained = {
            self.home / '.claude/history.json': 'code history',
            self.home / '.claude.json': '{"token":"fixture-only"}',
            self.home / '.config/Claude/preferences.json': 'desktop preferences',
            self.home / 'work/git/project/main.py': 'workspace source',
            self.root / 'custom/code/session': 'custom code state',
            self.root / 'custom/code.json': 'custom code JSON',
            self.root / 'custom/desktop/preferences': 'custom desktop state',
            self.root / 'state/claude-docker/history': 'persistent launcher state',
            self.root / 'config/claude-docker/customize.sh': 'invalid Bash (',
            self.root / 'cache/claude-docker/unknown': 'unknown cache payload',
            self.root / 'runtime/claude-docker/unknown': 'unknown runtime payload',
            self.program / 'user-owned/notes': 'unknown data payload',
        }
        config = ('[container]\nmounts = ["~/work/git"]\n'
                  '[state.code]\nsource = "' + str(self.root / 'custom/code') + '"\nmount = false\n'
                  '[state.code_json]\nsource = "' + str(self.root / 'custom/code.json') + '"\nmount = false\n'
                  '[state.desktop]\nsource = "' + str(self.root / 'custom/desktop') + '"\nmount = false\n')
        retained[self.root / 'config/claude-docker/config.toml'] = config
        for path, contents in retained.items():
            self.write(path, contents)
        (self.root / 'runtime/claude-docker').chmod(0o700)
        before = {path: self.snapshot(path)['.'] for path in retained}
        command_stat = self.snapshot(self.command)['.']
        old_release = self.snapshot(self.program / 'releases/0.1.0')

        (self.source / 'VERSION').write_text('0.2.0\n')
        (self.source / 'src/claude_docker/__init__.py').write_text('__version__ = "0.2.0"\n')
        self.run_command(self.source / 'install')
        self.assertEqual(self.run_command(self.command, '--version'), 'claude-docker 0.2.0\n')
        self.assertIn('uninstall', self.run_command(self.command, '--help'))
        self.assertEqual(self.snapshot(self.command)['.'], command_stat)
        self.assertEqual(self.command.resolve(), self.program / 'releases/0.2.0/claude-docker')
        self.assertEqual(self.snapshot(self.program / 'releases/0.1.0'), old_release)
        for directory in (self.program, self.source):
            self.assertEqual(list(directory.rglob('*.pyc')), [])
        self.write(self.root / 'state/claude-docker/active-mode', 'none\n')
        self.run_command(self.command, 'uninstall')
        self.assertFalse(self.command.is_symlink())
        self.assertFalse(self.command.exists())
        for name in ('releases', 'current', 'installation.json', 'installation-intent.json'):
            self.assertFalse(os.path.lexists(self.program / name), name)
        self.assert_cleanup_commands(self.program / 'releases/0.2.0')
        for path, snapshot in before.items():
            self.assertEqual(self.snapshot(path)['.'], snapshot, str(path))

    def test_installed_user_tools_init_lock_all_build_leave_bundle_immutable(self):
        # Fails if installed workflows rewrite shipped inputs or omit personal
        # tool inputs from either product build. Only Docker is simulated.
        example_lock = (ROOT / 'tests/fixtures/user-tools.lock').read_text()
        (self.source / 'VERSION').write_text('0.3.0\n')
        (self.source / 'src/claude_docker/__init__.py').write_text('__version__ = "0.3.0"\n')
        self.run_command(self.source / 'install')
        release = self.program / 'releases/0.3.0'
        before = self.snapshot(release)
        profile = self.root / 'config/claude-docker/mise.toml'
        docker = self.bin_dir / 'docker'
        docker.write_text(textwrap.dedent(f'''\
            #!{sys.executable}
            import csv, json, os, sys
            from pathlib import Path
            args = sys.argv[1:]
            if args[0] == 'version':
                print('linux/amd64')
            elif args[:2] == ['image', 'inspect']:
                print('linux/amd64', args[-1].split(':')[1], 'sha256:' + 'a' * 64)
            elif args[0] == 'create':
                assert args[args.index('--platform') + 1] == 'linux/amd64' and args[-1] == 'resolve', args
                print('b' * 64)
            elif args[0] == 'start':
                assert args == ['start', '--attach', 'b' * 64], args
                print('Fetching signed index (fixture)', file=sys.stderr)
                print('5.11492.5')
            elif args[0] == 'rm':
                assert args == ['rm', '--force', 'b' * 64], args
            elif args[0] == 'run':
                assert args[-4:] == ['lock', '--platform', 'linux-x64,linux-arm64', '--bump'] or args[-3:] == ['lock', '--platform', 'linux-x64,linux-arm64'], args
                fields = next(csv.reader([args[args.index('--mount') + 1]]))
                root = Path(next(v[7:] for v in fields if v.startswith('source=')))
                assert set(p.name for p in root.iterdir()) <= {{'mise.toml', 'mise.lock'}}
                assert root != Path({str(profile.parent)!r})
                (root / 'mise.lock').write_text({example_lock!r})
            else:
                assert args[0] == 'compose' and 'build' in args, args
                files = [Path(args[i+1]) for i, v in enumerate(args) if v == '-f']
                override = json.loads(files[-1].read_text())
                build = override['services'][args[-1]]['build']
                root = Path(build['context'])
                assert (root / 'image/mise-user.lock').read_text() == {example_lock!r}
                assert b'kubectl' in (root / 'image/mise-user.toml').read_bytes()
                assert (root / 'image/claude-desktop.version').read_text() == '5.11492.5\\n'
                with Path({str(self.docker_log)!r}).open('a') as log:
                    log.write(json.dumps({{'service': args[-1], 'context': str(root)}}) + '\\n')
            '''))
        docker.chmod(0o755)
        self.run_command(self.command, 'tools', 'init')
        # This lifecycle fixture pins a small personal profile independently of the init example.
        profile.write_text('[tools]\nkubectl = "1.36"\naws-cli = "2"\n')
        diagnostic = (f'claude-docker: config: {profile.parent / "config.toml"} '
                      '(not found; using defaults and environment overrides)\n'
                      f'claude-docker: tools: {profile}\n')
        self.run_command(self.command, '-d', 'tools', 'lock', expected_stderr=diagnostic)
        lock_before = profile.with_suffix('.lock').read_bytes()
        self.run_command(self.command, '-d', 'tools', 'lock', expected_stderr=diagnostic)
        self.assertEqual(profile.with_suffix('.lock').read_bytes(), lock_before)
        self.write(profile.parent / 'claude-code.version', '2.1.326\n')
        diagnostic = diagnostic.replace(f'claude-docker: tools: {profile}',
                                        'claude-docker: Claude Code version: 2.1.326\n'
                                        'claude-docker: Checking the Docker daemon for Claude Desktop release resolution.\n'
                                        'claude-docker: Checking the cached Claude Desktop resolver image for linux/amd64.\n'
                                        'claude-docker: Checking fresh signed Anthropic package metadata for Claude Desktop.\n'
                                        'Fetching signed index (fixture)\n'
                                        'claude-docker: Claude Desktop version: 5.11492.5\n'
                                        f'claude-docker: tools: {profile}')
        self.run_command(self.command, '-d', 'build', '--all', expected_stderr=diagnostic)
        self.assertEqual((profile.parent / 'claude-desktop.version').read_text(), '5.11492.5\n')
        builds = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        self.assertEqual([build['service'] for build in builds], ['claude-cli', 'claude-desktop'])
        self.assertEqual(builds[0]['context'], builds[1]['context'])
        self.assertEqual(self.snapshot(release), before)
        rejected = subprocess.run([str(self.command), 'tools', 'lock', '--base'],
                                  env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('immutable', rejected.stderr)
        self.assertEqual(self.snapshot(release), before)

    def test_shared_bundle_selects_each_simulated_accounts_tools_and_image_identity(self):
        # Simulates two accounts; this does not claim privileged multi-user
        # qualification. A shared bundle must not select the installer's profile.
        (self.source / 'VERSION').write_text('0.3.0\n')
        (self.source / 'src/claude_docker/__init__.py').write_text('__version__ = "0.3.0"\n')
        prefix = self.root / 'prefix'
        self.run_command(sys.executable, '-c', SYSTEM_BOOTSTRAP,
                         self.source / 'install', prefix, '--system')
        release = prefix / 'share/claude-docker/releases/0.3.0'
        before = self.snapshot(release)
        docker = self.bin_dir / 'docker'
        docker.write_text(textwrap.dedent(f'''\
            #!{sys.executable}
            import json, os, sys
            from pathlib import Path
            args = sys.argv[1:]
            assert args[0] == 'compose' and args[-2:] == ['build', 'claude-cli'], args
            files = [Path(args[i+1]) for i, value in enumerate(args) if value == '-f']
            build = json.loads(files[-1].read_text())['services']['claude-cli']['build']
            context = Path(build['context'])
            data = (context / 'image/mise-user.toml').read_text()
            assert not (context / 'image/mise-user.lock').exists()
            with Path({str(self.docker_log)!r}).open('a') as log:
                log.write(json.dumps({{'profile': data, 'host_uid': os.environ['CLAUDE_HOST_UID'],
                                      'project': args[args.index('--project-name') + 1],
                                      'build_identity': build['args']}}) + '\\n')
            '''))
        docker.chmod(0o755)
        harness = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.dont_write_bytecode = True
release, uid, home, *arguments = sys.argv[1:]
sys.path.insert(0, str(Path(release) / 'src'))
from claude_docker.cli import main
account = SimpleNamespace(pw_name='user' + uid, pw_dir=home)
with patch('os.getuid', return_value=int(uid)), patch('pwd.getpwuid', return_value=account):
    raise SystemExit(main(arguments))
'''
        original_env = self.env
        expected = []
        for uid, package in ((12345, 'ripgrep'), (23456, 'htop')):
            home = self.root / f'account-{uid}'
            home.mkdir()
            selected = self.root / f'xdg-{uid}/claude-docker/mise.toml'
            profile = f'[bootstrap.packages]\n"apt:{package}" = "latest"\n'
            self.write(selected, profile)
            self.write(selected.parent / 'claude-code.version', '2.1.326\n')
            self.env = dict(original_env, HOME=str(home), XDG_CONFIG_HOME=str(selected.parent.parent),
                            CLAUDE_CONTAINER_USER=f'user{uid}', CLAUDE_CONTAINER_UID=str(uid),
                            CLAUDE_CONTAINER_GID=str(uid), CLAUDE_CONTAINER_HOME=f'/home/user{uid}')
            # Runtime paths remain isolated too, although build must not create them.
            self.env.pop('XDG_RUNTIME_DIR')
            diagnostic = (f'claude-docker: config: {selected.parent / "config.toml"} '
                          '(not found; using defaults and environment overrides)\n'
                          'claude-docker: Claude Code version: 2.1.326\n'
                          f'claude-docker: tools: {selected}\n')
            self.run_command(sys.executable, '-c', harness, release, uid, home, '-d', 'build',
                             expected_stderr=diagnostic)
            expected.append((str(uid), profile))
        self.env = original_env
        records = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        self.assertEqual([(r['host_uid'], r['profile']) for r in records], expected)
        for record, (uid, _) in zip(records, expected):
            self.assertEqual(record['project'], 'claude-docker-' + uid)
            self.assertEqual(record['build_identity']['CLAUDE_HOST_UID'], uid)
            self.assertEqual(record['build_identity']['CLAUDE_UID'], uid)
        self.assertEqual(self.snapshot(release), before)

    def test_development_bootstrap_observes_edits_and_uninstall_preserves_checkout(self):
        # Breaks if --dev copies a release, suppresses live edits, generates
        # bytecode in the checkout, or treats the checkout as owned program data.
        self.fake_docker(self.source)
        before = self.snapshot(self.source)
        self.run_command(self.source / 'install', '--dev')
        self.assertEqual(self.snapshot(self.source), before)
        self.assertEqual(self.command.readlink(), self.source / 'claude-docker')
        self.assertEqual(self.run_command(self.command, '--version'), 'claude-docker 0.1.0\n')
        self.assertIn('uninstall', self.run_command(self.command, '--help'))
        (self.source / 'src/claude_docker/__init__.py').write_text('__version__ = "9.8.7"\n')
        edited = self.snapshot(self.source)
        self.assertEqual(self.run_command(self.command, '--version'), 'claude-docker 9.8.7\n')
        self.assertEqual(self.snapshot(self.source), edited)
        self.assertFalse((self.program / 'releases').exists())
        self.assertFalse((self.program / 'current').exists())
        self.assertFalse(self.docker_log.exists())

        self.write(self.root / 'state/claude-docker/active-mode', 'none\n')
        self.run_command(self.command, 'uninstall')
        self.assertFalse(os.path.lexists(self.command))
        self.assertFalse(self.program.exists())
        self.assertTrue(self.source.is_dir())
        self.assertEqual(self.snapshot(self.source), edited)
        self.assertEqual(list(self.source.rglob('*.pyc')), [])
        self.assert_cleanup_commands(self.source)

    def test_system_bootstrap_uses_only_injected_prefix_for_install_and_removal(self):
        # Breaks if system lifecycle derives user paths, calls Docker, removes
        # unrelated prefix files, or cannot finish after removing its own bundle.
        prefix = self.root / 'prefix'
        program = prefix / 'share/claude-docker'
        command = prefix / 'bin/claude-docker'
        unrelated = prefix / 'share/other-application/data'
        self.write(unrelated, 'outside installation ownership')
        protected = [self.home]
        for name in ('config', 'data', 'state', 'cache', 'runtime'):
            directory = self.root / name
            self.write(directory / 'sentinel', name)
            protected.append(directory)
        before = {directory: self.snapshot(directory) for directory in protected}
        self.run_command(sys.executable, '-c', SYSTEM_BOOTSTRAP,
                         self.source / 'install', prefix, '--system')
        self.assertEqual(command.resolve(), program / 'releases/0.1.0/claude-docker')
        self.assertEqual(self.run_command(command, '--version'), 'claude-docker 0.1.0\n')
        self.assertIn('uninstall', self.run_command(command, '--help'))
        self.assertEqual(list(program.rglob('*.pyc')), [])
        self.run_command(sys.executable, '-c', SYSTEM_BOOTSTRAP,
                         program / 'current/install', prefix, '--uninstall', '--system')
        self.assertFalse(os.path.lexists(command))
        self.assertFalse(program.exists())
        self.assertEqual(unrelated.read_text(), 'outside installation ownership')
        for directory, snapshot in before.items():
            self.assertEqual(self.snapshot(directory), snapshot, str(directory))
        self.assertFalse(self.docker_log.exists())


if __name__ == '__main__':
    unittest.main()

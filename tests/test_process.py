"""Exercise process-scoped launches through the real CLI and Compose boundary."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker.cli import main
from claude_docker.process import docker_exec_argv

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'task5-unique-secret-sentinel with spaces "quotes"\nTOKEN=a=b'


class DockerExecArgvTests(unittest.TestCase):
    def test_names_workdir_and_literal_arguments_are_forwarded(self):
        command = docker_exec_argv('container-one', ['claude', '--help', '--model=x', 'a; $(touch no)', '--', '-p'],
                                   workdir=Path('/work space/project'), env_names=('TOKEN', 'EMPTY'),
                                   interactive=True, tty=True)
        self.assertEqual(command, ['docker', 'exec', '-it', '--workdir', '/work space/project',
                                  '--env', 'TOKEN', '--env', 'EMPTY', 'container-one',
                                  'claude', '--help', '--model=x', 'a; $(touch no)', '--', '-p'])

    def test_tty_requires_both_input_interactivity_and_output_tty(self):
        for interactive, tty, flags in ((True, False, ['-i']), (False, True, []),
                                         (False, False, []), (True, True, ['-it'])):
            with self.subTest(interactive=interactive, tty=tty):
                self.assertEqual(docker_exec_argv('one', ['claude'], workdir=Path('/home/tester'),
                                                 env_names=(), interactive=interactive, tty=tty),
                                 ['docker', 'exec', *flags, '--workdir', '/home/tester', 'one', 'claude'])

    def test_detached_process_is_supported_without_changing_argument_boundaries(self):
        self.assertEqual(docker_exec_argv('one', ['app', '--flag'], workdir=Path('/home/tester'),
                                         env_names=('TOKEN',), interactive=False, tty=False, detach=True),
                         ['docker', 'exec', '-d', '--workdir', '/home/tester', '--env', 'TOKEN', 'one', 'app', '--flag'])


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace with space'
        self.caller = self.workspace / 'project'
        self.caller.mkdir(parents=True)
        self.runtime_base = self.root / 'runtime'
        self.runtime_base.mkdir(mode=0o700)
        self.config_file = self.root / 'config.toml'
        self.log = self.root / 'commands.jsonl'
        self.expected = self.root / 'expected.json'
        self.binary = self.root / 'bin'
        self.binary.mkdir()
        docker = self.binary / 'docker'
        docker.write_text('#!' + sys.executable + '\n' + '''import hashlib,json,os,sys
from pathlib import Path
args = sys.argv[1:]
with (Path(os.environ['HOME']) / 'commands.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
if args[0] == 'exec':
    expected = json.loads((Path(os.environ['HOME']) / 'expected.json').read_text())
    for name, digest in expected.items():
        if hashlib.sha256(os.environ.get(name, '').encode()).hexdigest() != digest:
            sys.exit(96)
    if '--read-stdin' in args:
        if not any(flag in args for flag in ('-i', '-it')):
            sys.exit(95)
        if sys.stdin.read() != 'piped prompt with spaces\\nsecond line':
            sys.exit(94)
    sys.exit(37)
if 'ps' in args:
    print('container-one')
''')
        docker.chmod(0o755)
        self.env = {'HOME': str(self.root), 'PATH': str(self.binary),
                    'CLAUDE_CONFIG_FILE': str(self.config_file), 'XDG_RUNTIME_DIR': str(self.runtime_base),
                    'XDG_STATE_HOME': str(self.root / 'state'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'TOKEN': SECRET, 'EMPTY': '', 'UNSELECTED': SECRET + 'extra'}
        self.write_config()
        self.expect_values({'TOKEN': SECRET, 'EMPTY': '', 'UNSELECTED': SECRET + 'extra'})

    def write_config(self, *, required=('TOKEN',), optional=('ABSENT', 'EMPTY'), mode='none', mounts=None):
        roots = [str(self.workspace)] if mounts is None else mounts
        self.config_file.write_text('docker_mode = ' + json.dumps(mode) + '\n[container]\nmounts = ' +
                                    json.dumps(roots) + '\n[environment]\nrequired = ' + json.dumps(required) +
                                    '\noptional = ' + json.dumps(optional) + '\n')

    def expect_values(self, values):
        self.expected.write_text(json.dumps({name: hashlib.sha256(value.encode()).hexdigest()
                                            for name, value in values.items()}))

    def invoke(self, args=(), env=None, input=None):
        return subprocess.run([sys.executable, str(ROOT / 'claude-docker'), 'claude', *args],
                              env=self.env if env is None else env, cwd=self.caller,
                              text=True, capture_output=True, input=input)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_launch_propagates_status_inherits_values_and_preserves_leading_dash_arguments(self):
        result = self.invoke(('--help', '--model', 'model name', '--', '-p', 'literal; $(false)'))
        self.assertEqual(result.returncode, 37, result.stderr)
        commands = self.commands()
        self.assertEqual(commands[0][-3:], ['up', '-d', 'claude-cli'])
        self.assertEqual(commands[1][-3:], ['ps', '-q', 'claude-cli'])
        self.assertEqual(commands[2], ['exec', '-i', '--workdir', str(self.caller), '--env', 'TOKEN',
                                      '--env', 'EMPTY', 'container-one', 'claude', '--help', '--model',
                                      'model name', '--', '-p', 'literal; $(false)'])
        override = (self.runtime_base / 'claude-docker/compose.none.json').read_text()
        for output in (json.dumps(commands), override, result.stdout, result.stderr):
            self.assertNotIn(SECRET, output)
            self.assertNotIn('task5-unique-secret-sentinel', output)
        self.assertNotIn('UNSELECTED', json.dumps(commands))
        self.assertNotIn('TOKEN', override)

    def test_each_reused_session_reselects_names_and_current_values(self):
        self.assertEqual(self.invoke().returncode, 37)
        self.write_config(required=('ROTATED',), optional=('EMPTY',))
        env = dict(self.env, ROTATED='current secret\nwith=new value')
        self.expect_values({'ROTATED': env['ROTATED']})
        self.assertEqual(self.invoke(env=env).returncode, 37)
        commands = self.commands()
        self.assertEqual(sum('down' in command for command in commands), 0)
        self.assertEqual(commands[-1], ['exec', '-i', '--workdir', str(self.caller), '--env', 'ROTATED',
                                       '--env', 'EMPTY', 'container-one', 'claude'])
        self.assertEqual(sum(command[-3:] == ['up', '-d', 'claude-cli'] for command in commands), 2)

    def test_mode_transition_stops_old_mode_before_cli_only_start(self):
        self.assertEqual(self.invoke().returncode, 37)
        self.write_config(mode='sysbox')
        self.assertEqual(self.invoke().returncode, 37)
        commands = self.commands()
        self.assertEqual(commands[3][-1], 'down')
        self.assertIn(str(ROOT / 'compose.yaml'), commands[3])
        self.assertNotIn(str(ROOT / 'compose.sysbox.yaml'), commands[3])
        self.assertEqual(commands[4][-3:], ['up', '-d', 'claude-cli'])
        self.assertIn(str(ROOT / 'compose.sysbox.yaml'), commands[4])

    def test_missing_or_empty_required_fails_before_any_docker_process(self):
        for value in (None, ''):
            env = dict(self.env)
            if value is None:
                del env['TOKEN']
            else:
                env['TOKEN'] = value
            with self.subTest(value=value):
                result = self.invoke(env=env)
                self.assertEqual(result.returncode, 1)
                self.assertIn('TOKEN', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn(SECRET, result.stderr)
                self.assertEqual(self.commands(), [])

    def test_missing_workspace_fails_before_docker(self):
        self.write_config(mounts=[str(self.root / 'missing')])
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Workspace root', result.stderr)
        self.assertEqual(self.commands(), [])

    def test_piped_prompt_is_forwarded_without_allocating_a_tty(self):
        result = self.invoke(('--read-stdin',), input='piped prompt with spaces\nsecond line')
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(self.commands()[-1][:2], ['exec', '-i'])

    def test_cli_tty_detection_and_diagnostic_containment_at_subprocess_boundary(self):
        for stdin_tty, stdout_tty, flags in ((True, True, ['-it']), (True, False, ['-i']),
                                              (False, True, ['-i']), (False, False, ['-i'])):
            calls = []
            def run(argv, **kwargs):
                calls.append((list(argv), kwargs))
                return subprocess.CompletedProcess(argv, 37 if argv[1] == 'exec' else 0,
                                                   'container-one\n' if 'ps' in argv else '', '')
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(stdin_tty=stdin_tty, stdout_tty=stdout_tty), \
                    patch.dict(os.environ, self.env, clear=True), \
                    patch('sys.stdin.isatty', return_value=stdin_tty), \
                    patch('subprocess.run', side_effect=run), \
                    patch('logging.Logger._log') as diagnostics, \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                with patch.object(stdout, 'isatty', return_value=stdout_tty):
                    self.assertEqual(main(['claude', '--help']), 37)
                exec_argv, kwargs = calls[-1]
                self.assertEqual(exec_argv[:2 + len(flags)], ['docker', 'exec', *flags])
                self.assertIsNone(kwargs.get('env'))
                self.assertFalse(kwargs['check'])
                self.assertNotIn('task5-unique-secret-sentinel', repr(calls))
                self.assertNotIn('task5-unique-secret-sentinel', repr(diagnostics.call_args_list))
                self.assertEqual(stdout.getvalue(), '')
                self.assertEqual(stderr.getvalue(), '')

    def test_compose_exception_diagnostic_does_not_expose_selected_values(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), \
                patch('subprocess.run', side_effect=subprocess.CalledProcessError(7, ['docker', 'compose'])), \
                patch('logging.Logger._log') as diagnostics, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(['claude']), 1)
        self.assertIn('exit 7', stderr.getvalue())
        for output in (stdout.getvalue(), stderr.getvalue(), repr(diagnostics.call_args_list)):
            self.assertNotIn('task5-unique-secret-sentinel', output)


if __name__ == '__main__':
    unittest.main()

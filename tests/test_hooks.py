"""Run actual host Bash hooks with synthetic data and no Docker."""

import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class HookTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.script = self.root / 'hook with spaces.sh'

    def run_hook(self, script, env=None, timeout=2):
        from claude_docker.hooks import launch_environment
        from claude_docker.model import EnvironmentConfig, HooksConfig

        self.script.write_text(script)
        return launch_environment(HooksConfig(self.script, timeout),
                                  EnvironmentConfig(('TOKEN',), ('REMOVE', 'EMPTY', 'LOCAL', 'DOCKER_HOST')),
                                  dict(os.environ) if env is None else env)

    def test_preserves_shell_bytes_and_filters_exports(self):
        env = {'TOKEN': 'before', 'REMOVE': 'old', 'DOCKER_HOST': 'ssh://caller',
               'HOOK_INPUT': 'inherited'}
        names, child_env = self.run_hook(
            "export TOKEN=\"$HOOK_INPUT = 'quoted'\"\n"
            "TOKEN+=$'\\r\\nlast line'\n"
            "unset REMOVE\nLOCAL=unexported\nexport EMPTY=''\nexport UNSELECTED=private\n"
            "export DOCKER_HOST=ssh://different\nexport PATH=/missing\n", env)
        self.assertEqual(names, ('TOKEN', 'EMPTY', 'DOCKER_HOST'))
        self.assertEqual(child_env, {'TOKEN': "inherited = 'quoted'\r\nlast line", 'EMPTY': '',
                                     'DOCKER_HOST': 'ssh://caller', 'HOOK_INPUT': 'inherited'})
        self.assertEqual(env['TOKEN'], 'before')
        self.assertEqual(env['REMOVE'], 'old')

    def test_hook_cannot_introduce_absent_docker_transport_settings(self):
        from claude_docker.hooks import launch_environment
        from claude_docker.model import EnvironmentConfig, HooksConfig

        self.script.write_text('export HOME=/different DOCKER_HOST=ssh://different\n')
        names, env = launch_environment(HooksConfig(self.script),
                                        EnvironmentConfig((), ('HOME', 'DOCKER_HOST')), {})
        self.assertEqual(names, ())
        self.assertEqual(env, {})

    def test_hook_preserves_resolver_settings_present_or_absent(self):
        from claude_docker.hooks import launch_environment
        from claude_docker.model import EnvironmentConfig, HooksConfig

        names = ('LOCALDOMAIN', 'RES_OPTIONS', 'HOSTALIASES')
        for original in ({}, dict.fromkeys(names, 'caller-value')):
            for body in ('export LOCALDOMAIN=other RES_OPTIONS=other HOSTALIASES=other',
                         'unset LOCALDOMAIN RES_OPTIONS HOSTALIASES'):
                with self.subTest(original=original, body=body):
                    self.script.write_text(body)
                    selected, env = launch_environment(HooksConfig(self.script),
                                                       EnvironmentConfig((), names), original)
                    self.assertEqual(env, original)
                    self.assertEqual(selected, tuple(original))

    def test_readme_aws_example_exports_credentials_and_aborts_on_aws_failure(self):
        from claude_docker.hooks import launch_environment
        from claude_docker.model import EnvironmentConfig, HooksConfig

        readme = (Path(__file__).resolve().parents[1] / 'README.md').read_text()
        [example] = [body for body in re.findall(r'```bash\n(.*?)```', readme, re.S)
                     if 'aws sts get-federation-token' in body]
        policy = EnvironmentConfig(('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'), ())
        # A shell function supplies synthetic AWS output; no network or host AWS CLI.
        self.script.write_text("aws() { printf 'fixture-key\\tfixture-secret\\tfixture-token\\n'; }\n" + example)
        names, env = launch_environment(HooksConfig(self.script), policy, {})
        self.assertEqual(names, policy.required)
        self.assertEqual([env[name] for name in names], ['fixture-key', 'fixture-secret', 'fixture-token'])
        self.script.write_text('aws() { return 42; }\n' + example)
        with self.assertRaisesRegex(ValueError, 'exit 42'):
            launch_environment(HooksConfig(self.script), policy, {})

    def test_ignores_noninteractive_startup_files_and_keeps_hook_output_visible(self):
        startup = self.root / 'startup'
        startup.write_text('echo must-not-run; exit 55\n')
        self.script.write_text('echo hook-progress; echo hook-warning >&2; export TOKEN=fixture\n')
        result = subprocess.run(
            [sys.executable, '-B', '-c',
             'import os, sys; from pathlib import Path; from claude_docker.hooks import launch_environment; '
             'from claude_docker.model import HooksConfig, EnvironmentConfig; '
             'names, env = launch_environment(HooksConfig(Path(sys.argv[1])), '
             'EnvironmentConfig(("TOKEN",), ()), os.environ); assert env["TOKEN"] == "fixture"',
             str(self.script)], cwd=self.root, capture_output=True, text=True,
            env=dict(os.environ, BASH_ENV=str(startup), ENV=str(startup),
                     PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'hook-progress\n')
        self.assertEqual(result.stderr, 'hook-warning\n')

    def test_reports_nonzero_or_incomplete_hook_without_dumping_exports(self):
        for body in ('false\nexport TOKEN=ignored', 'export TOKEN=private; exit 17',
                     'export TOKEN=private; exit 0', 'if then; fi'):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, r'hooks\.pre_launch') as caught:
                self.run_hook(body)
            self.assertNotIn('private', str(caught.exception))

    def test_missing_script_and_bash_fail_without_raw_os_errors(self):
        from claude_docker.hooks import launch_environment
        from claude_docker.model import EnvironmentConfig, HooksConfig

        config = HooksConfig(self.script)
        with self.assertRaisesRegex(ValueError, r'hooks\.pre_launch.*script'):
            launch_environment(config, EnvironmentConfig((), ()), os.environ)
        with self.assertRaisesRegex(ValueError, r'hooks\.pre_launch.*Bash'):
            self.run_hook('', {'PATH': str(self.root)})

    def test_rejects_oversize_environment_output_without_logging_it(self):
        with self.assertRaisesRegex(ValueError, r'hooks\.pre_launch.*size'):
            self.run_hook("printf -v large '%020000d' 0\n"
                          "for n in {1..20}; do export \"large_$n=$large\"; done\n")

    def test_timeout_kills_descendants_that_keep_the_pipe_open(self):
        pid_path = self.root / 'background.pid'
        env = dict(os.environ, PID_PATH=str(pid_path))

        def alive(pid):
            try:
                return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
            except (FileNotFoundError, ProcessLookupError):
                return False

        start = time.monotonic()
        try:
            with self.assertRaisesRegex(ValueError, r'hooks\.pre_launch.*timed out'):
                self.run_hook('sleep 30 &\necho "$!" > "$PID_PATH"\nexport TOKEN=partial\n', env, timeout=1)
            self.assertLess(time.monotonic() - start, 4)
            pid = int(pid_path.read_text())
            deadline = time.monotonic() + 1
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(alive(pid))
        finally:
            if pid_path.exists():
                pid = int(pid_path.read_text())
                if alive(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_failed_hook_promptly_kills_background_children_even_with_closed_protocol(self):
        pid_path = self.root / 'failed-background.pid'
        for close_pipe, code in ((False, 23), (True, 23), (True, 0)):
            with self.subTest(close_pipe=close_pipe, code=code):
                body = ('sleep 30' if not close_pipe else
                        'sleep 30 {__claude_docker_output}>&-')
                start = time.monotonic()
                message = 'exit 23' if code else 'complete environment'
                with self.assertRaisesRegex(ValueError, message):
                    self.run_hook(body + f' &\necho "$!" > "$PID_PATH"\nexit {code}\n',
                                  dict(os.environ, PID_PATH=str(pid_path)), timeout=3)
                self.assertLess(time.monotonic() - start, 2)
                pid = int(pid_path.read_text())
                deadline = time.monotonic() + 1
                while True:
                    try:
                        state = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0]
                    except (FileNotFoundError, ProcessLookupError):
                        state = 'Z'
                    if state == 'Z' or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                self.assertEqual(state, 'Z')

"""Execute real child processes while keeping command data out of a shell."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_docker.commands import CommandRunner


class CommandRunnerTests(unittest.TestCase):
    def test_shell_metacharacters_remain_one_literal_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            payload = "literal spaces; touch " + str(marker) + " $(touch " + str(marker) + ") `touch " + str(marker) + "`"
            result = CommandRunner().run([sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", payload], capture_output=True)
            self.assertEqual(json.loads(result.stdout), [payload])
            self.assertFalse(marker.exists())

    def test_default_environment_is_inherited(self):
        with patch.dict(os.environ, {"CLAUDE_DOCKER_RUNNER_TEST": "inherited value"}):
            result = CommandRunner().run((sys.executable, "-c", "import os; print(os.environ['CLAUDE_DOCKER_RUNNER_TEST'])"), capture_output=True)
        self.assertEqual(result.stdout, "inherited value\n")

    def test_explicit_environment_replaces_inherited_environment(self):
        with patch.dict(os.environ, {"CLAUDE_DOCKER_RUNNER_TEST": "host value"}):
            result = CommandRunner().run([sys.executable, "-c", "import os; print(os.environ.get('CLAUDE_DOCKER_RUNNER_TEST', 'absent')); print(os.environ['ONLY_THIS'])"], env={"ONLY_THIS": "selected value"}, capture_output=True)
        self.assertEqual(result.stdout, "absent\nselected value\n")

    def test_nonzero_status_is_checked_by_default_and_can_be_returned(self):
        command = [sys.executable, "-c", "import sys; print('failed', file=sys.stderr); sys.exit(7)"]
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            CommandRunner().run(command, capture_output=True)
        self.assertEqual(caught.exception.returncode, 7)
        self.assertEqual(caught.exception.stderr, "failed\n")
        result = CommandRunner().run(command, check=False, capture_output=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr, "failed\n")

    def test_stderr_is_visible_before_the_child_returns_its_stdout(self):
        child = ('from pathlib import Path; import time,sys\n'
                 'print("fetching", file=sys.stderr, flush=True)\n'
                 'while not Path(sys.argv[1]).exists(): time.sleep(0.01)\n'
                 'print("5.11-2")')
        launcher = ('import sys; from claude_docker.commands import CommandRunner; '
                    'result = CommandRunner().run([sys.executable, "-B", "-c", *sys.argv[1:]], '
                    'capture_output=True, stream_stderr=True, timeout=3); print(result.stdout, end="")')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'continue'
            progress = Path(directory) / 'progress'
            with progress.open('w') as stderr, subprocess.Popen(
                    [sys.executable, '-B', '-c', launcher, child, str(marker)], env=env,
                    stderr=stderr, stdout=subprocess.PIPE, text=True) as process:
                deadline = time.monotonic() + 2
                while not progress.stat().st_size and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(progress.read_text(), 'fetching\n')
                self.assertIsNone(process.poll())
                marker.touch()
                stdout, _ = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, progress.read_text())
                self.assertEqual(stdout, '5.11-2\n')

    def test_timeout_terminates_a_real_child(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / 'pid'
            command = [sys.executable, '-B', '-c',
                       'from pathlib import Path; import os,sys,time; '
                       'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(10)', str(pid_path)]
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                CommandRunner().run(command, timeout=0.5)
            self.assertLess(time.monotonic() - start, 5)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_path.read_text()), 0)

    def test_timeout_terminates_the_commands_inherited_transport_process(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / 'transport-pid'
            command = [sys.executable, '-B', '-c',
                       'from pathlib import Path; import subprocess,sys; '
                       'child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(10)"]); '
                       'Path(sys.argv[1]).write_text(str(child.pid))', str(pid_path)]
            with self.assertRaises(subprocess.TimeoutExpired):
                # The leader exits, but its transport still holds the output pipe open.
                CommandRunner().run(command, timeout=0.5, capture_output=True)
            pid = int(pid_path.read_text())

            def running():
                try:
                    return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
                except FileNotFoundError:
                    return False

            try:
                deadline = time.monotonic() + 0.5
                while running() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(running(), 'Transport child remained active after the command timed out')
            finally:
                if running():
                    os.kill(pid, signal.SIGKILL)

    def test_interrupt_keeps_an_exited_session_leader_unreaped_until_the_group_signal(self):
        popen = subprocess.Popen
        read = os.read
        child = None
        signal_found_leader = []

        def spawn(*args, **kwargs):
            nonlocal child
            child = popen(*args, **kwargs)
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
            return child

        def interrupt_stdout(fd, length):
            if child is not None and fd == child.stdout.fileno():
                raise KeyboardInterrupt
            return read(fd, length)

        def observe_group_signal(group, sig):
            try:
                exited_child = os.waitid(os.P_PID, group, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            except ChildProcessError:
                exited_child = None
            signal_found_leader.append((exited_child is not None and group == child.pid, sig))

        with patch('subprocess.Popen', side_effect=spawn), patch('os.read', side_effect=interrupt_stdout), \
                patch('os.killpg', side_effect=observe_group_signal), self.assertRaises(KeyboardInterrupt):
            CommandRunner().run([sys.executable, '-B', '-c', 'print("done")'],
                                capture_output=True, timeout=2)
        self.assertEqual(signal_found_leader, [(True, signal.SIGKILL)])
        with self.assertRaises(ChildProcessError):
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)

    def test_invalid_output_after_child_exit_does_not_signal_a_process_group(self):
        command = [sys.executable, '-B', '-c', 'import sys; sys.stdout.buffer.write(b"\\xff")']
        with patch('os.killpg') as kill_group, self.assertRaises(UnicodeDecodeError):
            CommandRunner().run(command, capture_output=True, timeout=2)
        kill_group.assert_not_called()

    def test_command_strings_and_nonstring_elements_are_rejected(self):
        for command in ("echo unsafe", b"echo unsafe", [sys.executable, 123]):
            with self.subTest(command=command):
                with self.assertRaises(TypeError):
                    CommandRunner().run(command)


if __name__ == "__main__":
    unittest.main()

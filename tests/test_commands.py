"""Execute real child processes while keeping command data out of a shell."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def test_command_strings_and_nonstring_elements_are_rejected(self):
        for command in ("echo unsafe", b"echo unsafe", [sys.executable, 123]):
            with self.subTest(command=command):
                with self.assertRaises(TypeError):
                    CommandRunner().run(command)


if __name__ == "__main__":
    unittest.main()

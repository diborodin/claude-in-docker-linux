"""Bootstrap must work from any directory and reject old Python before imports."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = ROOT / "claude-docker"


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docker_marker = self.root / "docker-called"
        docker = self.root / "docker"
        docker.write_text("#!/bin/sh\ntouch '" + str(self.docker_marker) + "'\nexit 97\n")
        docker.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("CLAUDE_DOCKER_DIR", "PYTHONPATH")}
        self.env["PATH"] = str(self.root) + os.pathsep + self.env.get("PATH", "")

    def run_launcher(self, *arguments, executable=EXECUTABLE, env=None):
        return subprocess.run([sys.executable, str(executable), *arguments], cwd=self.root,
                              env=self.env if env is None else env, text=True, capture_output=True)

    def test_root_help_and_version_have_no_docker_side_effect(self):
        self.assertTrue(EXECUTABLE.is_file(), "root claude-docker executable is missing")
        self.assertTrue(os.access(EXECUTABLE, os.X_OK))
        help_result = self.run_launcher("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("usage:", help_result.stdout.lower())
        version = self.run_launcher("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertRegex(version.stdout, r"claude-docker \d+\.\d+\.\d+")
        self.assertFalse(self.docker_marker.exists())

    def test_installed_symlink_finds_package_from_an_unrelated_cwd(self):
        link = self.root / "installed-command"
        link.symlink_to(EXECUTABLE)
        result = self.run_launcher("--help", executable=link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())

    def test_absolute_override_loads_the_selected_adjacent_package(self):
        bundle = self.root / "other-bundle"
        package = bundle / "src/claude_docker"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "cli.py").write_text("def main(argv=None):\n    print('selected-package')\n    return 23\n")
        result = self.run_launcher(env=dict(self.env, CLAUDE_DOCKER_DIR=str(bundle)))
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(result.stdout, "selected-package\n")

    def test_invalid_overrides_fail_cleanly_before_package_discovery(self):
        for value in ("relative", "", str(self.root / "missing"), str(self.root)):
            with self.subTest(override=value):
                result = self.run_launcher("--help", env=dict(self.env, CLAUDE_DOCKER_DIR=value))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CLAUDE_DOCKER_DIR", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_python_version_gate_precedes_package_import_and_path_discovery(self):
        self.assertTrue(EXECUTABLE.is_file(), "root claude-docker executable is missing")
        code = (
            "import builtins, runpy, sys\n"
            "original_import = builtins.__import__\n"
            "def guard(name, *args, **kwargs):\n"
            "    if name.startswith('claude_docker'):\n"
            "        raise AssertionError('package imported before version check')\n"
            "    return original_import(name, *args, **kwargs)\n"
            "builtins.__import__ = guard\n"
            "sys.version_info = (3, 10, 9, 'final', 0)\n"
            "runpy.run_path(sys.argv[1], run_name='__main__')\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(EXECUTABLE)],
                                env=dict(self.env, CLAUDE_DOCKER_DIR="missing"),
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11", result.stderr)
        self.assertIn("install", result.stderr.lower())
        self.assertNotIn("package imported", result.stderr)
        self.assertNotIn("CLAUDE_DOCKER_DIR", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_module_entrypoint_provides_help_without_docker(self):
        env = dict(self.env, PYTHONPATH=str(ROOT / "src"))
        result = subprocess.run([sys.executable, "-m", "claude_docker", "--help"],
                                cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())
        self.assertFalse(self.docker_marker.exists())


if __name__ == "__main__":
    unittest.main()

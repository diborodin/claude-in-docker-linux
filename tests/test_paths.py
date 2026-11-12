"""Host-path contracts: XDG placement, private runtime state, safe discovery."""

import dataclasses
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_docker.model import (
    AppConfig, ContainerConfig, DesktopConfig, EnvironmentConfig, StateMountConfig,
)
from claude_docker.paths import HostPaths, resolve_host_paths, resolve_program_dir


class HostPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home), "XDG_RUNTIME_DIR": str(self.runtime)}

    def test_standard_xdg_defaults_do_not_create_persistent_directories(self):
        paths = resolve_host_paths(self.env, os.getuid())
        self.assertEqual(paths.home, self.home)
        self.assertEqual(paths.config_dir, self.home / ".config/claude-docker")
        self.assertEqual(paths.config_file, self.home / ".config/claude-docker/config.toml")
        self.assertEqual(paths.data_dir, self.home / ".local/share/claude-docker")
        self.assertEqual(paths.state_dir, self.home / ".local/state/claude-docker")
        self.assertEqual(paths.cache_dir, self.home / ".cache/claude-docker")
        self.assertEqual(paths.runtime_dir, self.runtime / "claude-docker")
        self.assertFalse(self.home.exists())
        self.assertEqual(stat.S_IMODE(paths.runtime_dir.stat().st_mode), 0o700)

    def test_read_only_resolution_does_not_create_or_chmod_runtime(self):
        paths = resolve_host_paths(self.env, os.getuid(), create_runtime=False)
        self.assertEqual(paths.runtime_dir, self.runtime / "claude-docker")
        self.assertFalse(paths.runtime_dir.exists())
        paths.runtime_dir.mkdir(mode=0o755)
        resolve_host_paths(self.env, os.getuid(), create_runtime=False)
        self.assertEqual(stat.S_IMODE(paths.runtime_dir.stat().st_mode), 0o755)
        self.assertFalse(self.home.exists())

    def test_explicit_xdg_values_preserve_lexical_symlink_spelling(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        env = dict(self.env, XDG_CONFIG_HOME=str(link / "config"),
                   XDG_DATA_HOME=str(link / "data"), XDG_STATE_HOME=str(link / "state"),
                   XDG_CACHE_HOME=str(link / "cache"))
        paths = resolve_host_paths(env, os.getuid())
        self.assertEqual(paths.config_dir, link / "config/claude-docker")
        self.assertEqual(paths.data_dir, link / "data/claude-docker")
        self.assertEqual(paths.state_dir, link / "state/claude-docker")
        self.assertEqual(paths.cache_dir, link / "cache/claude-docker")
        self.assertEqual(list(target.iterdir()), [])

    def test_empty_or_relative_xdg_values_use_standard_defaults(self):
        paths = resolve_host_paths(dict(self.env, XDG_CONFIG_HOME="relative", XDG_DATA_HOME=""), os.getuid())
        self.assertEqual(paths.config_dir, self.home / ".config/claude-docker")
        self.assertEqual(paths.data_dir, self.home / ".local/share/claude-docker")

    def test_missing_home_uses_passwd_home_without_process_environment(self):
        account = type("Account", (), {"pw_dir": str(self.home)})()
        with patch("claude_docker.paths.pwd.getpwuid", return_value=account):
            paths = resolve_host_paths({"XDG_RUNTIME_DIR": str(self.runtime)}, os.getuid())
        self.assertEqual(paths.home, self.home)

    def test_unusable_runtime_falls_back_to_private_uid_directory(self):
        fallback = Path("/tmp") / ("claude-docker-" + str(os.getuid()))
        existed = fallback.exists()
        if not existed:
            self.addCleanup(lambda: fallback.rmdir() if fallback.exists() else None)
        for value in ("", "relative", str(self.root / "missing"), str(self.root / "file")):
            with self.subTest(runtime=value):
                (self.root / "file").touch()
                paths = resolve_host_paths(dict(self.env, XDG_RUNTIME_DIR=value), os.getuid())
                self.assertEqual(paths.runtime_dir, fallback)
                self.assertEqual(stat.S_IMODE(fallback.stat().st_mode), 0o700)

    def test_runtime_base_without_private_permissions_is_unusable(self):
        self.runtime.chmod(0o755)
        fallback = Path("/tmp") / ("claude-docker-" + str(os.getuid()))
        existed = fallback.exists()
        if not existed:
            self.addCleanup(lambda: fallback.rmdir() if fallback.exists() else None)
        paths = resolve_host_paths(self.env, os.getuid())
        self.assertEqual(paths.runtime_dir, fallback)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o755)

    def test_runtime_child_symlink_cannot_redirect_private_files(self):
        target = self.root / "unrelated"
        target.mkdir(mode=0o755)
        (self.runtime / "claude-docker").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "runtime"):
            resolve_host_paths(self.env, os.getuid())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(list(target.iterdir()), [])

    def test_relative_home_is_rejected_before_creating_runtime(self):
        with self.assertRaisesRegex(ValueError, "HOME"):
            resolve_host_paths(dict(self.env, HOME="relative"), os.getuid())
        self.assertFalse((self.runtime / "claude-docker").exists())

    def test_models_are_frozen_and_keep_typed_configuration_records(self):
        state = StateMountConfig(source=self.home / ".claude", mount=True)
        container = ContainerConfig(user="tester", uid=1000, gid=1000, home=self.home,
                                    mounts=(self.home / "work/git",))
        desktop = DesktopConfig(sandbox=True, clipboard="manual")
        environment = EnvironmentConfig(required=("TOKEN",), optional=("REGION",))
        config = AppConfig(docker_mode="none", container=container, state_code=state,
                           state_code_json=StateMountConfig(self.home / ".claude.json", True),
                           state_desktop=StateMountConfig(self.home / ".config/Claude", True),
                           desktop=desktop, environment=environment)
        paths = resolve_host_paths(self.env, os.getuid())
        for record, field in ((state, "mount"), (container, "uid"), (desktop, "sandbox"),
                              (environment, "required"), (config, "docker_mode"), (paths, "home")):
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(record, field, None)
        self.assertIsInstance(paths, HostPaths)
        self.assertEqual(config.container.mounts, (self.home / "work/git",))


class ProgramPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "program"
        (self.bundle / "src/claude_docker").mkdir(parents=True)
        (self.bundle / "src/claude_docker/cli.py").touch()
        (self.bundle / "claude-docker").touch()

    def test_program_discovery_follows_installed_executable_symlink(self):
        link = self.root / "installed-command"
        link.symlink_to(self.bundle / "claude-docker")
        self.assertEqual(resolve_program_dir(link, {}), self.bundle)

    def test_absolute_override_selects_another_bundle(self):
        self.assertEqual(resolve_program_dir(Path("/not/the/program"),
                         {"CLAUDE_DOCKER_DIR": str(self.bundle)}), self.bundle)

    def test_relative_missing_or_incomplete_override_is_actionable(self):
        for value in ("relative", "", str(self.root / "missing"), str(self.root)):
            with self.subTest(override=value):
                with self.assertRaisesRegex(ValueError, "CLAUDE_DOCKER_DIR"):
                    resolve_program_dir(self.bundle / "claude-docker", {"CLAUDE_DOCKER_DIR": value})


if __name__ == "__main__":
    unittest.main()

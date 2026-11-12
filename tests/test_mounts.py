"""Workspace identity and private Claude state preparation."""

from dataclasses import replace
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_docker.config import AccountInfo, load_config
from claude_docker.model import (
    AppConfig, BindMount, ContainerConfig, DesktopConfig, EnvironmentConfig, StateMountConfig,
)
from claude_docker.mounts import container_workdir, prepare_mounts
from claude_docker.paths import HostPaths


class MountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.workspace = self.root / "workspace with spaces"
        self.workspace.mkdir()
        self.container_home = Path("/home/container-user")
        self.config = AppConfig(
            "none", ContainerConfig("tester", 1234, 1234, self.container_home, (self.workspace,)),
            StateMountConfig(self.home / ".claude", False),
            StateMountConfig(self.home / ".claude.json", False),
            StateMountConfig(self.home / ".config/Claude", False),
            DesktopConfig(True, "manual"), EnvironmentConfig((), ()),
        )

    def with_roots(self, *roots):
        return replace(self.config, container=replace(self.config.container, mounts=roots))

    def with_state(self, name, source=None, enabled=True):
        field = "state_" + name
        state = getattr(self.config, field)
        self.config = replace(self.config, **{field: replace(
            state, source=source if source is not None else state.source, mount=enabled)})

    def test_workspaces_keep_exact_absolute_paths_and_are_read_write(self):
        second = self.root / "another workspace"
        second.mkdir()
        self.assertEqual(prepare_mounts(self.with_roots(self.workspace, second)), (
            BindMount(self.workspace, self.workspace, False), BindMount(second, second, False)))

    def test_symlink_workspace_spelling_is_preserved(self):
        alias = self.root / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        self.assertEqual(prepare_mounts(self.with_roots(alias)), (BindMount(alias, alias, False),))

    def test_duplicate_roots_are_rejected_without_state_creation(self):
        self.with_state("code")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            prepare_mounts(self.with_roots(self.workspace, self.workspace))
        self.assertFalse((self.home / ".claude").exists())

    def test_missing_default_root_is_actionable_before_docker_or_state_creation(self):
        paths = HostPaths(self.home, self.root / "config", self.root / "data",
                          self.root / "state", self.root / "cache", self.root / "run")
        config = load_config(env={}, paths=paths, account=AccountInfo("tester", 1234, 1234, self.home))
        with patch("subprocess.run", side_effect=AssertionError("Docker must not run")):
            with self.assertRaises(ValueError) as caught:
                prepare_mounts(config)
        self.assertIn(str(self.home / "work/git"), str(caught.exception))
        self.assertIn("container.mounts", str(caught.exception))
        self.assertFalse((self.home / "work").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".config").exists())

    def test_all_workspace_roots_are_validated_before_creating_state(self):
        self.with_state("code")
        missing = self.root / "missing"
        with self.assertRaisesRegex(ValueError, str(missing)):
            prepare_mounts(self.with_roots(self.workspace, missing))
        self.assertFalse((self.home / ".claude").exists())

    def test_file_workspace_is_rejected(self):
        file = self.root / "file"
        file.touch()
        with self.assertRaisesRegex(ValueError, str(file)):
            prepare_mounts(self.with_roots(file))

    def test_workdir_preserves_lexical_caller(self):
        alias = self.workspace / "link"
        alias.symlink_to(self.home, target_is_directory=True)
        caller = alias / "project"
        mounts = prepare_mounts(self.config)
        self.assertEqual(container_workdir(caller, mounts, self.container_home), caller)

    def test_workdir_matches_resolved_caller_alias(self):
        alias = self.root / "outside alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        self.assertEqual(container_workdir(alias / "project", prepare_mounts(self.config),
                                           self.container_home), self.workspace / "project")

    def test_physical_caller_maps_to_configured_symlink_target_spelling(self):
        alias = self.root / "configured alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        mounts = prepare_mounts(self.with_roots(alias))
        self.assertEqual(container_workdir(self.workspace / "project", mounts,
                                           self.container_home), alias / "project")

    def test_workdir_uses_longest_component_prefix_regardless_of_order(self):
        nested = self.workspace / "nested"
        mounts = (BindMount(self.workspace, Path("/outer"), False),
                  BindMount(nested, Path("/inner"), False))
        for order in (mounts, mounts[::-1]):
            with self.subTest(order=order):
                self.assertEqual(container_workdir(nested / "project", order, self.container_home),
                                 Path("/inner/project"))
                self.assertEqual(container_workdir(nested, order, self.container_home), Path("/inner"))

    def test_resolved_matches_rank_physical_prefix_specificity(self):
        alias = self.root / "very/deep/configured/alias"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(self.workspace, target_is_directory=True)
        nested = self.workspace / "nested"
        mounts = (BindMount(alias, Path("/outer"), False),
                  BindMount(nested, Path("/inner"), False))
        self.assertEqual(container_workdir(nested / "project", mounts, self.container_home),
                         Path("/inner/project"))

    def test_noncomponent_prefix_and_outside_caller_fall_back_home(self):
        mounts = prepare_mounts(self.config)
        for caller in (Path(str(self.workspace) + "-other/project"), self.root / "outside"):
            with self.subTest(caller=caller):
                self.assertEqual(container_workdir(caller, mounts, self.container_home),
                                 self.container_home)

    def test_empty_workspaces_are_supported(self):
        self.assertEqual(prepare_mounts(self.with_roots()), ())
        self.assertEqual(container_workdir(self.workspace, (), self.container_home), self.container_home)

    def test_missing_state_directories_and_parents_are_private_and_umask_restored(self):
        self.with_state("code")
        self.with_state("desktop")
        previous = os.umask(0)
        try:
            mounts = prepare_mounts(self.config)
            restored = os.umask(0)
            self.assertEqual(restored, 0)
        finally:
            os.umask(previous)
        for path in (self.home / ".claude", self.home / ".config", self.home / ".config/Claude"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(mounts[1:], (
            BindMount(self.home / ".claude", self.container_home / ".claude", False),
            BindMount(self.home / ".config/Claude", self.container_home / ".config/Claude", False)))

    def test_existing_state_directory_permissions_are_retained(self):
        self.with_state("code")
        self.with_state("desktop")
        for path, mode in ((self.home / ".claude", 0o750), (self.home / ".config", 0o755),
                           (self.home / ".config/Claude", 0o751)):
            path.mkdir(mode=mode)
        prepare_mounts(self.config)
        for path, mode in ((self.home / ".claude", 0o750), (self.home / ".config", 0o755),
                           (self.home / ".config/Claude", 0o751)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    def test_overridden_state_sources_use_fixed_container_home_targets(self):
        code, desktop, json = (self.root / name for name in ("custom-code", "custom-desktop", "custom.json"))
        json.write_text('{"preserve": true}')
        self.with_state("code", code)
        self.with_state("desktop", desktop)
        self.with_state("code_json", json)
        mounts = prepare_mounts(self.config)
        self.assertEqual(set(mounts[1:]), {
            BindMount(code, self.container_home / ".claude", False),
            BindMount(desktop, self.container_home / ".config/Claude", False),
            BindMount(json, self.container_home / ".claude.json", False)})
        self.assertEqual(json.read_text(), '{"preserve": true}')

    def test_state_mounts_are_independently_disabled(self):
        for enabled in ("code", "desktop", "code_json"):
            with self.subTest(enabled=enabled):
                self.config = replace(self.config, **{
                    field: replace(getattr(self.config, field), mount=field == "state_" + enabled)
                    for field in ("state_code", "state_desktop", "state_code_json")})
                self.config.state_code_json.source.write_text("{}")
                mounts = prepare_mounts(self.config)
                self.assertEqual(len(mounts), 2)
                self.assertEqual(mounts[1].source, getattr(self.config, "state_" + enabled).source)

    def test_disabled_missing_sources_are_never_created(self):
        prepare_mounts(self.config)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_existing_json_regular_file_is_mounted_without_changes(self):
        self.with_state("code_json")
        source = self.config.state_code_json.source
        source.write_text("existing state")
        source.chmod(0o640)
        self.assertEqual(prepare_mounts(self.config)[-1],
                         BindMount(source, self.container_home / ".claude.json", False))
        self.assertEqual(source.read_text(), "existing state")
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o640)

    def test_missing_json_is_not_created_or_mounted(self):
        self.with_state("code_json", self.root / "missing parent/state.json")
        self.assertEqual(prepare_mounts(self.config), (BindMount(self.workspace, self.workspace, False),))
        self.assertFalse((self.root / "missing parent").exists())

    def test_nonregular_json_sources_are_rejected_before_state_creation(self):
        self.with_state("code")
        self.with_state("code_json")
        self.config.state_code_json.source.mkdir()
        with self.assertRaisesRegex(ValueError, "state.code_json"):
            prepare_mounts(self.config)
        self.assertFalse(self.config.state_code.source.exists())

    def test_fifo_json_source_is_rejected(self):
        self.with_state("code_json")
        os.mkfifo(self.config.state_code_json.source)
        with self.assertRaisesRegex(ValueError, "state.code_json"):
            prepare_mounts(self.config)

    def test_dangling_json_symlink_is_rejected_without_creating_target(self):
        self.with_state("code_json")
        target = self.root / "absent json"
        self.config.state_code_json.source.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "state.code_json"):
            prepare_mounts(self.config)
        self.assertFalse(target.exists())

    def test_file_in_place_of_state_directory_is_rejected_before_creating_other_state(self):
        self.with_state("code")
        self.with_state("desktop")
        self.config.state_desktop.source.parent.mkdir()
        self.config.state_desktop.source.touch()
        with self.assertRaisesRegex(ValueError, "state.desktop"):
            prepare_mounts(self.config)
        self.assertFalse(self.config.state_code.source.exists())

    def test_creation_failure_restores_umask(self):
        self.with_state("code")
        def denied(path, **kwargs):
            raise PermissionError("denied")
        previous = os.umask(0o022)
        try:
            with self.assertRaises((ValueError, PermissionError)):
                prepare_mounts(self.config, mkdir=denied)
            self.assertEqual(os.umask(0o022), 0o022)
        finally:
            os.umask(previous)


if __name__ == "__main__":
    unittest.main()

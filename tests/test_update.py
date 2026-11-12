"""Atomic publication and alternate install modes, always in temporary roots."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_docker import installer
from claude_docker.installer import InstallLayout, install_release
from claude_docker.manifest import InstallManifest, InstallationRegistry


class InstallFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.root / "data"),
                    "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CACHE_HOME": str(self.root / "cache"), "PATH": os.defpath,
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.layout = InstallLayout.user(self.env)
        self.source = self.root / "checkout"
        shutil.copytree(ROOT, self.source, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    def install(self, version="0.1.0"):
        (self.source / "VERSION").write_text(version + "\n")
        return install_release(self.source, self.layout, version=version)

    def snapshot(self, root):
        return {str(path.relative_to(root)): (path.lstat().st_ino, path.lstat().st_mode,
                    path.lstat().st_mtime_ns, str(path.readlink()) if path.is_symlink()
                    else path.read_bytes() if path.is_file() else None)
                for path in (root, *root.rglob("*"))}

    def assert_command_works(self, version="0.1.0"):
        self.assertEqual(self.layout.command_link.resolve(), self.layout.release_dir(version) / "claude-docker")
        result = subprocess.run([str(self.layout.command_link), "--version"], env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @contextmanager
    def fail_publication(self, phase, version="0.2.0"):
        original_replace = os.replace
        original_read = Path.read_bytes
        original_symlink = Path.symlink_to
        def replace(source, destination):
            destination = Path(destination)
            fail = ((phase == "manifest" and destination.name == "manifest.json")
                    or (phase == "rename" and destination == self.layout.release_dir(version))
                    or (phase == "registry" and destination == self.layout.registry_file)
                    or (phase == "current" and destination == self.layout.current_link))
            if fail:
                raise OSError("injected " + phase)
            return original_replace(source, destination)
        def read(path):
            if phase == "copy" and path.name == "README.md" and self.layout.releases_dir in path.parents:
                raise OSError("injected copy")
            return original_read(path)
        def symlink(path, target, *args, **kwargs):
            if phase == "command" and path == self.layout.command_link:
                raise OSError("injected command")
            return original_symlink(path, target, *args, **kwargs)
        with patch("claude_docker.installer.os.replace", replace), patch.object(Path, "read_bytes", read), patch.object(Path, "symlink_to", symlink):
            yield


class UpdateTests(InstallFixture):
    def trim_release_inventory(self, removed):
        metadata = self.layout.manifest_file('0.1.0')
        data = json.loads(metadata.read_text())
        data['files'] = [entry for entry in data['files'] if entry['path'] not in removed]
        metadata.write_text(json.dumps(data))
        for name in removed:
            (self.layout.release_dir('0.1.0') / name).unlink(missing_ok=True)
        # Historical releases predate the whole security directory.
        security = self.layout.release_dir('0.1.0') / 'security'
        if security.exists() and not any(security.iterdir()):
            security.rmdir()
        return data

    def test_historical_inventory_survives_new_policy_assets(self):
        new = {'LICENSE', 'src/claude_docker/seccomp.py',
               'security/desktop-seccomp-v0.2.4-linux-amd64.json',
               'security/LICENSE.moby-profiles', 'security/NOTICE.moby-profiles'}
        for without_uninstall in (False, True):
            with self.subTest(without_uninstall=without_uninstall):
                self.layout = InstallLayout.user(dict(self.env, XDG_DATA_HOME=str(self.root / str(without_uninstall)),
                                                      HOME=str(self.root / ('home-' + str(without_uninstall)))))
                self.install()
                removed = new | ({'src/claude_docker/uninstall.py'} if without_uninstall else set())
                data = self.trim_release_inventory(removed)
                self.assertEqual(len(data['files']), 29 if without_uninstall else 30)
                self.install('0.2.0')
                self.assert_command_works('0.2.0')
                self.assertTrue(self.layout.manifest_file('0.1.0').exists())

    def test_historical_inventory_without_project_license_can_update(self):
        self.install()
        data = self.trim_release_inventory({'LICENSE'})
        self.assertEqual(len(data['files']), 34)
        self.install('0.2.0')
        self.assert_command_works('0.2.0')
        self.assertEqual((self.layout.release_dir('0.2.0') / 'LICENSE').read_bytes(),
                         (ROOT / 'LICENSE').read_bytes())

    def test_incomplete_new_policy_inventory_is_rejected(self):
        cases = [{'src/claude_docker/seccomp.py'},
                 {'security/desktop-seccomp-v0.2.4-linux-amd64.json'},
                 {'security/LICENSE.moby-profiles'}, {'security/NOTICE.moby-profiles'},
                 {'src/claude_docker/uninstall.py'},
                 {'security/LICENSE.moby-profiles', 'security/NOTICE.moby-profiles'}]
        for index, removed in enumerate(cases):
            with self.subTest(removed=removed):
                self.layout = InstallLayout.user(dict(self.env, XDG_DATA_HOME=str(self.root / str(index)),
                                                      HOME=str(self.root / ('home-' + str(index)))))
                self.install()
                self.trim_release_inventory(removed)
                before = self.snapshot(self.layout.root)
                with self.assertRaisesRegex(ValueError, 'fixed runtime bundle'):
                    self.install('0.2.0')
                self.assertEqual(self.snapshot(self.layout.root), before)

    def test_update_preserves_command_link_and_all_user_files(self):
        self.install()
        command_stat = self.layout.command_link.lstat()
        old_release = self.snapshot(self.layout.release_dir("0.1.0"))
        sentinels = []
        for name in ("config", "state", "cache", "data"):
            path = self.root / name / "claude-docker/user-owned"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
            sentinels.append((path, path.stat(), path.read_bytes()))
        manifest = self.install("0.2.0")
        self.assertEqual(manifest.version, "0.2.0")
        self.assert_command_works("0.2.0")
        # Reading/executing a symlink may update atime; publication must keep
        # its identity and write timestamps, not filesystem access accounting.
        current_stat = self.layout.command_link.lstat()
        for field in ('st_ino', 'st_dev', 'st_mode', 'st_mtime_ns', 'st_ctime_ns'):
            self.assertEqual(getattr(current_stat, field), getattr(command_stat, field))
        self.assertEqual(self.snapshot(self.layout.release_dir("0.1.0")), old_release)
        registry = InstallationRegistry.read(self.layout.registry_file)
        self.assertEqual(registry.releases, ("0.1.0", "0.2.0"))
        self.assertEqual(registry.active_version, "0.2.0")
        for path, metadata, contents in sentinels:
            self.assertEqual(path.stat(), metadata)
            self.assertEqual(path.read_bytes(), contents)

    def test_failures_keep_old_command_and_retry_recovers_pending_ownership(self):
        for phase in ("copy", "manifest", "rename", "registry", "current"):
            with self.subTest(phase=phase):
                # Each phase gets an independent installation to expose recovery.
                self.layout = InstallLayout.user(dict(self.env, XDG_DATA_HOME=str(self.root / phase),
                                                      HOME=str(self.root / (phase + "-home"))))
                self.install()
                old_manifest = self.layout.manifest_file("0.1.0").read_bytes()
                with self.fail_publication(phase), self.assertRaisesRegex(OSError, "injected " + phase):
                    self.install("0.2.0")
                self.assert_command_works()
                self.assertEqual(self.layout.manifest_file("0.1.0").read_bytes(), old_manifest)
                self.assertFalse(any(p.name.startswith(".0.2.0-") for p in self.layout.releases_dir.iterdir()))
                if phase in ("registry", "current"):
                    self.assertTrue(self.layout.intent_file.is_file())
                    self.assertEqual(json.loads(self.layout.intent_file.read_text())["version"], "0.2.0")
                if phase == "current":
                    registry = InstallationRegistry.read(self.layout.registry_file)
                    self.assertIn("0.2.0", registry.releases)
                    self.assertEqual(registry.active_version, "0.1.0")
                self.install("0.2.0")
                self.assert_command_works("0.2.0")
                self.assertFalse(self.layout.intent_file.exists())

    def test_failed_initial_command_creation_can_be_retried(self):
        with self.fail_publication("command", "0.1.0"), self.assertRaisesRegex(OSError, "injected command"):
            self.install()
        self.assertFalse(self.layout.command_link.exists())
        self.assertTrue(self.layout.intent_file.is_file())
        self.install()
        self.assert_command_works()
        self.assertFalse(self.layout.intent_file.exists())

    def test_recovery_of_pending_other_version_preserves_ownership(self):
        self.install()
        with self.fail_publication("registry"), self.assertRaises(OSError):
            self.install("0.2.0")
        self.install("0.3.0")
        registry = InstallationRegistry.read(self.layout.registry_file)
        self.assertEqual(registry.releases, ("0.1.0", "0.2.0", "0.3.0"))
        self.assertEqual(registry.active_version, "0.3.0")
        self.assert_command_works("0.3.0")

    def test_identical_reinstall_preserves_every_byte_inode_mode_and_mtime(self):
        manifest = self.install()
        before = self.snapshot(self.layout.root)
        command = self.layout.command_link.lstat()
        self.assertEqual(self.install(), manifest)
        self.assertEqual(self.snapshot(self.layout.root), before)
        self.assertEqual(self.layout.command_link.lstat(), command)

    def test_same_version_collision_never_mutates_trusted_installation(self):
        self.install()
        before = self.snapshot(self.layout.root)
        (self.source / "README.md").write_text("different source bytes")
        with self.assertRaisesRegex(ValueError, "[Cc]ollision"):
            self.install()
        self.assertEqual(self.snapshot(self.layout.root), before)
        self.assert_command_works()

    def test_same_version_reuse_rejects_tampered_bundle_and_unowned_release(self):
        self.install()
        release = self.layout.release_dir("0.1.0")
        (release / "README.md").write_text("tampered")
        before = self.snapshot(self.layout.root)
        with self.assertRaisesRegex(ValueError, "verification|manifest|checksum"):
            self.install()
        self.assertEqual(self.snapshot(self.layout.root), before)
        self.layout.registry_file.unlink()
        with self.assertRaisesRegex(ValueError, "owned|ownership"):
            self.install()

    def test_source_execute_bit_collision_is_rejected(self):
        self.install()
        before = self.snapshot(self.layout.root)
        (self.source / "README.md").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "[Cc]ollision"):
            self.install()
        self.assertEqual(self.snapshot(self.layout.root), before)

    def test_fsync_failure_before_rename_never_publishes_partial_release(self):
        self.install()
        original_sync = installer._sync_directory
        def sync(path):
            if path.name.startswith(".0.2.0-"):
                raise OSError("staged directory sync failed")
            return original_sync(path)
        with patch("claude_docker.installer._sync_directory", sync), self.assertRaisesRegex(OSError, "sync failed"):
            self.install("0.2.0")
        self.assert_command_works()
        self.assertFalse(self.layout.release_dir("0.2.0").exists())

    def test_pending_intent_and_current_targets_are_validated_before_recovery(self):
        self.install()
        with self.fail_publication("registry"), self.assertRaises(OSError):
            self.install("0.2.0")
        intent = json.loads(self.layout.intent_file.read_text())
        intent["release"] = str(self.root / "foreign")
        self.layout.intent_file.write_text(json.dumps(intent))
        before = self.snapshot(self.layout.root)
        with self.assertRaises(ValueError):
            self.install("0.2.0")
        self.assertEqual(self.snapshot(self.layout.root), before)
        self.assert_command_works()

    def test_late_registry_failure_restores_old_current_and_retry_is_safe(self):
        self.install()
        original_write = installer._write_json
        def write(path, data):
            if path == self.layout.registry_file and data.get("active_version") == "0.2.0":
                raise OSError("active registry write failed")
            return original_write(path, data)
        with patch("claude_docker.installer._write_json", write), self.assertRaisesRegex(OSError, "active registry"):
            self.install("0.2.0")
        self.assert_command_works()
        self.assertTrue(self.layout.intent_file.exists())
        self.install("0.2.0")
        self.assert_command_works("0.2.0")

    def test_current_directory_sync_failure_restores_old_current(self):
        self.install()
        original_sync = installer._sync_directory
        failed = False
        def sync(path):
            nonlocal failed
            if path == self.layout.root and self.layout.current_link.readlink() == self.layout.release_dir("0.2.0") and not failed:
                failed = True
                raise OSError("current directory fsync failed")
            return original_sync(path)
        with patch("claude_docker.installer._sync_directory", sync), self.assertRaisesRegex(OSError, "current directory"):
            self.install("0.2.0")
        self.assert_command_works()
        self.install("0.2.0")
        self.assert_command_works("0.2.0")


class AlternateModeTests(InstallFixture):
    def test_development_link_uses_checkout_edits_without_copy_or_source_mutation(self):
        layout = InstallLayout.development(self.source, self.layout.command_link.parent,
                                           metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        child_env = dict(self.env)
        child_env.pop("PYTHONDONTWRITEBYTECODE", None)
        before = self.snapshot(self.source)
        manifest = installer.install_development(self.source, layout)
        self.assertEqual(manifest.mode, "dev")
        self.assertEqual(manifest.source, self.source.resolve())
        self.assertEqual(layout.command_link.readlink(), self.source / "claude-docker")
        self.assertEqual(self.snapshot(self.source), before)
        self.assertFalse(layout.releases_dir.exists())
        self.assertFalse((layout.root / "current").exists())
        (self.source / "VERSION").write_text("9.9.9\n")
        result = subprocess.run([str(layout.command_link), "--version"], env=child_env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The command imports the live checkout, so use an observable source edit.
        (self.source / "src/claude_docker/__init__.py").write_text('__version__ = "9.9.9"\n')
        result = subprocess.run([str(layout.command_link), "--version"], env=child_env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("9.9.9", result.stdout)
        self.assertEqual(list(self.source.rglob("*.pyc")), [])
        registry = InstallationRegistry.read(layout.registry_file)
        self.assertEqual(registry.mode, "dev")
        self.assertEqual(registry.source, self.source.resolve())
        self.assertEqual(stat.S_IMODE(layout.registry_file.stat().st_mode), 0o600)

    def test_development_reinstall_is_idempotent_and_invalid_checkout_is_rejected(self):
        layout = InstallLayout.development(self.source, self.layout.command_link.parent,
                                           metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        installer.install_development(self.source, layout)
        before = self.snapshot(layout.root)
        link = layout.command_link.lstat()
        installer.install_development(self.source, layout)
        self.assertEqual(self.snapshot(layout.root), before)
        self.assertEqual(layout.command_link.lstat(), link)
        (self.source / "compose.yaml").unlink()
        with self.assertRaises((ValueError, OSError)):
            installer.install_development(self.source, layout)
        self.assertEqual(self.snapshot(layout.root), before)

    def test_mode_conversion_requires_uninstall_and_never_replaces_link(self):
        self.install()
        before = self.snapshot(self.layout.root)
        layout = InstallLayout.development(self.source, self.layout.command_link.parent,
                                           metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        with self.assertRaisesRegex(ValueError, "uninstall"):
            installer.install_development(self.source, layout)
        self.assertEqual(self.snapshot(self.layout.root), before)
        self.assert_command_works()
        other = InstallLayout.user(dict(self.env, XDG_DATA_HOME=str(self.root / "dev-data"),
                                       HOME=str(self.root / "dev-home")))
        dev = InstallLayout.development(self.source, other.command_link.parent, metadata_root=other.root, host_paths=other.host_paths)
        installer.install_development(self.source, dev)
        with self.assertRaisesRegex(ValueError, "uninstall"):
            install_release(self.source, other, version="0.1.0")
        self.assertEqual(dev.command_link.readlink(), self.source / "claude-docker")

    def test_development_command_inside_checkout_is_rejected_without_mutation(self):
        layout = InstallLayout.development(self.source, self.source / "bin", metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        before = self.snapshot(self.source)
        with self.assertRaisesRegex(ValueError, "outside.*checkout"):
            installer.install_development(self.source, layout)
        self.assertEqual(self.snapshot(self.source), before)
        self.assertFalse(self.layout.root.exists())

    def test_incomplete_dev_metadata_cannot_be_converted_to_release(self):
        layout = InstallLayout.development(self.source, self.layout.command_link.parent,
                                           metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        original_write = installer._write_json
        def write(path, data):
            if path == layout.registry_file:
                raise OSError("registry failed")
            return original_write(path, data)
        with patch("claude_docker.installer._write_json", write), self.assertRaisesRegex(OSError, "registry failed"):
            installer.install_development(self.source, layout)
        before = self.snapshot(self.layout.root)
        with self.assertRaisesRegex(ValueError, "uninstall"):
            self.install()
        self.assertEqual(self.snapshot(self.layout.root), before)
        installer.install_development(self.source, layout)
        self.assertEqual(layout.command_link.readlink(), self.source / "claude-docker")

    def test_system_layout_installs_under_injected_prefix_only(self):
        actual = InstallLayout.system()
        self.assertEqual(actual.root, Path("/usr/local/share/claude-docker"))
        self.assertEqual(actual.command_link, Path("/usr/local/bin/claude-docker"))
        layout = InstallLayout.system(self.root / "prefix")
        install_release(self.source, layout, version="0.2.0")
        self.assertEqual(layout.command_link.resolve(), layout.release_dir("0.2.0") / "claude-docker")
        self.assertEqual(InstallationRegistry.read(layout.registry_file).mode, "system")
        self.assertFalse(self.layout.root.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_cli_system_never_resolves_user_paths_and_reports_explicit_sudo(self):
        layout = InstallLayout.system(self.root / "prefix")
        with patch.object(InstallLayout, "system", return_value=layout), patch.object(InstallLayout, "user", side_effect=AssertionError("user paths read")):
            with _capture_stdout():
                self.assertEqual(installer.main(self.source, ["--system"], env={}), 0)
        denied = InstallLayout.system(self.root / "denied")
        with patch.object(InstallLayout, "system", return_value=denied), patch("claude_docker.installer._directory", side_effect=PermissionError("permission denied")), patch("subprocess.run", side_effect=AssertionError("installer launched a process")):
            with self.assertRaises(SystemExit) as raised, _capture_stderr() as stderr:
                installer.main(self.source, ["--system"], env={})
            self.assertEqual(raised.exception.code, 1)
            self.assertIn("sudo", stderr.getvalue())
            self.assertIn("--system", stderr.getvalue())

    def test_cli_dev_and_system_are_mutually_exclusive_and_dev_uses_xdg_metadata(self):
        result = subprocess.run([str(self.source / "install"), "--dev", "--system"],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.layout.root.exists())
        before = self.snapshot(self.source)
        result = subprocess.run([str(self.source / "install"), "--dev"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.layout.command_link.readlink(), self.source / "claude-docker")
        self.assertEqual(self.snapshot(self.source), before)
        self.assertEqual(InstallationRegistry.read(self.layout.registry_file).mode, "dev")


@contextmanager
def _capture_stderr():
    import io
    from contextlib import redirect_stderr
    stream = io.StringIO()
    with redirect_stderr(stream):
        yield stream


@contextmanager
def _capture_stdout():
    import io
    from contextlib import redirect_stdout
    with redirect_stdout(io.StringIO()):
        yield


if __name__ == "__main__":
    unittest.main()

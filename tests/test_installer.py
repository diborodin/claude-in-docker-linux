"""Versioned installation stays inside the selected temporary user roots."""

import dataclasses
import hashlib
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

from claude_docker import __version__
from claude_docker.installer import InstallLayout, install_release
from claude_docker.manifest import InstallManifest, InstallationRegistry


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_DATA_HOME": str(self.root / "data"),
                    "XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CACHE_HOME": str(self.root / "cache"),
                    "XDG_RUNTIME_DIR": str(runtime), "PATH": os.defpath,
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.layout = InstallLayout.user(self.env)

    def source_copy(self):
        source = self.root / "source"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        return source

    def test_layout_uses_exact_xdg_paths_without_creating_them(self):
        self.assertEqual(self.layout.root, self.root / "data/claude-docker")
        self.assertEqual(self.layout.command_link, self.home / ".local/bin/claude-docker")
        self.assertEqual(self.layout.releases_dir, self.root / "data/claude-docker/releases")
        self.assertEqual(self.layout.current_link, self.root / "data/claude-docker/current")
        self.assertEqual(self.layout.manifest_file("0.1.0"), self.root / "data/claude-docker/releases/0.1.0/manifest.json")
        self.assertEqual(self.layout.registry_file, self.root / "data/claude-docker/installation.json")
        for name in ("config", "state", "cache"):
            self.assertEqual(getattr(self.layout.host_paths, name + "_dir"), self.root / name / "claude-docker")
            self.assertFalse((self.root / name).exists())
        self.assertEqual(self.layout.host_paths.runtime_dir, self.root / "runtime/claude-docker")
        self.assertFalse(self.layout.host_paths.runtime_dir.exists())
        self.assertFalse(self.layout.root.exists())
        self.env["HOME"] = "/other"
        self.assertEqual(self.layout.command_link, self.home / ".local/bin/claude-docker")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.layout.root = Path("/other")

    def test_default_user_and_non_user_layouts_are_explicit(self):
        user = InstallLayout.user({"HOME": str(self.home)})
        self.assertEqual(user.root, self.home / ".local/share/claude-docker")
        dev = InstallLayout.development(self.root / "checkout", self.root / "bin", metadata_root=self.layout.root, host_paths=self.layout.host_paths)
        self.assertEqual(dev.mode, "dev")
        self.assertIs(dev.host_paths, self.layout.host_paths)
        self.assertEqual(dev.root, self.layout.root)
        self.assertIsNone(dev.current_link)
        system = InstallLayout.system(self.root / "prefix")
        self.assertEqual(system.mode, "system")
        self.assertEqual(system.root, self.root / "prefix/share/claude-docker")
        self.assertEqual(system.command_link, self.root / "prefix/bin/claude-docker")
        self.assertIsNone(system.host_paths)

    def test_version_file_agrees_with_package(self):
        self.assertEqual((ROOT / "VERSION").read_text().strip(), __version__)
        self.assertEqual(__version__, "0.2.0")

    def test_install_copies_runtime_bundle_with_verified_manifest_and_links(self):
        manifest = install_release(ROOT, self.layout, version=__version__)
        release = self.layout.release_dir(__version__)
        paths = {str(item.path) for item in manifest.files}
        self.assertTrue({"claude-docker", "install", "VERSION", "README.md", "LICENSE", ".dockerignore",
                         "Dockerfile", "compose.yaml", "compose.sysbox.yaml", "config.example.toml",
                         "src/claude_docker/cli.py", "src/claude_docker/installer.py",
                         "src/claude_docker/manifest.py", "image/mise.toml", "image/mise.lock", "image/customize.sh"} <= paths)
        self.assertEqual(manifest.version, __version__)
        self.assertEqual(manifest.mode, "user")
        for item in manifest.files:
            contents = (release / item.path).read_bytes()
            self.assertEqual(item.size, len(contents))
            self.assertEqual(item.sha256, hashlib.sha256(contents).hexdigest())
            self.assertEqual(stat.S_IMODE((release / item.path).stat().st_mode), item.mode)
            self.assertIn(item.mode, (0o644, 0o755))
        for name in ("claude-docker", "install", "image/customize.sh"):
            self.assertTrue(os.access(release / name, os.X_OK))
        self.assertEqual(self.layout.command_link.resolve(), release / "claude-docker")
        self.assertEqual(self.layout.current_link.resolve(), release)
        self.assertEqual(InstallManifest.read(self.layout.manifest_file(__version__)), manifest)
        registry = InstallationRegistry.read(self.layout.registry_file)
        self.assertEqual(registry, InstallationRegistry("user", __version__, (__version__,), self.layout.command_link, self.layout.current_link))
        for metadata in (self.layout.registry_file, self.layout.manifest_file(__version__)):
            self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
        self.assertNotIn("manifest.json", paths)
        self.assertFalse(self.layout.intent_file.exists())
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o700)
        self.assertFalse(self.layout.host_paths.config_dir.exists())
        self.assertFalse(self.layout.host_paths.state_dir.exists())
        self.assertFalse(self.layout.host_paths.cache_dir.exists())
        self.assertFalse(self.layout.host_paths.runtime_dir.exists())

    def test_policy_bundle_is_complete_readable_and_importable(self):
        manifest = install_release(ROOT, self.layout, version=__version__)
        self.assertEqual(len(manifest.files), 35)
        installed = self.layout.release_dir(__version__)
        policy = installed / 'security/desktop-seccomp-v0.2.4-linux-amd64.json'
        self.assertEqual(hashlib.sha256(policy.read_bytes()).hexdigest(),
                         'f245d64a3213793f730f394d44c507145b13ffc8d05acb21b5146413aa395a74')
        for item in manifest.files:
            if str(item.path).startswith('security/'):
                self.assertEqual(item.mode, 0o644)
        result = subprocess.run([sys.executable, '-c',
                                'from claude_docker.seccomp import chromium_policy_path; '
                                'from pathlib import Path; '
                                'print(chromium_policy_path(Path(".")))'],
                               cwd=installed, env=dict(self.env, PYTHONPATH=str(installed / 'src')),
                               text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(policy))

    def test_install_preserves_project_and_third_party_licenses(self):
        manifest = install_release(ROOT, self.layout, version=__version__)
        paths = {str(item.path) for item in manifest.files}
        self.assertIn('LICENSE', paths)
        release = self.layout.release_dir(__version__)
        for name in ('LICENSE', 'security/LICENSE.moby-profiles', 'security/NOTICE.moby-profiles'):
            with self.subTest(name=name):
                self.assertEqual((release / name).read_bytes(), (ROOT / name).read_bytes())
                self.assertEqual(stat.S_IMODE((release / name).stat().st_mode), 0o644)

    def test_new_release_bundles_importable_uninstall_module(self):
        manifest = install_release(ROOT, self.layout, version=__version__)
        self.assertIn(Path('src/claude_docker/uninstall.py'), {item.path for item in manifest.files})
        result = subprocess.run([str(self.layout.command_link), 'uninstall', '--help'],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('uninstall', result.stdout)

    def test_system_release_is_publicly_readable_with_private_stage_and_metadata(self):
        layout = InstallLayout.system(self.root / 'prefix')
        observed = []
        rename = os.replace
        def publish(source, destination):
            if Path(destination) == layout.release_dir(__version__):
                observed.append(stat.S_IMODE(Path(source).stat().st_mode))
            return rename(source, destination)
        with patch('claude_docker.installer.os.replace', publish):
            install_release(ROOT, layout, version=__version__)
        self.assertEqual(observed, [0o700])
        for directory in (layout.root, layout.releases_dir, layout.release_dir(__version__)):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        for metadata in (layout.registry_file, layout.manifest_file(__version__)):
            self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)

    def test_allowlist_excludes_state_history_tests_and_unknown_package_files(self):
        source = self.source_copy()
        forbidden = (
            'start.sh', 'tools.sh', 'claude-docker-session.md',
            '.git/config', 'tests/extra.py', 'docs/specs/spec.md', 'docs/plans/plan.md',
            '.scratch/notes.md', '.claude/credentials.json', '.claude.json',
            '.aws/credentials', '.config/Claude/session', 'config/token',
            'desktop-config/session', 'state/session', 'cache/token', 'workspaces/secret',
            'src/claude_docker/.aws/credentials', 'image/.claude/token',
            'src/claude_docker/secrets.txt', 'src/claude_docker/extra.py', 'image/secrets.txt',
            'security/extra.json',
        )
        for name in forbidden:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('not runtime')
        manifest = install_release(source, self.layout, version=__version__)
        paths = {str(item.path) for item in manifest.files}
        release = self.layout.release_dir(__version__)
        for name in forbidden:
            with self.subTest(name=name):
                self.assertNotIn(name, paths)
                self.assertFalse((release / name).exists(), name)
        expected = {
            '.dockerignore', 'claude-docker', 'install', 'VERSION', 'README.md', 'LICENSE',
            'Dockerfile', 'compose.yaml', 'compose.sysbox.yaml', 'config.example.toml',
            'image/customize.sh', 'image/mise.lock', 'image/mise.toml',
            'security/desktop-seccomp-v0.2.4-linux-amd64.json',
            'security/LICENSE.moby-profiles', 'security/NOTICE.moby-profiles',
        } | {f'src/claude_docker/{module}.py' for module in (
            '__init__', '__main__', 'checks', 'cli', 'clipboard', 'commands', 'compose',
            'config', 'desktop', 'environment', 'installer', 'manifest', 'model',
            'mounts', 'paths', 'process', 'seccomp', 'state', 'uninstall',
        )}
        self.assertEqual(paths, expected)
        self.assertEqual({str(path.relative_to(release)) for path in release.rglob('*')
                          if path.is_file()}, expected | {'manifest.json'})
        self.assertEqual({str(path.relative_to(release)) for path in release.rglob('*')
                          if path.is_dir()}, {'image', 'src', 'src/claude_docker', 'security'})

    def test_source_symlinks_are_rejected_including_directory_and_root(self):
        source = self.source_copy()
        original = source / "README.md"
        original.unlink()
        original.symlink_to(ROOT / "README.md")
        with self.assertRaisesRegex(ValueError, "symlink"):
            install_release(source, self.layout, version=__version__)
        original.unlink()
        original.write_text("readme")
        shutil.rmtree(source / "image")
        (source / "image").symlink_to(ROOT / "image", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            install_release(source, self.layout, version=__version__)
        link = self.root / "source-link"
        link.symlink_to(ROOT, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            install_release(link, self.layout, version=__version__)
        self.assertFalse(self.layout.current_link.exists())

    def test_missing_bundle_file_removes_only_own_stage(self):
        source = self.source_copy()
        (source / "compose.yaml").unlink()
        self.layout.releases_dir.mkdir(parents=True)
        unrelated = self.layout.releases_dir / ".unrelated"
        unrelated.mkdir()
        with self.assertRaises((ValueError, FileNotFoundError)):
            install_release(source, self.layout, version=__version__)
        self.assertEqual(list(self.layout.releases_dir.iterdir()), [unrelated])
        self.assertFalse(self.layout.registry_file.exists())

    def test_unsafe_version_cannot_escape_releases(self):
        for version in ("../escape", "", "/tmp/escape", ".", "a/b", "a\\b"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                install_release(ROOT, self.layout, version=version)
        self.assertFalse(self.layout.root.exists())

    def test_foreign_command_and_managed_directory_symlink_are_not_overwritten(self):
        self.layout.command_link.parent.mkdir(parents=True)
        self.layout.command_link.write_text("mine")
        with self.assertRaises((ValueError, FileExistsError)):
            install_release(ROOT, self.layout, version=__version__)
        self.assertEqual(self.layout.command_link.read_text(), "mine")
        self.layout.command_link.unlink()
        self.layout.root.parent.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside"
        outside.mkdir()
        self.layout.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            install_release(ROOT, self.layout, version=__version__)
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_dangerous_permissions_are_sanitized(self):
        source = self.source_copy()
        (source / "README.md").chmod(0o666)
        (source / "claude-docker").chmod(0o6777)
        install_release(source, self.layout, version=__version__)
        release = self.layout.release_dir(__version__)
        self.assertEqual(stat.S_IMODE((release / "README.md").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((release / "claude-docker").stat().st_mode), 0o755)

    def test_registry_failure_leaves_private_durable_ownership_intent(self):
        original_replace = os.replace
        def replace(source, destination):
            if Path(destination) == self.layout.registry_file:
                raise OSError("registry unavailable")
            return original_replace(source, destination)
        with patch("claude_docker.installer.os.replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "registry unavailable"):
                install_release(ROOT, self.layout, version=__version__)
        self.assertTrue(self.layout.release_dir(__version__).is_dir())
        self.assertFalse(self.layout.current_link.exists())
        self.assertEqual(stat.S_IMODE(self.layout.intent_file.stat().st_mode), 0o600)
        intent = json.loads(self.layout.intent_file.read_text())
        self.assertEqual(intent["version"], __version__)
        self.assertEqual(intent["release"], str(self.layout.release_dir(__version__)))
        self.assertEqual(intent["mode"], "user")

    def test_unsafe_existing_managed_directory_is_rejected_without_chmod(self):
        self.layout.root.mkdir(parents=True)
        self.layout.root.chmod(0o777)
        with self.assertRaises(ValueError):
            install_release(ROOT, self.layout, version=__version__)
        self.assertEqual(stat.S_IMODE(self.layout.root.stat().st_mode), 0o777)
        self.assertFalse(self.layout.registry_file.exists())

    def test_copy_failure_cleans_private_stage_without_removing_unrelated_stage(self):
        self.layout.releases_dir.mkdir(parents=True)
        self.layout.root.chmod(0o755)
        self.layout.releases_dir.chmod(0o755)
        unrelated = self.layout.releases_dir / ".unrelated"
        unrelated.mkdir()
        original_read = Path.read_bytes
        observed_modes = []
        def read(path):
            if path.name == "README.md" and self.layout.releases_dir in path.parents:
                observed_modes.append(stat.S_IMODE(path.parent.stat().st_mode))
                raise OSError("copy verification failed")
            return original_read(path)
        with patch.object(Path, "read_bytes", read):
            with self.assertRaisesRegex(OSError, "copy verification failed"):
                install_release(ROOT, self.layout, version=__version__)
        self.assertEqual(observed_modes, [0o700])
        self.assertEqual(list(self.layout.releases_dir.iterdir()), [unrelated])
        self.assertFalse(self.layout.intent_file.exists())

    def test_metadata_readers_reject_symlinks_unsafe_modes_and_malformed_records(self):
        install_release(ROOT, self.layout, version=__version__)
        for path, reader in ((self.layout.registry_file, InstallationRegistry.read),
                             (self.layout.manifest_file(__version__), InstallManifest.read)):
            with self.subTest(path=path):
                data = path.read_text()
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    reader(path)
                path.chmod(0o600)
                saved = path.with_suffix(".saved")
                path.rename(saved)
                path.symlink_to(saved)
                with self.assertRaises(ValueError):
                    reader(path)
                path.unlink()
                saved.rename(path)
                for record in ([], {"schema": 1}, {"schema": True}):
                    path.write_text(json.dumps(record))
                    with self.assertRaises(ValueError):
                        reader(path)
                path.write_text(data)

    def test_bootstrap_installs_without_shell_edits_and_prints_one_path_instruction(self):
        profile = self.home / ".profile"
        profile.write_text("# unchanged\n")
        result = subprocess.run([str(ROOT / "install")], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("PATH"), 1)
        self.assertIn(str(self.layout.command_link), result.stdout)
        self.assertIn("claude-docker check", result.stdout)
        self.assertEqual(profile.read_text(), "# unchanged\n")
        self.assertFalse((self.home / ".bashrc").exists())
        self.assertFalse(self.layout.host_paths.config_dir.exists())
        launched = subprocess.run([str(self.layout.command_link), "--version"], env=self.env, text=True, capture_output=True)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertIn(__version__, launched.stdout)

    def test_ordinary_installer_does_not_write_bytecode_into_source_or_release(self):
        source = self.source_copy()
        env = dict(self.env)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        result = subprocess.run([str(source / "install")], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(source.rglob("*.pyc")), [])
        release = self.layout.release_dir(__version__)
        manifest = InstallManifest.read(release / "manifest.json")
        expected_files = {item.path for item in manifest.files} | {Path("manifest.json")}
        self.assertEqual({path.relative_to(release) for path in release.rglob("*") if path.is_file()}, expected_files)

    def test_ordinary_source_and_installed_commands_create_no_unowned_bytecode(self):
        source = self.source_copy()
        manifest = install_release(source, self.layout, version=__version__)
        release = self.layout.release_dir(__version__)
        env = dict(self.env)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        for executable, bundle in ((source / "claude-docker", source),
                                   (self.layout.command_link, release)):
            with self.subTest(executable=executable):
                before = {path.relative_to(bundle) for path in bundle.rglob("*") if path.is_file()}
                result = subprocess.run([str(executable), "--version"], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(__version__, result.stdout)
                self.assertEqual(list(bundle.rglob("*.pyc")), [])
                self.assertEqual({path.relative_to(bundle) for path in bundle.rglob("*") if path.is_file()}, before)
        expected_files = {item.path for item in manifest.files} | {Path("manifest.json")}
        self.assertEqual({path.relative_to(release) for path in release.rglob("*") if path.is_file()}, expected_files)

    def test_bootstrap_does_not_print_path_instruction_when_bin_is_present(self):
        env = dict(self.env, PATH=str(self.layout.command_link.parent) + os.pathsep + os.defpath)
        result = subprocess.run([str(ROOT / "install")], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PATH", result.stdout)


if __name__ == "__main__":
    unittest.main()

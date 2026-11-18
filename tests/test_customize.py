"""Persistent customization through the launcher and private build inputs."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from claude_docker import __version__, cli
from claude_docker.image_tools import image_build_inputs
from claude_docker.installer import InstallLayout, install_release
from claude_docker.paths import resolve_host_paths


class CustomizeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {'HOME': str(self.root), 'XDG_CONFIG_HOME': str(self.root / 'config'),
                    'XDG_RUNTIME_DIR': str(self.root), 'PATH': os.defpath}
        self.paths = resolve_host_paths(self.env, os.getuid(), create_runtime=False)
        self.hook = self.root / 'config/claude-docker/customize.sh'
        self.errors = io.StringIO()

    def write_hook(self, contents):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_bytes(contents)

    def invoke_init(self, command=ROOT / 'claude-docker', env=None):
        return subprocess.run([sys.executable, str(command), 'customize', 'init'],
                              cwd=self.root, env=self.env if env is None else env,
                              capture_output=True, text=True, timeout=10)

    def test_init_creates_private_example_without_loading_configs(self):
        (self.root / 'broken.toml').write_text('invalid TOML [')
        env = dict(self.env, CLAUDE_CONFIG_FILE=str(self.root / 'broken.toml'),
                   CLAUDE_MISE_CONFIG_FILE=str(self.root / 'broken.toml'), PATH='')
        result = self.invoke_init(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.hook), result.stdout)
        self.assertEqual(stat.S_IMODE(self.hook.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.hook.parent.stat().st_mode), 0o700)
        self.assertEqual(self.hook.read_bytes(), (ROOT / 'image/customize.example.sh').read_bytes())
        self.assertFalse(self.paths.runtime_dir.exists())

    def test_init_never_overwrites_existing_paths(self):
        self.write_hook(b'edited script')
        existing = self.invoke_init()
        self.assertEqual(existing.returncode, 1, existing.stderr)
        self.assertEqual(self.hook.read_bytes(), b'edited script')
        self.hook.unlink()
        self.hook.mkdir()
        self.assertEqual(self.invoke_init().returncode, 1)
        self.assertTrue(self.hook.is_dir())
        self.hook.rmdir()
        target = self.root / 'missing-target'
        self.hook.symlink_to(target)
        self.assertEqual(self.invoke_init().returncode, 1)
        self.assertEqual(self.hook.readlink(), target)
        self.assertFalse(target.exists())

    def test_shared_bundle_init_uses_each_invocations_config_dir(self):
        layout = InstallLayout.system(self.root / 'prefix')
        install_release(ROOT, layout, version=__version__)
        release = layout.release_dir(__version__)
        before = {path: (path.stat().st_mtime_ns, path.read_bytes())
                  for path in release.rglob('*') if path.is_file()}
        for name in ('first', 'second'):
            home = self.root / name
            home.mkdir()
            env = dict(self.env, HOME=str(home), XDG_CONFIG_HOME=name)
            result = self.invoke_init(layout.command_link, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            hook = home / '.config/claude-docker/customize.sh'
            self.assertIn(str(hook), result.stdout)
            self.assertEqual(hook.read_bytes(), (ROOT / 'image/customize.example.sh').read_bytes())
        self.assertEqual({path: (path.stat().st_mtime_ns, path.read_bytes())
                          for path in release.rglob('*') if path.is_file()}, before)
        self.assertFalse(self.hook.exists())

    def test_missing_hook_does_not_add_a_context_file(self):
        self.hook.parent.mkdir(parents=True)
        self.hook.with_name('claude-code.version').write_text('2.1.326\n')
        with redirect_stderr(self.errors), image_build_inputs(ROOT, self.paths, self.env) as context:
            self.assertFalse((context / 'image/customize-user.sh').exists())
        self.assertFalse(context.exists())
        self.assertFalse(self.hook.exists())

    def test_build_all_reuses_exact_selected_bytes_and_cleans_both_contexts(self):
        marker = self.root / 'host-executed'
        original = f'touch "{marker}"\r\n'.encode() + b'# private script bytes\xff'
        actual = self.root / 'custom-script'
        actual.write_bytes(original)
        self.hook.parent.mkdir(parents=True)
        self.hook.symlink_to(actual)
        self.hook.with_name('claude-code.version').write_text('2.1.326\n')
        (self.hook.parent / 'private-token').write_text('must stay on host')
        contexts = []
        overrides = []
        services = []

        def run(runner, argv, **kwargs):
            self.assertEqual(argv[-2], 'build')
            service = argv[-1]
            services.append(service)
            override = Path(argv[argv.index('build') - 1])
            context = Path(json.loads(override.read_text())['services'][service]['build']['context'])
            contexts.append(context)
            overrides.append(override)
            self.assertEqual(stat.S_IMODE(context.stat().st_mode), 0o700)
            self.assertEqual({str(path.relative_to(context)) for path in context.rglob('*') if path.is_file()},
                             {'Dockerfile', '.dockerignore', 'image/mise.toml', 'image/mise.lock',
                              'image/customize.sh', 'image/customize-user.sh', 'image/claude-code.version'})
            snapshot = context / 'image/customize-user.sh'
            self.assertFalse(snapshot.is_symlink())
            self.assertEqual(snapshot.read_bytes(), original)
            if service == 'claude-cli':
                actual.write_bytes(b'edited after first build')
            else:
                raise subprocess.CalledProcessError(7, argv)
            return subprocess.CompletedProcess(argv, 0)

        with patch.dict(os.environ, self.env, clear=True), redirect_stderr(self.errors), \
                patch.object(cli.CommandRunner, 'run', run):
            self.assertEqual(cli.main(['build', '--all']), 1)
        self.assertEqual(services, ['claude-cli', 'claude-desktop'])
        self.assertEqual(contexts[0], contexts[1])
        self.assertTrue(all(not target.exists() for target in contexts + overrides))
        self.assertFalse(marker.exists())
        self.assertIn(str(self.hook), self.errors.getvalue())
        self.assertIn('Docker Compose failed (exit 7)', self.errors.getvalue())
        self.assertNotIn('private script bytes', self.errors.getvalue())

    def test_invalid_hook_fails_before_docker(self):
        self.hook.parent.mkdir(parents=True)
        for kind in ('directory', 'fifo', 'dangling', 'unreadable'):
            if kind == 'unreadable' and os.geteuid() == 0:
                continue  # Root bypasses file mode access checks.
            with self.subTest(kind=kind):
                if kind == 'directory':
                    self.hook.mkdir()
                elif kind == 'fifo':
                    os.mkfifo(self.hook)
                elif kind == 'dangling':
                    self.hook.symlink_to(self.root / 'absent')
                else:
                    self.write_hook(b'private content')
                    self.hook.chmod(0o000)

                errors = io.StringIO()
                docker = []

                def run(runner, argv, **kwargs):
                    docker.append(argv)
                    return subprocess.CompletedProcess(argv, 0)

                try:
                    with patch.dict(os.environ, self.env, clear=True), redirect_stderr(errors), \
                            redirect_stdout(io.StringIO()), patch.object(cli.CommandRunner, 'run', run):
                        status = cli.main(['build'])
                finally:
                    if kind == 'directory':
                        self.hook.rmdir()
                    else:
                        self.hook.unlink()
                self.assertEqual(status, 1)
                self.assertFalse(docker)
                self.assertIn(str(self.hook), errors.getvalue())
                self.assertNotIn('private content', errors.getvalue())

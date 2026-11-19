"""Locking snapshots exact inputs and publishes only validated output."""
from contextlib import redirect_stderr
import csv
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch
from tests.test_tool_config import ProfileFixture, NATIVE_LOCK
from tests.test_checks import Fixture, DockerRunner

ROOT = Path(__file__).resolve().parents[1]

class LockRunner:
    def __init__(self, test, *, fail=False, sidecar=False, change=False):
        self.test = test; self.calls = []; self.fail = fail; self.sidecar = sidecar; self.change = change
    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        mount = next(csv.reader([argv[argv.index('--mount') + 1]]))
        root = Path(next(v[7:] for v in mount if v.startswith('source=')))
        self.test.assertIn(sorted(p.name for p in root.iterdir()), (['mise.toml'], ['mise.lock', 'mise.toml']))
        self.test.assertNotEqual(root, self.test.paths.config_dir)
        self.test.assertNotIn('PRIVATE_TOKEN', kwargs['env'])
        self.test.assertEqual(argv[argv.index('--user') + 1], f'{os.getuid()}:{os.getgid()}')
        if self.change: (self.test.paths.config_dir / 'mise.toml').write_text('[tools]\nkubectl="1"')
        if self.fail: raise subprocess.CalledProcessError(7, argv, 'private-output')
        (root / 'mise.lock').write_bytes(NATIVE_LOCK.encode())
        if self.sidecar: (root / '.mise').mkdir()
        return subprocess.CompletedProcess(argv, 0, '', '')

class SnapshotFixture(ProfileFixture):
    def seed_code_version(self):
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.config_dir / 'claude-code.version').write_text('2.1.326\n')


class ImageToolsTests(SnapshotFixture):
    def test_python_helper_cache_is_platform_bound_and_excludes_host_credentials(self):
        from claude_docker.image_tools import _python_lock_image
        test = self
        class HelperRunner:
            built = False
            calls = []
            def run(inner, argv, **kwargs):
                inner.calls.append(list(argv))
                test.assertNotIn('PRIVATE_TOKEN', kwargs['env'])
                if argv[1] == 'version':
                    return subprocess.CompletedProcess(argv, 0, 'linux/arm64\n', '')
                if argv[1:3] == ['image', 'inspect']:
                    revision = argv[-1].split(':')[1]
                    return subprocess.CompletedProcess(argv, 0 if inner.built else 1,
                        'linux/arm64 ' + revision + ' sha256:' + 'a' * 64 if inner.built else '',
                        '' if inner.built else 'No such image')
                test.assertEqual(argv[1], 'build')
                test.assertEqual(argv[argv.index('--platform') + 1], 'linux/arm64')
                context = Path(argv[-1])
                test.assertEqual([p.name for p in context.iterdir()], ['Dockerfile'])
                text = (context / 'Dockerfile').read_text()
                test.assertIn('python:3.14-slim', text)
                test.assertIn('ghcr.io/astral-sh/uv:', text)
                inner.built = True
                return subprocess.CompletedProcess(argv, 0, '', '')
        runner = HelperRunner()
        env = dict(self.env, PRIVATE_TOKEN='secret-sentinel')
        first = _python_lock_image(ROOT, runner, env)
        second = _python_lock_image(ROOT, runner, env)
        self.assertEqual(first, second)
        self.assertEqual(sum(call[1] == 'build' for call in runner.calls), 1)

    def test_interrupted_graph_write_can_be_retried(self):
        from claude_docker.image_tools import _publish_graphs
        relative = Path('.mise/locks/claude-docker/test/uv.lock')
        code = '''import os,sys
from pathlib import Path
from claude_docker.image_tools import _publish_graphs
original = os.fdopen
class CrashWriter:
    def __init__(self, stream): self.stream=stream
    def __enter__(self): return self
    def __exit__(self,*args): self.stream.close()
    def write(self, raw):
        self.stream.write(raw[:3]); self.stream.flush(); os._exit(42)
os.fdopen=lambda *args,**kwargs: CrashWriter(original(*args,**kwargs))
_publish_graphs(Path(sys.argv[1]), {Path(sys.argv[2]): b'complete graph'})
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.root), str(relative)],
                                env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), timeout=5)
        self.assertEqual(result.returncode, 42)
        _publish_graphs(self.root, {relative: b'complete graph'})
        self.assertEqual((self.root / relative).read_bytes(), b'complete graph')

    def test_python_lock_publication_preserves_old_graph_and_build_snapshots_exact_files(self):
        from claude_docker.image_tools import lock_image_tools, image_build_inputs
        from claude_docker.tool_config import read_user_tools
        self.seed_code_version()
        source = self.write('[tools]\n"pipx:ansible-core"="latest"\n')
        class PythonRunner:
            revision = 1
            def run(inner, argv, **kwargs):
                fields = next(csv.reader([argv[argv.index('--mount') + 1]]))
                root = Path(next(value[7:] for value in fields if value.startswith('source=')))
                relative = '.mise/locks/pipx-ansible-core/2.22.0'
                if (root / 'mise.lock').exists():
                    relative = tomllib.loads((root / 'mise.lock').read_text())['tools']['pipx:ansible-core'][0]['uv']['path']
                directory = root / relative
                directory.mkdir(parents=True, exist_ok=True)
                graph = f'version=1\nrevision={inner.revision}\n'.encode()
                (directory / 'uv.lock').write_bytes(graph)
                (directory / 'pyproject.toml').write_text('[project]\nname="mise-tool"\nversion="0.0.0"\n')
                digest = hashlib.sha256(graph).hexdigest()
                (root / 'mise.lock').write_text('lockfile_version=4\n[[tools."pipx:ansible-core"]]\n'
                    'version="2.22.0"\nbackend="pipx:ansible-core"\nspecifiers=["latest"]\n'
                    'uv={path="' + relative + '",digest="sha256:' + digest + '"}\n')
                return subprocess.CompletedProcess(argv, 0, '', '')
        runner = PythonRunner()
        with patch('claude_docker.image_tools._python_lock_image', return_value='helper', create=True):
            lock_image_tools(ROOT, self.paths, runner, self.env)
            first = read_user_tools(self.paths, self.env, require_lock=True)
            runner.revision = 2
            lock_image_tools(ROOT, self.paths, runner, self.env, bump=True)
        second = read_user_tools(self.paths, self.env, require_lock=True)
        self.assertNotEqual(first.lock_bytes, second.lock_bytes)
        for name, raw in first.graphs.items():
            self.assertEqual((source.parent / name).read_bytes(), raw)
        with image_build_inputs(ROOT, self.paths, self.env) as context:
            expected = {context / 'image/mise-user-graphs' / Path(*name.parts[2:]): raw
                        for name, raw in second.graphs.items()}
            self.assertEqual({p: p.read_bytes() for p in (context / 'image/mise-user-graphs').rglob('*') if p.is_file()}, expected)
        project = next(name for name in second.graphs if name.name == 'pyproject.toml')
        (source.parent / project).write_bytes(second.graphs[project] + b'# changed project')
        with self.assertRaisesRegex(ValueError, 'digest'):
            read_user_tools(self.paths, self.env, require_lock=True)

    def setUp(self):
        super().setUp()
        self.stderr = io.StringIO()
        capture = redirect_stderr(self.stderr)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def base_checkout(self):
        checkout = self.root / 'checkout'
        (checkout / 'image').mkdir(parents=True)
        for name in ('Dockerfile', '.dockerignore', 'image/mise.toml', 'image/mise.lock', 'image/customize.sh',
                     'image/claude-code.version'):
            shutil.copyfile(ROOT / name, checkout / name)
        return checkout

    def no_docker(self):
        class RejectDocker:
            def run(inner, *args, **kwargs):
                self.fail('Empty base tools must not invoke Docker')
        return RejectDocker()

    def test_empty_base_lock_reconciles_removed_last_request_and_missing_lock(self):
        from claude_docker.image_tools import image_build_inputs, lock_image_tools
        self.seed_code_version()
        checkout = self.base_checkout()
        source = checkout / 'image/mise.toml'
        lock = source.with_suffix('.lock')
        for prior in (NATIVE_LOCK.encode(), None):
            with self.subTest(prior='populated' if prior else 'missing'):
                source.write_bytes(b'[tools]\nkubectl="latest"\n')
                lock.write_bytes(NATIVE_LOCK.encode())
                # Remove the final artifact but retain all approved raw settings/packages.
                declaration = b'[bootstrap.packages]\n"apt:jq"="latest"\n[tools]\n[settings]\nlockfile=true\n'
                source.write_bytes(declaration)
                if prior is None:
                    lock.unlink()
                lock_image_tools(checkout, self.paths, self.no_docker(), self.env, base=True)
                with image_build_inputs(checkout, self.paths, self.env) as context:
                    self.assertEqual((context / 'image/mise.toml').read_bytes(), declaration)
                    self.assertEqual((context / 'image/mise.lock').read_bytes(), lock.read_bytes())
                self.assertEqual(source.read_bytes(), declaration)
                self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_empty_base_publication_failure_preserves_source_and_previous_lock(self):
        from claude_docker.image_tools import lock_image_tools
        checkout = self.base_checkout()
        source = checkout / 'image/mise.toml'
        declaration = b'[tools]\n'
        source.write_bytes(declaration)
        lock = source.with_suffix('.lock')
        for prior in (NATIVE_LOCK.encode(), None):
            with self.subTest(prior='populated' if prior else 'missing'):
                if prior is None:
                    lock.unlink(missing_ok=True)
                else:
                    lock.write_bytes(prior)
                with patch('claude_docker.image_tools.os.replace', side_effect=OSError('publication failed')):
                    with self.assertRaisesRegex(OSError, 'publication failed'):
                        lock_image_tools(checkout, self.paths, self.no_docker(), self.env, base=True)
                self.assertEqual(source.read_bytes(), declaration)
                self.assertEqual(lock.read_bytes() if lock.exists() else None, prior)
                self.assertEqual(list(lock.parent.glob('.mise-lock-*')), [])

    def test_empty_base_checks_concurrent_changes_and_alias_before_publication(self):
        from claude_docker.image_tools import lock_image_tools
        from claude_docker.tool_config import validate_distinct_tool_paths
        checkout = self.base_checkout()
        source = checkout / 'image/mise.toml'
        lock = source.with_suffix('.lock')
        declaration = b'[tools]\n'
        for change, message in (('source', 'profile changed'), ('lock', 'lock changed'), ('alias', 'distinct')):
            with self.subTest(change=change):
                source.unlink()
                source.write_bytes(declaration)
                lock.write_text(NATIVE_LOCK)
                checks = 0
                def edit_before_check(selected, destination):
                    nonlocal checks
                    checks += 1
                    if checks == 2:
                        if change == 'source': source.write_bytes(declaration + b'# concurrent edit\n')
                        elif change == 'lock': lock.write_bytes(b'# concurrent lock edit\n')
                        else:
                            lock.write_bytes(declaration)
                            source.unlink()
                            source.symlink_to(lock)
                    validate_distinct_tool_paths(selected, destination)
                with patch('claude_docker.image_tools.validate_distinct_tool_paths', side_effect=edit_before_check):
                    with self.assertRaisesRegex(ValueError, message):
                        lock_image_tools(checkout, self.paths, self.no_docker(), self.env, base=True)
                self.assertEqual(source.read_bytes(), declaration + b'# concurrent edit\n' if change == 'source' else declaration)
                self.assertEqual(lock.read_bytes(), b'# concurrent lock edit\n' if change == 'lock' else
                                 declaration if change == 'alias' else NATIVE_LOCK.encode())

    def test_colliding_sources_never_reach_docker_or_change_declarations(self):
        from claude_docker.image_tools import lock_image_tools
        for alias in (False, True):
            with self.subTest(alias=alias):
                source = self.root / ('alias.toml' if alias else 'custom.lock')
                destination = source.with_suffix('.lock')
                declaration = b'[tools]\nkubectl="latest"\n'
                destination.write_bytes(declaration)
                if alias: source.symlink_to(destination)
                runner = LockRunner(self)
                with self.assertRaisesRegex(ValueError, 'distinct'):
                    lock_image_tools(ROOT, self.paths, runner,
                                     dict(self.env, CLAUDE_MISE_CONFIG_FILE=str(source)))
                self.assertEqual(runner.calls, [])
                self.assertEqual(source.read_bytes(), declaration)
                self.assertEqual(destination.read_bytes(), declaration)

    def test_source_alias_created_during_resolution_preserves_declarations(self):
        from claude_docker.image_tools import lock_image_tools
        source = self.write('[tools]\nkubectl="latest"')
        declaration = source.read_bytes()
        source.with_suffix('.lock').write_bytes(declaration)
        class AliasRunner(LockRunner):
            def run(inner, argv, **kwargs):
                result = super().run(argv, **kwargs)
                source.unlink()
                source.symlink_to(source.with_suffix('.lock'))
                return result
        with self.assertRaisesRegex(ValueError, 'distinct'):
            lock_image_tools(ROOT, self.paths, AliasRunner(self), self.env)
        self.assertEqual(source.read_bytes(), declaration)
        self.assertTrue(source.is_symlink())

    def test_lock_atomic_success_and_failure_preservation(self):
        from claude_docker.image_tools import lock_image_tools
        p = self.write('[tools]\nkubectl="latest"')
        runner = LockRunner(self)
        lock_image_tools(ROOT, self.paths, runner, dict(self.env, PRIVATE_TOKEN='secret'), bump=True)
        old = p.with_suffix('.lock').read_bytes()
        self.assertIn('--bump', runner.calls[0][0])
        self.assertEqual(p.with_suffix('.lock').stat().st_mode & 0o777, 0o600)
        for options in ({'fail': True}, {'sidecar': True}, {'change': True}):
            p.with_suffix('.lock').unlink(missing_ok=True)
            # Fresh request snapshot isolates each failure path.
            p.write_text('[tools]\nkubectl="latest"')
            with self.subTest(options=options), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                lock_image_tools(ROOT, self.paths, LockRunner(self, **options), self.env)
            self.assertFalse(p.with_suffix('.lock').exists())
        p.write_text('[tools]\nkubectl="latest"')
        p.with_suffix('.lock').write_bytes(old)
        with self.assertRaises(subprocess.CalledProcessError):
            lock_image_tools(ROOT, self.paths, LockRunner(self, fail=True), self.env)
        self.assertEqual(p.with_suffix('.lock').read_bytes(), old)
        self.assertTrue(old)
        self.assertNotIn(str(p), self.stderr.getvalue())
        lock_image_tools(ROOT, self.paths, LockRunner(self), self.env, report_paths=True)
        self.assertIn(str(p), self.stderr.getvalue())
        self.assertNotIn('private-output', self.stderr.getvalue())

    def test_no_default_and_apt_only_do_not_run_docker(self):
        from claude_docker.image_tools import lock_image_tools
        runner = DockerRunner()
        lock_image_tools(ROOT, self.paths, runner, self.env)
        source = self.write('[bootstrap.packages]\n"apt:htop"="latest"')
        lock_image_tools(ROOT, self.paths, runner, self.env)
        self.assertFalse(source.with_suffix('.lock').exists())
        source.with_suffix('.lock').write_text(NATIVE_LOCK)
        lock_image_tools(ROOT, self.paths, runner, self.env)
        self.assertEqual(source.with_suffix('.lock').read_bytes(), NATIVE_LOCK.encode())
        self.assertEqual(runner.calls, [])
        self.assertIn('tools init', self.stderr.getvalue())
        self.assertIn('no artifact requests', self.stderr.getvalue())

    def test_installed_base_lock_rejected_before_docker(self):
        from claude_docker.image_tools import lock_image_tools
        (self.root / 'manifest.json').write_text('{}')
        runner = DockerRunner()
        with self.assertRaisesRegex(ValueError, 'source/development'):
            lock_image_tools(self.root, self.paths, runner, self.env, base=True)
        self.assertEqual(runner.calls, [])

class ToolsCliTests(Fixture):
    def test_init_ignores_broken_launcher_config(self):
        self.env['CLAUDE_CONFIG_FILE'] = str(self.root / 'broken')
        Path(self.env['CLAUDE_CONFIG_FILE']).write_text('not valid toml')
        rc, out, err = self.invoke(['tools', 'init'], DockerRunner())
        self.assertEqual(rc, 0, err)
        p = Path(self.env['HOME']) / '.config/claude-docker/mise.toml'
        self.assertTrue(p.is_file())
        rc, _, _ = self.invoke(['tools', 'init'], DockerRunner())
        self.assertEqual(rc, 1)
    def test_missing_default_is_noop_with_guidance(self):
        runner = DockerRunner(); rc, _, err = self.invoke(['tools', 'lock'], runner)
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.calls, [])
        self.assertIn('tools init', err)

class BuildInputsTests(SnapshotFixture):
    def setUp(self):
        super().setUp()
        self.seed_code_version()

    def context(self, env=None):
        from claude_docker import image_tools
        self.assertTrue(hasattr(image_tools, 'image_build_inputs'), 'exact build snapshots are missing')
        return image_tools.image_build_inputs(ROOT, self.paths, self.env if env is None else env)

    def test_exact_private_snapshot_raw_bytes_and_cleanup_on_failure(self):
        source = self.write('# raw bytes\n[tools]\nkubectl="latest"\n')
        source.with_suffix('.lock').write_text(NATIVE_LOCK)
        (source.parent / 'secret').write_text('credential')
        (source.parent / 'linked-directory').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with self.context() as context:
                self.assertEqual(context.stat().st_mode & 0o777, 0o700)
                files = {str(p.relative_to(context)) for p in context.rglob('*') if p.is_file()}
                self.assertEqual(files, {'Dockerfile', '.dockerignore', 'image/mise.toml', 'image/mise.lock',
                                         'image/customize.sh', 'image/mise-user.toml', 'image/mise-user.lock',
                                         'image/claude-code.version'})
                self.assertEqual((context / 'image/claude-code.version').read_bytes(), b'2.1.326\n')
                self.assertEqual((context / 'image/mise-user.toml').read_bytes(), source.read_bytes())
                self.assertEqual((context / 'image/mise-user.lock').read_bytes(), source.with_suffix('.lock').read_bytes())
                self.assertEqual((context / 'image/mise.toml').read_bytes(), (ROOT / 'image/mise.toml').read_bytes())
                raise RuntimeError('synthetic')
        self.assertFalse(context.exists())

    def test_missing_stale_and_unchanged_locks(self):
        source = self.write('[tools]\nkubectl="latest"')
        with self.assertRaisesRegex(ValueError, 'Missing tool lock'):
            with self.context(): pass
        source.with_suffix('.lock').write_text(NATIVE_LOCK)
        source.write_text('# comment\n[tools]\nkubectl="latest"\n[bootstrap.packages]\n"apt:ripgrep"="latest"')
        with self.context(): pass
        source.write_text('[tools]\nkubectl="1"')
        with self.assertRaisesRegex(ValueError, 'Stale tool lock'):
            with self.context(): pass

    def test_missing_default_apt_only_and_explicit_missing(self):
        with self.context() as context:
            self.assertFalse((context / 'image/mise-user.toml').exists())
        source = self.write('[bootstrap.packages]\n"apt:ripgrep"="latest"')
        with self.context() as context:
            self.assertEqual((context / 'image/mise-user.toml').read_bytes(), source.read_bytes())
            self.assertFalse((context / 'image/mise-user.lock').exists())
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            with self.context(dict(self.env, CLAUDE_MISE_CONFIG_FILE=str(self.root / 'missing'))): pass

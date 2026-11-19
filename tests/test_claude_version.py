"""Claude Code pin selection, publication and build integration."""

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from http.client import HTTPResponse
import io
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.response import addinfourl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from claude_docker import cli
from claude_docker.paths import resolve_host_paths


class ReleaseResponse(io.BytesIO):
    length = None
    status = 200

    def __init__(self, body, url='https://downloads.claude.ai/claude-code-releases/latest'):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


def framed_response(headers, body, *, status=200):
    class BoundedBody(io.BytesIO):
        def read(self, size=-1):
            if not 0 <= size <= 129:
                raise AssertionError(f'Unbounded metadata response read: {size}')
            return super().read(size)

    class Socket:
        def makefile(self, mode):
            return BoundedBody(f'HTTP/1.1 {status} Fixture\r\n'.encode() + headers + b'\r\n' + body)

    response = HTTPResponse(Socket())
    response.url = 'https://downloads.claude.ai/claude-code-releases/latest'
    response.begin()
    return response


class ClaudeVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {'HOME': str(self.root), 'PATH': os.defpath,
                    'XDG_CONFIG_HOME': str(self.root / 'config')}
        self.paths = resolve_host_paths(self.env, os.getuid(), create_runtime=False)
        self.pin = self.root / 'config/claude-docker/claude-code.version'
        self.shipped = ROOT / 'image/claude-code.version'
        self.errors = io.StringIO()
        self.builds = []

    def version_api(self):
        self.assertTrue((ROOT / 'src/claude_docker/claude_version.py').is_file(),
                        'Claude Code version selection is missing')
        from claude_docker import claude_version
        return claude_version

    def write_pin(self, value):
        self.pin.parent.mkdir(parents=True, exist_ok=True)
        self.pin.write_bytes(value)

    def run_metadata_worker(self, argv, **kwargs):
        self.assertEqual(argv[0], sys.executable)
        output = io.StringIO()
        with redirect_stdout(output):
            status = self.version_api()._metadata_worker()
        result = subprocess.CompletedProcess(argv, status, output.getvalue())
        if kwargs.get('check', True):
            result.check_returncode()
        return result

    def build(self, arguments, response=None, *, fail=False, edit_after_cli=False):
        if '--desktop' in arguments or '--all' in arguments:
            self.pin.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.pin.with_name('claude-desktop.version').write_text('5.11492.5\n')

        def request(url, data=None, timeout=None):
            self.assertEqual(url, 'https://downloads.claude.ai/claude-code-releases/latest')
            self.assertGreater(timeout, 0)
            if isinstance(response, Exception):
                raise response
            self.assertIsNotNone(response, 'Ordinary builds must not request metadata')
            return ReleaseResponse(response) if isinstance(response, bytes) else response

        def run(runner, argv, **kwargs):
            if argv[0] == sys.executable:
                return self.run_metadata_worker(argv, **kwargs)
            service = argv[-1]
            index = argv.index('build')
            override = json.loads(Path(argv[index - 1]).read_text())
            context = Path(override['services'][service]['build']['context'])
            pin_file = context / 'image/claude-code.version'
            version = pin_file.read_bytes() if pin_file.exists() else None
            selected = self.pin.read_bytes() if self.pin.is_file() else None
            self.builds.append((service, context, version, selected, argv[index + 1:-1]))
            if edit_after_cli and service == 'claude-cli':
                self.pin.write_bytes(b'2.2.2\n')
            if fail:
                raise subprocess.CalledProcessError(7, argv)
            return subprocess.CompletedProcess(argv, 0)

        with patch.dict(os.environ, self.env, clear=True), redirect_stderr(self.errors), \
                patch('urllib.request.OpenerDirector.open', side_effect=request) as fetch, \
                patch.object(cli.CommandRunner, 'run', run):
            try:
                result = cli.main(arguments)
            except SystemExit as error:
                result = error.code
        self.assertEqual(fetch.call_count, int(response is not None))
        return result

    def test_first_ordinary_build_saves_latest_once_for_both_products(self):
        self.assertEqual(self.build(['build', '--all'], b'2.2.1\n'), 0, self.errors.getvalue())
        self.assertEqual([entry[0] for entry in self.builds], ['claude-cli', 'claude-desktop'])
        self.assertEqual({entry[1] for entry in self.builds}, {self.builds[0][1]})
        self.assertEqual([entry[2:4] for entry in self.builds], [(b'2.2.1\n', b'2.2.1\n')] * 2)
        self.assertEqual(self.pin.read_bytes(), b'2.2.1\n')
        self.assertEqual(self.build(['build']), 0, self.errors.getvalue())
        self.assertEqual(self.builds[-1][2], b'2.2.1\n')
        self.assertEqual(self.shipped.read_bytes(), b'latest\n')

    def test_ordinary_build_snapshots_existing_pin_without_fetch(self):
        self.write_pin(b' \t2.2.0\r\n')
        config = self.root / 'other.toml'
        config.write_text('')
        self.env['CLAUDE_CONFIG_FILE'] = str(config)
        self.assertEqual(self.build(['build', '--desktop']), 0, self.errors.getvalue())
        self.assertEqual(self.builds[-1][0], 'claude-desktop')
        self.assertEqual(self.builds[-1][2:], (b'2.2.0\n', b' \t2.2.0\r\n', []))

    def test_failed_first_lookup_does_not_create_a_pin_or_build(self):
        self.assertEqual(self.build(['build'], TimeoutError('private metadata error')), 1)
        self.assertFalse(self.pin.parent.exists())
        self.assertFalse(self.builds)

    def test_update_publishes_private_pin_and_builds_both_products_from_one_snapshot(self):
        self.assertEqual(self.build(['build', '--all', '--update-claude'], b'2.2.1\n', edit_after_cli=True),
                         0, self.errors.getvalue())
        self.assertEqual([service for service, *_ in self.builds], ['claude-cli', 'claude-desktop'])
        paths = {context for _, context, *_ in self.builds}
        self.assertEqual(len(paths), 1)
        self.assertFalse(next(iter(paths)).exists())
        self.assertEqual([snapshot for _, _, snapshot, *_ in self.builds], [b'2.2.1\n'] * 2)
        self.assertEqual(self.builds[0][3], b'2.2.1\n')
        self.assertEqual(stat.S_IMODE(self.pin.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.pin.parent.stat().st_mode), 0o700)

    def test_rebuild_update_persists_selection_on_docker_failure(self):
        self.assertEqual(self.build(['rebuild', '--desktop', '--update-claude'], b'2.2.0', fail=True),
                         1, self.errors.getvalue())
        self.assertIn('Docker Compose failed (exit 7)', self.errors.getvalue())
        service, _, snapshot, selected, options = self.builds[-1]
        self.assertEqual(service, 'claude-desktop')
        self.assertEqual((snapshot, selected), (b'2.2.0\n', b'2.2.0\n'))
        self.assertCountEqual(options, ['--pull', '--no-cache'])
        self.assertEqual(self.pin.read_bytes(), selected)
        self.assertEqual(self.build(['build']), 0, self.errors.getvalue())
        self.assertEqual(self.builds[-1][2], b'2.2.0\n')

    def test_equal_or_older_latest_keeps_existing_pin_bytes_and_metadata(self):
        self.write_pin(b' \t2.10.0\n\n')
        self.pin.chmod(0o640)
        initial = self.pin.stat()
        for metadata in (b'2.10.0', b'2.9.99'):
            with self.subTest(metadata=metadata):
                self.assertEqual(self.build(['build', '--update-claude'], metadata), 0, self.errors.getvalue())
                self.assertEqual(self.builds[-1][2], b'2.10.0\n')
                current = self.pin.stat()
                self.assertEqual((current.st_ino, current.st_mtime_ns, current.st_mode),
                                 (initial.st_ino, initial.st_mtime_ns, initial.st_mode))
                self.assertEqual(self.pin.read_bytes(), b' \t2.10.0\n\n')

    def test_invalid_metadata_and_network_errors_preserve_pin_and_prevent_docker(self):
        self.write_pin(b'2.2.0\n')
        for response in (b'', b'v2.2.1', b'2.2.1+private-response', b'2.2.1\0',
                         '２.2.1'.encode(), b'2.2.1' + b' ' * 256,
                         URLError('private-proxy-detail'), TimeoutError('private-timeout-detail'),
                         ValueError('private-invalid-proxy-url')):
            with self.subTest(response=response):
                self.assertEqual(self.build(['build', '--update-claude'], response), 1, self.errors.getvalue())
                self.assertIn('Claude Code', self.errors.getvalue())
                self.assertNotIn('private-', self.errors.getvalue())
                self.assertEqual(self.pin.read_bytes(), b'2.2.0\n')
                self.assertEqual(list(self.pin.parent.iterdir()), [self.pin])
                self.assertFalse(self.builds)

    def test_metadata_failures_report_safe_categories(self):
        self.write_pin(b'2.2.0\n')
        for error, diagnostic in (
            (URLError(ssl.SSLCertVerificationError('private-tls-detail')), 'TLS certificate verification failed'),
            (URLError(TimeoutError('private-timeout-detail')), 'request timed out'),
            (ValueError('private-proxy-detail'), 'network request failed'),
            (HTTPError('https://example.invalid/private-url', 503, 'private-reason', {}, None), 'HTTP status 503'),
        ):
            with self.subTest(diagnostic=diagnostic):
                self.errors.seek(0)
                self.errors.truncate()
                self.assertEqual(self.build(['build', '--update-claude'], error), 1)
                self.assertIn(diagnostic, self.errors.getvalue())
                self.assertNotIn('private-', self.errors.getvalue())
                self.assertEqual(self.pin.read_bytes(), b'2.2.0\n')
                self.assertFalse(self.builds)

    def test_incomplete_http_metadata_preserves_pin_and_prevents_build(self):
        self.write_pin(b'2.2.0\n')
        self.pin.chmod(0o640)
        before = self.pin.stat()
        for headers, body in (
            (b'Content-Length: 13\r\n', b'2.3.4'),
            (b'Content-Length: 512\r\n', b'2.3.4'),
            (b'Transfer-Encoding: chunked\r\n', b'5\r\n2.3.4\r\n'),
        ):
            with self.subTest(headers=headers):
                self.errors.seek(0)
                self.errors.truncate()
                with framed_response(headers, body) as response:
                    self.assertEqual(self.build(['build', '--update-claude'], response),
                                     1, self.errors.getvalue())
                self.assertIn('incomplete HTTP response', self.errors.getvalue())
                self.assertNotIn('2.3.4', self.errors.getvalue())
                self.assertFalse(self.builds)
                after = self.pin.stat()
                self.assertEqual((after.st_ino, after.st_mode, after.st_mtime_ns),
                                 (before.st_ino, before.st_mode, before.st_mtime_ns))
                self.assertEqual(self.pin.read_bytes(), b'2.2.0\n')

    def test_complete_http_metadata_accepts_fixed_streamed_and_chunked_bodies(self):
        version_api = self.version_api()
        for headers, body in (
            (b'Content-Length: 128\r\n', b'2.3.4' + b' ' * 123),
            (b'', b'2.3.4'),
            (b'Transfer-Encoding: chunked\r\nContent-Length: 12\r\n',
             b'2\r\n2.\r\n3\r\n3.4\r\n0\r\n\r\n'),
        ):
            with self.subTest(headers=headers), framed_response(headers, body) as response, \
                    patch('urllib.request.OpenerDirector.open', return_value=response), redirect_stderr(self.errors):
                self.assertEqual(version_api._fetch_latest_version(), '2.3.4')

    def test_other_success_statuses_are_rejected_before_reading_metadata(self):
        version_api = self.version_api()
        for status in (206, 201, 202, 204):
            with self.subTest(status=status), framed_response(b'Content-Length: 5\r\n', b'2.3.4',
                                                               status=status) as response, \
                    patch('urllib.request.OpenerDirector.open', return_value=response), \
                    patch.object(response, 'read', wraps=response.read) as read, redirect_stderr(self.errors):
                with self.assertRaisesRegex(ValueError, f'HTTP status {status}'):
                    version_api._fetch_latest_version()
                read.assert_not_called()

    def test_partial_http_metadata_preserves_pin_and_prevents_build(self):
        self.write_pin(b' \t2.2.0\r\n')
        self.pin.chmod(0o640)
        before = self.pin.stat()
        with framed_response(b'Content-Length: 5\r\nContent-Range: bytes 0-4/10\r\n', b'2.3.4',
                             status=206) as response:
            self.assertEqual(self.build(['build', '--update-claude'], response), 1, self.errors.getvalue())
        self.assertIn('HTTP status 206', self.errors.getvalue())
        self.assertFalse(self.builds)
        after = self.pin.stat()
        self.assertEqual((after.st_ino, after.st_mode, after.st_mtime_ns),
                         (before.st_ino, before.st_mode, before.st_mtime_ns))
        self.assertEqual(self.pin.read_bytes(), b' \t2.2.0\r\n')

    def test_partial_http_metadata_does_not_create_first_pin_or_build(self):
        with framed_response(b'Content-Length: 5\r\nContent-Range: bytes 0-4/10\r\n', b'2.3.4',
                             status=206) as response:
            self.assertEqual(self.build(['build'], response), 1, self.errors.getvalue())
        self.assertIn('HTTP status 206', self.errors.getvalue())
        self.assertFalse(self.pin.parent.exists())
        self.assertFalse(self.builds)

    def test_oversized_http_metadata_stays_bounded(self):
        version_api = self.version_api()
        with framed_response(b'Content-Length: 512\r\n', b'2.3.4' + b' ' * 507) as response, \
                patch('urllib.request.OpenerDirector.open', return_value=response), redirect_stderr(self.errors):
            with self.assertRaisesRegex(ValueError, 'Invalid Claude Code version'):
                version_api._fetch_latest_version()

    def test_metadata_redirect_to_http_is_rejected(self):
        self.write_pin(b'2.2.0\n')
        response = ReleaseResponse(b'2.3.0', 'http://untrusted.test/latest')
        self.assertEqual(self.build(['build', '--update-claude'], response), 1, self.errors.getvalue())
        self.assertEqual(self.pin.read_bytes(), b'2.2.0\n')
        self.assertFalse(self.builds)

    def test_metadata_rejects_http_redirect_before_requesting_the_next_hop(self):
        version_api = self.version_api()
        calls = []

        def response(request, code, location=None):
            headers = Message()
            if location:
                headers['Location'] = location
            result = addinfourl(io.BytesIO(b'2.2.3'), headers, request.full_url, code)
            result.msg = 'fixture'
            return result

        def https(handler, request):
            calls.append(request.full_url)
            if request.full_url.endswith('/latest'):
                return response(request, 302, 'http://example.invalid/insecure')
            return response(request, 200)

        def http(handler, request):
            calls.append(request.full_url)
            return response(request, 302, 'https://example.invalid/secure')

        with patch('urllib.request._opener', None), patch('urllib.request.HTTPSHandler.https_open', https), \
                patch('urllib.request.HTTPHandler.http_open', http):
            with self.assertRaisesRegex(ValueError, 'redirect refused'):
                version_api._fetch_latest_version()
        self.assertEqual(calls, ['https://downloads.claude.ai/claude-code-releases/latest'])

    def test_metadata_rejects_https_redirect_without_reading_its_body(self):
        class RedirectBody(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError(f'Redirect handler attempted a body read with size {size!r}')

        self.write_pin(b'2.2.0\n')
        url = 'https://downloads.claude.ai/claude-code-releases/latest'
        headers = Message()
        headers['Location'] = 'https://example.invalid/private-redirect'
        with RedirectBody(b'2.3.0') as body:
            response = addinfourl(body, headers, url, 302)
            response.msg = 'private-redirect'
            with patch.dict(os.environ, self.env, clear=True), redirect_stderr(self.errors), \
                    patch('urllib.request.HTTPSHandler.https_open', return_value=response) as https, \
                    patch.object(cli.CommandRunner, 'run', side_effect=self.run_metadata_worker):
                self.assertEqual(cli.main(['build', '--update-claude']), 1)
            self.assertTrue(body.closed)
        self.assertEqual([call.args[0].full_url for call in https.call_args_list], [url])
        self.assertIn('redirect refused', self.errors.getvalue())
        self.assertNotIn('private-redirect', self.errors.getvalue())
        self.assertEqual(self.pin.read_bytes(), b'2.2.0\n')

    def test_config_alias_cannot_publish_a_pin_in_a_checkout_or_installed_bundle(self):
        from claude_docker.installer import InstallLayout, install_release

        version_api = self.version_api()
        checkout = self.root / 'checkout'
        (checkout / 'image').mkdir(parents=True)
        (checkout / 'image/claude-code.version').write_bytes(b'2.1.326\n')
        layout = InstallLayout.user(self.env)
        install_release(ROOT, layout, version='0.4.0')
        installed = layout.release_dir('0.4.0')
        self.pin.parent.parent.mkdir(parents=True)
        for program in (checkout, installed):
            with self.subTest(program=program):
                shipped = program / 'image/claude-code.version'
                before = shipped.read_bytes(), shipped.stat().st_ino
                for target in (program / 'image', program):
                    self.pin.parent.symlink_to(target)
                    try:
                        with patch.object(version_api, '_latest_version', return_value='2.2.3') as fetch:
                            with self.assertRaisesRegex(ValueError, 'program'):
                                version_api.update_code_version(program, self.paths)
                            if target == program:
                                with self.assertRaisesRegex(ValueError, 'program'):
                                    version_api.read_code_version(program, self.paths)
                            fetch.assert_not_called()
                        self.assertEqual((shipped.read_bytes(), shipped.stat().st_ino), before)
                        self.assertFalse((program / self.pin.name).exists())
                    finally:
                        self.pin.parent.unlink()

    def test_non_advancing_update_rechecks_the_file_and_configuration_directory(self):
        version_api = self.version_api()
        self.write_pin(b'2.3.0\n')

        def edit_pin(*args, **kwargs):
            self.pin.write_bytes(b'2.2.0\n')
            return '2.2.0'

        with patch.object(version_api, '_latest_version', side_effect=edit_pin):
            with self.assertRaisesRegex(ValueError, 'changed'):
                version_api.update_code_version(ROOT, self.paths)
        self.assertEqual(self.pin.read_bytes(), b'2.2.0\n')

        def move_directory(*args, **kwargs):
            self.pin.parent.rename(self.root / 'moved-config')
            return '2.2.0'

        with patch.object(version_api, '_latest_version', side_effect=move_directory):
            with self.assertRaisesRegex(ValueError, 'changed'):
                version_api.update_code_version(ROOT, self.paths)
        self.assertEqual((self.root / 'moved-config' / self.pin.name).read_bytes(), b'2.2.0\n')

    def test_both_builds_use_the_returned_selection_if_pin_changes_after_publication(self):
        original_replace = os.replace
        for arguments in (['build', '--all', '--update-claude'], ['build', '--all']):
            with self.subTest(arguments=arguments):
                self.pin.unlink(missing_ok=True)
                self.builds.clear()

                def replace(source, destination, **kwargs):
                    original_replace(source, destination, **kwargs)
                    if destination == self.pin.name:
                        self.pin.write_bytes(b'2.3.0\n')

                with patch('os.replace', side_effect=replace):
                    self.assertEqual(self.build(arguments, b'2.2.3'), 0, self.errors.getvalue())
                self.assertEqual([entry[2] for entry in self.builds], [b'2.2.3\n'] * 2)
                self.assertEqual(self.pin.read_bytes(), b'2.3.0\n')
                self.assertIn('Claude Code version: 2.2.3', self.errors.getvalue())

    def test_invalid_pin_is_rejected_before_docker_or_update_lookup(self):
        self.write_pin(b'invalid-private-pin')
        for args in (['build'], ['build', '--update-claude']):
            self.assertEqual(self.build(args), 1, self.errors.getvalue())
            self.assertIn(str(self.pin), self.errors.getvalue())
            self.assertNotIn('invalid-private-pin', self.errors.getvalue())
        self.pin.unlink()
        for kind in ('dangling', 'directory', 'fifo', 'unreadable'):
            if kind == 'unreadable' and os.geteuid() == 0:
                continue
            with self.subTest(kind=kind):
                if kind == 'dangling':
                    self.pin.symlink_to('absent')
                elif kind == 'directory':
                    self.pin.mkdir()
                elif kind == 'fifo':
                    os.mkfifo(self.pin)
                else:
                    self.write_pin(b'2.2.0')
                    self.pin.chmod(0)
                try:
                    self.assertEqual(self.build(['build']), 1, self.errors.getvalue())
                finally:
                    self.pin.rmdir() if kind == 'directory' else self.pin.unlink()
        self.assertFalse(self.builds)

    def test_ordinary_build_accepts_regular_file_symlink_but_update_cannot_replace_it(self):
        self.pin.parent.mkdir(parents=True)
        target = self.root / 'managed-pin'
        target.write_bytes(b'2.2.0\n')
        self.pin.symlink_to(target)
        self.assertEqual(self.build(['build']), 0, self.errors.getvalue())
        self.assertEqual(self.builds[-1][2], b'2.2.0\n')
        self.assertEqual(self.build(['build', '--update-claude']), 1, self.errors.getvalue())
        self.assertIn('symlink', self.errors.getvalue())
        self.assertEqual(len(self.builds), 1)
        self.assertEqual(self.pin.readlink(), target)
        self.assertEqual(target.read_bytes(), b'2.2.0\n')

    def test_concurrent_pin_edit_during_lookup_is_preserved(self):
        version_api = self.version_api()
        for previous in (None, b'2.2.0\n'):
            with self.subTest(previous=previous):
                if previous is None:
                    self.pin.unlink(missing_ok=True)
                else:
                    self.write_pin(previous)

                def request(*args, **kwargs):
                    self.write_pin(b'2.3.0\n')
                    return '2.2.1'

                with patch.object(version_api, '_latest_version', side_effect=request):
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        version_api.update_code_version(ROOT, self.paths)
                self.assertEqual(self.pin.read_bytes(), b'2.3.0\n')
                self.assertEqual(list(self.pin.parent.iterdir()), [self.pin])

    def test_failed_atomic_publish_retains_previous_pin_and_cleans_draft(self):
        version_api = self.version_api()
        self.write_pin(b'2.2.0\n')
        with patch.object(version_api, '_latest_version', return_value='2.2.1'), \
                patch('os.replace', side_effect=OSError('injected publication failure')):
            with self.assertRaisesRegex((ValueError, OSError), 'publish|publication'):
                version_api.update_code_version(ROOT, self.paths)
        self.assertEqual(self.pin.read_bytes(), b'2.2.0\n')
        self.assertEqual(list(self.pin.parent.iterdir()), [self.pin])

    def test_update_rejects_retargeting_the_configuration_directory(self):
        version_api = self.version_api()
        self.pin.parent.parent.mkdir(parents=True)
        original = self.root / 'original-config'
        other = self.root / 'other-config'
        original.mkdir()
        other.mkdir()
        config = self.pin.parent
        config.symlink_to(original)

        def request(*args, **kwargs):
            config.unlink()
            config.symlink_to(other)
            return '2.2.1'

        with patch.object(version_api, '_latest_version', side_effect=request):
            with self.assertRaisesRegex(ValueError, 'changed'):
                version_api.update_code_version(ROOT, self.paths)
        self.assertFalse((original / self.pin.name).exists())
        self.assertFalse((other / self.pin.name).exists())

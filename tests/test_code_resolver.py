"""Exercise the real metadata worker process with controlled transport and failures."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from claude_docker import claude_version
from claude_docker.installer import InstallLayout, install_release
from claude_docker.paths import resolve_host_paths


LAUNCHER = '''\
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

program, ready, scenario = sys.argv[1:]
sys.path.insert(0, str(Path(program) / 'src'))
from claude_docker import claude_version
from claude_docker import cli
from claude_docker.paths import resolve_host_paths

popen = subprocess.Popen

def spawn(argv, **kwargs):
    # Run the real worker bootstrap with a controlled transport or failure.
    index = argv.index('-c') + 1
    source = str(Path(ready).with_name('missing-source')) if scenario == 'import' else argv[index + 1]
    return popen([*argv[:index], TRANSPORT + argv[index], source, ready, scenario],
                 **kwargs)

paths = resolve_host_paths(os.environ, os.getuid(), create_runtime=False)
with patch.object(claude_version, '_RESOLVE_TIMEOUT', 1.5, create=True), \\
        patch('urllib.request.OpenerDirector.open', side_effect=OSError('parent cannot use the network')), \\
        patch('subprocess.Popen', side_effect=spawn):
    if scenario not in ('dns', 'body', 'environment'):
        with patch.object(cli.ComposeProject, 'build', side_effect=AssertionError('Docker build reached')):
            raise SystemExit(cli.main(['build', '--update-claude']))
    try:
        print(claude_version.update_code_version(Path(program), paths))
    except ValueError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
'''


TRANSPORT = '''\
import io
import os
from pathlib import Path
import socket
import ssl
import sys
import time
import urllib.request

ready, scenario = sys.argv[-2:]
ready = Path(ready)

def stall():
    draft = ready.with_suffix('.tmp')
    draft.write_text(str(os.getpid()))
    draft.replace(ready)
    time.sleep(8)

class Response(io.BytesIO):
    length = None
    status = 200

    def geturl(self):
        return 'https://downloads.claude.ai/claude-code-releases/latest'

    def read(self, size=-1):
        assert size == 129
        if scenario == 'body':
            stall()
        return super().read(size)

def open_fixture(opener, url, *, timeout):
    assert url == 'https://downloads.claude.ai/claude-code-releases/latest'
    assert timeout == 10
    if scenario == 'environment':
        assert os.environ['https_proxy'] == 'http://test.invalid:3128'
        assert os.environ['NO_PROXY'] == 'localhost'
        assert os.environ['SSL_CERT_FILE'] == '/test/ca.pem'
        assert os.environ['SSL_CERT_DIR'] == '/test/certs'
        assert not {'ANTHROPIC_API_KEY', 'PYTHONPATH', 'PYTHONHOME', 'SSLKEYLOGFILE'} & os.environ.keys()
        assert sys.flags.isolated and sys.dont_write_bytecode
    errors = {
        'http': urllib.error.HTTPError(url, 503, 'private-http-reason', {}, None),
        'redirect': urllib.error.HTTPError(url, 302, 'private-redirect-target', {}, None),
        'cert': urllib.error.URLError(ssl.SSLCertVerificationError('private-certificate-detail')),
        'tls': urllib.error.URLError(ssl.SSLError('private-tls-detail')),
        'timeout': urllib.error.URLError(TimeoutError('private-timeout-detail')),
        'network': ValueError('private-proxy-url'),
        'unexpected': RuntimeError('private-unexpected-detail'),
    }
    if scenario in errors:
        raise errors[scenario]
    if scenario == 'invalid':
        return Response(b'2.3.4-private-body')
    if scenario == 'incomplete':
        response = Response(b'2.3.4')
        response.length = 5
        return response
    return Response(b'2.3.4\\n')

if scenario.startswith('malformed:'):
    sys.stdout.write(scenario.removeprefix('malformed:'))
    raise SystemExit(1)
if scenario == 'unexpected-exit':
    print('network')
    raise SystemExit(2)
if scenario == 'dns':
    socket.getaddrinfo = lambda *args, **kwargs: stall()
else:
    urllib.request.OpenerDirector.open = open_fixture
'''


class CodeResolverProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='claude code resolver ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {'HOME': str(self.root), 'PATH': os.defpath,
                    'XDG_CONFIG_HOME': str(self.root / 'config')}
        self.ready = self.root / 'worker-pid'
        self.stderr = self.root / 'stderr'
        self.paths = resolve_host_paths(self.env, os.getuid(), create_runtime=False)
        self.pin = self.paths.config_dir / 'claude-code.version'
        self.pin.parent.mkdir(parents=True)
        self.pin.write_bytes(b' \t2.2.0\r\n')
        self.pin.chmod(0o640)
        self.previous = self.pin.stat()

    def start(self, stderr, scenario, program=ROOT):
        return subprocess.Popen(
            [sys.executable, '-I', '-S', '-B', '-c', f'TRANSPORT = {TRANSPORT!r}\n{LAUNCHER}',
             str(program), str(self.ready), scenario], cwd=self.root, env=self.env,
            stdout=subprocess.PIPE, stderr=stderr, text=True,
        )

    def wait_for_worker(self, process):
        deadline = time.monotonic() + 3
        while not self.ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.ready.exists(), self.stderr.read_text())
        self.assertIsNone(process.poll(), self.stderr.read_text())
        self.assertIn('Requesting', self.stderr.read_text())
        self.assertIn('Claude Code', self.stderr.read_text())

    def assert_pin_preserved(self):
        current = self.pin.stat()
        self.assertEqual((current.st_ino, current.st_mode, current.st_mtime_ns),
                         (self.previous.st_ino, self.previous.st_mode, self.previous.st_mtime_ns))
        self.assertEqual(self.pin.read_bytes(), b' \t2.2.0\r\n')
        self.assertEqual(list(self.pin.parent.iterdir()), [self.pin])

    def assert_worker_reaped(self):
        with self.assertRaises(ProcessLookupError):
            os.kill(int(self.ready.read_text()), 0)

    def assert_cli_failure(self, scenario, expected):
        with self.stderr.open('w') as stderr, self.start(stderr, scenario) as process:
            stdout, _ = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 1, self.stderr.read_text())
        self.assertEqual(stdout, '')
        logs = self.stderr.read_text()
        self.assertIn('Requesting the latest Claude Code version over HTTPS.', logs)
        errors = [line for line in logs.splitlines() if not line.startswith((
            'claude-docker: config: ',
            'claude-docker: launcher version: ',
            'claude-docker: base mise ',
            'claude-docker: user mise ',
            'claude-docker: base customize ',
            'claude-docker: user customize ',
            'claude-docker: Requesting ',
            'claude-docker: Reading ',
        ))]
        self.assertEqual(errors, [f'claude-docker: {expected}'])
        self.assertNotIn('private-', logs)
        self.assert_pin_preserved()

    def test_known_worker_failures_reach_the_cli_as_one_safe_diagnostic(self):
        for scenario, diagnostic in (
            ('http', 'Cannot fetch the latest Claude Code version: HTTP status 503'),
            ('redirect', 'Cannot fetch the latest Claude Code version: redirect refused'),
            ('cert', 'Cannot fetch the latest Claude Code version: TLS certificate verification failed'),
            ('tls', 'Cannot fetch the latest Claude Code version: TLS connection failed'),
            ('timeout', 'Cannot fetch the latest Claude Code version: request timed out'),
            ('network', 'Cannot fetch the latest Claude Code version: network request failed'),
            ('invalid', 'Invalid Claude Code version in latest release metadata; expected numeric X.Y.Z'),
            ('incomplete', 'Invalid Claude Code release metadata: incomplete HTTP response'),
        ):
            with self.subTest(scenario=scenario):
                self.assert_cli_failure(scenario, diagnostic)

    def test_unexpected_worker_exits_report_the_generic_failure_once(self):
        for scenario in ('unexpected', 'import', 'unexpected-exit'):
            with self.subTest(scenario=scenario):
                self.assert_cli_failure(scenario, 'Cannot fetch the latest Claude Code version')

    def test_malformed_worker_records_report_the_generic_failure_once(self):
        for record in ('2.3.4\n', 'private-category\n', 'http 999\n', 'http 200\n',
                       'http true\n', 'http\n', 'network 503\n', 'network',
                       'network\nprivate-extra-line\n', 'private-oversized' * 1024):
            with self.subTest(record=record[:48]):
                self.assert_cli_failure(f'malformed:{record}', 'Cannot fetch the latest Claude Code version')

    def test_stalled_dns_and_body_are_bounded_with_live_progress(self):
        for scenario in ('dns', 'body'):
            with self.subTest(scenario=scenario):
                self.ready.unlink(missing_ok=True)
                start = time.monotonic()
                with self.stderr.open('w') as stderr, self.start(stderr, scenario) as process:
                    self.wait_for_worker(process)
                    if scenario == 'body':
                        self.assertIn('Reading Claude Code release metadata', self.stderr.read_text())
                    stdout, _ = process.communicate(timeout=4)
                    self.assertEqual(process.returncode, 1, self.stderr.read_text())
                self.assertLess(time.monotonic() - start, 4)
                self.assertEqual(stdout, '')
                self.assertIn('timed out', self.stderr.read_text())
                self.assert_worker_reaped()
                self.assert_pin_preserved()

    def test_sigint_and_sigterm_kill_the_worker_and_preserve_the_pin(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                self.ready.unlink(missing_ok=True)
                with self.stderr.open('w') as stderr, self.start(stderr, 'body') as process:
                    self.wait_for_worker(process)
                    process.send_signal(signum)
                    stdout, _ = process.communicate(timeout=4)
                    self.assertEqual(process.returncode, -signum, self.stderr.read_text())
                self.assertEqual(stdout, '')
                self.assert_worker_reaped()
                self.assert_pin_preserved()

    def test_installed_worker_ignores_startup_injection_and_keeps_tls_and_proxy_settings(self):
        self.env.update({'https_proxy': 'http://test.invalid:3128', 'NO_PROXY': 'localhost',
                         'SSL_CERT_FILE': '/test/ca.pem', 'SSL_CERT_DIR': '/test/certs',
                         'ANTHROPIC_API_KEY': 'private-api-key', 'SSLKEYLOGFILE': str(self.root / 'keys'),
                         'PYTHONPATH': str(self.root), 'PYTHONHOME': str(self.root / 'fake-python'),
                         'XDG_DATA_HOME': str(self.root / 'installed data')})
        for name in ('sitecustomize.py', 'claude_docker.py'):
            (self.root / name).write_text("raise AssertionError('untrusted startup')\n")
        layout = InstallLayout.user(self.env)
        install_release(ROOT, layout, version='0.4.5')
        program = layout.release_dir('0.4.5')
        with self.stderr.open('w') as stderr, self.start(stderr, 'environment', program) as process:
            stdout, _ = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 0, self.stderr.read_text())
        self.assertEqual(stdout, '2.3.4\n')
        self.assertEqual(self.pin.read_bytes(), b'2.3.4\n')
        self.assertFalse(list(program.rglob('__pycache__')))
        self.assertNotIn('private-api-key', self.stderr.read_text())
        self.assertFalse((self.root / 'keys').exists())

    def test_saved_pin_skips_process_creation_and_network(self):
        with patch('subprocess.Popen', side_effect=AssertionError('metadata worker was started')), \
                patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('metadata was fetched')):
            self.assertEqual(claude_version.read_code_version(ROOT, self.paths), '2.2.0')
        self.assert_pin_preserved()

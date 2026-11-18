"""Discover the exact builtin profile from immutable official source URLs."""

from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPSHandler
from urllib.response import addinfourl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from claude_docker.seccomp_source import Baseline, fetch_official, resolve_baseline


SOURCE_COMMIT = '06db1734267bf7770914b417bff6556d835bf8cc'
PROFILE_COMMIT = '4b8214fc4971e06172938a1f1554ccff6647ee80'
SOURCE_API = 'https://api.github.com/repos/moby/moby/commits/06db173'
SOURCE_RAW = f'https://raw.githubusercontent.com/moby/moby/{SOURCE_COMMIT}'
GO_MOD = f'{SOURCE_RAW}/go.mod'
VENDOR_MOD = f'{SOURCE_RAW}/vendor.mod'
REF_API = 'https://api.github.com/repos/moby/moby/git/ref/tags/'
TAG_API = 'https://api.github.com/repos/moby/moby/git/tags/'
RELEASE = 'docker-v29.10.0'
SOURCE_REF = REF_API + RELEASE
PROFILE_API = 'https://api.github.com/repos/moby/profiles/git/ref/tags/seccomp/v0.3.0'
PROFILE_RAW = f'https://raw.githubusercontent.com/moby/profiles/{PROFILE_COMMIT}'
PROFILE_URL = f'{PROFILE_RAW}/seccomp/default.json'
LICENSE_URL = f'{PROFILE_RAW}/LICENSE'
PROFILE = b'{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[{"names":["read"],"action":"SCMP_ACT_ALLOW"}]}'
LICENSE = b'Apache License\nVersion 2.0\n'
DEPENDENCY = b'github.com/moby/profiles/seccomp'
MANIFEST = (b'module github.com/moby/moby/v2\ngo 1.26.8\n'
            b'require (\n\tgithub.com/moby/profiles/apparmor v0.3.0\n\t'
            + DEPENDENCY + b' v0.3.0 // builtin profile\n)\n')
EXPECTED = Baseline(source_commit=SOURCE_COMMIT, profile_commit=PROFILE_COMMIT,
                    module_version='v0.3.0', profile=PROFILE, license=LICENSE,
                    profile_path='seccomp/default.json', source_ref='refs/tags/' + RELEASE)


def commit_json(sha):
    return json.dumps({'sha': sha, 'commit': {'message': 'Release source'}}).encode()


def ref_json(tag, sha, kind='commit'):
    return json.dumps({'ref': 'refs/tags/' + tag, 'object': {'type': kind, 'sha': sha}}).encode()


class OfficialSources:
    def __init__(self):
        self.calls = []
        self.responses = {
            SOURCE_API: commit_json(SOURCE_COMMIT),
            SOURCE_REF: ref_json(RELEASE, SOURCE_COMMIT),
            GO_MOD: MANIFEST,
            PROFILE_API: ref_json('seccomp/v0.3.0', PROFILE_COMMIT),
            PROFILE_URL: PROFILE,
            LICENSE_URL: LICENSE,
        }

    def __call__(self, url):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


def http_response(url, *, status=200, body=b'', headers=None):
    fields = Message()
    for key, value in (headers or {}).items():
        fields[key] = value
    response = addinfourl(BytesIO(body), fields, url, status)
    response.msg = 'test response'
    return response


class SourceDiscoveryTests(unittest.TestCase):
    def test_resolves_exact_source_and_uses_commit_for_profile_and_license(self):
        fetch = OfficialSources()

        self.assertEqual(resolve_baseline('06db173', '29.10.0', fetch=fetch), EXPECTED)
        self.assertEqual(fetch.calls, [SOURCE_API, SOURCE_REF, GO_MOD, PROFILE_API, PROFILE_URL, LICENSE_URL])

    def test_rejects_missing_or_mismatched_official_release_before_manifest(self):
        absent = HTTPError(SOURCE_REF, 404, 'absent', None, None)
        for response in (absent, ref_json(RELEASE, 'a' * 40), ref_json('other', SOURCE_COMMIT)):
            fetch = OfficialSources()
            fetch.responses[SOURCE_REF] = response
            fetch.responses[REF_API + 'v29.10.0'] = absent
            with self.subTest(response=response), self.assertRaises(ValueError):
                resolve_baseline('06db173', '29.10.0', fetch=fetch)
            self.assertNotIn(GO_MOD, fetch.calls)
            self.assertEqual(REF_API + 'v29.10.0' in fetch.calls, response is absent)

    def test_older_release_with_annotated_tag(self):
        fetch = OfficialSources()
        release = 'v28.5.2'
        tag_sha = 'f' * 40
        first = REF_API + 'docker-' + release
        fetch.responses[first] = HTTPError(first, 404, 'absent', None, None)
        fetch.responses[REF_API + release] = ref_json(release, tag_sha, 'tag')
        fetch.responses[TAG_API + tag_sha] = json.dumps({
            'sha': tag_sha, 'tag': release, 'object': {'type': 'commit', 'sha': SOURCE_COMMIT}}).encode()
        base = resolve_baseline('06db173', '28.5.2', fetch=fetch)
        self.assertEqual(base.source_ref, 'refs/tags/' + release)
        for bad in ({'type': 'tag', 'sha': tag_sha}, {'type': 'tree', 'sha': SOURCE_COMMIT}):
            fetch.responses[TAG_API + tag_sha] = json.dumps({
                'sha': tag_sha, 'tag': release, 'object': bad}).encode()
            with self.assertRaises(ValueError):
                resolve_baseline('06db173', '28.5.2', fetch=fetch)

    def test_uses_vendor_manifest_only_when_root_manifest_is_missing(self):
        fetch = OfficialSources()
        fetch.responses[GO_MOD] = HTTPError(GO_MOD, 404, 'not found', None, None)
        fetch.responses[VENDOR_MOD] = (b'module github.com/docker/docker\n'
                                       b'require ' + DEPENDENCY + b' v0.3.0\n'
                                       b'exclude github.com/example/other v1.0.0\n')

        self.assertEqual(resolve_baseline('06db173', '29.10.0', fetch=fetch), EXPECTED)
        self.assertEqual(fetch.calls, [SOURCE_API, SOURCE_REF, GO_MOD, VENDOR_MOD, PROFILE_API, PROFILE_URL, LICENSE_URL])

    def test_full_reported_commit_matches_api_response(self):
        fetch = OfficialSources()
        fetch.responses[f'{SOURCE_API[:-7]}{SOURCE_COMMIT}'] = commit_json(SOURCE_COMMIT)

        self.assertEqual(resolve_baseline(SOURCE_COMMIT, '29.10.0', fetch=fetch), EXPECTED)

    def test_rejects_unsupported_commit_identifiers_before_fetching(self):
        for value in ('main', 'abcdef', 'a' * 41, '06db173?private-data', '../06db173'):
            with self.subTest(value=value):
                fetch = OfficialSources()
                with self.assertRaisesRegex(ValueError, 'GitCommit'):
                    resolve_baseline(value, '29.10.0', fetch=fetch)
                self.assertEqual(fetch.calls, [])

    def test_rejects_source_response_missing_full_matching_sha(self):
        for response in (b'[]', b'{"sha":null}', commit_json('06db173'),
                         commit_json('a' * 40), b'{"sha":"ignored","sha":"' + SOURCE_COMMIT.encode() + b'"}'):
            with self.subTest(response=response):
                fetch = OfficialSources()
                fetch.responses[SOURCE_API] = response
                with self.assertRaisesRegex(ValueError, 'Moby source commit'):
                    resolve_baseline('06db173', '29.10.0', fetch=fetch)
                self.assertEqual(fetch.calls, [SOURCE_API])

    def test_tag_response_must_contain_full_commit(self):
        fetch = OfficialSources()
        fetch.responses[PROFILE_API] = commit_json('4b8214f')
        with self.assertRaisesRegex(ValueError, 'profile module commit'):
            resolve_baseline('06db173', '29.10.0', fetch=fetch)
        self.assertNotIn(PROFILE_URL, fetch.calls)

    def test_legacy_in_tree_profile_fails_with_supported_path_guidance(self):
        fetch = OfficialSources()
        fetch.responses[GO_MOD] = HTTPError(GO_MOD, 404, 'not found', None, None)
        fetch.responses[VENDOR_MOD] = b'module github.com/docker/docker\nrequire github.com/example/other v1.0.0\n'
        with self.assertRaisesRegex(ValueError, 'Unsupported Moby seccomp source.*in-tree'):
            resolve_baseline('06db173', '29.10.0', fetch=fetch)
        self.assertEqual(fetch.calls, [SOURCE_API, SOURCE_REF, GO_MOD, VENDOR_MOD])

    def test_missing_manifests_do_not_trigger_profile_path_guesses(self):
        fetch = OfficialSources()
        for url in (GO_MOD, VENDOR_MOD):
            fetch.responses[url] = HTTPError(url, 404, 'not found', None, None)
        with self.assertRaisesRegex(ValueError, 'Unsupported Moby seccomp source'):
            resolve_baseline('06db173', '29.10.0', fetch=fetch)
        self.assertEqual(fetch.calls, [SOURCE_API, SOURCE_REF, GO_MOD, VENDOR_MOD])

    def test_malformed_or_ambiguous_manifest_does_not_fall_through(self):
        for manifest in (
            MANIFEST.replace(b'\n)\n', b'\n'),
            MANIFEST + b'require ' + DEPENDENCY + b' v0.3.0\n',
            MANIFEST + b'require ' + DEPENDENCY + b' v0.4.0\n',
            MANIFEST.replace(b'v0.3.0 // builtin profile', b'v0.3.0 ../../unexpected'),
            MANIFEST.replace(b'v0.3.0 // builtin profile', b'v0.0.0-20251006153554-3cec751e60c7'),
            MANIFEST.replace(b'go 1.26.8', b'go invalid'),
            MANIFEST.replace(b'require (', b'"require" ('),
            b'module github.com/other/repository\nrequire ' + DEPENDENCY + b' v0.3.0\n',
            b'require ' + DEPENDENCY + b' v0.3.0\n',
            b'\xff',
        ):
            with self.subTest(manifest=manifest):
                fetch = OfficialSources()
                fetch.responses[GO_MOD] = manifest
                with self.assertRaisesRegex(ValueError, r'go\.mod'):
                    resolve_baseline('06db173', '29.10.0', fetch=fetch)
                self.assertEqual(fetch.calls, [SOURCE_API, SOURCE_REF, GO_MOD])

    def test_rejects_malformed_singleton_directives_before_resolving_profile_tag(self):
        for extra in (b'go 1.25.0\n',
                      b'toolchain go1.26.8\ntoolchain go1.27.0\n',
                      b'toolchain bogus\n',
                      b'toolchain "go1.26.8"\n'):
            with self.subTest(extra=extra):
                fetch = OfficialSources()
                fetch.responses[GO_MOD] = MANIFEST + extra
                with self.assertRaisesRegex(ValueError, r'malformed.*go\.mod'):
                    resolve_baseline('06db173', '29.10.0', fetch=fetch)
                self.assertEqual(fetch.calls, [SOURCE_API, SOURCE_REF, GO_MOD])

    def test_accepts_valid_toolchain_declarations(self):
        for extra in (b'toolchain go1.26.8\n', b'toolchain default\n'):
            with self.subTest(extra=extra):
                fetch = OfficialSources()
                fetch.responses[GO_MOD] = MANIFEST + extra
                self.assertEqual(resolve_baseline('06db173', '29.10.0', fetch=fetch), EXPECTED)

    def test_profile_dependency_replacements_and_exclusions_are_unsupported(self):
        for override in (
            b'replace ' + DEPENDENCY + b' => ./local-profile\n',
            b'replace (\n\t' + DEPENDENCY + b' v0.3.0 => github.com/other/seccomp v0.3.0\n)\n',
            b'replace "' + DEPENDENCY + b'" => ../local-profile\n',
            b'exclude ' + DEPENDENCY + b' v0.3.0\n',
        ):
            with self.subTest(override=override):
                fetch = OfficialSources()
                fetch.responses[GO_MOD] = MANIFEST + override
                with self.assertRaisesRegex(ValueError, 'seccomp dependency'):
                    resolve_baseline('06db173', '29.10.0', fetch=fetch)
                self.assertEqual(fetch.calls, [SOURCE_API, SOURCE_REF, GO_MOD])

    def test_unrelated_replacement_does_not_change_seccomp_dependency(self):
        fetch = OfficialSources()
        fetch.responses[GO_MOD] = MANIFEST + b'replace github.com/example/other => ./other\n'
        self.assertEqual(resolve_baseline('06db173', '29.10.0', fetch=fetch), EXPECTED)

    def test_source_and_license_failures_are_actionable_and_redacted(self):
        for url, label, response in (
            (SOURCE_API, 'Moby source commit', TimeoutError('credential-sentinel')),
            (GO_MOD, 'go.mod', HTTPError(GO_MOD, 403, 'credential-sentinel', None, None)),
            (PROFILE_URL, 'seccomp/default.json', b'credential-sentinel'),
            (LICENSE_URL, 'LICENSE', HTTPError(LICENSE_URL, 404, 'credential-sentinel', None, None)),
            (LICENSE_URL, 'LICENSE', b' \n'),
        ):
            with self.subTest(url=url):
                fetch = OfficialSources()
                fetch.responses[url] = response
                with self.assertRaisesRegex(ValueError, label) as caught:
                    resolve_baseline('06db173', '29.10.0', fetch=fetch)
                self.assertNotIn('credential-sentinel', str(caught.exception))
                self.assertEqual(fetch.calls[-1], url)


class OfficialFetchTests(unittest.TestCase):
    def test_official_fetch_reads_bytes_with_ten_second_timeout(self):
        def open_https(request):
            self.assertEqual(request.timeout, 10)
            return http_response(request.full_url, body=PROFILE)

        with patch.object(HTTPSHandler, 'https_open', side_effect=open_https):
            self.assertEqual(fetch_official(PROFILE_URL), PROFILE)

    def test_rejects_transport_hosts_and_paths_outside_source_layout(self):
        with patch.object(HTTPSHandler, 'https_open', side_effect=AssertionError('unexpected request')):
            for url in (
                PROFILE_URL.replace('https:', 'http:'),
                PROFILE_URL.replace('raw.githubusercontent.com', 'raw.githubusercontent.com.example.com'),
                PROFILE_URL.replace('raw.githubusercontent.com', 'user@raw.githubusercontent.com'),
                PROFILE_URL.replace('raw.githubusercontent.com', 'raw.githubusercontent.com:443'),
                PROFILE_URL.replace(PROFILE_COMMIT, 'main'),
                PROFILE_URL.replace('/seccomp/default.json', '/seccomp/../LICENSE'),
                SOURCE_API.replace('/06db173', '/06db173?x=y'),
                SOURCE_API + '#x',
                '\t' + SOURCE_API,
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    fetch_official(url)

    def test_closes_error_response_without_following_redirect_or_reading_body(self):
        for status in (302, 404, 500):
            with self.subTest(status=status):
                response = http_response(SOURCE_API, status=status, body=b'credential-sentinel',
                                         headers={'Location': 'https://example.com/private'})
                with (patch.object(HTTPSHandler, 'https_open', side_effect=[response]) as opener,
                      patch.object(response, 'read', side_effect=AssertionError('unexpected error body read')),
                      self.assertRaises(ValueError) as caught):
                    fetch_official(SOURCE_API)
                self.assertEqual(opener.call_count, 1)
                self.assertTrue(response.closed)
                self.assertNotIn('credential-sentinel', str(caught.exception))

    def test_reports_transport_timeout_without_leaking_details(self):
        with (patch.object(HTTPSHandler, 'https_open', side_effect=TimeoutError('credential-sentinel')),
              self.assertRaisesRegex(ValueError, 'timed out') as caught):
            fetch_official(SOURCE_API)
        self.assertNotIn('credential-sentinel', str(caught.exception))

    def test_rejects_oversized_body_with_or_without_content_length(self):
        size_limit = 2 * 1024 * 1024
        for headers, body_size in (({}, size_limit + 1), ({'Content-Length': str(size_limit + 1)}, 10)):
            with self.subTest(headers=headers):
                response = http_response(PROFILE_URL, headers=headers, body=b'a' * body_size)
                with (patch.object(HTTPSHandler, 'https_open', return_value=response),
                      patch.object(response, 'read', wraps=response.read) as read,
                      self.assertRaisesRegex(ValueError, '2 MiB')):
                    fetch_official(PROFILE_URL)
                self.assertTrue(response.closed)
                if headers:
                    read.assert_not_called()
                else:
                    read.assert_called_once_with(size_limit + 1)


if __name__ == '__main__':
    unittest.main()

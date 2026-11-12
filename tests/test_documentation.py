"""User-manual contracts: navigation, safe procedures, and required decisions."""

import argparse
import importlib
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from textwrap import dedent
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / 'README.md'
sys.path.insert(0, str(ROOT / 'src'))
from claude_docker import __version__, cli, config, installer
HEADINGS = [
    'What the program does', 'Security model', 'Requirements', 'Install',
    'Start Claude', 'Configure the program', 'Use Claude Desktop',
    'Use inner Docker', 'Check the system', 'Update or uninstall', 'TODO', 'License',
]


class DocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = README.read_text(encoding='utf-8')
        cls.sections = dict(re.findall(r'^## ([^\n]+)\n(.*?)(?=^## |\Z)', cls.text, re.M | re.S))
        cls.shell = '\n'.join(re.findall(r'^[ \t]*```(?:bash|sh)\n(.*?)^[ \t]*```', cls.text, re.M | re.S))

    def section(self, heading):
        self.assertIn(heading, self.sections)
        return self.sections[heading]

    def contains_all(self, text, terms):
        for term in terms:
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_release_files_are_complete_and_have_runnable_modes(self):
        # An omitted module breaks installed imports; an added unreviewed input
        # can ship checkout state. This release inventory is deliberately fixed.
        modules = {'__init__', '__main__', 'checks', 'cli', 'clipboard', 'commands',
                   'compose', 'config', 'desktop', 'environment', 'installer', 'manifest',
                   'model', 'mounts', 'paths', 'process', 'seccomp', 'state', 'uninstall'}
        expected = {'.dockerignore', 'claude-docker', 'install', 'VERSION', 'README.md', 'LICENSE',
                    'Dockerfile', 'compose.yaml', 'compose.sysbox.yaml', 'config.example.toml',
                    'image/customize.sh', 'image/mise.lock', 'image/mise.toml',
                    'security/desktop-seccomp-v0.2.4-linux-amd64.json',
                    'security/LICENSE.moby-profiles', 'security/NOTICE.moby-profiles'}
        expected |= {f'src/claude_docker/{module}.py' for module in modules}
        manifest, inputs = installer._source_manifest(ROOT, 'user', __version__)
        self.assertEqual({str(item.path) for item in manifest.files}, expected)
        self.assertEqual(len(manifest.files), len(expected))
        self.assertEqual({name for name, _ in inputs}, expected)
        executables = {'claude-docker', 'install', 'image/customize.sh'}
        for item in manifest.files:
            with self.subTest(path=item.path):
                wanted = 0o755 if str(item.path) in executables else 0o644
                self.assertEqual(item.mode, wanted)
                self.assertEqual(bool((ROOT / item.path).stat().st_mode & 0o111),
                                 str(item.path) in executables)
        self.assertEqual({path.stem for path in (ROOT / 'src/claude_docker').glob('*.py')}, modules)
        for module in modules - {'__main__'}:
            with self.subTest(module=module):
                loaded = importlib.import_module('claude_docker.' + module)
                self.assertEqual(Path(loaded.__file__).parent, ROOT / 'src/claude_docker')

    def test_version_matches_package_and_executable_from_unrelated_directory(self):
        version = (ROOT / 'VERSION').read_text().strip()
        self.assertRegex(version, r'^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$')
        self.assertEqual(version, '0.2.0')
        self.assertEqual(__version__, version)
        with tempfile.TemporaryDirectory() as directory:
            env = {'HOME': directory, 'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}
            for arguments in ([str(ROOT / 'claude-docker'), '--version'],
                              ['-m', 'claude_docker', '--version']):
                with self.subTest(arguments=arguments):
                    result = subprocess.run([sys.executable, *arguments], cwd=directory,
                                            env={**env, 'PYTHONPATH': str(ROOT / 'src')},
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'claude-docker ' + version)
                    self.assertEqual(result.stderr, '')
            result = subprocess.run([sys.executable, str(ROOT / 'install'), '--help'],
                                    cwd=directory, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--uninstall', result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_config_example_demonstrates_every_accepted_key_and_is_loadable(self):
        example = (ROOT / 'config.example.toml').read_text()
        self.assertEqual(example, config.EXAMPLE_CONFIG)
        # Identity stays optional in the shipped example; activate its documented
        # assignments to check those accepted keys alongside every active key.
        complete = re.sub(r'^# ((?:user|uid|gid|home) = .+)$', r'\1', example, flags=re.M)
        data = tomllib.loads(complete)

        def leaves(table, prefix=''):
            return {name for key, value in table.items()
                    for name in (leaves(value, prefix + key + '.') if isinstance(value, dict)
                                 else {prefix + key})}

        self.assertEqual(leaves(data), leaves(config._SCHEMA))
        with tempfile.TemporaryDirectory() as directory:
            paths = config.HostPaths(*(Path(directory) / name for name in
                                      ('home', 'config', 'data', 'state', 'cache', 'runtime')))
            selected = Path(directory) / 'complete.toml'
            selected.write_text(complete)
            loaded = config.load_config(env={}, paths=paths, config_file=selected,
                                        account=config.AccountInfo('tester', 12345, 12345, paths.home))
            self.assertEqual((loaded.container.user, loaded.container.uid, loaded.container.gid,
                              str(loaded.container.home)), ('developer', 1000, 1000, '/home/developer'))
            self.assertEqual(loaded.environment.required, ())
            generated = Path(directory) / 'generated.toml'
            config.write_example_config(generated)
            self.assertEqual(generated.read_text(), example)
            self.assertEqual(stat.S_IMODE(generated.stat().st_mode), 0o600)

    def test_readme_covers_every_public_command_path(self):
        class Captured(Exception):
            pass

        parsers = []

        def capture(parser, *args, **kwargs):
            parsers.append(parser)
            raise Captured

        with patch.object(argparse.ArgumentParser, 'parse_args', capture):
            with self.assertRaises(Captured):
                cli.main([])

        def command_paths(parser, prefix=()):
            actions = [action for action in parser._actions
                       if isinstance(action, argparse._SubParsersAction)]
            if not actions:
                return {prefix}
            return {path for action in actions for name, child in action.choices.items()
                    for path in command_paths(child, (*prefix, name))}

        documented = {tuple(word for word in arguments if not word.startswith('-'))
                      for executable, *arguments in self.documented_commands()
                      if executable == 'claude-docker'}
        public = command_paths(parsers[0])
        self.assertTrue(public)
        for command in public:
            with self.subTest(command=command):
                self.assertTrue(any(words[:len(command)] == command for words in documented), command)

    def test_navigation_and_size(self):
        self.assertEqual(HEADINGS, re.findall(r'^## (.+)$', self.text, re.M))
        self.assertEqual(1, len(re.findall(r'^# [^#]', self.text, re.M)))
        self.assertLessEqual(len(self.text.splitlines()), 540)
        self.assertRegex(self.text, r'\]\((?:\./)?config\.example\.toml\)')

    def test_ordinary_install_and_cli_procedures_precede_optional_features(self):
        self.contains_all(self.section('Install'), ['./install', '$HOME/.local/bin', 'claude-docker config init'])
        self.contains_all(self.section('Start Claude'), ['claude-docker build', 'claude-docker claude', 'claude-docker stop'])
        ordinary = self.section('Install') + self.section('Start Claude')
        self.assertNotRegex(ordinary, r'claude-docker (?:desktop|clipboard)|docker_mode\s*=\s*"sysbox"')

    def test_terms_and_plain_language(self):
        intro = self.section('What the program does')
        for term in ('host', 'container', 'workspace', 'mount', 'inner Docker'):
            self.assertRegex(intro, rf'(?m)^- \*\*{term}\*\*:')
        self.assertNotRegex(self.text, r"(?i)\b(?:\w+n't|\w+['’](?:re|ve|ll|d)|it['’]s|that['’]s)\b")
        self.assertNotRegex(self.text, r'\bdoctor\b|claude-docker-session\.md|`(?:config|desktop-config)/`')

    def test_examples_do_not_request_unsafe_host_access(self):
        # Inspect executable examples; safe negative statements must remain legal.
        self.assertNotRegex(self.shell, r'(?:^|\s)-ac(?:\s|$)|--privileged|/var/run/docker\.sock|--no-sandbox')
        self.assertNotRegex(self.shell, r'(?m)^.*(?:apt(?:-get)? install|pip install).*(?:\bmise\b|\bjq\b|\bpip\b)')
        self.assertNotRegex(self.shell, r'curl[^\n]*\|\s*(?:sudo\s+)?(?:ba)?sh')
        requirements = '\n'.join(re.findall(r'^- (.+)$', self.section('Requirements'), re.M))
        self.assertNotRegex(requirements, r'\b(?:mise|jq|pip)\b')
        self.contains_all(self.section('Security model'), ['root-equivalent', 'host Docker socket', 'environment', 'mount'])

    def test_support_matrix_preserves_observed_limits(self):
        requirements = self.section('Requirements')
        self.contains_all(requirements, ['Python 3.11', 'Compose v2', 'Debian 13', 'Ubuntu 24.04', 'amd64', 'arm64'])
        self.assertRegex(requirements, r'(?i)intended reference')
        self.assertRegex(requirements, r'(?i)qualification')
        self.contains_all(requirements, ['EPERM', '133'])
        self.assertRegex(requirements, r'(?i)Desktop.*failed|failed.*Desktop')
        self.assertRegex(requirements, r'(?i)arm64.*(?:not|un).*')
        self.assertRegex(self.section('Use inner Docker'), r'(?is)Debian 13.*experimental.*hang')

    def test_configuration_storage_identity_and_precedence(self):
        config = self.section('Configure the program')
        self.contains_all(config, ['~/.config/claude-docker/config.toml', '~/.local/share/claude-docker',
                                  '~/.local/state/claude-docker', '~/.cache/claude-docker',
                                  '$XDG_RUNTIME_DIR/claude-docker', '/tmp/claude-docker-<uid>',
                                  '~/.claude', '~/.claude.json', '~/.config/Claude',
                                  'CLAUDE_CONFIG_FILE', 'CLAUDE_MOUNTS_FILE', 'CLAUDE_DOCKER_DIR'])
        self.assertRegex(config, r'(?is)environment.*TOML.*detected')
        self.assertRegex(config, r'(?is)CLI.*identity.*override')
        self.assertRegex(self.section('Use Claude Desktop'), r'(?is)UID.*match.*host UID')

    def test_state_retention_distinguishes_host_mounts_from_container_local_state(self):
        # Catch a renewed promise that stop preserves all Claude state, or an
        # omitted warning when state has no persistent host source.
        start = self.section('Start Claude')
        self.assertRegex(start, r'(?is)(?:stop|command).*removes? (?:the )?containers')
        self.assertRegex(start, r'(?is)preserves?[^.]*state[^.]*enabled host mounts')
        self.assertRegex(start, r'(?is)preserves?[^.]*named inner Docker volumes')
        state_controls = self.section('Configure the program').split(
            'Claude state mounts are enabled by default.', 1)[1].split(
                '`CLAUDE_CONFIG_FILE`', 1)[0]
        self.assertRegex(state_controls, r'(?is)(?:disable|disabled)[^.]*mount')
        self.assertRegex(state_controls, r'(?is)(?:missing|does not exist)[^.]*JSON|JSON[^.]*does not exist')
        self.assertRegex(state_controls, r'(?is)container-local state.*(?:lost|discarded).*container.*(?:removed|removal)')

    def test_toml_examples_use_mounts_and_environment_names_only(self):
        examples = [tomllib.loads(dedent(block)) for block in
                    re.findall(r'^[ \t]*```toml\n(.*?)^[ \t]*```', self.text, re.M | re.S)]
        self.assertTrue(any(len(example.get('container', {}).get('mounts', [])) > 1 for example in examples))
        environments = [example['environment'] for example in examples if 'environment' in example]
        self.assertTrue(environments, 'Include the names-only AWS forwarding example')
        self.assertEqual(['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'], environments[0]['required'])
        self.assertEqual(['AWS_REGION', 'AWS_DEFAULT_REGION'], environments[0]['optional'])

    def test_forwarded_credentials_refresh_per_process(self):
        config = self.section('Configure the program')
        self.assertRegex(config, r'(?is)each.*(?:process|invocation)')
        self.assertRegex(config, r'(?is)required.*(?:missing|empty)')
        self.assertRegex(config, r'(?is)optional.*(?:present|set)')
        self.assertIn('claude-docker desktop --restart', config)
        self.assertRegex(config, r'(?i)inner containers')
        self.assertRegex(config, r'(?is)(?:not|no).*automatic.*inherit')

    def test_aws_credentials_are_exported_before_configuration_example(self):
        config = self.section('Configure the program')
        before_example = config.split('required = ["AWS_ACCESS_KEY_ID"', 1)[0]
        self.assertRegex(before_example, r'(?is)export\b[^.]*\bAWS\b[^.]*\bhost\b')

    def test_environment_visibility_warning_names_all_exposed_principals(self):
        security = self.section('Security model')
        self.assertRegex(security, r'(?is)environment.*not.*secret store')
        self.contains_all(security, ['Claude', 'child processes', 'Docker daemon'])
        self.assertRegex(security, r'(?is)(?:inspect|inspection).*process')

    def test_desktop_policy_and_shared_profile_warning(self):
        desktop = self.section('Use Claude Desktop')
        self.contains_all(desktop, ['manual', 'claude-docker clipboard push', 'claude-docker clipboard pull',
                                    '--clipboard-sync', '--no-sandbox', 'claude-docker desktop --restart'])
        self.assertRegex(desktop, r'(?is)shared.*profile.*lock')
        self.assertRegex(desktop, r'(?is)--no-sandbox.*unsafe')
        self.assertRegex(desktop, r'(?is)clipboard.*secret')
        self.assertRegex(desktop, r'(?is)persist.*(?:restart|stop)')

    def test_manual_explains_explicit_seccomp_opt_in_and_limits(self):
        section = self.section('Use Claude Desktop')
        self.contains_all(section, ['seccomp = "default"', 'seccomp = "chromium"',
                                   'CLAUDE_DESKTOP_SECCOMP', '29.9.2', 'Community',
                                   'amd64', 'docker_mode = "none"', 'Sysbox',
                                   'claude-docker desktop --restart'])
        self.assertRegex(section, r'(?is)kernel.*namespace.*attack surface')
        self.assertRegex(section, r'(?is)no automatic fallback')
        self.assertRegex(section, r'(?is)renderer.*GPU.*(?:not|unqualified)')
        self.assertIn('default-policy', self.section('Requirements'))

    def test_checks_do_not_claim_running_lifetime_verification(self):
        checks = self.section('Check the system')
        self.contains_all(checks, ['claude-docker check', '--smoke', '--desktop'])
        self.assertRegex(checks, r'(?is)Desktop checks.*(?:policy|settings|choices).*next (?:start|launch)')
        self.assertRegex(checks, r'(?is)(?:do\s+not|does\s+not|cannot).*verif.*running.*lifetime')

    def test_updates_and_contributor_tools_respect_immutable_bundles(self):
        update = self.section('Update or uninstall')
        self.contains_all(update, ['./install', 'claude-docker rebuild', 'claude-docker stop',
                                   './claude-docker tools lock', 'trusted source', 'immutable'])
        self.assertRegex(update, r'(?i)contributor')

    def test_removal_retention_consent_and_recovery(self):
        removal = self.section('Update or uninstall')
        self.contains_all(removal, ['claude-docker uninstall', 'claude-docker uninstall --purge --yes',
                                    'sudo ./install --uninstall --system', './install --uninstall',
                                    '~/.claude', '~/.claude.json', '~/.config/Claude',
                                    'custom state', 'workspaces', 'HOME', 'XDG', 'labels'])
        self.assertRegex(removal, r'(?is)normal.*retain.*(?:configuration|config).*images.*networks.*volumes')
        self.assertRegex(removal, r'(?is)purge.*inventory.*consent')
        self.assertRegex(removal, r'(?is)pending.*purge.*original.*choice')
        self.assertRegex(removal, r'(?is)finish.*removal.*(?:reinstall|install again)')

    def test_obsolete_entrypoints_are_absent(self):
        for name in ('start.sh', 'tools.sh', 'claude-docker-session.md'):
            with self.subTest(name=name):
                self.assertFalse((ROOT / name).exists() or (ROOT / name).is_symlink(), name)
        self.assertNotRegex(self.text, r'start\.sh|tools\.sh')
        self.assertNotIn('claude-docker-session.md', self.text)

    def test_build_context_admits_only_four_image_inputs(self):
        patterns = [line.strip() for line in (ROOT / '.dockerignore').read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(patterns[0], '**')
        self.assertCountEqual(patterns[1:], ['!Dockerfile', '!image/mise.toml',
                                             '!image/mise.lock', '!image/customize.sh'])

    def test_repository_file_references_exist(self):
        references = set(re.findall(r'\]\(([^)]+)\)', self.text))
        for token in re.findall(r'`([^`\n]+)`', self.text):
            # HOME/XDG and absolute host/container paths are generated data,
            # not references to files in the checkout.
            if (not re.search(r'\s', token) and not token.startswith(('~', '$', '/'))
                    and (token.startswith('./') or Path(token).suffix in ('.toml', '.yaml', '.sh', '.md'))):
                references.add(token)
        self.assertIn('config.example.toml', references)
        self.assertIn('image/customize.sh', references)
        for reference in references:
            if '://' in reference or reference.startswith('#'):
                continue
            with self.subTest(reference=reference):
                self.assertTrue((ROOT / reference.split('#')[0]).is_file(), reference)

    def documented_commands(self):
        candidates = self.shell.splitlines() + re.findall(r'`([^`\n]+)`', self.text)
        commands = set()
        for candidate in candidates:
            words = shlex.split(candidate.strip())
            if words[:1] == ['sudo']:
                words = words[1:]
            if words and words[0] in ('claude-docker', './claude-docker', './install'):
                commands.add((words[0].removeprefix('./'), *words[1:]))
        return sorted(commands)

    def parse_without_action(self, executable, arguments):
        class Parsed(Exception):
            pass

        original = argparse.ArgumentParser.parse_args
        observed = []

        def stop_after_parse(parser, args=None, namespace=None):
            parsed = original(parser, args, namespace)
            observed.append((list(args), parsed))
            raise Parsed

        # Stop at the real parser boundary: no configuration, Docker, clipboard,
        # installer or deletion operation is reachable, including claude --help.
        with patch.object(argparse.ArgumentParser, 'parse_args', stop_after_parse):
            with self.assertRaises(Parsed):
                if executable == 'install':
                    installer.main(ROOT, list(arguments))
                else:
                    cli.main(list(arguments))
        return observed[0]

    def test_documented_claude_tails_reach_application_unchanged_without_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'
            runtime.mkdir(mode=0o700)
            config = root / 'config.toml'
            config.write_text('[container]\nmounts = []\n'
                              '[state.code]\nmount = false\n'
                              '[state.code_json]\nmount = false\n'
                              '[state.desktop]\nmount = false\n')
            environment = {'HOME': directory, 'XDG_RUNTIME_DIR': str(runtime),
                           'CLAUDE_CONFIG_FILE': str(config), 'PATH': ''}
            calls = []

            def record_command(runner, argv, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, 'fixture-container\n', '')

            for executable, *arguments in self.documented_commands():
                if executable != 'claude-docker' or arguments[:1] != ['claude']:
                    continue
                with self.subTest(arguments=arguments):
                    calls.clear()
                    with patch.dict(os.environ, environment, clear=True), \
                            patch.object(cli.CommandRunner, 'run', record_command):
                        self.assertEqual(cli.main(arguments), 0)
                    executions = [argv for argv in calls if 'exec' in argv]
                    self.assertEqual(len(executions), 1)
                    application = executions[0].index('fixture-container') + 1
                    self.assertEqual(executions[0][application:], ['claude', *arguments[1:]])

    def test_documented_commands_match_real_parser_and_help(self):
        commands = self.documented_commands()
        self.assertTrue(commands)
        with tempfile.TemporaryDirectory() as directory:
            environment = {'HOME': directory, 'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}
            help_cache = {}

            def help_for(executable, words):
                key = (executable, *words)
                if key not in help_cache:
                    result = subprocess.run([sys.executable, str(ROOT / executable), *words, '--help'],
                                            env=environment, cwd=directory, capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    help_cache[key] = result.stdout
                return help_cache[key]

            for executable, *arguments in commands:
                with self.subTest(command=(executable, *arguments)):
                    parsed_args, parsed = self.parse_without_action(executable, arguments)
                    if executable == 'claude-docker':
                        self.assertRegex(help_for(executable, []), rf'\b{re.escape(arguments[0])}\b')
                    if arguments[:1] == ['claude']:
                        self.assertEqual(parsed.command, 'claude')
                        self.assertEqual(parsed_args, ['claude'], 'Claude tail belongs to the application')
                        continue  # claude --help would launch the application.
                    words = [word for word in arguments if not word.startswith('-')]
                    help_text = help_for(executable, words)
                    for option in (word for word in arguments if word.startswith('--')):
                        self.assertIn(option, help_text)
                    for index, word in enumerate(words):
                        self.assertRegex(help_for(executable, words[:index]), rf'\b{re.escape(word)}\b')
            # Prose also documents options without repeating a full command.
            for heading, command_words in (
                    ('Use Claude Desktop', ['desktop']),
                    ('Check the system', ['check']),
                    ('Update or uninstall', ['rebuild'])):
                help_text = help_for('claude-docker', command_words)
                if heading == 'Update or uninstall':
                    help_text += help_for('claude-docker', ['uninstall']) + help_for('install', [])
                for option in re.findall(r'`(--[a-z-]+)`', self.section(heading)):
                    self.assertIn(option, help_text)


if __name__ == '__main__':
    unittest.main()

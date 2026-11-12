"""Configuration precedence, strict schema, and explicit private initialization."""

import dataclasses
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_docker.config import AccountInfo, load_config, write_example_config
from claude_docker.paths import HostPaths

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "host"
        self.home.mkdir()
        self.paths = HostPaths(self.home, self.root / "config", self.root / "data",
                               self.root / "state", self.root / "cache", self.root / "run")
        self.account = AccountInfo("tester", 1234, 2345, self.root / "passwd-home")

    def load(self, text=None, env=None, config_file=None):
        if text is not None:
            self.paths.config_dir.mkdir(exist_ok=True)
            self.paths.config_file.write_text(text)
        return load_config(env=env or {}, paths=self.paths, account=self.account,
                           config_file=config_file)

    def test_opt_in_and_process_override_precedence(self):
        self.assertEqual(self.load().desktop.seccomp, 'default')
        self.assertEqual(self.load('[desktop]\nseccomp = "chromium"').desktop.seccomp, 'chromium')
        self.assertEqual(self.load('[desktop]\nseccomp = "chromium"',
                         {'CLAUDE_DESKTOP_SECCOMP': 'default'}).desktop.seccomp, 'default')
        self.assertEqual(self.load('[desktop]\nseccomp = "default"',
                         {'CLAUDE_DESKTOP_SECCOMP': 'chromium'}).desktop.seccomp, 'chromium')
        # Cross-field launch restrictions must not prevent ordinary config loading.
        loaded = self.load('docker_mode = "sysbox"\n[desktop]\nsandbox = false\nseccomp = "chromium"')
        self.assertEqual(loaded.desktop.seccomp, 'chromium')

    def test_policy_tokens_are_exact_and_errors_redact_values(self):
        for value in ['CHROMIUM', ' chromium', 'chromium ', '', '/tmp/profile.json',
                      'unknown', 'credential-sentinel', True, False, 1]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.load('[desktop]\nseccomp = ' +
                              (repr(value).replace("'", '"') if isinstance(value, str)
                               else str(value).lower()))
                self.assertNotIn('credential-sentinel', str(caught.exception))
                with self.assertRaises(ValueError) as caught:
                    self.load('', {'CLAUDE_DESKTOP_SECCOMP': value})
                self.assertNotIn('credential-sentinel', str(caught.exception))

    def test_detected_defaults_use_invoking_home_and_do_not_create_state(self):
        config = self.load()
        self.assertEqual((config.container.user, config.container.uid, config.container.gid),
                         ("tester", 1234, 2345))
        self.assertEqual(config.container.home, self.home)
        self.assertEqual(config.container.mounts, (self.home / "work/git",))
        self.assertEqual(config.docker_mode, "none")
        self.assertEqual((config.state_code.source, config.state_code_json.source,
                          config.state_desktop.source),
                         (self.home / ".claude", self.home / ".claude.json", self.home / ".config/Claude"))
        self.assertTrue(all((config.state_code.mount, config.state_code_json.mount,
                             config.state_desktop.mount, config.desktop.sandbox)))
        self.assertEqual(config.desktop.clipboard, "manual")
        self.assertEqual(config.environment.required, ())
        self.assertEqual(config.environment.optional, ())
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse(self.paths.config_dir.exists())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.account.uid = 9

    def test_valid_overrides_replace_invalid_detected_defaults(self):
        (self.home / 'work').mkdir()
        (self.home / 'work/git').touch()
        self.account = AccountInfo('UNSAFE DETECTED NAME', 1234, 2345, self.home)
        config = self.load('[container]\nuser = "safe_user"\nmounts = []')
        self.assertEqual(config.container.user, 'safe_user')
        self.assertEqual(config.container.mounts, ())

    def test_complete_toml_overrides_and_home_expansion(self):
        config = self.load('''docker_mode = "sysbox"
[container]
user = "other"
uid = 1
gid = 4294967294
home = "~/container"
mounts = ["~/first", "/srv/second"]
[state.code]
source = "~/code"
mount = false
[state.code_json]
source = "~/code.json"
mount = false
[state.desktop]
source = "~/desktop"
mount = false
[desktop]
sandbox = false
clipboard = "bidirectional"
[environment]
required = ["TOKEN", "_SECOND2"]
optional = ["REGION"]
''')
        self.assertEqual((config.docker_mode, config.container.user, config.container.uid,
                          config.container.gid), ("sysbox", "other", 1, 4294967294))
        self.assertEqual(config.container.home, self.home / "container")
        self.assertEqual(config.container.mounts, (self.home / "first", Path("/srv/second")))
        self.assertEqual((config.state_code.source, config.state_code_json.source,
                          config.state_desktop.source),
                         (self.home / "code", self.home / "code.json", self.home / "desktop"))
        self.assertFalse(any((config.state_code.mount, config.state_code_json.mount,
                              config.state_desktop.mount, config.desktop.sandbox)))
        self.assertEqual(config.desktop.clipboard, "bidirectional")
        self.assertEqual(config.environment.required, ("TOKEN", "_SECOND2"))
        self.assertEqual(config.environment.optional, ("REGION",))

    def test_scalar_environment_overrides_toml_without_mutating_environment(self):
        env = {
            "CLAUDE_DOCKER_MODE": "sysbox", "CLAUDE_CONTAINER_USER": "env_user",
            "CLAUDE_CONTAINER_UID": "42", "CLAUDE_CONTAINER_GID": "43",
            "CLAUDE_CONTAINER_HOME": "~/env-home", "CLAUDE_CODE_CONFIG_SOURCE": "~/env-code",
            "CLAUDE_CODE_CONFIG_MOUNT": "false", "CLAUDE_CODE_JSON_SOURCE": "~/env.json",
            "CLAUDE_CODE_JSON_MOUNT": "false", "CLAUDE_DESKTOP_CONFIG_SOURCE": "~/env-desktop",
            "CLAUDE_DESKTOP_CONFIG_MOUNT": "false", "CLAUDE_DESKTOP_SANDBOX": "false",
            "CLAUDE_DESKTOP_CLIPBOARD": "bidirectional", "UNRELATED_TOKEN": "sensitive-value",
        }
        original = dict(env)
        config = self.load('''docker_mode = "none"
[container]
user = "toml"
uid = 1
gid = 2
home = "/toml"
[state.code]
source = "/toml-code"
mount = true
[state.code_json]
source = "/toml.json"
mount = true
[state.desktop]
source = "/toml-desktop"
mount = true
[desktop]
sandbox = true
clipboard = "manual"
''', env)
        self.assertEqual(config.container.user, "env_user")
        self.assertEqual((config.container.uid, config.container.gid), (42, 43))
        self.assertEqual(config.container.home, self.home / "env-home")
        self.assertEqual(config.docker_mode, "sysbox")
        self.assertEqual((config.state_code.source, config.state_code_json.source,
                          config.state_desktop.source),
                         (self.home / "env-code", self.home / "env.json", self.home / "env-desktop"))
        self.assertFalse(any((config.state_code.mount, config.state_code_json.mount,
                              config.state_desktop.mount, config.desktop.sandbox)))
        self.assertEqual(config.desktop.clipboard, "bidirectional")
        self.assertEqual(env, original)

    def test_config_file_selection_and_explicit_argument_precedence(self):
        selected = self.home / "selected.toml"
        selected.write_text('docker_mode = "sysbox"\n')
        self.assertEqual(self.load('docker_mode = "none"',
                         {"CLAUDE_CONFIG_FILE": "~/selected.toml"}).docker_mode, "sysbox")
        self.assertEqual(self.load(env={"CLAUDE_CONFIG_FILE": str(selected)},
                                  config_file=self.paths.config_file).docker_mode, "none")

    def test_explicit_missing_config_is_an_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "CLAUDE_CONFIG_FILE"):
            self.load(env={"CLAUDE_CONFIG_FILE": str(self.root / "missing.toml")})
        with self.assertRaisesRegex(ValueError, "config_file"):
            self.load(config_file=self.root / "missing.toml")

    def test_mounts_file_replaces_toml_with_trimmed_noncomment_paths(self):
        mounts = self.home / "mounts.txt"
        mounts.write_text("  # comment\n\n ~/workspace one  \n /srv/second \n")
        config = self.load('[container]\nmounts = ["/toml"]',
                           {"CLAUDE_MOUNTS_FILE": "~/mounts.txt"})
        self.assertEqual(config.container.mounts, (self.home / "workspace one", Path("/srv/second")))
        mounts.write_text("# explicitly empty list\n")
        self.assertEqual(self.load(env={"CLAUDE_MOUNTS_FILE": str(mounts)}).container.mounts, ())
        self.assertEqual(self.load(env={"CLAUDE_MOUNTS_FILE": ""}).container.mounts, (Path("/toml"),))

    def test_toml_repeated_slashes_after_tilde_preserve_invoking_home(self):
        for value, expected in [('~//work/git', self.home / 'work/git'),
                                ('~//', self.home),
                                ('~////work/git', self.home / 'work/git')]:
            with self.subTest(path=value):
                config = self.load(f'[container]\nhome = "{value}"\nmounts = ["{value}"]')
                self.assertEqual(config.container.home, expected)
                self.assertEqual(config.container.mounts, (expected,))

    def test_mounts_file_repeated_slashes_after_tilde_preserve_invoking_home(self):
        mounts_file = self.home / 'mounts.txt'
        for value, expected in [('~//work/git', self.home / 'work/git'),
                                ('~//', self.home),
                                ('~////work/git', self.home / 'work/git')]:
            with self.subTest(path=value):
                mounts_file.write_text(value + '\n')
                config = self.load(env={'CLAUDE_MOUNTS_FILE': str(mounts_file)})
                self.assertEqual(config.container.mounts, (expected,))

    def test_toml_unknown_tables_and_keys_are_rejected(self):
        for text, key in [('typo = 1', 'typo'), ('[unknown]\na = 1', 'unknown'),
                          ('[container]\ntypo = 1', 'container.typo'),
                          ('[state.typo]\nmount = false', 'state.typo'),
                          ('[state.code]\ntypo = true', 'state.code.typo'),
                          ('[desktop]\ntypo = true', 'desktop.typo'),
                          ('[environment]\ntypo = []', 'environment.typo')]:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, key):
                self.load(text)

    def test_toml_types_are_strict_including_bool_integer_distinction(self):
        cases = [('docker_mode = 1', 'docker_mode'), ('container = []', 'container'),
                 ('state = true', 'state'), ('[state]\ncode = "bad"', 'state.code'),
                 ('[container]\nuser = 12', 'container.user'),
                 ('[container]\nuid = true', 'container.uid'),
                 ('[container]\ngid = 1.0', 'container.gid'),
                 ('[container]\nhome = 1', 'container.home'),
                 ('[container]\nmounts = "/srv"', 'container.mounts'),
                 ('[container]\nmounts = [1]', 'container.mounts'),
                 ('[state.code]\nmount = "true"', 'state.code.mount'),
                 ('[state.code_json]\nsource = false', 'state.code_json.source'),
                 ('[state.desktop]\nmount = 0', 'state.desktop.mount'),
                 ('[desktop]\nsandbox = 1', 'desktop.sandbox'),
                 ('[desktop]\nclipboard = false', 'desktop.clipboard'),
                 ('[environment]\nrequired = "TOKEN"', 'environment.required'),
                 ('[environment]\noptional = [1]', 'environment.optional')]
        for text, key in cases:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, key):
                self.load(text)

    def test_relative_empty_newline_and_other_users_home_paths_are_rejected(self):
        for value in ('relative', '', '~other/work', '~', '/absolute\\npath'):
            for table, key in [('container', 'home'), ('container', 'mounts'),
                               ('state.code', 'source'), ('state.code_json', 'source'),
                               ('state.desktop', 'source')]:
                literal = f'["{value}"]' if key == 'mounts' else f'"{value}"'
                with self.subTest(value=value, table=table, key=key):
                    with self.assertRaisesRegex(ValueError, table + '.' + key):
                        self.load(f'[{table}]\n{key} = {literal}')
        self.load('')
        for key in ('CLAUDE_CONFIG_FILE', 'CLAUDE_MOUNTS_FILE', 'CLAUDE_CONTAINER_HOME'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.load(env={key: 'relative'})

    def test_unsafe_account_names_and_out_of_range_ids_are_rejected(self):
        for value in ('root', 'tester$', 'root:name', '-option', 'with space', '', 'a' * 33, 'UPPER', 'x\\ny'):
            with self.subTest(user=value), self.assertRaisesRegex(ValueError, 'container.user'):
                self.load(f'[container]\nuser = "{value}"')
        for key in ('uid', 'gid'):
            for value in (0, -1, 4294967295, 2**40):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, 'container.' + key):
                        self.load(f'[container]\n{key} = {value}')
        self.load('')
        for key in ('CLAUDE_CONTAINER_UID', 'CLAUDE_CONTAINER_GID'):
            for value in ('0', '-1', '4294967295', '1.0', 'true', '1_000', '1\n'):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    self.load(env={key: value})

    def test_container_home_rejects_filesystem_root_including_lexical_aliases(self):
        for value in ('/', '//', '/.', '/..', '/a/..', '/a/../.', '//a/../', '/a/../../'):
            with self.subTest(home=value):
                with self.assertRaisesRegex(ValueError, 'container.home'):
                    self.load(f'[container]\nhome = "{value}"')
                self.load('')
                with self.assertRaisesRegex(ValueError, 'CLAUDE_CONTAINER_HOME'):
                    self.load(env={'CLAUDE_CONTAINER_HOME': value})

    def test_unprivileged_identity_boundaries_apply_to_defaults_and_overrides(self):
        for value in ('root', 'tester$'):
            with self.subTest(user=value), self.assertRaisesRegex(ValueError, 'CLAUDE_CONTAINER_USER'):
                self.load(env={'CLAUDE_CONTAINER_USER': value})
        for field, value in (('name', 'root'), ('name', 'tester$'), ('uid', 0), ('gid', 0)):
            with self.subTest(default=field, value=value):
                self.account = dataclasses.replace(AccountInfo('tester', 1234, 2345, self.home),
                                                   **{field: value})
                with self.assertRaisesRegex(ValueError, 'container'):
                    self.load()
        self.account = AccountInfo('root', 0, 0, Path('/'))
        config = self.load(env={'CLAUDE_CONTAINER_USER': '_supported-user9',
                                'CLAUDE_CONTAINER_UID': '1', 'CLAUDE_CONTAINER_GID': '4294967294',
                                'CLAUDE_CONTAINER_HOME': '/home/supported'})
        self.assertEqual((config.container.user, config.container.uid, config.container.gid,
                          config.container.home), ('_supported-user9', 1, 4294967294,
                                                   Path('/home/supported')))

    def test_invalid_modes_booleans_and_empty_scalar_environment_are_rejected(self):
        for text, key in [('docker_mode = "privileged"', 'docker_mode'),
                          ('[desktop]\nclipboard = "automatic"', 'desktop.clipboard')]:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, key):
                self.load(text)
        self.load('')
        keys = ('CLAUDE_DOCKER_MODE', 'CLAUDE_CONTAINER_USER', 'CLAUDE_CONTAINER_UID',
                'CLAUDE_CONTAINER_GID', 'CLAUDE_CONTAINER_HOME', 'CLAUDE_CODE_CONFIG_SOURCE',
                'CLAUDE_CODE_CONFIG_MOUNT', 'CLAUDE_CODE_JSON_SOURCE', 'CLAUDE_CODE_JSON_MOUNT',
                'CLAUDE_DESKTOP_CONFIG_SOURCE', 'CLAUDE_DESKTOP_CONFIG_MOUNT',
                'CLAUDE_DESKTOP_SANDBOX', 'CLAUDE_DESKTOP_CLIPBOARD', 'CLAUDE_CONFIG_FILE')
        for key in keys:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.load(env={key: ''})
        for value in ('maybe', '2', 'true\nfalse'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'CLAUDE_DESKTOP_SANDBOX'):
                self.load(env={'CLAUDE_DESKTOP_SANDBOX': value})

    def test_documented_boolean_spellings(self):
        for value, expected in [('true', True), ('1', True), ('yes', True), ('on', True),
                                ('false', False), ('0', False), ('no', False), ('off', False),
                                (' TRUE ', True), (' Off ', False)]:
            with self.subTest(value=value):
                self.assertEqual(self.load(env={'CLAUDE_DESKTOP_SANDBOX': value}).desktop.sandbox,
                                 expected)

    def test_duplicate_mounts_are_rejected_without_checking_mutable_source_types(self):
        file = self.home / 'file'
        file.touch()
        self.assertEqual(self.load(f'[container]\nmounts = ["{file}"]').container.mounts, (file,))
        for mounts in ('["/srv/a", "/srv/a/."]',):
            with self.subTest(mounts=mounts), self.assertRaisesRegex(ValueError, 'container.mounts'):
                self.load('[container]\nmounts = ' + mounts)
        self.load('')
        mounts_file = self.home / 'mounts.txt'
        mounts_file.write_text('/srv/a\n/srv/a\n')
        with self.assertRaisesRegex(ValueError, 'CLAUDE_MOUNTS_FILE'):
            self.load(env={'CLAUDE_MOUNTS_FILE': str(mounts_file)})
        with self.assertRaisesRegex(ValueError, 'CLAUDE_MOUNTS_FILE'):
            self.load(env={'CLAUDE_MOUNTS_FILE': str(self.home / 'missing')})

    def test_environment_allowlists_contain_only_unique_exact_names(self):
        cases = ['required = ["TOKEN", "TOKEN"]', 'optional = ["TOKEN", "TOKEN"]',
                 'required = ["TOKEN"]\noptional = ["TOKEN"]',
                 'required = ["PREFIX*"]', 'optional = ["TOKEN=value"]',
                 'required = ["1TOKEN"]', 'optional = [""]']
        for text in cases:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'environment'):
                self.load('[environment]\n' + text)

    def test_errors_do_not_leak_environment_or_invalid_secret_values(self):
        secret = 'credential-never-print'
        with self.assertRaises(ValueError) as caught:
            self.load(env={'CLAUDE_DESKTOP_SANDBOX': secret, 'AWS_SECRET_ACCESS_KEY': secret})
        self.assertIn('CLAUDE_DESKTOP_SANDBOX', str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'config'):
            self.load('invalid = [')

    def test_example_is_complete_loadable_and_private_without_overwrite(self):
        destination = self.root / 'private/config.toml'
        write_example_config(destination)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
        self.assertEqual(destination.read_text(), (ROOT / 'config.example.toml').read_text())
        config = self.load(config_file=destination)
        self.assertEqual(config.docker_mode, 'none')
        self.assertEqual(config.environment.required, ())
        self.assertEqual(config.environment.optional, ())
        self.assertGreaterEqual(len(config.container.mounts), 2)
        original = destination.read_bytes()
        with self.assertRaises((ValueError, FileExistsError)):
            write_example_config(destination)
        self.assertEqual(destination.read_bytes(), original)

    def test_example_refuses_dangling_symlink_and_preserves_existing_parent_mode(self):
        existing = self.root / 'existing'
        existing.mkdir(mode=0o755)
        write_example_config(existing / 'config.toml')
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755)
        link = self.root / 'link.toml'
        target = self.root / 'must-not-exist'
        link.symlink_to(target)
        with self.assertRaises((ValueError, FileExistsError)):
            write_example_config(link)
        self.assertFalse(target.exists())

    def test_config_init_resolves_override_and_never_needs_external_commands(self):
        destination = self.home / 'private/config.toml'
        env = {'HOME': str(self.home), 'CLAUDE_CONFIG_FILE': '~/private/config.toml', 'PATH': ''}
        command = [sys.executable, str(ROOT / 'claude-docker'), 'config', 'init']
        result = subprocess.run(command, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(destination.is_file())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        original = destination.read_bytes()
        result = subprocess.run(command, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exist', result.stderr.lower())
        self.assertEqual(destination.read_bytes(), original)
        self.assertFalse((self.home / '.claude').exists())

    def test_config_init_uses_xdg_default_and_help_does_not_create_config(self):
        env = {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.root / 'xdg'), 'PATH': ''}
        command = [sys.executable, str(ROOT / 'claude-docker')]
        for args in (['--help'], ['config', '--help'], ['config', 'init', '--help']):
            result = subprocess.run(command + args, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'xdg').exists())
        result = subprocess.run(command + ['config', 'init'], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'xdg/claude-docker/config.toml').is_file())


if __name__ == '__main__':
    unittest.main()

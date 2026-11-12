"""Exact-name forwarding returns names, never selected environment values."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_docker.environment import select_environment_names
from claude_docker.model import EnvironmentConfig


class EnvironmentTests(unittest.TestCase):
    def test_empty_policy_forwards_nothing_from_nonempty_environment(self):
        self.assertEqual(select_environment_names(EnvironmentConfig((), ()), {'SECRET': 'private'}), ())

    def test_exact_names_have_stable_required_then_present_optional_order(self):
        config = EnvironmentConfig(('Z_TOKEN', '_key2'), ('EMPTY', 'ABSENT', 'REGION'))
        env = {'REGION': 'west', 'Z_TOKEN': 'secret', '_key2': 'other', 'EMPTY': '',
               'Z_TOKEN_EXTRA': 'never', 'UNRELATED': 'never'}
        self.assertEqual(select_environment_names(config, env), ('Z_TOKEN', '_key2', 'EMPTY', 'REGION'))

    def test_missing_or_empty_required_names_fail_without_values(self):
        secret = 'sentinel secret = "quoted"\nnext line'
        for env in ({'OTHER': secret}, {'TOKEN': '', 'OTHER': secret}):
            with self.subTest(env_keys=tuple(env)):
                with self.assertRaises(ValueError) as caught:
                    select_environment_names(EnvironmentConfig(('TOKEN',), ('OTHER',)), env)
                self.assertIn('TOKEN', str(caught.exception))
                self.assertNotIn(secret, str(caught.exception))

    def test_duplicate_and_overlapping_policy_names_are_rejected(self):
        for required, optional in ((('TOKEN', 'TOKEN'), ()), ((), ('TOKEN', 'TOKEN')),
                                   (('TOKEN',), ('TOKEN',))):
            with self.subTest(required=required, optional=optional):
                with self.assertRaises(ValueError):
                    select_environment_names(EnvironmentConfig(required, optional), {'TOKEN': 'secret'})

    def test_invalid_names_literals_wildcards_and_prefixes_are_rejected_without_values(self):
        for name in ('', '9TOKEN', 'AWS-*', 'AWS_*', 'AWS_', 'TOKEN=value-sentinel',
                     'TOKEN\nvalue-sentinel', 'TOKEN-name', 'TOKEN.name', 'éTOKEN', None):
            # AWS_ is an exact valid name, even if someone intended a prefix.
            if name == 'AWS_':
                self.assertEqual(select_environment_names(EnvironmentConfig((), (name,)),
                                                         {'AWS_ACCESS_KEY_ID': 'secret'}), ())
                continue
            for required in (True, False):
                with self.subTest(name=name, required=required):
                    config = EnvironmentConfig((name,) if required else (), () if required else (name,))
                    with self.assertRaises(ValueError) as caught:
                        select_environment_names(config, {})
                    self.assertNotIn('value-sentinel', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

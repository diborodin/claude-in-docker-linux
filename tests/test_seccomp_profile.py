"""Desktop derivation preserves the upstream rules and grants four exact arguments."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from claude_docker.seccomp_profile import decode_profile, desktop_profile


BASELINE = {
    'defaultAction': 'SCMP_ACT_ERRNO',
    'defaultErrnoRet': 1,
    'archMap': [{'architecture': 'SCMP_ARCH_X86_64', 'subArchitectures': ['SCMP_ARCH_X86', 'SCMP_ARCH_X32']},
                {'architecture': 'SCMP_ARCH_RISCV64', 'subArchitectures': None}],
    'unfamiliarTopLevelField': {'nested': ['upstream value']},
    'syscalls': [
        {'names': ['clone', 'unshare', 'setns'], 'action': 'SCMP_ACT_ALLOW',
         'includes': {'caps': ['CAP_SYS_ADMIN']}},
        {'names': ['clone'], 'action': 'SCMP_ACT_ALLOW',
         'args': [{'index': 0, 'value': 2114060288, 'op': 'SCMP_CMP_MASKED_EQ'}],
         'excludes': {'caps': ['CAP_SYS_ADMIN'], 'arches': ['s390', 's390x']}},
        {'names': ['clone3'], 'action': 'SCMP_ACT_ERRNO', 'errnoRet': 38,
         'excludes': {'caps': ['CAP_SYS_ADMIN']}, 'unfamiliarMetadata': {'flag': True}},
        {'names': ['getpid'], 'action': 'SCMP_ACT_ALLOW',
         'includes': {'minKernel': '4.11', 'futureCondition': 'preserve'}},
    ],
}
DESKTOP_RULES = [
    {'names': ['clone'], 'action': 'SCMP_ACT_ALLOW',
     'args': [{'index': 0, 'value': 268435473, 'op': 'SCMP_CMP_EQ'}]},
    {'names': ['unshare'], 'action': 'SCMP_ACT_ALLOW',
     'args': [{'index': 0, 'value': 268435456, 'op': 'SCMP_CMP_EQ'}]},
    {'names': ['clone'], 'action': 'SCMP_ACT_ALLOW',
     'args': [{'index': 0, 'value': 1879048209, 'op': 'SCMP_CMP_EQ'}]},
    {'names': ['clone'], 'action': 'SCMP_ACT_ALLOW',
     'args': [{'index': 0, 'value': 536870929, 'op': 'SCMP_CMP_EQ'}]},
]


class DesktopProfileTests(unittest.TestCase):
    def test_appends_only_the_four_exact_rules_and_preserves_all_upstream_fields(self):
        base = decode_profile(json.dumps(BASELINE).encode())
        desired = desktop_profile(base)
        expected = deepcopy(BASELINE)
        expected['syscalls'].extend(DESKTOP_RULES)

        self.assertEqual(desired, expected)
        self.assertEqual(base, BASELINE)

        desired['unfamiliarTopLevelField']['nested'].append('output-only')
        desired['syscalls'][0]['includes']['caps'].clear()
        self.assertEqual(base, BASELINE)

    def test_rejects_non_denying_default(self):
        for action in ('SCMP_ACT_ALLOW', 'SCMP_ACT_LOG', 'SCMP_ACT_NOTIFY', 'unknown'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'denying defaultAction'):
                decode_profile(json.dumps(dict(BASELINE, defaultAction=action)).encode())

    def test_rejects_duplicate_json_keys_at_all_levels(self):
        cases = (
            b'{"defaultAction":"SCMP_ACT_ALLOW","defaultAction":"SCMP_ACT_ERRNO","syscalls":[]}',
            b'{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[{"names":["read"],"action":"SCMP_ACT_ALLOW","names":["clone"]}]}',
            b'{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[],"future":{"first":0,"first":1}}',
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
                decode_profile(raw)

    def test_rejects_case_variants_of_fields_that_moby_would_decode_as_known(self):
        base = dict(BASELINE, DEFAULTACTION='SCMP_ACT_ALLOW')
        with self.assertRaisesRegex(ValueError, 'defaultAction'):
            decode_profile(json.dumps(base).encode())

        base = deepcopy(BASELINE)
        base['syscalls'][2]['NAMES'] = ['clone']
        with self.assertRaisesRegex(ValueError, 'names'):
            desktop_profile(base)

    def test_rejects_unknown_profile_structures(self):
        for base in (
            [], {'syscalls': []}, {'defaultAction': 'SCMP_ACT_ERRNO'},
            dict(BASELINE, syscalls={}), dict(BASELINE, archMap=[[]]),
            dict(BASELINE, syscalls=[None]),
            dict(BASELINE, syscalls=[{'name': 'clone', 'action': 'SCMP_ACT_ERRNO'}]),
            dict(BASELINE, syscalls=[{'names': 'clone', 'action': 'SCMP_ACT_ERRNO'}]),
            dict(BASELINE, syscalls=[{'names': ['read'], 'action': 'invalid'}]),
            dict(BASELINE, syscalls=[{'names': ['read'], 'action': 'SCMP_ACT_ALLOW', 'includes': []}]),
            dict(BASELINE, syscalls=[{'names': ['read'], 'action': 'SCMP_ACT_ALLOW', 'args': [{}]}]),
        ):
            with self.subTest(base=base), self.assertRaises(ValueError):
                decode_profile(json.dumps(base).encode())

    def test_rejects_non_finite_json_values_in_unfamiliar_fields(self):
        for number in (b'NaN', b'Infinity', b'1e9999'):
            raw = b'{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[],"future":' + number + b'}'
            with self.subTest(number=number), self.assertRaises(ValueError):
                decode_profile(raw)

    def test_preserves_serializable_json_fraction_in_unfamiliar_metadata(self):
        raw = b'{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[],"future":{"fraction":0.1}}'
        base = decode_profile(raw)
        encoded = json.dumps(desktop_profile(base))
        self.assertEqual(json.loads(encoded)['future'], {'fraction': 0.1})

    def test_rejects_fractions_that_would_lose_precision_on_serialization(self):
        for number in (b'0.123456789012345678901', b'1e-400'):
            raw = b'{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[],"future":{"fraction":' + number + b'}}'
            with self.subTest(number=number), self.assertRaisesRegex(ValueError, 'precision loss'):
                decode_profile(raw)

    def test_rejects_unconditional_deny_overlapping_an_appended_rule(self):
        base = deepcopy(BASELINE)
        base['syscalls'].append({'names': ['clone', 'clone3'], 'action': 'SCMP_ACT_ERRNO'})
        with self.assertRaisesRegex(ValueError, 'clone.*SCMP_ACT_ERRNO.*overlap'):
            desktop_profile(base)

    def test_rejects_unfamiliar_conditional_deny_overlapping_an_appended_rule(self):
        base = deepcopy(BASELINE)
        base['syscalls'].append({
            'names': ['unshare'], 'action': 'SCMP_ACT_TRAP', 'includes': {'futureCondition': 'on'},
            'args': [{'index': 0, 'value': 268435456, 'op': 'SCMP_CMP_EQ'}],
        })
        with self.assertRaisesRegex(ValueError, 'unshare.*SCMP_ACT_TRAP.*overlap'):
            desktop_profile(base)

    def test_preserves_explicit_deny_for_a_disjoint_exact_argument(self):
        base = deepcopy(BASELINE)
        base['syscalls'].append({
            'names': ['clone'], 'action': 'SCMP_ACT_ERRNO', 'errnoRet': 9,
            'args': [{'index': 0, 'value': 123456, 'op': 'SCMP_CMP_EQ'}],
        })
        desired = desktop_profile(base)
        self.assertEqual(desired['syscalls'], base['syscalls'] + DESKTOP_RULES)

    def test_does_not_assume_disjointness_for_wide_or_unfamiliar_comparisons(self):
        for argument in (
            {'index': 0, 'value': (1 << 32) + 268435473, 'op': 'SCMP_CMP_EQ'},
            {'index': 0, 'value': 123, 'op': 'SCMP_CMP_EQ', 'futureComparatorField': True},
            {'index': 0, 'value': 0, 'op': 'SCMP_CMP_MASKED_EQ', 'valueTwo': 0},
        ):
            with self.subTest(argument=argument):
                base = deepcopy(BASELINE)
                base['syscalls'].append({'names': ['clone'], 'action': 'SCMP_ACT_ERRNO', 'args': [argument]})
                with self.assertRaisesRegex(ValueError, 'clone.*overlap'):
                    desktop_profile(base)


if __name__ == '__main__':
    unittest.main()

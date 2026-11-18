"""Validate upstream seccomp JSON and derive the Linux amd64 Desktop policy."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import math
import re


PATCH_REVISION = 1
# These values assume the Linux amd64 clone/unshare argument convention.
_NAMESPACE_ARGUMENTS = (
    ('clone', 268435473),
    ('unshare', 268435456),
    ('clone', 1879048209),
    ('clone', 536870929),
)
_DENY_ACTIONS = frozenset({
    'SCMP_ACT_ERRNO', 'SCMP_ACT_TRAP', 'SCMP_ACT_KILL',
    'SCMP_ACT_KILL_THREAD', 'SCMP_ACT_KILL_PROCESS',
})
_ACTIONS = _DENY_ACTIONS | {'SCMP_ACT_ALLOW', 'SCMP_ACT_LOG', 'SCMP_ACT_NOTIFY', 'SCMP_ACT_TRACE'}
_OPERATIONS = frozenset({
    'SCMP_CMP_NE', 'SCMP_CMP_LT', 'SCMP_CMP_LE', 'SCMP_CMP_EQ',
    'SCMP_CMP_GE', 'SCMP_CMP_GT', 'SCMP_CMP_MASKED_EQ',
})
_ARGUMENT_FIELDS = {'index', 'op', 'value', 'valueTwo'}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('non-finite JSON number')


def _round_trip_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('non-finite JSON number')
    try:
        # The stdlib JSON encoder uses the float representation for finite numbers.
        if Decimal(value) == Decimal(repr(parsed)):
            return parsed
    except InvalidOperation:
        pass
    raise ValueError('JSON number would cause precision loss in the Desktop policy. '
                     'Use a supported daemon baseline.')


def _decode_json(raw: bytes):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_round_trip_float)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError('invalid UTF-8 JSON') from None


def _validate_object(value, label, fields):
    if not isinstance(value, dict):
        raise ValueError(f'Unsupported seccomp {label}: expected an object')
    # Go's JSON decoder matches struct fields case-insensitively. A variant
    # could override the meaning of a canonical field after serialization.
    folded = {field.casefold(): field for field in fields}
    for key in value:
        if not isinstance(key, str):
            raise ValueError(f'Unsupported seccomp {label}: expected string keys')
        canonical = folded.get(key.casefold())
        if canonical is not None and key != canonical:
            raise ValueError(f'Unsupported seccomp {label}: use canonical field {canonical}')


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _uint(value, maximum=2**64 - 1):
    return type(value) is int and 0 <= value <= maximum


def _validate_architectures(base):
    for key in ('architectures', 'flags'):
        if key in base and not _strings(base[key]):
            raise ValueError(f'Unsupported seccomp {key}: expected a list of strings')
    if 'archMap' not in base:
        return
    if not isinstance(base['archMap'], list):
        raise ValueError('Unsupported seccomp archMap: expected a list')
    for entry in base['archMap']:
        _validate_object(entry, 'archMap entry', ('architecture', 'subArchitectures'))
        architecture = entry.get('architecture')
        if not isinstance(architecture, str) or not architecture:
            raise ValueError('Unsupported seccomp archMap: missing architecture')
        subarches = entry.get('subArchitectures')
        if subarches is not None and not _strings(subarches):
            raise ValueError('Unsupported seccomp subArchitectures: expected a list of strings or null')


def _validate_filter(value, label):
    _validate_object(value, label, ('caps', 'arches', 'minKernel'))
    for key in ('caps', 'arches'):
        if key in value and not _strings(value[key]):
            raise ValueError(f'Unsupported seccomp {label}.{key}: expected a list of strings')
    if 'minKernel' in value:
        version = value['minKernel']
        if not isinstance(version, str) or (version and not re.fullmatch(r'[0-9]+\.[0-9]+', version)):
            raise ValueError(f'Unsupported seccomp {label}.minKernel: expected a kernel version')
        if version:
            major, minor = map(int, version.split('.'))
            if not (0 <= major <= 255 and 0 <= minor <= 255 and (major or minor)):
                raise ValueError(f'Unsupported seccomp {label}.minKernel: invalid kernel version')


def _validate_syscall(rule):
    _validate_object(rule, 'syscall rule', ('name', 'names', 'action', 'args', 'includes',
                                           'excludes', 'errnoRet', 'comment'))
    names = rule.get('names')
    if 'name' in rule or not _strings(names) or not names:
        raise ValueError('Unsupported seccomp syscall rule: expected a nonempty names array')
    action = rule.get('action')
    if not isinstance(action, str) or action not in _ACTIONS:
        raise ValueError('Unsupported seccomp syscall rule: unrecognized action')
    if 'errnoRet' in rule and not _uint(rule['errnoRet']):
        raise ValueError('Unsupported seccomp syscall rule: invalid errnoRet')
    if 'comment' in rule and not isinstance(rule['comment'], str):
        raise ValueError('Unsupported seccomp syscall rule: invalid comment')
    for label in ('includes', 'excludes'):
        if label in rule:
            _validate_filter(rule[label], label)
    args = rule.get('args', [])
    if not isinstance(args, list):
        raise ValueError('Unsupported seccomp syscall rule: expected an args array')
    for arg in args:
        _validate_object(arg, 'syscall argument', _ARGUMENT_FIELDS)
        operation = arg.get('op')
        if (not _uint(arg.get('index'), 5) or not _uint(arg.get('value'))
                or not _uint(arg.get('valueTwo', 0))
                or not isinstance(operation, str) or operation not in _OPERATIONS):
            raise ValueError('Unsupported seccomp syscall argument: invalid index, value, or operation')


def _validate_profile(base):
    _validate_object(base, 'profile', ('defaultAction', 'defaultErrnoRet', 'architectures',
                                       'archMap', 'flags', 'listenerPath', 'listenerMetadata', 'syscalls'))
    action = base.get('defaultAction')
    if not isinstance(action, str) or action not in _DENY_ACTIONS:
        raise ValueError('Unsupported seccomp profile: expected a denying defaultAction')
    if 'defaultErrnoRet' in base and not _uint(base['defaultErrnoRet']):
        raise ValueError('Unsupported seccomp profile: invalid defaultErrnoRet')
    for key in ('listenerPath', 'listenerMetadata'):
        if key in base and not isinstance(base[key], str):
            raise ValueError(f'Unsupported seccomp {key}: expected a string')
    _validate_architectures(base)
    if not isinstance(base.get('syscalls'), list):
        raise ValueError('Unsupported seccomp profile: expected a syscalls array')
    for rule in base['syscalls']:
        _validate_syscall(rule)


def decode_profile(raw: bytes) -> dict:
    """Read an upstream profile without normalizing or dropping its fields."""
    base = _decode_json(raw)
    _validate_profile(base)
    return base


def _may_overlap(rule, value):
    args = rule.get('args', [])
    if len(args) != 1:
        return True
    arg = args[0]
    # Prove disjointness only for a known exact arg0 comparison whose value
    # fits the narrowest argument width in the preserved compatibility ABIs.
    return (not arg.keys() <= _ARGUMENT_FIELDS or arg['index'] != 0 or arg['op'] != 'SCMP_CMP_EQ'
            or arg['value'] >= 2**32 or arg['value'] == value)


def desktop_profile(base: dict) -> dict:
    """Copy the baseline and append only the four Desktop namespace predicates."""
    _validate_profile(base)
    for rule in base['syscalls']:
        if rule['action'] == 'SCMP_ACT_ALLOW':
            continue
        for syscall, value in _NAMESPACE_ARGUMENTS:
            if syscall in rule['names'] and _may_overlap(rule, value):
                raise ValueError(f'Upstream seccomp rule for {syscall} ({rule["action"]}) may overlap '
                                 'a Desktop namespace rule. Use a supported daemon baseline.')
    profile = deepcopy(base)
    profile['syscalls'].extend(
        {'names': [syscall], 'action': 'SCMP_ACT_ALLOW',
         'args': [{'index': 0, 'value': value, 'op': 'SCMP_CMP_EQ'}]}
        for syscall, value in _NAMESPACE_ARGUMENTS
    )
    return profile

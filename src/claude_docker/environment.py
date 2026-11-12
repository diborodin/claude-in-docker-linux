"""Select explicitly configured environment names without returning values."""

from collections.abc import Mapping
import re

from .model import EnvironmentConfig


def select_environment_names(config: EnvironmentConfig, env: Mapping[str, str]) -> tuple[str, ...]:
    """Require nonempty required values and preserve present optional names."""
    seen = set()
    for group, names in (('required', config.required), ('optional', config.optional)):
        for name in names:
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                # Invalid entries can be accidental NAME=secret assignments.
                raise ValueError(f'environment.{group}: expected exact environment variable names')
            if name in seen:
                raise ValueError(f'environment: duplicate or overlapping name: {name}')
            seen.add(name)
    for name in config.required:
        if not env.get(name):
            raise ValueError(f'environment.required: missing or empty variable: {name}')
    return config.required + tuple(name for name in config.optional if name in env)

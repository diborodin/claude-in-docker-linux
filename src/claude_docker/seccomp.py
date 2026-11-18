"""Resolve the optional Desktop profile against the selected outer daemon."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import seccomp_cache
from .commands import CommandRunner, docker_environment
from .model import AppConfig
from .paths import resolve_host_paths


def validate_chromium_daemon(server: Mapping[str, object], info: Mapping[str, object], *,
                             docker_mode: str) -> dict[str, str]:
    error = 'Desktop chromium seccomp policy requires identifiable Linux amd64 Docker Engine metadata and builtin seccomp'
    if docker_mode not in ('none', 'sysbox') or not isinstance(server, Mapping) or not isinstance(info, Mapping):
        raise ValueError(error)
    platform = server.get('Platform')
    name = platform.get('Name') if isinstance(platform, Mapping) else None
    version = server.get('Version')
    commit = server.get('GitCommit')
    arches = ('amd64', 'x86_64')
    security = info.get('SecurityOptions')
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,199}', name)
            or not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', version)
            or version != info.get('ServerVersion')
            or not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{7,40}', commit)
            or server.get('Os') != 'linux' or info.get('OSType') != 'linux'
            or server.get('Arch') not in arches or info.get('Architecture') not in arches
            or not isinstance(security, list) or not all(isinstance(item, str) for item in security)
            or [item for item in security if item.split(',')[0] == 'name=seccomp'] != ['name=seccomp,profile=builtin']):
        raise ValueError(error)
    components = server.get('Components', [])
    if not isinstance(components, list):
        raise ValueError(error)
    for component in components:
        if not isinstance(component, Mapping):
            raise ValueError(error)
        if component.get('Name') == 'Engine':
            details = component.get('Details', {})
            if not isinstance(details, Mapping):
                raise ValueError(error)
            if component.get('Version', version) != version:
                raise ValueError(error)
            other = details.get('GitCommit', commit)
            if (not isinstance(other, str) or not re.fullmatch('[a-f0-9]{7,40}', other)
                    or not (other.startswith(commit) or commit.startswith(other))):
                raise ValueError(error)
            commit = max((commit, other), key=len)
    return dict(engine=name, version=version, git_commit=commit, os='linux', arch='amd64')


def _daemon_json(runner: CommandRunner, argv: list[str]) -> Mapping[str, object]:
    try:
        result = runner.run(argv, capture_output=True, env=docker_environment(os.environ))
        if result.returncode:
            raise ValueError('command failed')
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError('malformed response')
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise ValueError('Cannot verify selected Docker daemon for Desktop chromium seccomp policy') from None
    return data


def resolve_desktop_seccomp(config: AppConfig, runner: CommandRunner, *, program_dir: Path,
                            unsafe: bool = False, cache_dir: Path | None = None,
                            create: bool = True) -> Path | None:
    if config.desktop.seccomp == 'default':
        return None
    if unsafe or not config.desktop.sandbox:
        raise ValueError('Desktop chromium seccomp policy requires the internal sandbox enabled')
    server = _daemon_json(runner, ['docker', 'version', '--format', '{{json .Server}}'])
    info = _daemon_json(runner, ['docker', 'info', '--format', '{{json .}}'])
    identity = validate_chromium_daemon(server, info, docker_mode=config.docker_mode)
    if cache_dir is None:
        cache_dir = resolve_host_paths(os.environ, os.getuid(), create_runtime=False).cache_dir
    path = seccomp_cache.resolve_cached_profile(cache_dir, identity, create=create)
    if create:
        print(f"Desktop chromium seccomp for {identity['engine']} {identity['version']} "
              f"({identity['git_commit']}): {path}", file=sys.stderr)
    return path

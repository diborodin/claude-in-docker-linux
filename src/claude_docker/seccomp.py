"""Integrity and selected-daemon checks for the explicit Desktop policy."""

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from .commands import CommandRunner
from .model import AppConfig


POLICY_RELATIVE_PATH = Path('security/desktop-seccomp-v0.2.4-linux-amd64.json')
_POLICY_SHA256 = 'f245d64a3213793f730f394d44c507145b13ffc8d05acb21b5146413aa395a74'


def chromium_policy_path(program_dir: Path) -> Path:
    """Return the verified packaged regular file, never an alternate policy."""
    path = program_dir.absolute() / POLICY_RELATIVE_PATH
    try:
        current = program_dir.absolute()
        for part in POLICY_RELATIVE_PATH.parts:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('symlink')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('file type')
            digest = hashlib.sha256(stream.read()).hexdigest()
        if digest != _POLICY_SHA256:
            raise ValueError('checksum')
    except (OSError, ValueError):
        raise ValueError('Desktop chromium seccomp policy is missing or failed integrity verification') from None
    return path


def validate_chromium_daemon(server: Mapping[str, object], info: Mapping[str, object], *,
                             docker_mode: str) -> None:
    """Fail closed unless the selected daemon matches the qualified policy."""
    error = 'Desktop chromium seccomp policy requires Docker Engine Community 29.9.2, Linux amd64, builtin seccomp and docker_mode=none'
    if docker_mode != 'none' or not isinstance(server, Mapping) or not isinstance(info, Mapping):
        raise ValueError(error)
    platform = server.get('Platform')
    arches = ('amd64', 'x86_64')
    security = info.get('SecurityOptions')
    if (not isinstance(platform, Mapping) or platform.get('Name') != 'Docker Engine - Community'
            or server.get('Version') != '29.9.2' or info.get('ServerVersion') != '29.9.2'
            or server.get('Os') != 'linux' or info.get('OSType') != 'linux'
            or server.get('Arch') not in arches or info.get('Architecture') not in arches
            or not isinstance(security, list) or not all(isinstance(item, str) for item in security)
            or 'name=seccomp,profile=builtin' not in security):
        raise ValueError(error)


def _daemon_json(runner: CommandRunner, argv: list[str]) -> Mapping[str, object]:
    try:
        result = runner.run(argv, capture_output=True)
        if result.returncode:
            raise ValueError('command failed')
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError('malformed response')
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise ValueError('Cannot verify selected Docker daemon for Desktop chromium seccomp policy') from None
    return data


def resolve_desktop_seccomp(config: AppConfig, runner: CommandRunner, *, program_dir: Path,
                            unsafe: bool = False) -> Path | None:
    """Resolve only an explicitly selected policy; defaults have no side effects."""
    if config.desktop.seccomp == 'default':
        return None
    if unsafe or not config.desktop.sandbox:
        raise ValueError('Desktop chromium seccomp policy requires the internal sandbox enabled')
    if config.docker_mode != 'none':
        raise ValueError('Desktop chromium seccomp policy requires docker_mode=none')
    path = chromium_policy_path(program_dir)
    server = _daemon_json(runner, ['docker', 'version', '--format', '{{json .Server}}'])
    info = _daemon_json(runner, ['docker', 'info', '--format', '{{json .}}'])
    validate_chromium_daemon(server, info, docker_mode=config.docker_mode)
    return path

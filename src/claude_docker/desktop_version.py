"""Select an exact stable Claude Desktop release via the selected Docker daemon."""

from collections.abc import Mapping
from pathlib import Path
import re

from .commands import CommandRunner
from .desktop_resolver import resolve_desktop_candidate
from .paths import HostPaths
from .version_pin import MAX_VERSION_BYTES, select_version_pin


def desktop_version_path(paths: HostPaths) -> Path:
    return paths.config_dir / 'claude-desktop.version'


def _parse_version(raw: bytes, source: str) -> str:
    version = raw.strip()
    epoch, has_epoch, remainder = version.partition(b':')
    release = remainder if has_epoch else version
    upstream, has_revision, revision = release.rpartition(b'-')
    if not has_revision:
        upstream = release
    if (len(raw) > MAX_VERSION_BYTES or len(version) >= MAX_VERSION_BYTES
            or (has_epoch and (not epoch.isdigit() or int(epoch) > 2_147_483_647))
            or not re.fullmatch(rb'[0-9][0-9A-Za-z.+~:-]*', upstream)
            or (has_revision and not re.fullmatch(rb'[0-9A-Za-z.+~]+', revision))):
        raise ValueError(f'Invalid Claude Desktop version in {source}; expected an exact Debian package version')
    return version.decode('ascii')


def _latest_version(apt_script: bytes, runner: CommandRunner, env: Mapping[str, str]) -> str:
    candidate = resolve_desktop_candidate(apt_script, runner, env)
    return _parse_version(candidate.encode('utf-8'), 'signed apt candidate')


def select_desktop_version(program_dir: Path, paths: HostPaths, runner: CommandRunner, env: Mapping[str, str],
                           *, apt_script: bytes, refresh: bool) -> str:
    return select_version_pin(program_dir, paths, desktop_version_path(paths), product='Claude Desktop',
                              parse=_parse_version, resolve=lambda: _latest_version(apt_script, runner, env),
                              refresh=refresh)

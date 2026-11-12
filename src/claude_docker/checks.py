"""Read-only host diagnostics and explicitly requested Docker maintenance."""

import csv
from dataclasses import dataclass
import io
import json
import os
import platform
from pathlib import Path
import re
import shutil
import sys
from typing import Literal

from .commands import CommandRunner
from .compose import ComposeProject, validate_desktop_uid
from .model import AppConfig
from .paths import resolve_program_dir
from .seccomp import resolve_desktop_seccomp


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Literal['ok', 'warning', 'error', 'skip']
    message: str


def run_checks(config: AppConfig, *, smoke: bool, runner: CommandRunner,
               desktop: bool = False, clipboard: bool = False,
               clipboard_sync: bool = False, no_sandbox: bool = False,
               program_dir: Path | None = None) -> list[CheckResult]:
    """Probe prerequisites without creating files or containers unless smoke=True."""
    results = [
        CheckResult('python', 'ok' if sys.version_info >= (3, 11) else 'error',
                    'Python 3.11 or newer is required.'),
        CheckResult('linux', 'ok' if platform.system() == 'Linux' else 'error',
                    'Linux is required.'),
        CheckResult('architecture', 'ok' if platform.machine().lower() in (
            'x86_64', 'amd64', 'aarch64', 'arm64') else 'error',
                    'Supported architectures: x86_64 and arm64.'),
    ]

    def probe(argv: list[str]):
        try:
            return runner.run(argv, check=False, capture_output=True)
        except OSError:
            return None

    docker = probe(['docker', '--version'])
    docker_ok = docker is not None and docker.returncode == 0
    results.append(CheckResult('docker', 'ok' if docker_ok else 'error',
                               'Docker CLI is available.' if docker_ok else 'Install the Docker CLI.'))
    daemon_ok = False
    if docker_ok:
        daemon = probe(['docker', 'info', '--format', '{{.ServerVersion}}'])
        daemon_ok = daemon is not None and daemon.returncode == 0
        results.append(CheckResult('daemon', 'ok' if daemon_ok else 'error',
            'Docker daemon is reachable.' if daemon_ok else 'Cannot reach the Docker daemon.'))
        compose = probe(['docker', 'compose', 'version', '--short'])
        version = (re.fullmatch(
            r'v?([1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
            r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
            r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?', compose.stdout.strip())
            if compose is not None and compose.returncode == 0 else None)
        compose_ok = version is not None and int(version.group(1)) >= 2
        results.append(CheckResult('compose', 'ok' if compose_ok else 'error',
            'Docker Compose plugin is available.' if compose_ok else 'Install the Docker Compose plugin (v2 or newer).'))
    else:
        for name in ('daemon', 'compose'):
            results.append(CheckResult(name, 'skip', 'Docker CLI is unavailable.'))

    if config.docker_mode == 'sysbox':
        # Python's stdlib reads /etc/os-release (falling back to /usr/lib/os-release).
        try:
            distro = platform.freedesktop_os_release()
        except OSError:
            distro = {}
        if distro.get('ID') == 'ubuntu' and distro.get('VERSION_ID') == '24.04':
            results.append(CheckResult('sysbox-host', 'ok', 'Ubuntu 24.04 is the Sysbox reference host.'))
        elif distro.get('ID') == 'debian' and distro.get('VERSION_ID', '').split('.')[0] == '13':
            results.append(CheckResult('sysbox-host', 'warning', 'Debian 13 Sysbox support is experimental.'))
        else:
            results.append(CheckResult('sysbox-host', 'warning',
                'This distribution is outside the Ubuntu 24.04 Sysbox reference host.'))
        if daemon_ok:
            runtime = probe(['docker', 'info', '--format', '{{json .Runtimes}}'])
            try:
                runtimes = json.loads(runtime.stdout) if runtime is not None and runtime.returncode == 0 else None
            except (ValueError, TypeError):
                runtimes = None
            runtime_ok = isinstance(runtimes, dict) and 'sysbox-runc' in runtimes
            results.append(CheckResult('sysbox-runtime', 'ok' if runtime_ok else 'error',
                'sysbox-runc is registered.' if runtime_ok else 'Register sysbox-runc with the Docker daemon.'))
        else:
            results.append(CheckResult('sysbox-runtime', 'skip', 'Docker daemon is unavailable.'))

    for names, missing_status in ((config.environment.required, 'error'),
                                  (config.environment.optional, 'warning')):
        for name in names:
            present = (bool(os.environ.get(name)) if missing_status == 'error'
                       else name in os.environ)
            results.append(CheckResult('environment:' + name, 'ok' if present else missing_status,
                                       'set' if present else 'missing'))

    if desktop or clipboard or clipboard_sync or no_sandbox:
        try:
            validate_desktop_uid(config, os.getuid())
        except ValueError as error:
            results.append(CheckResult('desktop-uid', 'error', str(error)))
        else:
            results.append(CheckResult('desktop-uid', 'ok',
                                       'Configured container.uid matches the host UID for private X11 grants.'))
        display_ok = bool(os.environ.get('DISPLAY'))
        results.append(CheckResult('display', 'ok' if display_ok else 'error',
                                   'Host DISPLAY is set.' if display_ok else 'Set the host DISPLAY.'))
        synchronize = clipboard_sync or config.desktop.clipboard == 'bidirectional'
        tools = ['Xephyr', 'xauth', 'setxkbmap']
        if clipboard or synchronize:
            tools.append('xclip')
        for tool in tools:
            available = shutil.which(tool) is not None
            results.append(CheckResult(tool, 'ok' if available else 'error',
                                       f'{tool} is available.' if available else f'Install {tool}.'))
        results.append(CheckResult('clipboard-mode', 'warning' if synchronize else 'ok',
            'Requested policy for next Desktop start: ' +
            ('bidirectional host/Desktop clipboard transfer, including secrets.' if synchronize
             else 'manual clipboard transfers.')))
        unsafe = no_sandbox or not config.desktop.sandbox
        results.append(CheckResult('sandbox-mode', 'warning' if unsafe else 'ok',
            'Requested policy for next Desktop start: ' +
            ('Chromium sandbox disabled (unsafe).' if unsafe else 'Chromium sandbox enabled.')))
        try:
            # Default policy has no packaged-asset dependency or daemon probes.
            selected_program = program_dir
            if selected_program is None and config.desktop.seccomp != 'default':
                selected_program = resolve_program_dir(
                    Path(__file__).resolve().parents[2] / 'claude-docker', os.environ)
            resolve_desktop_seccomp(config, runner,
                                    program_dir=selected_program or Path('.'), unsafe=unsafe)
        except ValueError as error:
            results.append(CheckResult('desktop-seccomp', 'error', str(error)))
        else:
            results.append(CheckResult('desktop-seccomp', 'ok',
                'Requested policy for next Desktop start: ' + config.desktop.seccomp +
                ('. Bundled asset integrity and selected-daemon compatibility verified.'
                 if config.desktop.seccomp == 'chromium' else '. Docker default policy.')))
        results.append(CheckResult('desktop-lifetime', 'warning',
            'Running Desktop lifetime was not verified. Existing per-run sandbox, clipboard and '
            'container security choices persist until restart or stop; changing configuration alone '
            'does not reset them.'))

    if smoke:
        if any(result.status == 'error' for result in results):
            results.append(CheckResult('smoke', 'skip', 'Resolve prerequisite errors before running smoke.'))
        else:
            argv = ['docker', 'run', '--rm']
            if config.docker_mode == 'sysbox':
                argv.extend(['--runtime', 'sysbox-runc'])
            argv.extend(['--entrypoint', '/bin/true', 'debian:13-slim'])
            result = probe(argv)
            success = result is not None and result.returncode == 0
            results.append(CheckResult('smoke', 'ok' if success else 'error',
                'Temporary container completed.' if success else 'Temporary container smoke failed.'))
    return results


def lock_tools(project: ComposeProject) -> None:
    """Run the image's mise entrypoint as the invoking user, with one writable bind."""
    program = project.program_dir.resolve()
    if (program / 'manifest.json').exists() or (program / 'manifest.json').is_symlink():
        raise ValueError('tools lock requires a source/development checkout; installed bundles are immutable. '
                         'Obtain a trusted source checkout and run ./claude-docker tools lock there.')
    image_dir = project.program_dir / 'image'
    if not image_dir.is_dir():
        raise ValueError(f'Tool image directory does not exist: {image_dir}')
    # Docker parses --mount using CSV; encode paths containing commas literally.
    mount = io.StringIO()
    csv.writer(mount, lineterminator='').writerow([
        'type=bind', 'source=' + str(image_dir), 'target=/workspace'])
    project.runner.run([
        'docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}',
        '--mount', mount.getvalue(), '--workdir', '/workspace',
        '--tmpfs', '/tmp:rw,mode=1777', '--env', 'HOME=/tmp',
        '--env', 'MISE_CACHE_DIR=/tmp/mise-cache', '--env', 'MISE_STATE_DIR=/tmp/mise-state',
        '--env', 'MISE_DATA_DIR=/tmp/mise-data', '--env', 'MISE_TRUSTED_CONFIG_PATHS=/workspace',
        'jdxcode/mise:2026.11.5', 'lock', '--platform', 'linux-x64,linux-arm64',
    ], env=project.environment)


def cleanup_inner_docker(project: ComposeProject, *, yes: bool) -> None:
    """Remove just the current host UID's managed Compose inner-data volume."""
    target = project.project_name + '_inner-docker-data'
    print(f'Inner Docker data volume: {target}')
    if not yes:
        if not sys.stdin.isatty():
            raise ValueError('Cleanup requires interactive confirmation; use --yes for noninteractive cleanup.')
        try:
            answer = input(f'Type {target} to permanently delete this volume: ')
        except EOFError:
            answer = ''
        if answer != target:
            print('Cleanup cancelled.')
            return
    inspected = project.runner.run(['docker', 'volume', 'inspect', target],
                                   env=project.environment, capture_output=True)
    prefix = 'io.github.diborodin.claude-in-docker-linux.'
    required = {prefix + 'managed': 'true', prefix + 'owner-uid': str(project.host_uid),
                'com.docker.compose.project': project.project_name,
                'com.docker.compose.volume': 'inner-docker-data'}
    try:
        volumes = json.loads(inspected.stdout)
        valid = (isinstance(volumes, list) and len(volumes) == 1
                 and isinstance(volumes[0], dict) and volumes[0].get('Name') == target
                 and isinstance(volumes[0].get('Labels'), dict)
                 and all(volumes[0]['Labels'].get(key) == value for key, value in required.items()))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError(f'Refusing cleanup: volume {target} does not have the expected ownership labels.')
    project.runner.run(['docker', 'volume', 'rm', target], env=project.environment)

"""Run the signed Desktop candidate lookup with a bounded Docker lifecycle."""

from collections.abc import Callable, Mapping
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from .commands import CommandRunner, docker_environment, resolver_signals
from .tool_config import MISE_IMAGE


_RESOLVE_TIMEOUT = 300
_CLEANUP_TIMEOUT = 10
_CACHE_LABEL = 'org.claude-docker.desktop-resolver'
_OWNER_LABEL = 'org.claude-docker.desktop-resolver-invocation'
_SCRIPT = '/usr/local/lib/claude-desktop-apt.sh'
_DOCKERFILE = f'''FROM {MISE_IMAGE} AS mise
FROM ubuntu:26.04
COPY --from=mise /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY claude-desktop-apt.sh {_SCRIPT}
LABEL {_CACHE_LABEL}={{revision}}
'''
_ID = r'[a-f0-9]{64}'


def _progress(message: str) -> None:
    print(f'claude-docker: {message}', file=sys.stderr, flush=True)


def resolve_desktop_candidate(apt_script: bytes, runner: CommandRunner, env: Mapping[str, str]) -> str:
    with resolver_signals() as check_cancelled:
        return _resolve_candidate(apt_script, runner, env, check_cancelled)


def _resolve_candidate(apt_script: bytes, runner: CommandRunner, env: Mapping[str, str],
                       check_cancelled: Callable[[], None] | None) -> str:
    deadline = time.monotonic() + _RESOLVE_TIMEOUT
    docker_env = docker_environment(env)

    def docker(*arguments: str, until: float = deadline, check: bool = True,
               capture_output: bool = True, stream_stderr: bool = True,
               cancellable: bool = True) -> subprocess.CompletedProcess[str]:
        cancellation = check_cancelled if cancellable else None
        if cancellation is not None:
            cancellation()
        command = ['docker', *arguments]
        remaining = until - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        return runner.run(command, env=docker_env, timeout=remaining, check=check,
                          capture_output=capture_output, stream_stderr=stream_stderr,
                          check_cancelled=cancellation)

    def remove_container(name: str, container_id: str | None) -> bool:
        cleanup_deadline = time.monotonic() + _CLEANUP_TIMEOUT
        try:
            # Create can finish on the daemon after a timeout or transport
            # failure. Recover only an ID with this invocation's label.
            while container_id is None:
                inspected = docker('container', 'inspect', '--format',
                                   f'{{{{.Id}}}} {{{{index .Config.Labels "{_OWNER_LABEL}"}}}}', name,
                                   until=cleanup_deadline, check=False, stream_stderr=False, cancellable=False)
                if inspected.returncode == 0:
                    match = re.fullmatch(rf'({_ID}) {name}', inspected.stdout.strip())
                    if match is None:
                        return True
                    container_id = match[1]
                elif time.monotonic() + 0.25 >= cleanup_deadline:
                    break
                else:
                    time.sleep(0.25)
            if container_id is not None:
                removed = docker('rm', '--force', container_id, until=cleanup_deadline,
                                 check=False, stream_stderr=False, cancellable=False)
                if removed.returncode == 0 or 'No such container' in removed.stderr:
                    return True
        except (OSError, subprocess.SubprocessError):
            pass
        if container_id is None:
            _progress(f'Could not confirm whether temporary Desktop resolver {name} was created or removed.')
        else:
            _progress(f'Could not confirm removal of temporary Desktop resolver {name}.')
        return False

    phase = 'checking the Docker daemon'
    try:
        _progress('Checking the Docker daemon for Claude Desktop release resolution.')
        platform = docker('version', '--format', '{{.Server.Os}}/{{.Server.Arch}}').stdout.strip()
        if platform not in ('linux/amd64', 'linux/arm64'):
            raise ValueError('Unsupported Claude Desktop platform; the Docker daemon must run Linux amd64 or arm64')

        revision = hashlib.sha256(_DOCKERFILE.encode() + platform.encode() + apt_script).hexdigest()
        tag = f'claude-docker-desktop-resolver:{revision}'
        inspect = ('image', 'inspect', '--format',
                   f'{{{{.Os}}}}/{{{{.Architecture}}}} {{{{index .Config.Labels "{_CACHE_LABEL}"}}}} {{{{.Id}}}}', tag)
        phase = 'checking the cached Desktop resolver image'
        _progress(f'Checking the cached Claude Desktop resolver image for {platform}.')
        image = docker(*inspect, check=False, stream_stderr=False)
        if image.returncode:
            if 'No such image' not in image.stderr:
                image.check_returncode()
            phase = 'preparing the Ubuntu resolver helper'
            _progress(f'Preparing the Claude Desktop resolver image for {platform}.')
            with tempfile.TemporaryDirectory(prefix='claude-desktop-resolver-') as context:
                (Path(context) / 'Dockerfile').write_text(_DOCKERFILE.format(revision=revision), encoding='ascii')
                (Path(context) / 'claude-desktop-apt.sh').write_bytes(apt_script)
                docker('build', '--progress', 'plain', '--platform', platform, '--tag', tag, context,
                       capture_output=False)
            phase = 'checking the cached Desktop resolver image'
            image = docker(*inspect)
        cached = re.fullmatch(rf'{platform} {revision} (sha256:{_ID})', image.stdout.strip())
        if image.returncode or cached is None:
            raise ValueError('Cannot verify the cached Claude Desktop resolver image on the selected Docker daemon')

        phase = 'checking fresh signed Anthropic package metadata'
        _progress('Checking fresh signed Anthropic package metadata for Claude Desktop.')
        name = f'claude-desktop-resolver-{uuid.uuid4().hex}'
        container_id = None
        try:
            created = docker('create', '--name', name, '--label', f'{_OWNER_LABEL}={name}',
                             '--platform', platform, '--pull', 'never', cached[1],
                             '/bin/sh', _SCRIPT, 'resolve').stdout.strip()
            if not re.fullmatch(_ID, created):
                raise ValueError('Cannot identify the temporary Claude Desktop resolver container')
            container_id = created
            result = docker('start', '--attach', container_id)
        finally:
            cleaned = remove_container(name, container_id)
        if not cleaned:
            raise ValueError('Could not remove the temporary Claude Desktop resolver container')
        return result.stdout
    except subprocess.TimeoutExpired:
        raise ValueError(f'Claude Desktop release resolution timed out after {_RESOLVE_TIMEOUT} seconds '
                         f'while {phase}') from None
    except (OSError, subprocess.SubprocessError) as error:
        status = f' (Docker exit {error.returncode})' if isinstance(error, subprocess.CalledProcessError) else ''
        raise ValueError(f'Cannot resolve the latest Claude Desktop version while {phase}{status}') from None

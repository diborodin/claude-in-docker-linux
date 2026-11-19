"""Prepare selected app credentials and host files in a trusted Bash child."""

from collections.abc import Mapping
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .commands import _DOCKER_ENVIRONMENT, _collect_before_reap, child_session, resolver_signals
from .config import _OVERRIDES
from .environment import select_environment_names
from .model import EnvironmentConfig, HooksConfig


_MAX_ENVIRONMENT_BYTES = 256 * 1024
_STARTUP_VARIABLES = frozenset(('BASH_ENV', 'ENV', 'BASHOPTS', 'SHELLOPTS', 'BASH_XTRACEFD'))
_CONTROL_NAMES = frozenset(_OVERRIDES) | frozenset(_DOCKER_ENVIRONMENT) | _STARTUP_VARIABLES | {
    'CLAUDE_CONFIG_FILE', 'CLAUDE_DOCKER_DIR', 'CLAUDE_MOUNTS_FILE', 'CLAUDE_MISE_CONFIG_FILE',
    'DISPLAY', 'XAUTHORITY', 'BASH', 'PWD', 'OLDPWD', 'SHLVL', '_',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'LOCALDOMAIN', 'RES_OPTIONS', 'HOSTALIASES',
    'GODEBUG', 'GOMAXPROCS', 'GOMEMLIMIT', 'GOGC', 'GCONV_PATH', 'LOCPATH', 'GLIBC_TUNABLES',
}
_CONTROL_PREFIXES = ('DOCKER_', 'COMPOSE_', 'XDG_', 'SSH_', 'LD_', 'DYLD_', 'MALLOC_', 'PYTHON')

# Keep the protocol separate from the script's ordinary stdout and stderr.
# The script is sourced as a top-level command so Bash honors errexit.
_SOURCE_HOOK = '''
readonly __claude_docker_source="$1" __claude_docker_output="$2" __claude_docker_env="$3"
shift 3
builtin source "$__claude_docker_source"
__claude_docker_status=$?
(( __claude_docker_status == 0 )) || exit "$__claude_docker_status"
command "$__claude_docker_env" -0 >&"$__claude_docker_output"
'''


def _parse_exports(output: bytearray) -> dict[str, str]:
    if not output.endswith(b'\0'):
        raise ValueError('hooks.pre_launch: script did not return a complete environment')
    exports = {}
    for entry in bytes(output[:-1]).split(b'\0'):
        key, separator, value = entry.partition(b'=')
        name = os.fsdecode(key)
        if not name or not separator or name in exports:
            raise ValueError('hooks.pre_launch: malformed exported environment')
        exports[name] = os.fsdecode(value)
    return exports


def _hook_exports(script: Path, timeout: int, env: Mapping[str, str]) -> dict[str, str]:
    try:
        if not script.is_file():
            raise ValueError('hooks.pre_launch: script must be a regular file')
        bash = shutil.which('bash', path=env.get('PATH', os.defpath))
        if bash is None:
            raise ValueError('hooks.pre_launch: Bash is required on the host')
        exporter = shutil.which('env', path=os.defpath)
        if exporter is None:
            raise ValueError('hooks.pre_launch: the host env utility is required')

        hook_env = {name: value for name, value in env.items() if name not in _STARTUP_VARIABLES}
        output = bytearray()
        deadline = time.monotonic() + min(timeout, sys.maxsize)
        with resolver_signals() as check_cancelled, ExitStack() as stack:
            read_fd, write_fd = os.pipe()
            stack.callback(os.close, read_fd)
            with os.fdopen(write_fd, 'wb') as writer:
                arguments = [bash, '--noprofile', '--norc', '-ec', _SOURCE_HOOK,
                             'claude-docker pre-launch', str(script), str(write_fd), exporter]
                with child_session(arguments, env=hook_env, pass_fds=(write_fd,)) as process:
                    writer.close()
                    _collect_before_reap(process, deadline, timeout, check_cancelled,
                                         extra_output=(read_fd, output, _MAX_ENVIRONMENT_BYTES))
                    exports = _parse_exports(output)
                    process.wait()
    except subprocess.TimeoutExpired:
        raise ValueError(f'hooks.pre_launch: timed out after {timeout} seconds') from None
    except subprocess.CalledProcessError as error:
        raise ValueError(f'hooks.pre_launch: Bash script failed (exit {error.returncode})') from None
    except BufferError:
        raise ValueError('hooks.pre_launch: exported environment exceeds the size limit') from None
    except OSError:
        raise ValueError('hooks.pre_launch: could not start Bash or collect its environment') from None

    return exports


def launch_environment(hooks: HooksConfig, config: EnvironmentConfig,
                       env: Mapping[str, str]) -> tuple[tuple[str, ...], dict[str, str] | None]:
    """Apply hook exports and unsets only to names explicitly selected for the app."""
    if hooks.pre_launch is None:
        return select_environment_names(config, env), None

    exports = _hook_exports(hooks.pre_launch, hooks.timeout_seconds, env)
    child_env = dict(env)
    # Docker consumes names in its own environment for --env NAME. Keep the
    # caller's launcher and transport settings even when listed for forwarding.
    for name in (*config.required, *config.optional):
        if name.upper() in _CONTROL_NAMES or name.startswith(_CONTROL_PREFIXES):
            continue
        if name in exports:
            child_env[name] = exports[name]
        else:
            child_env.pop(name, None)
    return select_environment_names(config, child_env), child_env

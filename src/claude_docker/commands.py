"""One shell-free subprocess boundary shared by launcher operations."""

from collections.abc import Mapping, Sequence
import subprocess


# Keep Docker client configuration usable while excluding arbitrary application
# credentials and ambient Compose settings. Container credentials use docker exec.
_DOCKER_ENVIRONMENT = (
    'PATH', 'HOME', 'XDG_RUNTIME_DIR', 'DOCKER_HOST', 'DOCKER_CONTEXT',
    'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION',
    'SSH_AUTH_SOCK', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
)


def docker_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Keep Docker transport settings without ambient application credentials."""
    return {key: env[key] for key in _DOCKER_ENVIRONMENT if key in env}


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise TypeError("Command must be an argument sequence, not a command string")
        if not all(isinstance(argument, str) for argument in argv):
            raise TypeError("Every command argument must be a string")
        return subprocess.run(list(argv), env=env, check=check,
                              capture_output=capture_output, text=True)

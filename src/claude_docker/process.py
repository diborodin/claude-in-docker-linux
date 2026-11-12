"""Shell-free Docker exec arguments containing environment names only."""

from collections.abc import Sequence
from pathlib import Path


def docker_exec_argv(container_id: str, command: Sequence[str], *, workdir: Path,
                     env_names: Sequence[str], interactive: bool, tty: bool,
                     detach: bool = False) -> list[str]:
    argv = ['docker', 'exec']
    if detach:
        argv.append('-d')
    if interactive:
        argv.append('-it' if tty else '-i')
    argv.extend(['--workdir', str(workdir)])
    for name in env_names:
        argv.extend(['--env', name])
    return argv + [container_id, *command]

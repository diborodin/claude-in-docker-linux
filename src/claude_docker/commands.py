"""One shell-free subprocess boundary shared by launcher operations."""

from collections.abc import Mapping, Sequence
import subprocess


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

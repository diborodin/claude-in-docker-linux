"""Private, atomic records of successful and potentially partial Compose starts."""

import os
from pathlib import Path
import tempfile


class RuntimeState:
    def __init__(self, state_dir: Path):
        self.path = state_dir / 'active-mode'
        self.pending_path = state_dir / 'pending-mode'

    @staticmethod
    def _validate(mode: str) -> str:
        if mode not in ('none', 'sysbox'):
            raise ValueError('Runtime mode must be none or sysbox; inspect active-mode and pending-mode state')
        return mode

    def _read(self, path: Path) -> str | None:
        try:
            text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None
        return self._validate(text.strip())

    def _write(self, path: Path, mode: str, *, if_absent: bool = False) -> bool:
        self._validate(mode)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(mode + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            if if_absent:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    return False
            else:
                os.replace(temporary, path)
            return True
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read_mode(self) -> str | None:
        return self._read(self.path)

    def write_mode(self, mode: str) -> None:
        self._write(self.path, mode)

    def write_mode_if_absent(self, mode: str) -> bool:
        """Publish a complete first-mode record without replacing another writer."""
        return self._write(self.path, mode, if_absent=True)

    def read_pending(self) -> str | None:
        return self._read(self.pending_path)

    def require_modes(self, mode: str | None, pending: str | None) -> None:
        """Reject a Desktop launch whose recorded mode or transition changed."""
        if self.read_mode() != mode or self.read_pending() != pending:
            raise ValueError('Docker mode or pending transition changed during Desktop launch. '
                             'Retry after the transition completes.')

    def write_pending(self, mode: str) -> None:
        self._write(self.pending_path, mode)

    def clear_pending(self) -> None:
        self.pending_path.unlink(missing_ok=True)

    def clear_mode(self) -> None:
        self.path.unlink(missing_ok=True)

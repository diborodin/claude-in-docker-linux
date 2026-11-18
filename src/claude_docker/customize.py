"""Select, initialize, and snapshot the optional user image build script."""

import os
from pathlib import Path
import stat
import tempfile

from .paths import HostPaths


def user_customize_path(paths: HostPaths) -> Path:
    return paths.config_dir / 'customize.sh'


def init_user_customize(paths: HostPaths, program_dir: Path) -> Path:
    destination = user_customize_path(paths)
    example = (program_dir / 'image/customize.example.sh').read_bytes()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Publish the complete example at once. A hard link fails if anything
    # already occupies the destination, including a broken symlink.
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.customize-') as draft:
        os.fchmod(draft.fileno(), 0o600)
        draft.write(example)
        draft.flush()
        os.link(draft.name, destination)
    return destination


def read_user_customize(paths: HostPaths) -> bytes | None:
    source = user_customize_path(paths)
    try:
        info = source.stat()
    except FileNotFoundError:
        if source.is_symlink():
            raise ValueError(f'Customization script is a dangling symlink: {source}') from None
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Customization script must be a regular file: {source}')
    # Recheck the opened file. If the path was replaced with a FIFO between
    # stat and open, the nonblocking open keeps a build from hanging.
    descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f'Customization script must be a regular file: {source}')
        return stream.read()

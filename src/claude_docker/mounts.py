"""Validate workspace bindings and prepare enabled Claude application state."""

from collections.abc import Callable, Sequence
import os
from pathlib import Path

from .model import AppConfig, BindMount


def prepare_mounts(config: AppConfig, *, mkdir: Callable[..., None] = Path.mkdir) -> tuple[BindMount, ...]:
    """Validate all workspace/state sources before creating enabled directories.

    The injected mkdir has the same calling convention as unbound Path.mkdir.
    Workspace spellings remain intact so existing absolute project paths work
    identically on the host and in the container.
    """
    roots = config.container.mounts
    if len(set(roots)) != len(roots):
        raise ValueError("container.mounts: duplicate workspace roots are not allowed")
    for source in roots:
        if not source.is_dir():
            raise ValueError(
                f"Workspace root is missing or is not a directory: {source}; "
                "configure container.mounts in config.toml with existing directories "
                "(or [] to disable workspace mounts)"
            )

    home = config.container.home
    directories = (
        ("state.code", config.state_code, home / ".claude"),
        ("state.desktop", config.state_desktop, home / ".config/Claude"),
    )
    for label, state, _ in directories:
        if state.mount and (state.source.exists() or state.source.is_symlink()) and not state.source.is_dir():
            raise ValueError(f"{label}.source must be a directory: {state.source}")
    json = config.state_code_json
    json_present = json.mount and (json.source.exists() or json.source.is_symlink())
    if json_present and not json.source.is_file():
        raise ValueError(f"state.code_json.source must be a regular file: {json.source}")

    mounts = [BindMount(source, source, False) for source in roots]
    for label, state, target in directories:
        if not state.mount:
            continue
        if not state.source.is_dir():
            previous = os.umask(0o077)
            try:
                mkdir(state.source, mode=0o700, parents=True, exist_ok=True)
            except OSError as error:
                raise ValueError(f"Cannot create {label}.source directory: {state.source}") from error
            finally:
                os.umask(previous)
        if not state.source.is_dir():
            raise ValueError(f"{label}.source must be a directory: {state.source}")
        mounts.append(BindMount(state.source, target, False))
    if json_present:
        mounts.append(BindMount(json.source, home / ".claude.json", False))
    return tuple(mounts)


def container_workdir(caller: Path, mounts: Sequence[BindMount], container_home: Path) -> Path:
    """Map the longest component match, preserving configured target spelling.

    Lexical matching comes first for each mount. A resolved source/caller pair
    additionally handles symlink aliases in either direction without changing
    the mount or the workdir's configured prefix.
    """
    best_length = -1
    workdir = container_home
    resolved_caller = caller.resolve(strict=False)
    for mount in mounts:
        source = mount.source
        try:
            relative = caller.relative_to(source)
        except ValueError:
            source = source.resolve(strict=False)
            try:
                relative = resolved_caller.relative_to(source)
            except ValueError:
                continue
        length = len(source.parts)
        if length > best_length:
            best_length = length
            workdir = mount.target / relative
    return workdir

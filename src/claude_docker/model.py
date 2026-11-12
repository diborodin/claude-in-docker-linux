"""Immutable resolved launcher settings."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ContainerConfig:
    user: str
    uid: int
    gid: int
    home: Path
    mounts: tuple[Path, ...]


@dataclass(frozen=True)
class StateMountConfig:
    source: Path
    mount: bool


@dataclass(frozen=True)
class DesktopConfig:
    sandbox: bool
    clipboard: Literal["manual", "bidirectional"]
    seccomp: Literal["default", "chromium"] = "default"


@dataclass(frozen=True)
class EnvironmentConfig:
    required: tuple[str, ...]
    optional: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    docker_mode: Literal["none", "sysbox"]
    container: ContainerConfig
    state_code: StateMountConfig
    state_code_json: StateMountConfig
    state_desktop: StateMountConfig
    desktop: DesktopConfig
    environment: EnvironmentConfig


@dataclass(frozen=True)
class BindMount:
    source: Path
    target: Path
    read_only: bool


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    executable: str


@dataclass(frozen=True)
class DesktopRuntime:
    display: int
    socket_dir: Path
    xauthority: Path
    xephyr_identity: ProcessIdentity

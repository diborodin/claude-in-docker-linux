"""Strict TOML settings and process overrides, without modifying host state."""

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tomllib
from typing import Any

from .model import AppConfig, ContainerConfig, DesktopConfig, EnvironmentConfig, StateMountConfig
from .paths import HostPaths


@dataclass(frozen=True)
class AccountInfo:
    name: str
    uid: int
    gid: int
    home: Path


# The on-disk example and this embedded copy must remain identical. Embedding it
# keeps `config init` independent of the current directory and external tools.
EXAMPLE_CONFIG = '''# Settings omitted here retain detected host-account and home defaults.
# Process CLAUDE_* overrides take precedence over this file.
# Paths are absolute or start with ~/ (the invoking host user's home).
# UID/GID range: 1..4294967294. Account names: [a-z_][a-z0-9_-]*, max 32.
# The root account and filesystem root as container home are not supported.
# Boolean environment overrides accept true/1/yes/on or false/0/no/off,
# case-insensitively, with surrounding spaces permitted. Empty scalars fail.
docker_mode = "none" # "sysbox" explicitly enables isolated inner Docker.

[container]
# Uncomment to override detected identity:
# Desktop requires uid to equal the invoking host UID for private X11 grants.
# Differing uid overrides are supported for CLI commands only.
# user = "developer"
# uid = 1000
# gid = 1000
# home = "/home/developer"
# Each distinct workspace is mounted at the SAME absolute path in the container.
# Use the narrowest useful roots. An empty [] disables workspace mounts.
mounts = ["~/work/git", "~/work/etc", "/srv/shared-projects"]

[state.code]
source = "~/.claude"
mount = true # false disables this state mount and uses container-local state.

[state.code_json]
source = "~/.claude.json"
mount = true # false disables; a missing host file is never created.

[state.desktop]
source = "~/.config/Claude"
mount = true # false disables this mount.

[desktop]
sandbox = true # false explicitly requests Chromium --no-sandbox.
clipboard = "manual" # "bidirectional" opts into background synchronization.
seccomp = "default" # "chromium" explicitly enables the bundled namespace policy.

[environment]
# These lists contain exact variable NAMES only, never credential values.
required = []
optional = []
# AWS example: replace the empty required list above with:
# required = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
# optional = ["AWS_REGION", "AWS_DEFAULT_REGION"]
'''

_SCHEMA = {
    "docker_mode": "mode",
    "container": {"user": "user", "uid": "id", "gid": "id", "home": "container_home", "mounts": "mounts"},
    "state": {name: {"source": "path", "mount": "bool"} for name in ("code", "code_json", "desktop")},
    "desktop": {"sandbox": "bool", "clipboard": "clipboard", "seccomp": "seccomp"},
    "environment": {"required": "names", "optional": "names"},
}
_OVERRIDES = {
    "CLAUDE_DOCKER_MODE": ("docker_mode", "mode"),
    "CLAUDE_CONTAINER_USER": ("container.user", "user"),
    "CLAUDE_CONTAINER_UID": ("container.uid", "id"),
    "CLAUDE_CONTAINER_GID": ("container.gid", "id"),
    "CLAUDE_CONTAINER_HOME": ("container.home", "container_home"),
    "CLAUDE_CODE_CONFIG_SOURCE": ("state.code.source", "path"),
    "CLAUDE_CODE_CONFIG_MOUNT": ("state.code.mount", "bool"),
    "CLAUDE_CODE_JSON_SOURCE": ("state.code_json.source", "path"),
    "CLAUDE_CODE_JSON_MOUNT": ("state.code_json.mount", "bool"),
    "CLAUDE_DESKTOP_CONFIG_SOURCE": ("state.desktop.source", "path"),
    "CLAUDE_DESKTOP_CONFIG_MOUNT": ("state.desktop.mount", "bool"),
    "CLAUDE_DESKTOP_SANDBOX": ("desktop.sandbox", "bool"),
    "CLAUDE_DESKTOP_CLIPBOARD": ("desktop.clipboard", "clipboard"),
    "CLAUDE_DESKTOP_SECCOMP": ("desktop.seccomp", "seccomp"),
}


def _invalid(setting: str, requirement: str, value: Any) -> ValueError:
    # Values may contain credentials accidentally assigned to an override. Show
    # only their type, and never dump the process environment or TOML contents.
    return ValueError(f"{setting}: {requirement} (got {type(value).__name__})")


def _text(value: Any, setting: str) -> str:
    if type(value) is not str or not value.strip() or any(c in value for c in "\x00\r\n"):
        raise _invalid(setting, "expected nonempty single-line text", value)
    return value


def _path(value: Any, setting: str, home: Path) -> Path:
    text = _text(value, setting)
    path = home / text[2:].lstrip("/") if text.startswith("~/") else Path(text)
    if not path.is_absolute():
        raise _invalid(setting, "expected an absolute path or ~/ path", value)
    return path


def _validate(value: Any, kind: str, setting: str, home: Path) -> Any:
    if kind == "bool":
        if type(value) is not bool:
            raise _invalid(setting, "expected a Boolean", value)
    elif kind == "id":
        if type(value) is not int or not 1 <= value <= 4294967294:
            raise _invalid(setting, "expected an integer in 1..4294967294", value)
    elif kind in ("path", "container_home"):
        path = _path(value, setting, home)
        if kind == "container_home" and not os.path.normpath(str(path)).strip('/'):
            raise _invalid(setting, "container home must not be filesystem root", value)
        return path
    elif kind == "user":
        text = _text(value, setting)
        if text == "root" or len(text) > 32 or not re.fullmatch(r"[a-z_][a-z0-9_-]*", text):
            raise _invalid(setting, "expected an unprivileged account name of at most 32 characters", value)
    elif kind in ("mode", "clipboard", "seccomp"):
        allowed = {"mode": ("none", "sysbox"), "clipboard": ("manual", "bidirectional"),
                   "seccomp": ("default", "chromium")}[kind]
        if _text(value, setting) not in allowed:
            raise _invalid(setting, "expected one of " + ", ".join(allowed), value)
    elif kind in ("mounts", "names"):
        if type(value) is not list:
            raise _invalid(setting, "expected an array", value)
        if kind == "mounts":
            result = tuple(_path(item, setting, home) for item in value)
        else:
            for item in value:
                if type(item) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
                    raise _invalid(setting, "expected exact environment variable names", item)
            result = tuple(value)
        if len(set(result)) != len(result):
            raise _invalid(setting, "duplicate entries are not allowed", value)
        return result
    return value


def _read_table(data: Any, schema: dict, home: Path, prefix: str = "",
                overridden: frozenset[str] = frozenset()) -> dict:
    if type(data) is not dict:
        raise _invalid(prefix or "config", "expected a TOML table", data)
    values = {}
    for key, value in data.items():
        setting = f"{prefix}.{key}" if prefix else key
        if key not in schema:
            raise _invalid(setting, "unknown configuration key", value)
        if setting in overridden:
            continue
        kind = schema[key]
        if isinstance(kind, dict):
            values.update(_read_table(value, kind, home, setting, overridden))
        else:
            values[setting] = _validate(value, kind, setting, home)
    return values


def resolve_config_file(*, env: Mapping[str, str], paths: HostPaths,
                        config_file: Path | None = None) -> Path:
    """Resolve the shared loader/init destination without reading its contents."""
    if config_file is not None:
        return _path(str(config_file), "config_file", paths.home)
    if "CLAUDE_CONFIG_FILE" in env:
        return _path(env["CLAUDE_CONFIG_FILE"], "CLAUDE_CONFIG_FILE", paths.home)
    return paths.config_file


def load_config(*, env: Mapping[str, str], paths: HostPaths, account: AccountInfo,
                config_file: Path | None = None) -> AppConfig:
    """Apply validated defaults, selected TOML, then explicit process overrides."""
    defaults = {
        "docker_mode": "none",
        "container": {"user": account.name, "uid": account.uid, "gid": account.gid,
                      "home": str(paths.home), "mounts": [str(paths.home / "work/git")]},
        "state": {
            "code": {"source": str(paths.home / ".claude"), "mount": True},
            "code_json": {"source": str(paths.home / ".claude.json"), "mount": True},
            "desktop": {"source": str(paths.home / ".config/Claude"), "mount": True},
        },
        "desktop": {"sandbox": True, "clipboard": "manual", "seccomp": "default"},
        "environment": {"required": [], "optional": []},
    }
    values = {}
    selected = resolve_config_file(env=env, paths=paths, config_file=config_file)
    label = "config_file" if config_file is not None else (
        "CLAUDE_CONFIG_FILE" if "CLAUDE_CONFIG_FILE" in env else "config")
    try:
        with selected.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        if config_file is not None or "CLAUDE_CONFIG_FILE" in env:
            raise ValueError(f"{label}: selected configuration file does not exist: {selected}") from None
        data = {}
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        raise ValueError(f"{label}: cannot read a valid TOML configuration: {selected}") from None
    values.update(_read_table(data, _SCHEMA, paths.home))
    for name, (setting, kind) in _OVERRIDES.items():
        if name not in env:
            continue
        value = _text(env[name], name)
        if kind == "bool":
            text = value.strip().lower()
            if text not in ("true", "1", "yes", "on", "false", "0", "no", "off"):
                raise _invalid(name, "expected true/1/yes/on or false/0/no/off", value)
            value = text in ("true", "1", "yes", "on")
        elif kind == "id":
            if not re.fullmatch(r"[0-9]+", value):
                raise _invalid(name, "expected an integer in 1..4294967294", value)
            try:
                value = int(value)
            except ValueError:
                raise _invalid(name, "expected an integer in 1..4294967294", value) from None
        values[setting] = _validate(value, kind, name, paths.home)
    if env.get("CLAUDE_MOUNTS_FILE", ""):
        mounts_file = _path(env["CLAUDE_MOUNTS_FILE"], "CLAUDE_MOUNTS_FILE", paths.home)
        try:
            lines = mounts_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            raise ValueError(f"CLAUDE_MOUNTS_FILE: cannot read mounts file: {mounts_file}") from None
        mounts = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        values["container.mounts"] = _validate(mounts, "mounts", "CLAUDE_MOUNTS_FILE", paths.home)
    values = {**_read_table(defaults, _SCHEMA, paths.home, overridden=frozenset(values)), **values}
    if set(values["environment.required"]) & set(values["environment.optional"]):
        raise ValueError("environment: a variable cannot be both required and optional")
    return AppConfig(
        docker_mode=values["docker_mode"],
        container=ContainerConfig(**{key: values[f"container.{key}"] for key in _SCHEMA["container"]}),
        state_code=StateMountConfig(values["state.code.source"], values["state.code.mount"]),
        state_code_json=StateMountConfig(values["state.code_json.source"], values["state.code_json.mount"]),
        state_desktop=StateMountConfig(values["state.desktop.source"], values["state.desktop.mount"]),
        desktop=DesktopConfig(values["desktop.sandbox"], values["desktop.clipboard"], values["desktop.seccomp"]),
        environment=EnvironmentConfig(values["environment.required"], values["environment.optional"]),
    )


def write_example_config(destination: Path) -> None:
    """Create private configuration explicitly, refusing files and symlinks."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(EXAMPLE_CONFIG)

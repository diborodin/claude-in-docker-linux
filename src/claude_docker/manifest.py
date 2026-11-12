"""Typed ownership records for installed program files and release selection."""

from dataclasses import dataclass
import json
import os
import stat
from pathlib import Path
import re


def _read_metadata(path: Path) -> dict:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError(f"Installation metadata must be a private owned regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        actual = os.fstat(stream.fileno())
        if ((actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino)
                or not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid()
                or stat.S_IMODE(actual.st_mode) != 0o600):
            raise ValueError(f"Installation metadata changed while reading: {path}")
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("Installation metadata must be a JSON object")
    return data


def validate_version(version: str) -> str:
    """A version is one ordinary directory component, never a traversal path."""
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise ValueError("Invalid release version")
    return version


@dataclass(frozen=True)
class ManifestFile:
    path: Path
    size: int
    sha256: str
    mode: int

    def __post_init__(self):
        if (not isinstance(self.path, Path) or self.path.is_absolute() or not self.path.parts or ".." in self.path.parts
                or "\\" in str(self.path)):
            raise ValueError("Manifest file must be a relative bundle path")
        if (type(self.size) is not int or self.size < 0
                or not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
                or type(self.mode) is not int or self.mode not in (0o644, 0o755)):
            raise ValueError("Invalid manifest file metadata")


@dataclass(frozen=True)
class InstallManifest:
    version: str
    mode: str
    files: tuple[ManifestFile, ...]
    source: Path | None = None

    def __post_init__(self):
        validate_version(self.version)
        if self.mode not in ("user", "dev", "system"):
            raise ValueError("Invalid installation mode")
        if not isinstance(self.files, tuple) or not all(isinstance(item, ManifestFile) for item in self.files):
            raise ValueError("Invalid manifest files")
        if ((self.mode == "dev") != (self.source is not None)
                or (self.source is not None and not self.source.is_absolute())
                or (self.mode == "dev" and self.files)):
            raise ValueError("Invalid development manifest source or owned files")
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("Duplicate manifest file")

    def to_dict(self) -> dict:
        data = {"schema": 1, "version": self.version, "mode": self.mode,
                "files": [{"path": item.path.as_posix(), "size": item.size,
                           "sha256": item.sha256, "mode": item.mode} for item in self.files]}
        if self.source is not None:
            data["source"] = str(self.source)
        return data

    @classmethod
    def read(cls, path: Path) -> "InstallManifest":
        return cls.from_dict(_read_metadata(path))

    @classmethod
    def from_dict(cls, data: dict) -> "InstallManifest":
        expected = {"schema", "version", "mode", "files"}
        if data.get("mode") == "dev":
            expected.add("source")
        if (set(data) != expected
                or type(data["schema"]) is not int or data["schema"] != 1
                or not isinstance(data["files"], list)
                or not isinstance(data["version"], str) or not isinstance(data["mode"], str)):
            raise ValueError("Invalid release manifest schema")
        files = []
        for entry in data["files"]:
            if (not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256", "mode"}
                    or not isinstance(entry["path"], str)):
                raise ValueError("Invalid release manifest entry")
            files.append(ManifestFile(Path(entry["path"]), entry["size"], entry["sha256"], entry["mode"]))
        source = data.get("source")
        if source is not None and not isinstance(source, str):
            raise ValueError("Invalid development source")
        return cls(data["version"], data["mode"], tuple(files), Path(source) if source is not None else None)


@dataclass(frozen=True)
class InstallationRegistry:
    mode: str
    active_version: str | None
    releases: tuple[str, ...]
    command_link: Path
    current_link: Path | None
    source: Path | None = None

    def __post_init__(self):
        if self.mode not in ("user", "dev", "system"):
            raise ValueError("Invalid installation mode")
        if not isinstance(self.releases, tuple) or len(set(self.releases)) != len(self.releases):
            raise ValueError("Invalid release inventory")
        for version in self.releases:
            validate_version(version)
        if self.active_version is not None:
            validate_version(self.active_version)
            if self.active_version not in self.releases:
                raise ValueError("Active version is missing from release inventory")
        if ((self.mode == "dev") != (self.source is not None)
                or (self.source is not None and not self.source.is_absolute())
                or (self.mode == "dev" and (self.releases or self.active_version is not None or self.current_link is not None))):
            raise ValueError("Invalid development registry")
        if not self.command_link.is_absolute() or (self.current_link is not None and not self.current_link.is_absolute()):
            raise ValueError("Installation links must be absolute")

    def to_dict(self) -> dict:
        data = {"schema": 1, "mode": self.mode, "active_version": self.active_version,
                "releases": list(self.releases), "command_link": str(self.command_link),
                "current_link": str(self.current_link) if self.current_link is not None else None}
        if self.source is not None:
            data["source"] = str(self.source)
        return data

    @classmethod
    def read(cls, path: Path) -> "InstallationRegistry":
        return cls.from_dict(_read_metadata(path))

    @classmethod
    def from_dict(cls, data: dict) -> "InstallationRegistry":
        expected = {"schema", "mode", "active_version", "releases", "command_link", "current_link"}
        if data.get("mode") == "dev":
            expected.add("source")
        if (set(data) != expected
                or type(data["schema"]) is not int or data["schema"] != 1
                or not isinstance(data["releases"], list)
                or not all(isinstance(item, str) for item in data["releases"])
                or not isinstance(data["mode"], str)
                or not isinstance(data["command_link"], str)
                or (data["current_link"] is not None and not isinstance(data["current_link"], str))):
            raise ValueError("Invalid installation registry schema")
        source = data.get("source")
        if source is not None and not isinstance(source, str):
            raise ValueError("Invalid development source")
        return cls(data["mode"], data["active_version"], tuple(data["releases"]),
                   Path(data["command_link"]), Path(data["current_link"]) if data["current_link"] is not None else None,
                   Path(source) if source is not None else None)

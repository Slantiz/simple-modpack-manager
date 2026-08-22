"""Core data types shared across the pipeline.

Three layers, deliberately kept separate:

- ``Mod`` / ``Profile``    — desired state, parsed from the TOML (the source of truth).
- ``ResolvedVersion``      — what a provider says the newest/pinned version is.
- ``LockEntry`` / ``Lock`` — the resolved current state, persisted as JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

VALID_SIDES = {"client", "server", "both"}
VALID_SOURCES = {"modrinth", "curseforge", "manual", "url"}
VALID_CHANNELS = {"release", "beta", "alpha"}
VALID_TYPES = {"mod", "datapack"}


class ConfigError(Exception):
    """Raised when a profile TOML is malformed or semantically invalid."""


# ── Desired state (from TOML) ────────────────────────────────────────────────


@dataclass(frozen=True)
class Mod:
    """A single ``[[mods]]`` entry as written in a profile TOML."""

    name: str
    source: str  # modrinth | curseforge | manual | url
    project_id: str  # slug / project id / (for url) the download URL
    side: str  # client | server | both
    pin: str | bool | None = None  # version string, True (freeze current), or None
    channel: str = "alpha"  # lowest acceptable release type; default = newest of any
    enabled: bool = True
    file: str | None = None  # manual mods: exact jar filename to capture from side dirs
    url: str | None = None  # manual mods: where to download it (shown in check output)
    type: str = "mod"  # mod | datapack — decides resolution loader + destination

    @property
    def is_datapack(self) -> bool:
        return self.type == "datapack"

    @property
    def key(self) -> str:
        """Stable identity used as the lock/store reference key.

        Keyed on ``source:project_id`` so a mod keeps its identity across name
        edits and version bumps. Changing the source or id is intentionally a
        new identity (old jar is swept, new one resolved).
        """
        return f"{self.source}:{self.project_id}"

    @property
    def is_pinned(self) -> bool:
        return self.pin is not None and self.pin is not False

    def pinned_version(self) -> str | None:
        """The explicit version to hold at, or None for 'freeze current' / unpinned."""
        return self.pin if isinstance(self.pin, str) else None

    def sides(self) -> tuple[str, ...]:
        return ("client", "server") if self.side == "both" else (self.side,)


@dataclass(frozen=True)
class Profile:
    """A whole modpack: its own game version, loader, dirs, and mod list."""

    id: str
    game_version: str
    loader: str
    client_dir: Path
    server_dir: Path
    datapacks_dir: Path
    singleplayer_dir: Path
    mods: tuple[Mod, ...]
    path: Path  # the source TOML, for error messages
    build_singleplayer: bool = False  # also emit a singleplayer/ folder (client ∪ server)

    def dir_for_side(self, side: str) -> Path:
        return self.client_dir if side == "client" else self.server_dir

    def mod_by_key(self, key: str) -> Mod | None:
        return next((m for m in self.mods if m.key == key), None)


# ── Provider output ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedVersion:
    """A concrete downloadable version returned by a provider.

    ``sha512`` is the store address. Some sources (CurseForge) don't advertise a
    sha512, so it may be empty here and is filled in after download; ``sha1`` (if
    present) is used to verify integrity in that case.
    """

    source: str
    project_id: str
    version_id: str
    version_number: str
    filename: str
    download_url: str | None  # None => manual download required
    sha512: str = ""  # lowercase hex; empty => compute on download
    sha1: str | None = None  # fallback integrity check when sha512 unknown
    dependencies: tuple[str, ...] = ()  # required dependency canonical ids
    canonical_id: str | None = None  # provider's own project id (for dep matching)
    client_side: str | None = None  # modrinth support: required|optional|unsupported
    server_side: str | None = None
    release_type: str = "release"  # release | beta | alpha (for channel enforcement)


# ── Resolved current state (the lockfile) ────────────────────────────────────


@dataclass(frozen=True)
class LockEntry:
    """One mod's resolved state, persisted in the lockfile."""

    key: str
    name: str
    source: str
    project_id: str
    version_id: str
    version_number: str
    filename: str
    sha512: str
    side: str
    download_url: str | None = None
    enabled: bool = True
    pinned: bool = False
    type: str = "mod"
    dependencies: tuple[str, ...] = ()
    canonical_id: str | None = None
    client_side: str | None = None
    server_side: str | None = None
    release_type: str = ""  # release | beta | alpha; "" = unknown (pre-existing lock)

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "source": self.source,
            "project_id": self.project_id,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "filename": self.filename,
            "sha512": self.sha512,
            "side": self.side,
            "download_url": self.download_url,
            "enabled": self.enabled,
            "pinned": self.pinned,
            "type": self.type,
            "dependencies": list(self.dependencies),
            "canonical_id": self.canonical_id,
            "client_side": self.client_side,
            "server_side": self.server_side,
            "release_type": self.release_type,
        }

    @classmethod
    def from_json(cls, d: dict) -> LockEntry:
        return cls(
            key=d["key"],
            name=d["name"],
            source=d["source"],
            project_id=d["project_id"],
            version_id=d["version_id"],
            version_number=d["version_number"],
            filename=d["filename"],
            sha512=d["sha512"],
            side=d["side"],
            download_url=d.get("download_url"),
            enabled=d.get("enabled", True),
            pinned=d.get("pinned", False),
            type=d.get("type", "mod"),
            dependencies=tuple(d.get("dependencies", ())),
            canonical_id=d.get("canonical_id"),
            client_side=d.get("client_side"),
            server_side=d.get("server_side"),
            release_type=d.get("release_type", ""),
        )

    @property
    def is_datapack(self) -> bool:
        return self.type == "datapack"

    def sides(self) -> tuple[str, ...]:
        return ("client", "server") if self.side == "both" else (self.side,)


@dataclass(frozen=True)
class Lock:
    """The resolved current state of a profile, keyed by ``Mod.key``."""

    profile_id: str
    game_version: str
    loader: str
    entries: dict[str, LockEntry] = field(default_factory=dict)
    updated: str | None = None

    def with_entries(self, entries: dict[str, LockEntry], *, updated: str) -> Lock:
        return replace(self, entries=dict(entries), updated=updated)

    def referenced_hashes(self) -> set[str]:
        """sha512 hashes this lock keeps alive in the store (enabled or not)."""
        return {e.sha512 for e in self.entries.values()}

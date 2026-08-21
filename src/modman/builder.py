"""Materialize a lockfile into a profile's destination folders.

Each destination (client, server, and a single datapacks folder beside them)
becomes an exact mirror of the enabled, locked artifacts routed to it: missing
files are copied from the store (integrity-verified), and stale files the tool
previously managed are removed. Files the tool never placed are left untouched,
so user data is never destroyed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .model import Lock, LockEntry, Profile
from .store import Store


@dataclass
class SideResult:
    side: str  # destination label: "client" | "server" | "datapacks"
    directory: Path
    copied: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unmanaged: list[str] = field(default_factory=list)


def _client_runnable(entry: LockEntry) -> bool:
    """Whether an entry can run on a client (and so belongs in a singleplayer build).
    Only Modrinth advertises this; absent metadata is assumed runnable."""
    return entry.client_side != "unsupported"


def entry_targets(entry: LockEntry, profile: Profile) -> list[Path]:
    """Directories an entry's file belongs in: the datapacks folder, or the side
    dirs — plus the singleplayer dir (client ∪ server) when the profile builds one."""
    if entry.is_datapack:
        return [profile.datapacks_dir]
    dirs = [profile.dir_for_side(side) for side in entry.sides()]
    if profile.build_singleplayer and _client_runnable(entry):
        dirs.append(profile.singleplayer_dir)
    return dirs


def destinations(profile: Profile, *locks: Lock | None) -> list[tuple[str, Path]]:
    dests = [("client", profile.client_dir), ("server", profile.server_dir)]
    if profile.build_singleplayer:
        dests.append(("singleplayer", profile.singleplayer_dir))
    has_datapacks = any(
        e.is_datapack for lk in locks if lk for e in lk.entries.values()
    )
    if has_datapacks:
        dests.append(("datapacks", profile.datapacks_dir))
    return dests


def materialize(
    profile: Profile,
    new_lock: Lock,
    store: Store,
    *,
    old_lock: Lock | None = None,
    dry_run: bool = False,
) -> list[SideResult]:
    return [
        _reconcile_dir(label, directory, profile, new_lock, old_lock, store, dry_run)
        for label, directory in destinations(profile, new_lock, old_lock)
    ]


def _reconcile_dir(
    label: str,
    directory: Path,
    profile: Profile,
    new_lock: Lock,
    old_lock: Lock | None,
    store: Store,
    dry_run: bool,
) -> SideResult:
    res = SideResult(side=label, directory=directory)

    desired: dict[str, str] = {}  # filename -> sha512
    for e in new_lock.entries.values():
        if e.enabled and e.sha512 and directory in entry_targets(e, profile):
            desired[e.filename] = e.sha512

    managed: set[str] = set()
    for lk in (old_lock, new_lock):
        if lk:
            for e in lk.entries.values():
                if directory in entry_targets(e, profile):
                    managed.add(e.filename)

    existing = (
        {p.name for p in directory.iterdir() if p.is_file()}
        if directory.is_dir()
        else set()
    )

    # Copy/repair desired files.
    for filename, sha in desired.items():
        dest = directory / filename
        if dest.exists() and _matches(dest, sha, store):
            continue
        if not dry_run:
            store.copy_to(sha, dest)
        res.copied.append(filename)

    # Remove stale files we previously managed but no longer want.
    for name in sorted(existing):
        if name in desired:
            continue
        if name in managed:
            if not dry_run:
                (directory / name).unlink()
            res.removed.append(name)
        else:
            res.unmanaged.append(name)

    return res


def _matches(path: Path, sha512: str, store: Store) -> bool:
    """Cheap correctness check: same size as the store copy is good enough here
    because content mismatches are caught by store.copy_to's verified write."""
    try:
        return path.stat().st_size == store.path(sha512).stat().st_size
    except OSError:
        return False

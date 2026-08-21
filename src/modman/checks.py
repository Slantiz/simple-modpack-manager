"""Correctness checks over a committed lock (used by ``verify``)."""

from __future__ import annotations

from dataclasses import dataclass

from .builder import destinations, entry_targets
from .model import Lock, Profile
from .store import Store


@dataclass
class Issue:
    level: str  # "error" | "warn"
    mod: str
    message: str


def side_mismatches(lock: Lock) -> list[Issue]:
    """Warn when a mod is placed on a side its Modrinth metadata says is unsupported."""
    issues = []
    for e in lock.entries.values():
        if not e.enabled or e.is_datapack:
            continue
        sides = set(e.sides())
        if "client" in sides and e.client_side == "unsupported":
            issues.append(Issue("warn", e.name,
                                 "placed on client but marked client-unsupported"))
        if "server" in sides and e.server_side == "unsupported":
            issues.append(Issue("warn", e.name,
                                 "placed on server but marked server-unsupported"))
    return issues


def duplicate_files(profile: Profile, lock: Lock) -> list[Issue]:
    """Error on two enabled entries that would write the same filename to a folder."""
    issues = []
    for label, directory in destinations(profile, lock):
        by_name: dict[str, list[str]] = {}
        for e in lock.entries.values():
            if e.enabled and e.filename and directory in entry_targets(e, profile):
                by_name.setdefault(e.filename, []).append(e.name)
        for filename, owners in by_name.items():
            if len(owners) > 1:
                issues.append(Issue("error", ", ".join(owners),
                                    f"share filename '{filename}' in {label} (a crash risk)"))
    return issues


def duplicate_projects(lock: Lock) -> list[Issue]:
    """Warn when the same underlying project is listed under two sources."""
    issues = []
    seen: dict[str, str] = {}
    for e in lock.entries.values():
        cid = e.canonical_id or f"{e.source}:{e.project_id}"
        if cid in seen and seen[cid] != e.key:
            issues.append(Issue("warn", e.name,
                                f"looks like a duplicate of '{seen[cid]}'"))
        else:
            seen[cid] = e.key
    return issues


def missing_dependencies(lock: Lock) -> list[Issue]:
    """Report a required dependency that IS in the pack but not on the side that
    needs it (disabled, or only on the other side).

    We deliberately don't warn about dependencies absent from the lock entirely:
    those ids can't be verified offline — dependency metadata is over-declared, and
    ids don't cross sources (a CurseForge mod's numeric dep id never matches the same
    library installed from Modrinth), so "absent" is almost all false positives.
    """
    issues = []
    present: dict[str, set[str]] = {"client": set(), "server": set()}
    names: dict[str, str] = {}  # canonical_id -> display name
    for e in lock.entries.values():
        if e.canonical_id:
            names[e.canonical_id] = e.name
        if not e.enabled or not e.canonical_id:
            continue
        for side in e.sides():
            present[side].add(e.canonical_id)
    for e in lock.entries.values():
        if not e.enabled or not e.dependencies:
            continue
        for side in e.sides():
            for dep in e.dependencies:
                if dep in names and dep not in present[side]:
                    issues.append(Issue("warn", e.name,
                                        f"requires {names[dep]}, which is in the pack "
                                        f"but not on {side}"))
    return issues


def store_integrity(lock: Lock, store: Store) -> list[Issue]:
    """Error when a locked jar is absent from the store or fails its hash."""
    issues = []
    for e in lock.entries.values():
        if not e.sha512:
            continue
        if not store.has(e.sha512):
            issues.append(Issue("error", e.name, "jar missing from store"))
        elif not store.verify(e.sha512):
            issues.append(Issue("error", e.name, "stored jar fails sha512 check"))
    return issues


def disk_drift(profile: Profile, lock: Lock) -> list[Issue]:
    """Report built-folder drift: missing expected files, stale managed extras."""
    issues = []
    for label, directory in destinations(profile, lock):
        expected = {
            e.filename for e in lock.entries.values()
            if e.enabled and e.sha512 and directory in entry_targets(e, profile)
        }
        present = (
            {p.name for p in directory.iterdir() if p.is_file()}
            if directory.is_dir()
            else set()
        )
        managed = {
            e.filename for e in lock.entries.values()
            if directory in entry_targets(e, profile)
        }
        for name in sorted(expected - present):
            issues.append(Issue("warn", name, f"missing from {label} folder (run install)"))
        for name in sorted(present - expected):
            if name in managed:
                issues.append(Issue("warn", name, f"stale in {label} folder (run install)"))
    return issues


def run_all(profile: Profile, lock: Lock, store: Store) -> list[Issue]:
    return (
        store_integrity(lock, store)
        + duplicate_files(profile, lock)
        + duplicate_projects(lock)
        + side_mismatches(lock)
        + missing_dependencies(lock)
        + disk_drift(profile, lock)
    )

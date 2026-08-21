"""Read/write the per-profile lockfile (the resolved current state).

The lockfile is tool-owned, derived, and reconstructable — but always written
atomically so it can never be observed half-updated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .model import Lock, LockEntry
from .workspace import Workspace, atomic_write_json

_FORMAT = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load(profile_id: str, ws: Workspace | None = None) -> Lock:
    """Load a lockfile, or return an empty lock if none exists yet."""
    ws = ws or Workspace()
    path = ws.lock_path(profile_id)
    if not path.exists():
        return Lock(profile_id=profile_id, game_version="", loader="")
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = {
        key: LockEntry.from_json(e) for key, e in data.get("entries", {}).items()
    }
    return Lock(
        profile_id=data.get("profile_id", profile_id),
        game_version=data.get("game_version", ""),
        loader=data.get("loader", ""),
        entries=entries,
        updated=data.get("updated"),
    )


def save(lock: Lock, ws: Workspace | None = None) -> Lock:
    """Persist a lock atomically, stamping ``updated``. Returns the stamped lock."""
    ws = ws or Workspace()
    stamped = lock.with_entries(lock.entries, updated=now_iso())
    payload = {
        "format": _FORMAT,
        "profile_id": stamped.profile_id,
        "game_version": stamped.game_version,
        "loader": stamped.loader,
        "updated": stamped.updated,
        "entries": {k: e.to_json() for k, e in stamped.entries.items()},
    }
    atomic_write_json(ws.lock_path(profile_id=stamped.profile_id), payload)
    return stamped


def all_referenced_hashes(profile_ids: list[str], ws: Workspace | None = None) -> set[str]:
    """Union of hashes referenced by every profile's committed lockfile."""
    ws = ws or Workspace()
    hashes: set[str] = set()
    for pid in profile_ids:
        hashes |= load(pid, ws).referenced_hashes()
    return hashes

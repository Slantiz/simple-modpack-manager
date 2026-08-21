"""Manual known-good snapshots and reference-aware rollback.

A snapshot is metadata only (no bytes) — it records which version each mod was.
Rolling back re-secures those jars in the store (re-downloading if the working set
no longer holds them), then commits and materializes. Jars still referenced by any
other profile are never disturbed, because the post-rollback sweep unions all locks.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

from . import lock as lock_io
from .builder import SideResult, materialize
from .engine import ApplyResult, EngineError, _reingest_from_side, _sweep_all
from .model import Lock, LockEntry, Profile
from .providers import Provider
from .providers.base import new_session
from .store import Store
from .workspace import Workspace, atomic_write_json

_LABEL_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class Snapshot:
    name: str
    label: str
    created: str
    game_version: str
    loader: str
    count: int


def save(profile_id: str, lock: Lock, label: str = "", ws: Workspace | None = None) -> str:
    """Write the current lock as a known-good snapshot. Returns the snapshot name."""
    ws = ws or Workspace()
    if not lock.entries:
        raise EngineError(f"{profile_id}: nothing to save (empty lock — run install first)")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = _LABEL_RE.sub("-", label.strip().lower()).strip("-")
    name = f"{stamp}_{slug}" if slug else stamp
    payload = {
        "name": name,
        "label": label,
        "created": lock_io.now_iso(),
        "profile_id": profile_id,
        "game_version": lock.game_version,
        "loader": lock.loader,
        "entries": {k: e.to_json() for k, e in lock.entries.items()},
    }
    atomic_write_json(ws.history_profile_dir(profile_id) / f"{name}.json", payload)
    return name


def list_snapshots(profile_id: str, ws: Workspace | None = None) -> list[Snapshot]:
    ws = ws or Workspace()
    d = ws.history_profile_dir(profile_id)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        out.append(
            Snapshot(
                name=data.get("name", p.stem),
                label=data.get("label", ""),
                created=data.get("created", ""),
                game_version=data.get("game_version", ""),
                loader=data.get("loader", ""),
                count=len(data.get("entries", {})),
            )
        )
    return out


def _load(profile_id: str, name: str, ws: Workspace) -> Lock:
    path = ws.history_profile_dir(profile_id) / f"{name}.json"
    if not path.exists():
        raise EngineError(f"{profile_id}: no snapshot '{name}'")
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = {k: LockEntry.from_json(e) for k, e in data.get("entries", {}).items()}
    return Lock(
        profile_id=profile_id,
        game_version=data.get("game_version", ""),
        loader=data.get("loader", ""),
        entries=entries,
    )


def load_snapshot(profile_id: str, name: str, ws: Workspace | None = None) -> Lock:
    """Load a saved snapshot as a Lock (for previewing before rollback)."""
    return _load(profile_id, name, ws or Workspace())


def rollback(
    profile: Profile,
    current_lock: Lock,
    name: str,
    registry: dict[str, Provider],
    store: Store,
    ws: Workspace,
    *,
    session=None,
    on_download=None,
) -> ApplyResult:
    """Restore a saved snapshot: re-secure jars, commit lock, materialize, sweep.

    ``on_download`` is called with each re-downloaded LockEntry as it lands (out of
    order), for live progress."""
    session = session or new_session()
    target = _load(profile.id, name, ws)

    try:
        # Ensure every jar in the snapshot is present in the store. Entries with a
        # download URL that fail are a hard error (network); a manual entry with no
        # URL that can't be found on disk is skipped with a warning (can't re-fetch).
        missing = [e for e in target.entries.values() if e.sha512 and not store.has(e.sha512)]
        skipped: list[LockEntry] = []

        def ensure(entry: LockEntry) -> tuple[LockEntry, str | None]:
            if entry.download_url:
                store.fetch(
                    entry.download_url, entry.sha512,
                    headers=registry[entry.source].download_headers(),
                    session=session,
                )
                return entry, None
            try:
                _reingest_from_side(profile, entry, store)
                return entry, None
            except EngineError as e:
                return entry, str(e)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(ensure, e): e for e in missing}
            for fut in as_completed(futures):
                entry, err = fut.result()
                if err:
                    skipped.append(entry)
                if on_download is not None:
                    on_download(entry, err)

        # Commit only what we could secure; drop un-restorable manual entries.
        committed = {
            k: e for k, e in target.entries.items()
            if not e.sha512 or store.has(e.sha512)
        }
        restored = Lock(profile_id=target.profile_id, game_version=target.game_version,
                        loader=target.loader, entries=committed)
        saved = lock_io.save(restored, ws)
        sides: list[SideResult] = materialize(profile, saved, store, old_lock=current_lock)
        swept = _sweep_all(ws, store)
        warnings = [
            f"could not restore '{e.name}' ({e.filename}) — not found in store"
            for e in skipped
        ]
        return ApplyResult(lock=saved, sides=sides, swept=swept, warnings=warnings)
    except BaseException:
        _sweep_all(ws, store)
        raise

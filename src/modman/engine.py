"""The reconciler: diff desired (TOML) vs lock vs disk, then apply atomically.

Two modes over the same machinery:

- ``install`` — apply the TOML without bumping locked versions (add new mods at
  latest, honor explicit pins, keep everything else, prune removed).
- ``update``  — deliberately bump unpinned mods (all, or a named subset) to newest.

Integrity: all bytes are secured in the store first; the lockfile is committed only
if every required mod succeeded; a hard failure aborts and leaves the last good
state untouched. Materialize is a pure function of the committed lock.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import lock as lock_io
from .builder import SideResult, materialize
from .model import Lock, LockEntry, Mod, Profile, ResolvedVersion
from .providers import Provider, ResolveError
from .providers.base import ManualRequired, new_session
from .store import HashMismatch, Store
from .workspace import Workspace

_MAX_WORKERS = 8

# Plan item kinds
ADD = "add"
UPDATE = "update"
REPIN = "repin"
KEEP = "keep"
HELD = "held"  # pinned, newer available
MANUAL = "manual"  # needs hand placement (report only)
CAPTURED = "captured"  # manual jar found and ingested
REMOVE = "remove"
FAIL = "fail"


class EngineError(Exception):
    pass


@dataclass
class PlanItem:
    key: str
    name: str
    kind: str
    mod: Mod | None = None
    existing: LockEntry | None = None
    resolved: ResolvedVersion | None = None
    capture_path: Path | None = None
    note: str = ""
    error: str | None = None
    available: str | None = None
    url: str | None = None

    @property
    def from_version(self) -> str | None:
        return self.existing.version_number if self.existing else None

    @property
    def to_version(self) -> str | None:
        if self.resolved:
            return self.resolved.version_number
        return self.existing.version_number if self.existing else None

    @property
    def filename(self) -> str | None:
        if self.resolved:
            return self.resolved.filename
        if self.existing:
            return self.existing.filename
        return self.mod.file if self.mod else None

    @property
    def source(self) -> str | None:
        if self.resolved:
            return self.resolved.source
        if self.existing:
            return self.existing.source
        return self.mod.source if self.mod else None

    @property
    def is_change(self) -> bool:
        return self.kind in (ADD, UPDATE, REPIN, REMOVE, CAPTURED)


@dataclass
class Plan:
    profile: Profile
    items: list[PlanItem]

    def by_kind(self, *kinds: str) -> list[PlanItem]:
        return [i for i in self.items if i.kind in kinds]

    @property
    def has_hard_failures(self) -> bool:
        return any(i.kind == FAIL for i in self.items)

    @property
    def has_changes(self) -> bool:
        return any(i.is_change for i in self.items)


@dataclass
class ApplyResult:
    lock: Lock
    sides: list[SideResult]
    swept: list[str]
    warnings: list[str] = field(default_factory=list)


# ── planning ─────────────────────────────────────────────────────────────────


def match_targets(profile: Profile, names: list[str]) -> set[str]:
    """Map user-supplied mod identifiers to mod keys (by key, id, or name)."""
    if not names:
        return {m.key for m in profile.mods}
    wanted = {n.lower() for n in names}
    keys = set()
    for m in profile.mods:
        if m.key.lower() in wanted or m.project_id.lower() in wanted or m.name.lower() in wanted:
            keys.add(m.key)
    missing = wanted - {
        s
        for m in profile.mods
        for s in (m.key.lower(), m.project_id.lower(), m.name.lower())
    }
    if missing:
        raise EngineError(
            f"{profile.id}: no such mod(s): {', '.join(sorted(missing))}"
        )
    return keys


def plan(
    profile: Profile,
    lock: Lock,
    registry: dict[str, Provider],
    *,
    mode: str,
    update_keys: set[str] | None = None,
    session=None,
    on_item=None,
) -> Plan:
    """Resolve every mod against its source. If ``on_item`` is given, it is called
    with each ``PlanItem`` the moment it resolves (out of order) for live output."""
    session = session or new_session()

    # A lock resolved for a different game version / loader is stale: on install we
    # must re-resolve so the built jars actually match the TOML.
    target_changed = bool(lock.entries) and (
        lock.game_version != profile.game_version or lock.loader != profile.loader
    )

    def work(mod: Mod) -> PlanItem:
        return _plan_mod(mod, lock.entries.get(mod.key), registry, profile, mode,
                         update_keys, session, target_changed)

    items: list[PlanItem] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        futures = [ex.submit(work, m) for m in profile.mods]
        for fut in as_completed(futures):
            item = fut.result()
            if on_item is not None:
                on_item(item)
            items.append(item)

    desired = {m.key for m in profile.mods}
    for key, entry in lock.entries.items():
        if key not in desired:
            items.append(PlanItem(key=key, name=entry.name, kind=REMOVE, existing=entry))

    return Plan(profile=profile, items=items)


def _plan_mod(
    mod: Mod,
    existing: LockEntry | None,
    registry: dict[str, Provider],
    profile: Profile,
    mode: str,
    update_keys: set[str] | None,
    session,
    target_changed: bool = False,
) -> PlanItem:
    base = PlanItem(key=mod.key, name=mod.name, kind=KEEP, mod=mod, existing=existing)

    if mod.source == "manual":
        return _plan_manual(mod, existing, profile, base)

    pin_str = mod.pinned_version()
    targeted = update_keys is None or mod.key in update_keys

    # Decide whether we must consult the network, and toward what.
    if existing is None:
        return _resolve_into(base, mod, registry, profile, session, kind=ADD)

    if mode == "install":
        # install keeps versions, but must still make the lock satisfy the TOML:
        # honor explicit pins, and re-resolve anything that violates its channel or
        # was resolved for a now-changed game version / loader.
        if pin_str and not _at_version(existing, pin_str):
            return _resolve_into(base, mod, registry, profile, session, kind=REPIN,
                                 pin=pin_str)
        if not pin_str and (target_changed or not _satisfies_channel(existing, mod)):
            item = _resolve_into(base, mod, registry, profile, session, kind=REPIN)
            if item.resolved and _at_version(existing, item.resolved.version_id):
                return replace(base, kind=KEEP)
            return item
        return replace(base, kind=KEEP)

    # mode == update
    if not targeted:
        return replace(base, kind=KEEP)
    if mod.pin is True:  # frozen: keep, but report if newer exists
        item = _resolve_into(base, mod, registry, profile, session, kind=HELD)
        if item.kind == HELD and item.resolved and not _at_version(
            existing, item.resolved.version_id
        ):
            return replace(item, available=item.resolved.version_number, resolved=None)
        return replace(base, kind=KEEP)
    if pin_str:
        if _at_version(existing, pin_str):
            return replace(base, kind=KEEP)
        return _resolve_into(base, mod, registry, profile, session, kind=REPIN,
                             pin=pin_str)
    # unpinned + targeted: resolve latest, bump if changed
    item = _resolve_into(base, mod, registry, profile, session, kind=UPDATE)
    if item.kind == UPDATE and item.resolved and _at_version(
        existing, item.resolved.version_id
    ):
        return replace(base, kind=KEEP)
    return item


def _resolve_into(
    base: PlanItem,
    mod: Mod,
    registry: dict[str, Provider],
    profile: Profile,
    session,
    *,
    kind: str,
    pin: str | None = None,
) -> PlanItem:
    provider = registry[mod.source]
    resolve_mod = replace(mod, pin=pin) if pin is not None else mod
    try:
        rv = provider.resolve(resolve_mod, profile.game_version, profile.loader, session)
        return replace(base, kind=kind, resolved=rv)
    except ManualRequired as e:
        # A source that turned out to need manual download (e.g. CF disabled).
        return replace(base, kind=MANUAL, note=str(e), url=mod.url or e.url)
    except ResolveError as e:
        return replace(base, kind=FAIL, error=str(e))


def _plan_manual(mod: Mod, existing: LockEntry | None, profile: Profile,
                 base: PlanItem) -> PlanItem:
    """Manual mods are always reported as MANUAL (we can't auto-check them), but if
    the expected jar (``file``) is present we capture it so it's tracked and not
    pruned; if it's nowhere to be found we flag it as not present."""
    item = replace(base, kind=MANUAL, url=mod.url)
    expected = mod.file or (existing.filename if existing else None)
    if not expected:
        return replace(item, note="not present")  # never added — flag it
    found = _find_side_file(profile, mod, expected)
    if found is not None:
        if not (existing and existing.filename == expected):
            item = replace(item, capture_path=found)  # newly placed → ingest
        return item
    if existing and existing.filename == expected:
        return item  # previously captured; restorable from the store
    return replace(item, note="not present")


def _find_side_file(profile: Profile, mod: Mod, filename: str) -> Path | None:
    for side in mod.sides():
        candidate = profile.dir_for_side(side) / filename
        if candidate.exists():
            return candidate
    return None


def _at_version(entry: LockEntry, token: str) -> bool:
    return entry.version_id == token or entry.version_number == token


_CHANNEL_RANK = {"release": 3, "beta": 2, "alpha": 1}


def _satisfies_channel(entry: LockEntry, mod: Mod) -> bool:
    """Whether a locked version meets the mod's declared channel floor. An unknown
    release type (a lock from before this was tracked) is assumed fine, so old locks
    don't churn until their next update stamps a real type."""
    if not entry.release_type:
        return True
    floor = _CHANNEL_RANK.get(mod.channel, 3)
    return _CHANNEL_RANK.get(entry.release_type, 3) >= floor


# ── applying ─────────────────────────────────────────────────────────────────


def apply(
    plan: Plan,
    lock: Lock,
    registry: dict[str, Provider],
    store: Store,
    ws: Workspace,
    *,
    session=None,
    on_download=None,
) -> ApplyResult:
    """Commit a plan atomically: secure bytes, then lock, then materialize, then sweep.

    If ``on_download`` is given, it is called with each ``PlanItem`` as its bytes
    land in the store (out of order), for live progress."""
    if plan.has_hard_failures:
        fails = plan.by_kind(FAIL)
        raise EngineError(
            "aborting — could not resolve:\n"
            + "\n".join(f"  {i.name}: {i.error}" for i in fails)
        )

    session = session or new_session()
    profile = plan.profile

    try:
        # 1. Secure all needed bytes into the store (concurrently), compute entries.
        new_entries = _secure_and_build(plan, registry, store, session, on_download)

        # 2. Commit the lockfile atomically.
        new_lock = Lock(
            profile_id=profile.id,
            game_version=profile.game_version,
            loader=profile.loader,
            entries=new_entries,
        )
        saved = lock_io.save(new_lock, ws)

        # 3. Materialize the side folders (pure function of the lock).
        sides = materialize(profile, saved, store, old_lock=lock)

        # 4. Sweep the store against every profile's committed lock.
        swept = _sweep_all(ws, store)
        return ApplyResult(lock=saved, sides=sides, swept=swept)
    except BaseException:
        # Reclaim any partial downloads; leave committed state as-is.
        _sweep_all(ws, store)
        raise


def _secure_and_build(plan, registry, store, session, on_download=None) -> dict[str, LockEntry]:
    profile = plan.profile
    entries: dict[str, LockEntry] = {}
    downloads: list[PlanItem] = []

    for item in plan.items:
        kind = item.kind
        if kind == REMOVE:
            continue
        if kind == MANUAL:
            if item.capture_path:  # a newly-placed jar → ingest it
                sha = store.add_file(item.capture_path)
                store.register_manual(sha, item.mod.name, item.capture_path.name)
                entries[item.key] = _entry_captured(item.mod, item.capture_path.name, sha)
            elif item.existing and store.has(item.existing.sha512):
                store.register_manual(item.existing.sha512, item.name, item.existing.filename)
                entries[item.key] = replace(  # keep previously captured jar
                    item.existing, name=item.name,
                    side=item.mod.side, enabled=item.mod.enabled,
                    pinned=item.mod.is_pinned, type=item.mod.type,
                )
            # else: not present anywhere → omit from the lock (reported as missing)
            continue
        if kind in (KEEP, HELD):
            entry = replace(
                item.existing, name=item.name,
                side=item.mod.side, enabled=item.mod.enabled,
                pinned=item.mod.is_pinned, type=item.mod.type,
            )
            entries[item.key] = entry
            downloads.append(replace(item, kind=KEEP, resolved=None))  # ensure-present
            continue
        if kind in (ADD, UPDATE, REPIN):
            downloads.append(item)
            continue

    # Concurrent fetch for (ADD/UPDATE/REPIN) and ensure-present for KEEP.
    # The bool result flags whether bytes were actually secured (so callers can
    # show a "downloading" line for it — including KEEP restores on a fresh build).
    def fetch(item: PlanItem) -> tuple[str, LockEntry | None, bool]:
        if item.resolved is not None:
            rv = item.resolved
            headers = registry[rv.source].download_headers()
            sha = store.fetch(rv.download_url, rv.sha512 or None,
                              headers=headers, session=session)
            if rv.sha512 == "" and rv.sha1:
                _verify_sha1(store, sha, rv)
            return item.key, _entry_resolved(item.mod, rv, sha), True
        # KEEP ensure-present: make sure the locked jar is in the store.
        entry = entries[item.key]
        if entry.sha512 and not store.has(entry.sha512):
            if entry.download_url:
                store.fetch(entry.download_url, entry.sha512,
                            headers=registry[entry.source].download_headers(),
                            session=session)
            else:
                _reingest_from_side(profile, entry, store)
            return item.key, None, True
        return item.key, None, False

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, item): item for item in downloads}
        for fut in as_completed(futures):
            key, entry, secured = fut.result()
            if entry is not None:
                entries[key] = entry
            if on_download is not None and secured:
                on_download(futures[fut])
    return entries


def _entry_resolved(mod: Mod, rv: ResolvedVersion, sha512: str) -> LockEntry:
    return LockEntry(
        key=mod.key, name=mod.name, source=rv.source, project_id=rv.project_id,
        version_id=rv.version_id, version_number=rv.version_number,
        filename=rv.filename, sha512=sha512, side=mod.side,
        download_url=rv.download_url, enabled=mod.enabled, pinned=mod.is_pinned,
        type=mod.type, dependencies=rv.dependencies, canonical_id=rv.canonical_id,
        client_side=rv.client_side, server_side=rv.server_side,
        release_type=rv.release_type,
    )


def capture_manual(
    profile: Profile, mod: Mod, jar_path: Path, store: Store, ws: Workspace
) -> tuple[Lock, list[SideResult], list[str]]:
    """Register a manually-downloaded jar for ``mod``: ingest it into the store,
    record it in the profile lock as a captured entry (so it's tracked, built, and
    never swept), rebuild the folders, and sweep. Returns (lock, sides, swept)."""
    if mod.source != "manual":
        raise EngineError(f"'{mod.name}' is not a manual mod — it's downloaded "
                          f"automatically from {mod.source}.")
    old = lock_io.load(profile.id, ws)
    with ws.exclusive():
        try:
            sha = store.add_file(jar_path)
            store.register_manual(sha, mod.name, jar_path.name)
            entries = dict(old.entries)
            entries[mod.key] = _entry_captured(mod, jar_path.name, sha)
            new_lock = Lock(
                profile_id=profile.id,
                game_version=old.game_version or profile.game_version,
                loader=old.loader or profile.loader,
                entries=entries,
            )
            saved = lock_io.save(new_lock, ws)
            sides = materialize(profile, saved, store, old_lock=old)
            swept = _sweep_all(ws, store)
            return saved, sides, swept
        except BaseException:
            _sweep_all(ws, store)
            raise


def _entry_captured(mod: Mod, filename: str, sha512: str) -> LockEntry:
    return LockEntry(
        key=mod.key, name=mod.name, source="manual", project_id=mod.project_id,
        version_id=sha512[:12], version_number=filename, filename=filename,
        sha512=sha512, side=mod.side, download_url=None,
        enabled=mod.enabled, pinned=mod.is_pinned, type=mod.type,
    )


def _verify_sha1(store: Store, sha512: str, rv: ResolvedVersion) -> None:
    import hashlib

    data = store.path(sha512).read_bytes()
    actual = hashlib.sha1(data).hexdigest()
    if actual != rv.sha1:
        store.path(sha512).unlink(missing_ok=True)
        raise HashMismatch(rv.sha1, actual, rv.download_url)


def _reingest_from_side(profile: Profile, entry: LockEntry, store: Store) -> None:
    for side in entry.sides():
        p = profile.dir_for_side(side) / entry.filename
        if p.exists():
            store.add_file(p, entry.sha512)
            return
    raise EngineError(
        f"'{entry.name}': jar {entry.filename} is missing from the store and cannot "
        "be restored (no download URL and no copy on disk). Re-add or update it."
    )


def _sweep_all(ws: Workspace, store: Store) -> list[str]:
    ids = ws.discover_profile_ids()
    # Live = jars any profile's lock references, plus every manual jar (they can't be
    # re-downloaded, so they're kept permanently so rollback always works).
    live = lock_io.all_referenced_hashes(ids, ws) | store.manual_hashes()
    return store.sweep(live)

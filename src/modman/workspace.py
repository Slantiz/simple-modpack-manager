"""Filesystem layout and atomic primitives.

Everything lives under a workspace root (the ``mod-manager`` directory):

    profiles/<id>.toml        desired state (user-owned)
    profiles/<id>.lock.json   resolved current state (tool-owned)
    store/<sha512>.jar        content-addressed working set
    history/<id>/*.json       manual known-good snapshots
    mods/<id>/{client,server} built outputs
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

def _discover_root() -> Path:
    """The workspace root is the nearest ancestor of the CWD holding ``profiles/``.

    This keeps the tool location-independent (works from the repo root, a
    subdirectory, or after ``pip install``). ``MODMAN_ROOT`` overrides it.
    """
    env = os.environ.get("MODMAN_ROOT")
    if env:
        return Path(env)
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        if (d / "profiles").is_dir():
            return d
    return cwd


class Workspace:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root).resolve() if root else _discover_root().resolve()

    # ── directories ──────────────────────────────────────────────────────────
    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"

    @property
    def store_dir(self) -> Path:
        return self.root / "store"

    @property
    def history_dir(self) -> Path:
        return self.root / "history"

    @property
    def mods_dir(self) -> Path:
        return self.root / "mods"

    # ── per-profile paths ────────────────────────────────────────────────────
    def profile_toml(self, profile_id: str) -> Path:
        return self.profiles_dir / f"{profile_id}.toml"

    def lock_path(self, profile_id: str) -> Path:
        return self.profiles_dir / f"{profile_id}.lock.json"

    def history_profile_dir(self, profile_id: str) -> Path:
        return self.history_dir / profile_id

    def default_client_dir(self, profile_id: str) -> Path:
        return self.mods_dir / profile_id / "client"

    def default_server_dir(self, profile_id: str) -> Path:
        return self.mods_dir / profile_id / "server"

    def default_datapacks_dir(self, profile_id: str) -> Path:
        return self.mods_dir / profile_id / "datapacks"

    def default_singleplayer_dir(self, profile_id: str) -> Path:
        return self.mods_dir / profile_id / "singleplayer"

    def discover_profile_ids(self) -> list[str]:
        if not self.profiles_dir.is_dir():
            return []
        return sorted(p.stem for p in self.profiles_dir.glob("*.toml"))

    # ── single-writer lock ───────────────────────────────────────────────────
    @contextmanager
    def exclusive(self):
        """Best-effort cross-process lock so two runs can't race the store."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_file = self.root / ".modman.lock"
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            raise RuntimeError(
                f"Another modman run holds the lock ({lock_file}). "
                "If no run is active, delete that file."
            )
        try:
            os.write(fd, str(os.getpid()).encode())
            yield
        finally:
            os.close(fd)
            try:
                lock_file.unlink()
            except FileNotFoundError:
                pass


def atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` atomically (temp file in same dir, then rename)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)  # atomic on same filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(dest: Path, obj) -> None:
    atomic_write_bytes(dest, (json.dumps(obj, indent=2) + "\n").encode("utf-8"))

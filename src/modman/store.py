"""Content-addressed jar store — the deduplicated working set.

A jar's filename *is* its sha512, so a corrupt or partial download can never be
mistaken for a valid one. Bytes only enter the store after their hash is verified,
and only via an atomic rename, so a crash never leaves a half-written jar.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import requests

from .workspace import Workspace

_CHUNK = 1 << 16
_USER_AGENT = "modman/0.1 (+https://github.com/local/modman)"


class StoreError(Exception):
    pass


class HashMismatch(StoreError):
    def __init__(self, expected: str, actual: str, url: str | None = None):
        self.expected, self.actual, self.url = expected, actual, url
        src = f" from {url}" if url else ""
        super().__init__(
            f"sha512 mismatch{src}: expected {expected[:16]}…, got {actual[:16]}…"
        )


def sha512_file(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class Store:
    def __init__(self, ws: Workspace | None = None) -> None:
        self.ws = ws or Workspace()

    @property
    def dir(self) -> Path:
        return self.ws.store_dir

    def path(self, sha512: str) -> Path:
        return self.dir / f"{sha512}.jar"

    def has(self, sha512: str) -> bool:
        return self.path(sha512).exists()

    def verify(self, sha512: str) -> bool:
        """True iff the stored jar exists and still hashes to its name."""
        p = self.path(sha512)
        return p.exists() and sha512_file(p) == sha512

    # ── ingest ───────────────────────────────────────────────────────────────
    def add_file(self, src: Path, expected_sha512: str | None = None) -> str:
        """Copy an existing file into the store, verifying its hash. Returns hash."""
        digest = sha512_file(src)
        if expected_sha512 and digest != expected_sha512:
            raise HashMismatch(expected_sha512, digest, str(src))
        if not self.has(digest):
            self._atomic_ingest(src.read_bytes(), digest)
        return digest

    def _atomic_ingest(self, data: bytes, sha512: str) -> None:
        """Write bytes into the store under their hash via a temp file + rename."""
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".part")
        tmp_path = Path(tmp)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            final = self.path(sha512)
            if final.exists():
                os.unlink(tmp_path)
            else:
                os.replace(tmp_path, final)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

    def fetch(
        self,
        url: str,
        expected_sha512: str | None = None,
        *,
        headers: dict | None = None,
        session: requests.Session | None = None,
    ) -> str:
        """Download ``url`` into the store, verifying the hash. Returns the hash.

        If ``expected_sha512`` is already present, this is a no-op (offline-safe).
        """
        if expected_sha512 and self.has(expected_sha512):
            return expected_sha512

        self.dir.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha512()
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".part")
        tmp_path = Path(tmp)
        try:
            getter = session.get if session else requests.get
            with getter(
                url,
                headers={"User-Agent": _USER_AGENT, **(headers or {})},
                stream=True,
                timeout=60,
            ) as resp:
                resp.raise_for_status()
                with os.fdopen(fd, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=_CHUNK):
                        f.write(chunk)
                        h.update(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            digest = h.hexdigest()
            if expected_sha512 and digest != expected_sha512:
                raise HashMismatch(expected_sha512, digest, url)
            final = self.path(digest)
            if final.exists():
                os.unlink(tmp_path)
            else:
                os.replace(tmp_path, final)
            return digest
        except BaseException:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

    # ── materialize / sweep ──────────────────────────────────────────────────
    def copy_to(self, sha512: str, dest: Path) -> None:
        """Copy a stored jar to ``dest`` atomically, verifying integrity first."""
        src = self.path(sha512)
        if not src.exists():
            raise StoreError(f"jar {sha512[:16]}… not in store")
        data = src.read_bytes()
        actual = hashlib.sha512(data).hexdigest()
        if actual != sha512:
            raise HashMismatch(sha512, actual, str(src))
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def sweep(self, live_hashes: set[str]) -> list[str]:
        """Delete every stored jar not in ``live_hashes``. Returns removed hashes.

        Callers must pass the union of *all* profiles' committed lockfiles, so a
        jar referenced by any profile is never removed.
        """
        if not self.dir.is_dir():
            return []
        removed = []
        for p in self.dir.glob("*.jar"):
            if p.stem not in live_hashes:
                p.unlink()
                removed.append(p.stem)
        # Clean up any stray temp files from interrupted downloads.
        for p in self.dir.glob("*.part"):
            p.unlink()
        for p in self.dir.glob("*.tmp"):
            p.unlink()
        return removed

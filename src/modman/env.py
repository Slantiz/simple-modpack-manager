"""Minimal .env reader for secrets (CurseForge API key)."""

from __future__ import annotations

import os

from .workspace import Workspace


def load_env(ws: Workspace | None = None) -> None:
    ws = ws or Workspace()
    path = ws.root / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def curseforge_api_key(ws: Workspace | None = None) -> str | None:
    load_env(ws)
    return os.environ.get("CURSEFORGE_API_KEY") or None

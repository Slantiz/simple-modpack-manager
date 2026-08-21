"""Version-resolution providers.

Each provider turns a desired ``Mod`` (plus the profile's game version + loader)
into a concrete ``ResolvedVersion`` with a sha512, download URL, and dependencies.
"""

from __future__ import annotations

from ..model import Mod
from .base import Provider, ResolveError, ManualRequired
from .curseforge import CurseForgeProvider
from .manual import ManualProvider
from .modrinth import ModrinthProvider
from .url import UrlProvider


def build_registry(
    curseforge_api_key: str | None = None,
) -> dict[str, Provider]:
    return {
        "modrinth": ModrinthProvider(),
        "curseforge": CurseForgeProvider(curseforge_api_key),
        "manual": ManualProvider(),
        "url": UrlProvider(),
    }


__all__ = [
    "Provider",
    "ResolveError",
    "ManualRequired",
    "ModrinthProvider",
    "CurseForgeProvider",
    "ManualProvider",
    "UrlProvider",
    "build_registry",
    "Mod",
]

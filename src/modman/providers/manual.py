"""Manual provider — mods the user places by hand.

Resolution never yields an auto-download; it always signals ``ManualRequired`` so
the engine knows to look for a hand-placed jar in the side folders instead.
"""

from __future__ import annotations

import requests

from ..model import Mod, ResolvedVersion
from .base import ManualRequired, Provider


class ManualProvider(Provider):
    name = "manual"

    def resolve(
        self, mod: Mod, game_version: str, loader: str, session: requests.Session
    ) -> ResolvedVersion:
        page = (
            f"https://www.curseforge.com/minecraft/mc-mods/{mod.project_id}"
            if mod.project_id and "/" not in mod.project_id
            else None
        )
        raise ManualRequired(
            f"'{mod.name}' is manual — place its jar in the side folder(s)",
            url=page,
        )

"""CurseForge provider (fallback source).

CurseForge does not advertise sha512, so the store address is computed on download;
the API's sha1 (when present) is carried for an integrity check.
"""

from __future__ import annotations

import requests

from ..model import Mod, ResolvedVersion
from .base import Provider, ManualRequired, ResolveError, request_json

BASE = "https://api.curseforge.com/v1"
GAME_ID = 432  # Minecraft
LOADER_TYPE = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
# CurseForge release types: 1=release, 2=beta, 3=alpha  (rank so higher = more stable)
_CHANNEL_RANK = {"release": 3, "beta": 2, "alpha": 1}
_CF_RELEASE_RANK = {1: 3, 2: 2, 3: 1}
_CF_RELEASE_NAME = {1: "release", 2: "beta", 3: "alpha"}


class CurseForgeProvider(Provider):
    name = "curseforge"

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "Accept": "application/json"}

    def download_headers(self) -> dict:
        return {"x-api-key": self.api_key} if self.api_key else {}

    def resolve(
        self, mod: Mod, game_version: str, loader: str, session: requests.Session
    ) -> ResolvedVersion:
        if not self.api_key:
            raise ResolveError(
                f"'{mod.project_id}' needs CurseForge, but no API key "
                "(set CURSEFORGE_API_KEY in .env)"
            )
        loader_type = LOADER_TYPE.get(loader)
        if loader_type is None:
            raise ResolveError(f"unknown loader '{loader}' for CurseForge")

        mod_id = self._mod_id(session, mod.project_id)
        files = self._files(session, mod_id, game_version, loader_type)
        if not files:
            raise ResolveError(
                f"no CurseForge build for {game_version} + {loader} "
                f"('{mod.project_id}')"
            )

        chosen = self._select(files, mod)
        if chosen is None:
            raise ResolveError(
                f"no '{mod.channel}'+ CurseForge build for {game_version} + {loader} "
                f"('{mod.project_id}')"
            )

        url = chosen.get("downloadUrl")
        page = f"https://www.curseforge.com/minecraft/mc-mods/{mod.project_id}"
        if not url:
            raise ManualRequired(
                f"'{mod.name}' has API downloads disabled by the author",
                url=page,
                filename=chosen.get("fileName"),
            )

        return ResolvedVersion(
            source="curseforge",
            project_id=mod.project_id,
            version_id=str(chosen["id"]),
            version_number=chosen.get("displayName", str(chosen["id"])),
            filename=chosen["fileName"],
            download_url=url,
            sha512="",  # computed on download
            sha1=_sha1(chosen),
            dependencies=tuple(
                str(d["modId"])
                for d in chosen.get("dependencies", [])
                if d.get("relationType") == 3 and d.get("modId")  # 3 = required
            ),
            release_type=_CF_RELEASE_NAME.get(chosen.get("releaseType", 1), "release"),
        )

    def _mod_id(self, session, slug: str) -> int:
        data = request_json(
            session,
            "GET",
            f"{BASE}/mods/search",
            params={"gameId": GAME_ID, "slug": slug},
            headers=self._headers(),
        )
        results = data.get("data", [])
        if not results:
            raise ResolveError(f"slug '{slug}' not found on CurseForge")
        return results[0]["id"]

    def _files(self, session, mod_id: int, game_version: str, loader_type: int) -> list:
        data = request_json(
            session,
            "GET",
            f"{BASE}/mods/{mod_id}/files",
            params={
                "gameVersion": game_version,
                "modLoaderType": loader_type,
                "pageSize": 50,
            },
            headers=self._headers(),
        )
        files = data.get("data", [])
        if files:
            return files
        # Some mods are mislabeled; retry without the loader filter.
        data = request_json(
            session,
            "GET",
            f"{BASE}/mods/{mod_id}/files",
            params={"gameVersion": game_version, "pageSize": 50},
            headers=self._headers(),
        )
        return data.get("data", [])

    def _select(self, files: list[dict], mod: Mod) -> dict | None:
        files = sorted(files, key=lambda f: f.get("fileDate", ""), reverse=True)
        pinned = mod.pinned_version()
        floor = _CHANNEL_RANK.get(mod.channel, 3)
        for f in files:
            if pinned is not None:
                if f.get("displayName") == pinned or str(f.get("id")) == pinned:
                    return f
                continue
            if _CF_RELEASE_RANK.get(f.get("releaseType", 1), 3) >= floor:
                return f
        return None


def _sha1(file: dict) -> str | None:
    for h in file.get("hashes", []):
        if h.get("algo") == 1:  # 1 = sha1
            return str(h.get("value", "")).lower() or None
    return None

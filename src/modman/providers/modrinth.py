"""Modrinth provider."""

from __future__ import annotations

import requests

from ..model import Mod, ResolvedVersion
from .base import Provider, ResolveError, request_json

BASE = "https://api.modrinth.com/v2"

_CHANNEL_RANK = {"release": 3, "beta": 2, "alpha": 1}


class ModrinthProvider(Provider):
    name = "modrinth"

    def resolve(
        self, mod: Mod, game_version: str, loader: str, session: requests.Session
    ) -> ResolvedVersion:
        # Modrinth models datapacks as their own "loader".
        effective_loader = "datapack" if mod.is_datapack else loader
        try:
            versions = request_json(
                session,
                "GET",
                f"{BASE}/project/{mod.project_id}/version",
                params={
                    "game_versions": f'["{game_version}"]',
                    "loaders": f'["{effective_loader}"]',
                },
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise ResolveError(
                    f"project '{mod.project_id}' not found on Modrinth"
                ) from e
            raise ResolveError(f"Modrinth error for '{mod.project_id}': {e}") from e

        if not versions:
            raise ResolveError(
                f"no Modrinth build for {game_version} + {effective_loader} "
                f"('{mod.project_id}')"
            )

        chosen = self._select(versions, mod)
        if chosen is None:
            raise ResolveError(
                f"no '{mod.channel}'+ Modrinth build for {game_version} + "
                f"{effective_loader} ('{mod.project_id}')"
            )

        file = _primary_file(chosen)
        sha512 = (file.get("hashes") or {}).get("sha512")
        if not sha512:
            raise ResolveError(
                f"Modrinth file for '{mod.project_id}' has no sha512 hash"
            )

        deps = tuple(
            d["project_id"]
            for d in chosen.get("dependencies", [])
            if d.get("dependency_type") == "required" and d.get("project_id")
        )
        client_side, server_side = self._support(session, mod.project_id)

        return ResolvedVersion(
            source="modrinth",
            project_id=mod.project_id,
            version_id=chosen["id"],
            version_number=chosen.get("version_number", chosen["id"]),
            filename=file["filename"],
            sha512=sha512.lower(),
            download_url=file["url"],
            dependencies=deps,
            canonical_id=chosen.get("project_id"),
            client_side=client_side,
            server_side=server_side,
        )

    def _select(self, versions: list[dict], mod: Mod) -> dict | None:
        """Pick the version honoring an explicit pin and the channel floor.

        Versions come newest-first from the API; we keep that order but filter by
        channel and, if pinned to a specific version string, match it exactly.
        """
        pinned = mod.pinned_version()
        floor = _CHANNEL_RANK.get(mod.channel, 3)
        for v in versions:
            if pinned is not None:
                if v.get("version_number") == pinned or v.get("id") == pinned:
                    return v
                continue
            if _CHANNEL_RANK.get(v.get("version_type", "release"), 3) >= floor:
                return v
        return None

    def _support(self, session, project_id: str) -> tuple[str | None, str | None]:
        try:
            proj = request_json(session, "GET", f"{BASE}/project/{project_id}")
        except (ResolveError, requests.HTTPError):
            return None, None
        return proj.get("client_side"), proj.get("server_side")


def _primary_file(version: dict) -> dict:
    files = version.get("files", [])
    if not files:
        raise ResolveError("Modrinth version has no files")
    for f in files:
        if f.get("primary"):
            return f
    return files[0]

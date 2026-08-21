"""Direct-URL provider — the mod's ``id`` is a download URL.

There is nothing to query, so the URL itself is the version identity and the
sha512 is computed on download. Pin to a specific URL by editing the TOML.
"""

from __future__ import annotations

import hashlib

import requests

from ..model import Mod, ResolvedVersion
from .base import Provider, ResolveError


class UrlProvider(Provider):
    name = "url"

    def resolve(
        self, mod: Mod, game_version: str, loader: str, session: requests.Session
    ) -> ResolvedVersion:
        url = mod.project_id
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ResolveError(f"'{mod.name}': url source id must be an http(s) URL")
        filename = url.rstrip("/").rsplit("/", 1)[-1] or f"{mod.name}.jar"
        # A stable id derived from the URL so re-resolving is a no-op when unchanged.
        version_id = hashlib.sha1(url.encode()).hexdigest()[:12]
        return ResolvedVersion(
            source="url",
            project_id=url,
            version_id=version_id,
            version_number=filename,
            filename=filename,
            download_url=url,
            sha512="",
        )

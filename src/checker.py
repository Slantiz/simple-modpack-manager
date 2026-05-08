import json
import requests
from dataclasses import dataclass, field

from . import colors

# ── Modrinth ──────────────────────────────────────────────────────────────────

MR_BASE = "https://api.modrinth.com/v2"
MR_HEADERS = {"User-Agent": "create-modpack-manager/1.0"}

# ── CurseForge ────────────────────────────────────────────────────────────────

CF_BASE = "https://api.curseforge.com/v1"
CF_GAME_ID = 432  # Minecraft
CF_LOADER_TYPE = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}


# ── Shared result type ────────────────────────────────────────────────────────


@dataclass
class VersionInfo:
    version_id: str
    version_number: str
    filename: str
    download_url: str
    source: str = field(default="Modrinth")


# ── Verbose dump ──────────────────────────────────────────────────────────────


def _verbose_dump(response: requests.Response) -> None:
    print(colors.gray("    ┌─ Response dump ────────────────────────"))
    print(colors.gray(f"    │  URL     : {response.request.url}"))
    print(colors.gray(f"    │  Method  : {response.request.method}"))
    print(colors.gray(f"    │  Status  : {response.status_code} {response.reason}"))
    print(colors.gray("    │  Headers :"))
    for k, v in response.headers.items():
        print(colors.gray(f"    │    {k}: {v}"))
    print(colors.gray("    │  Body    :"))
    try:
        body = json.dumps(response.json(), indent=2)
    except Exception:
        body = response.text or "(empty)"
    for line in body.splitlines():
        print(colors.gray(f"    │    {line}"))
    print(colors.gray("    └───────────────────────────────────────"))


# ── Modrinth internals ────────────────────────────────────────────────────────


def _modrinth_latest(slug: str, game_version: str, loader: str) -> VersionInfo | None:
    response = requests.get(
        f"{MR_BASE}/project/{slug}/version",
        params={
            "game_versions": f'["{game_version}"]',
            "loaders": f'["{loader}"]',
        },
        headers=MR_HEADERS,
    )
    response.raise_for_status()
    versions = response.json()
    if not versions:
        return None
    latest = versions[0]
    file = latest["files"][0]
    return VersionInfo(
        version_id=latest["id"],
        version_number=latest["version_number"],
        filename=file["filename"],
        download_url=file["url"],
        source="Modrinth",
    )


# ── CurseForge internals ──────────────────────────────────────────────────────


def _cf_headers(api_key: str) -> dict:
    return {
        "User-Agent": "create-modpack-manager/1.0",
        "x-api-key": api_key,
        "Accept": "application/json",
    }


def validate_cf_key(api_key: str) -> tuple[bool, str]:
    try:
        r = requests.get(f"{CF_BASE}/games/{CF_GAME_ID}", headers=_cf_headers(api_key))
        if r.status_code == 200:
            return True, ""
        body = _cf_error_body(r)
        if r.status_code == 401:
            return False, f"401 Unauthorized{body}"
        if r.status_code == 403:
            return (
                False,
                f"403 Forbidden{body} — check the key is active on console.curseforge.com",
            )
        return False, f"HTTP {r.status_code}{body}"
    except Exception as e:
        return False, str(e)


def _cf_error_body(response) -> str:
    try:
        data = response.json()
        msg = data.get("message") or data.get("error") or data.get("detail")
        return f": {msg}" if msg else ""
    except Exception:
        text = response.text.strip()
        return f": {text[:200]}" if text else ""


def _cf_mod_id(cf_slug: str, api_key: str) -> int | None:
    response = requests.get(
        f"{CF_BASE}/mods/search",
        params={"gameId": CF_GAME_ID, "slug": cf_slug},
        headers=_cf_headers(api_key),
    )
    if not response.ok:
        raise requests.HTTPError(
            f"{response.status_code}{_cf_error_body(response)}",
            response=response,
        )
    results = response.json().get("data", [])
    return results[0]["id"] if results else None


def _curseforge_latest(
    cf_slug: str, game_version: str, loader: str, api_key: str
) -> VersionInfo | None:
    loader_type = CF_LOADER_TYPE.get(loader.lower())
    if loader_type is None:
        raise ValueError(f"Unknown loader '{loader}' for CurseForge")

    mod_id = _cf_mod_id(cf_slug, api_key)
    if mod_id is None:
        return None

    response = requests.get(
        f"{CF_BASE}/mods/{mod_id}/files",
        params={"gameVersion": game_version, "modLoaderType": loader_type},
        headers=_cf_headers(api_key),
    )
    response.raise_for_status()
    files = response.json().get("data", [])
    if not files:
        return None

    files.sort(key=lambda f: f["fileDate"], reverse=True)
    latest = files[0]
    download_url = latest.get("downloadUrl")
    if not download_url:
        return None  # author-restricted download

    return VersionInfo(
        version_id=str(latest["id"]),
        version_number=latest["displayName"],
        filename=latest["fileName"],
        download_url=download_url,
        source="CurseForge",
    )


# ── Main check loop ───────────────────────────────────────────────────────────


def check_all(
    mods: list,
    game_version: str,
    loader: str,
    cache: dict,
    curseforge_api_key: str | None = None,
    verbose: bool = False,
    force: bool = False,
) -> tuple[list, list, list]:
    to_download = []
    up_to_date = []
    not_found = []
    pad = max(len(m.name) for m in mods)

    for mod in mods:
        print(f"  Checking {mod.name:<{pad}} ...", end=" ", flush=True)
        latest = None
        modrinth_miss = False

        if not mod.slug:
            print(colors.yellow("skipping Modrinth (no slug),"), end=" ", flush=True)
            modrinth_miss = True
        else:
            try:
                latest = _modrinth_latest(mod.slug, game_version, loader)
                if latest is None:
                    modrinth_miss = True
            except requests.HTTPError as e:
                if e.response.status_code == 404:
                    modrinth_miss = True
                else:
                    reason = f"Modrinth error: {e}"
                    print(colors.red(f"[!] {reason}"))
                    if verbose:
                        _verbose_dump(e.response)
                    not_found.append({"mod": mod, "reason": reason})
                    continue

        if modrinth_miss:
            if mod.curseforge_slug and curseforge_api_key:
                print(
                    colors.yellow("not on Modrinth, trying CurseForge ..."),
                    end=" ",
                    flush=True,
                )
                try:
                    latest = _curseforge_latest(
                        mod.curseforge_slug, game_version, loader, curseforge_api_key
                    )
                except requests.HTTPError as e:
                    if e.response.status_code == 403:
                        reason = f"CurseForge: mod has restricted API access (author disabled third-party downloads){_cf_error_body(e.response)}"
                    elif e.response.status_code == 404:
                        reason = f"CurseForge: slug '{mod.curseforge_slug}' not found"
                    else:
                        reason = f"CurseForge error {e.response.status_code}{_cf_error_body(e.response)}"
                    print(colors.red(f"[!] {reason}"))
                    if verbose:
                        _verbose_dump(e.response)
                    not_found.append({"mod": mod, "reason": reason})
                    continue
                except Exception as e:
                    reason = f"CurseForge error: {e}"
                    print(colors.red(f"[!] {reason}"))
                    not_found.append({"mod": mod, "reason": reason})
                    continue
            else:
                if mod.curseforge_slug and not curseforge_api_key:
                    reason = "CurseForge slug set but no API key — add CURSEFORGE_API_KEY to .env"
                else:
                    reason = "not found on Modrinth (no curseforge_slug set)"
                print(colors.red(f"[!] {reason}"))
                not_found.append({"mod": mod, "reason": reason})
                continue

        if latest is None:
            reason = f"no matching version for {game_version} + {loader}"
            print(colors.red(f"[!] {reason}"))
            not_found.append({"mod": mod, "reason": reason})
            continue

        cached_version_id = cache.get(mod.slug, {}).get("version_id")
        if cached_version_id is None:
            print(colors.green(f"[+] new ({latest.version_number}) [{latest.source}]"))
            to_download.append({"mod": mod, "version": latest, "is_new": True})
        elif cached_version_id == latest.version_id and not force:
            print(
                colors.gray(
                    f"[=] up to date ({latest.version_number}) [{latest.source}]"
                )
            )
            up_to_date.append({"mod": mod, "version": latest})
        else:
            tag = (
                "[↑] update available"
                if cached_version_id != latest.version_id
                else "[~] forced re-download"
            )
            print(colors.yellow(f"{tag} ({latest.version_number}) [{latest.source}]"))
            to_download.append({"mod": mod, "version": latest, "is_new": False})

    return to_download, up_to_date, not_found

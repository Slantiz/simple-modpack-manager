"""Parse and validate profile TOMLs into ``Profile`` objects.

The TOML is the single source of truth and is *only ever read* here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .model import (
    VALID_CHANNELS,
    VALID_SIDES,
    VALID_SOURCES,
    VALID_TYPES,
    ConfigError,
    Mod,
    Profile,
)
from .workspace import Workspace

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-") or "mod"


def load_profile(profile_id: str, ws: Workspace | None = None) -> Profile:
    ws = ws or Workspace()
    path = ws.profile_toml(profile_id)
    if not path.exists():
        raise ConfigError(f"No profile '{profile_id}' at {path}")
    return parse_profile(path, ws)


def load_all(ws: Workspace | None = None) -> list[Profile]:
    ws = ws or Workspace()
    return [parse_profile(ws.profile_toml(pid), ws) for pid in ws.discover_profile_ids()]


def resolve_targets(targets: list[str], ws: Workspace | None = None) -> list[Profile]:
    """Resolve CLI target(s) — a list of ids, or ['all'] / [] — to profiles."""
    ws = ws or Workspace()
    if not targets or targets == ["all"]:
        profiles = load_all(ws)
        if not profiles:
            raise ConfigError(f"No profiles found in {ws.profiles_dir}")
        return profiles
    return [load_profile(t, ws) for t in targets]


def parse_profile(path: Path, ws: Workspace | None = None) -> Profile:
    ws = ws or Workspace()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: invalid TOML — {e}") from e
    except OSError as e:
        raise ConfigError(f"{path.name}: cannot read — {e}") from e

    settings = data.get("profile") or {}
    file_id = path.stem
    declared_id = settings.get("id")
    if declared_id and declared_id != file_id:
        raise ConfigError(
            f"{path.name}: profile id '{declared_id}' does not match filename "
            f"(expected '{file_id}'). Rename the file or fix the id."
        )
    profile_id = file_id

    game_version = settings.get("game_version")
    loader = settings.get("loader")
    if not game_version:
        raise ConfigError(f"{path.name}: missing [profile].game_version")
    if not loader:
        raise ConfigError(f"{path.name}: missing [profile].loader")

    client_dir = (
        Path(settings["client_dir"])
        if settings.get("client_dir")
        else ws.default_client_dir(profile_id)
    )
    server_dir = (
        Path(settings["server_dir"])
        if settings.get("server_dir")
        else ws.default_server_dir(profile_id)
    )
    datapacks_dir = (
        Path(settings["datapacks_dir"])
        if settings.get("datapacks_dir")
        else ws.default_datapacks_dir(profile_id)
    )
    singleplayer_dir = (
        Path(settings["singleplayer_dir"])
        if settings.get("singleplayer_dir")
        else ws.default_singleplayer_dir(profile_id)
    )

    build_singleplayer = settings.get("singleplayer", False)
    if not isinstance(build_singleplayer, bool):
        raise ConfigError(
            f"{path.name}: [profile].singleplayer must be true/false, "
            f"got {build_singleplayer!r}"
        )

    raw_mods = data.get("mods", [])
    if not isinstance(raw_mods, list):
        raise ConfigError(f"{path.name}: [[mods]] must be a list of tables")

    mods: list[Mod] = []
    seen: dict[str, str] = {}
    for i, m in enumerate(raw_mods):
        mod = _parse_mod(m, path, i)
        if mod.key in seen:
            raise ConfigError(
                f"{path.name}: duplicate mod '{mod.key}' "
                f"('{seen[mod.key]}' and '{mod.name}'). Each source:id must be unique."
            )
        seen[mod.key] = mod.name
        mods.append(mod)

    return Profile(
        id=profile_id,
        game_version=str(game_version),
        loader=str(loader).lower(),
        client_dir=client_dir,
        server_dir=server_dir,
        datapacks_dir=datapacks_dir,
        singleplayer_dir=singleplayer_dir,
        mods=tuple(mods),
        path=path,
        build_singleplayer=build_singleplayer,
    )


def _parse_mod(m: dict, path: Path, index: int) -> Mod:
    where = f"{path.name}: mods[{index}]"
    name = m.get("name")
    if not name:
        raise ConfigError(f"{where}: missing 'name'")
    where = f"{path.name}: mod '{name}'"

    source, project_id = _resolve_source(m, name, where)

    side = m.get("side", "both")
    if side not in VALID_SIDES:
        raise ConfigError(
            f"{where}: invalid side '{side}' (use {', '.join(sorted(VALID_SIDES))})"
        )

    channel = str(m.get("channel", "alpha")).lower()
    if channel not in VALID_CHANNELS:
        raise ConfigError(
            f"{where}: invalid channel '{channel}' "
            f"(use {', '.join(sorted(VALID_CHANNELS))})"
        )

    mtype = str(m.get("type", "mod")).lower()
    if mtype not in VALID_TYPES:
        raise ConfigError(
            f"{where}: invalid type '{mtype}' (use {', '.join(sorted(VALID_TYPES))})"
        )

    pin = m.get("pin")
    if pin is not None and not isinstance(pin, (str, bool)):
        raise ConfigError(
            f"{where}: 'pin' must be a version string or true/false, got {pin!r}"
        )

    enabled = m.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: 'enabled' must be true/false, got {enabled!r}")

    file = m.get("file")
    if file is not None and not isinstance(file, str):
        raise ConfigError(f"{where}: 'file' must be a string filename, got {file!r}")

    url = m.get("url")
    if url is not None and not isinstance(url, str):
        raise ConfigError(f"{where}: 'url' must be a string, got {url!r}")

    return Mod(
        name=str(name),
        source=source,
        project_id=project_id,
        side=side,
        pin=pin,
        channel=channel,
        enabled=enabled,
        file=file,
        url=url,
        type=mtype,
    )


def _resolve_source(m: dict, name: str, where: str) -> tuple[str, str]:
    """Determine (source, project_id) from an explicit ``source`` + ``id``."""
    source = m.get("source")
    if not source:
        raise ConfigError(f"{where}: missing 'source' (modrinth | curseforge | manual | url)")
    source = str(source).lower()
    if source not in VALID_SOURCES:
        raise ConfigError(
            f"{where}: invalid source '{source}' "
            f"(use {', '.join(sorted(VALID_SOURCES))})"
        )
    project_id = m.get("id")
    if source == "manual":
        return "manual", str(project_id or _slugify(name))
    if not project_id:
        raise ConfigError(f"{where}: source '{source}' requires an 'id'")
    return source, str(project_id)

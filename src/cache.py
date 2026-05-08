import json
from pathlib import Path


def _cache_file(profile: str) -> Path:
    return Path(f"mod_cache_{profile}.json")


def load(profile: str) -> dict:
    f = _cache_file(profile)
    if not f.exists():
        return {}
    return json.loads(f.read_text(encoding="utf-8"))


def save(cache: dict, profile: str) -> None:
    _cache_file(profile).write_text(json.dumps(cache, indent=2), encoding="utf-8")


def update_entry(cache: dict, slug: str, version_id: str, filename: str) -> None:
    cache[slug] = {"version_id": version_id, "filename": filename}

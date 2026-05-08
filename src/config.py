import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

VALID_SIDES = {"client", "server", "both"}


@dataclass
class Mod:
    name: str
    slug: str  # empty string = skip Modrinth, go straight to curseforge_slug
    side: str
    curseforge_slug: str | None = field(default=None)


@dataclass
class Config:
    game_version: str
    loader: str
    client_mods_dir: Path
    server_mods_dir: Path
    mods: list[Mod]
    curseforge_api_key: str | None = field(default=None)

    def mods_for_profile(self, profile: str) -> list[Mod]:
        return [m for m in self.mods if m.side == profile or m.side == "both"]

    def mods_dir(self, profile: str) -> Path:
        return self.client_mods_dir if profile == "client" else self.server_mods_dir


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path) if Path(path).is_absolute() else _PROJECT_ROOT / path
    if not p.exists():
        example = _PROJECT_ROOT / ".env.example"
        if example.exists():
            import shutil

            shutil.copy(example, p)
            print(
                "Created .env from .env.example — add your CurseForge API key to enable CurseForge fallback."
            )
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def _bootstrap_config(path: Path) -> None:
    example = _PROJECT_ROOT / "mods.example.toml"
    if example.exists():
        import shutil

        shutil.copy(example, path)
        print(f"Created {path.name} from mods.example.toml — edit it and run again.")
    else:
        print(
            f"No {path.name} found. Create one (see mods.example.toml or the README)."
        )
    raise SystemExit(0)


def load(path: str | Path = "mods.toml") -> Config:
    _load_dotenv()

    p = Path(path) if Path(path).is_absolute() else _PROJECT_ROOT / path
    if not p.exists():
        _bootstrap_config(p)

    with open(p, "rb") as f:
        data = tomllib.load(f)

    settings = data.get("settings", {})
    mods = []
    for m in data.get("mods", []):
        side = m.get("side", "both")
        if side not in VALID_SIDES:
            raise ValueError(
                f"Mod '{m['name']}' has invalid side '{side}'. Must be one of: {', '.join(VALID_SIDES)}"
            )
        mods.append(
            Mod(
                name=m["name"],
                slug=m.get("slug", ""),
                side=side,
                curseforge_slug=m.get("curseforge_slug"),
            )
        )

    cf_key = os.environ.get("CURSEFORGE_API_KEY") or None

    return Config(
        game_version=settings["game_version"],
        loader=settings["loader"],
        client_mods_dir=Path(settings.get("client_mods_dir", "mods/client")),
        server_mods_dir=Path(settings.get("server_mods_dir", "mods/server")),
        mods=mods,
        curseforge_api_key=cf_key,
    )

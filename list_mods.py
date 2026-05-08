import sys

from src import cache, colors, config

sys.stdout.reconfigure(encoding="utf-8")


def _lookup(mod, cache_data: dict) -> str:
    entry = cache_data.get(mod.slug) or cache_data.get(mod.curseforge_slug or "")
    return entry.get("filename", "") if entry else ""


def _print_section(label: str, mods: list, cache_data: dict) -> None:
    if not mods:
        return
    print(f"\n  {colors.bold(label)}  ({len(mods)})")
    print(f"  {'─' * 48}")
    pad = max(len(m.name) for m in mods) + 2
    for mod in mods:
        filename = _lookup(mod, cache_data)
        suffix = (
            colors.gray(f"  {filename}") if filename else colors.gray("  (not cached)")
        )
        print(f"  {mod.name:<{pad}}{suffix}")


def run() -> None:
    cfg = config.load("mods.toml")
    client_cache = cache.load("client")
    server_cache = cache.load("server")

    both = sorted(
        [m for m in cfg.mods if m.side == "both"], key=lambda m: m.name.casefold()
    )
    client = sorted(
        [m for m in cfg.mods if m.side == "client"], key=lambda m: m.name.casefold()
    )
    server = sorted(
        [m for m in cfg.mods if m.side == "server"], key=lambda m: m.name.casefold()
    )

    print(colors.bold(f"\n{'─' * 52}"))
    print(colors.bold(f"  Mod List  ·  {cfg.game_version}  ·  {cfg.loader}"))
    print(colors.bold(f"{'─' * 52}"))

    _print_section("BOTH", both, client_cache)
    _print_section("CLIENT", client, client_cache)
    _print_section("SERVER", server, server_cache)

    total = len(cfg.mods)
    print(
        f"\n  {colors.bold('Total:')} {total} mods"
        f"  ({len(both)} both · {len(client)} client · {len(server)} server)\n"
    )


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)

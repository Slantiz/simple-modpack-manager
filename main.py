import argparse
import sys
from datetime import datetime
from pathlib import Path

from src import cache, checker, colors, config, downloader

sys.stdout.reconfigure(encoding="utf-8")


def find_removed(mods: list, mod_cache: dict) -> dict:
    active_slugs = {m.slug or m.curseforge_slug for m in mods}
    return {
        slug: entry for slug, entry in mod_cache.items() if slug not in active_slugs
    }


def print_section(title: str) -> None:
    print(colors.bold(f"\n{'─' * 50}"))
    print(colors.bold(f"  {title}"))
    print(colors.bold(f"{'─' * 50}"))


def run(profile: str, verbose: bool = False, force: bool = False) -> None:
    cfg = config.load("mods.toml")
    mods = cfg.mods_for_profile(profile)
    mods_dir = cfg.mods_dir(profile)

    print("Modpack Manager")
    print(f"Game: {cfg.game_version}  |  Loader: {cfg.loader}  |  Profile: {profile}")
    print(f"Mods for this profile: {len(mods)}")

    cf_key = cfg.curseforge_api_key
    sources = "Modrinth"
    if cf_key:
        ok, err = checker.validate_cf_key(cf_key)
        if ok:
            sources += " + CurseForge"
        else:
            print(colors.red(f"  [!] CurseForge key invalid — {err}"))
            print(
                colors.red(
                    "      CurseForge disabled. Check CURSEFORGE_API_KEY in .env"
                )
            )
            cf_key = None
    print_section(f"Checking {len(mods)} mods against {sources}")

    mod_cache = cache.load(profile)
    to_download, up_to_date, not_found, manual_needed = checker.check_all(
        mods, cfg.game_version, cfg.loader, mod_cache, cf_key, verbose, force, mods_dir
    )
    cache.save(mod_cache, profile)

    to_add = [e for e in to_download if e.get("is_new")]
    to_update = [e for e in to_download if not e.get("is_new")]
    manually_placed = [e for e in up_to_date if e.get("is_manual")]

    deleted = []
    removed = find_removed(mods, mod_cache)
    if removed:
        print_section("Removed mods (no longer in config)")
        for slug, entry in removed.items():
            print(colors.red(f"  [-] {entry['filename']}"))
        print()
        if input("Delete these files? [y/N] ").strip().lower() == "y":
            for slug, entry in removed.items():
                downloader.remove_old(mods_dir, entry["filename"])
                del mod_cache[slug]
                deleted.append(entry["filename"])
            cache.save(mod_cache, profile)
            print(colors.green("  Removed."))

    manual_up_to_date = len(manually_placed)
    manual_add = [e for e in manual_needed if e["is_new"]]
    manual_update = [e for e in manual_needed if not e["is_new"]]
    total_up_to_date = len(up_to_date)
    total_add = len(to_add) + len(manual_add)
    total_update = len(to_update) + len(manual_update)

    def _manual_suffix(n):
        return f" ({colors.blue(f'{n} manual')})" if n else ""

    print_section("Results")
    print(f"  Up to date : {total_up_to_date}{_manual_suffix(manual_up_to_date)}")
    print(
        colors.green(f"  To add     : {total_add}") + _manual_suffix(len(manual_add))
        if total_add
        else "  To add     : 0"
    )
    print(
        colors.yellow(f"  To update  : {total_update}")
        + _manual_suffix(len(manual_update))
        if total_update
        else "  To update  : 0"
    )
    print(
        colors.red(f"  Not found  : {len(not_found)}")
        if not_found
        else "  Not found  : 0"
    )

    if manual_needed:
        print_section("Manual downloads required")
        for entry in manual_needed:
            mod, version = entry["mod"], entry["version"]
            tag = "[+]" if entry["is_new"] else "[↑]"
            print(colors.blue(f"  {tag} {mod.name} — {version.version_number}"))
            print(colors.blue(f"      Download from : {entry['url']}"))
            print(colors.blue(f"      File          : {version.filename}"))
            print(colors.blue(f"      Place in      : {mods_dir}"))

    if not_found:
        print_section("Not found / errors")
        for entry in not_found:
            print(colors.red(f"  [!] {entry['mod'].name} — {entry['reason']}"))

    if not to_download:
        if not not_found and not manual_needed:
            print(colors.green("\nAll mods are up to date."))
        write_summary(cfg, profile, [], [], not_found, [], deleted, manual_needed)
        return

    print()
    confirm = input("Download these mods? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    print_section("Downloading")
    added = []
    updated = []
    failed = []

    for entry in to_download:
        mod = entry["mod"]
        version = entry["version"]
        is_new = entry.get("is_new", False)
        label = "[+]" if is_new else "[↑]"
        print(f"  {label} {mod.name} ({version.filename})...", end=" ", flush=True)

        cache_key = mod.slug or mod.curseforge_slug
        old_filename = mod_cache.get(cache_key, {}).get("filename")

        try:
            downloader.download(version.download_url, mods_dir, version.filename)

            if old_filename and old_filename != version.filename:
                downloader.remove_old(mods_dir, old_filename)

            cache.update_entry(
                mod_cache, cache_key, version.version_id, version.filename
            )
            cache.save(mod_cache, profile)
            print(colors.green("done"))
            if is_new:
                added.append({"mod": mod, "version": version})
            else:
                updated.append({"mod": mod, "version": version})
        except Exception as e:
            print(colors.red(f"FAILED ({e})"))
            failed.append({"mod": mod, "error": str(e)})

    write_summary(
        cfg, profile, added, updated, not_found, failed, deleted, manual_needed
    )


def write_summary(
    cfg,
    profile: str,
    added,
    updated,
    not_found,
    failed,
    deleted=None,
    manual_needed=None,
) -> None:
    deleted = deleted or []
    manual_needed = manual_needed or []
    if (
        not added
        and not updated
        and not not_found
        and not failed
        and not deleted
        and not manual_needed
    ):
        return

    summary_dir = Path("summaries")
    summary_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    summary_path = summary_dir / f"update_{profile}_{timestamp}.txt"

    lines = [
        f"Modpack Update Summary — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Game: {cfg.game_version}  |  Loader: {cfg.loader}  |  Profile: {profile}",
        "",
    ]

    if added:
        lines.append(f"ADDED ({len(added)})")
        for e in added:
            lines.append(
                f"  + {e['mod'].name} → {e['version'].version_number} [{e['version'].source}]"
            )
        lines.append("")

    if updated:
        lines.append(f"UPDATED ({len(updated)})")
        for e in updated:
            lines.append(
                f"  ↑ {e['mod'].name} → {e['version'].version_number} [{e['version'].source}]"
            )
        lines.append("")

    if deleted:
        lines.append(f"DELETED ({len(deleted)})")
        for filename in deleted:
            lines.append(f"  - {filename}")
        lines.append("")

    if manual_needed:
        lines.append(f"MANUAL DOWNLOAD REQUIRED ({len(manual_needed)})")
        for e in manual_needed:
            tag = "+" if e["is_new"] else "↑"
            lines.append(f"  {tag} {e['mod'].name} — {e['version'].version_number}")
            lines.append(f"    {e['url']}")
        lines.append("")

    if not_found:
        lines.append(f"NOT FOUND / ERRORS ({len(not_found)})")
        for e in not_found:
            lines.append(f"  ! {e['mod'].name} — {e['reason']}")
        lines.append("")

    if failed:
        lines.append(f"DOWNLOAD FAILED ({len(failed)})")
        for e in failed:
            lines.append(f"  x {e['mod'].name} — {e['error']}")
        lines.append("")

    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(colors.gray(f"\nSummary written to: {summary_path}"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Modpack mod updater")
    parser.add_argument(
        "--profile",
        choices=["client", "server"],
        required=True,
        help="Which profile to update (client or server)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full HTTP response details on errors",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Re-download all mods regardless of cache",
    )
    args = parser.parse_args()

    try:
        run(args.profile, args.verbose, args.force)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)

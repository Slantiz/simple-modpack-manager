"""Command-line interface.

    modman list [profile|all]
    modman check [profile|all]
    modman install [profile|all] [--dry-run] [-y]
    modman update [profile|all] [mod ...] [-y]
    modman add <mod> <jar> [--profile <id>]
    modman verify [profile|all]
    modman save <profile> [--label "..."]
    modman history <profile>
    modman rollback <profile> <snapshot>
    modman reset [-y]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import checks, config, engine, history
from . import lock as lock_io
from . import report
from .builder import materialize
from .engine import HELD, KEEP, ApplyResult, EngineError, apply, match_targets, plan
from .env import curseforge_api_key
from .model import ConfigError
from .providers import build_registry
from .providers.base import new_session
from .store import Store
from .workspace import Workspace


def _setup(ws: Workspace):
    registry = build_registry(curseforge_api_key(ws))
    return Store(ws), registry, new_session()


def _targets(ws: Workspace, raw: list[str]):
    return config.resolve_targets(raw, ws)


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_list(args, ws: Workspace) -> int:
    for profile in _targets(ws, args.target):
        lk = lock_io.load(profile.id, ws)
        print(report.format_list(profile, lk))
    return 0


def _by_name(items):
    return sorted(items, key=lambda x: x.name.lower())


def _stream_check(profile, lk, registry, session, *, mode="update", update_keys=None):
    """Resolve every mod concurrently, but stream status lines in a fixed
    alphabetical order (flushing as each prefix completes). Returns the Plan."""
    print(report.check_intro(profile))
    pad = report.check_pad(profile)
    order = [m.key for m in _by_name(profile.mods)]
    stream = report.OrderedStream(order, label="checking")
    pl = plan(profile, lk, registry, mode=mode, update_keys=update_keys,
              session=session,
              on_item=lambda item: stream.submit(item.key, report.check_line(item, pad)))
    stream.finish()
    for section in (report.check_removed(pl), report.check_manual(pl),
                    report.check_summary(pl)):
        if section:
            print(section)
    return pl


def _will_fetch(item, store) -> bool:
    """True if applying this item will actually pull bytes into the store — a
    resolved (ADD/UPDATE/REPIN) download, or a KEEP/HELD restore whose locked jar
    is missing (e.g. a fresh install after the store was reset)."""
    if item.resolved is not None:
        return True
    e = item.existing
    return bool(item.kind in (KEEP, HELD) and e and e.sha512 and not store.has(e.sha512))


def _apply_streamed(pl, lk, registry, store, ws, session, profile):
    """Apply a plan, streaming download lines in a fixed alphabetical order."""
    downloads = _by_name(i for i in pl.items if _will_fetch(i, store))
    if downloads:
        print(report.header("Downloading"))
    stream = report.OrderedStream([i.key for i in downloads], label="downloading")
    result = apply(pl, lk, registry, store, ws, session=session,
                   on_download=lambda i: stream.submit(i.key, report.download_line(i)))
    stream.finish()
    print(report.format_apply(result, profile))
    return result


def _build_gap(profile, lk, store) -> tuple[int, int]:
    """(missing, total) distinct locked jars not present in the built folders."""
    total = len({e.filename for e in lk.entries.values() if e.enabled and e.sha512})
    missing: set[str] = set()
    for side in materialize(profile, lk, store, old_lock=lk, dry_run=True):
        missing.update(side.copied)
    return len(missing), total


def cmd_check(args, ws: Workspace) -> int:
    store, registry, session = _setup(ws)
    for profile in _targets(ws, args.target):
        lk = lock_io.load(profile.id, ws)
        pl = _stream_check(profile, lk, registry, session)
        not_installed, total = _build_gap(profile, lk, store)
        print("\n" + report.check_verdict(pl, not_installed, total))
    return 0


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _install_has_work(pl, profile, lk, store) -> bool:
    """install does more than TOML changes: it also restores jars deleted from the
    built folders (a KEEP item, so has_changes misses it). Detect that via a
    dry-run materialize against the current lock."""
    if pl.has_changes:
        return True
    sides = materialize(profile, lk, store, old_lock=lk, dry_run=True)
    return any(s.copied or s.removed for s in sides)


def cmd_install(args, ws: Workspace) -> int:
    store, registry, session = _setup(ws)
    rc = 0
    with ws.exclusive():
        for profile in _targets(ws, args.target):
            lk = lock_io.load(profile.id, ws)
            pl = _stream_check(profile, lk, registry, session, mode="install")
            if not args.dry_run:
                if pl.has_hard_failures:
                    rc = 1
                    print(report.red("\n  Not applied (resolve failures above)."))
                elif not _install_has_work(pl, profile, lk, store):
                    pass
                elif not args.yes and not _confirm("\nApply these changes? [y/N] "):
                    print(report.dim("  Aborted."))
                else:
                    try:
                        _apply_streamed(pl, lk, registry, store, ws, session, profile)
                    except EngineError as e:
                        rc = 1
                        print(report.red(f"  {e}"))
            print("\n" + report.action_verdict(pl))
    return rc


def cmd_update(args, ws: Workspace) -> int:
    store, registry, session = _setup(ws)
    profiles = _targets(ws, [args.profile] if args.profile else [])
    rc = 0
    with ws.exclusive():
        for profile in profiles:
            try:
                keys = match_targets(profile, args.mods)
            except EngineError as e:
                print(report.red(f"  {e}"))
                rc = 1
                continue
            lk = lock_io.load(profile.id, ws)
            pl = _stream_check(profile, lk, registry, session, update_keys=keys)
            if pl.has_hard_failures:
                rc = 1
                print(report.red("\n  Not applied (resolve failures above)."))
            elif not pl.has_changes:
                pass
            elif not args.yes and not _confirm("\nApply these changes? [y/N] "):
                print(report.dim("  Aborted."))
            else:
                try:
                    _apply_streamed(pl, lk, registry, store, ws, session, profile)
                except EngineError as e:
                    rc = 1
                    print(report.red(f"  {e}"))
            print("\n" + report.action_verdict(pl))
    return rc


def _mod_matches(mod, token: str) -> bool:
    t = token.strip().lower()
    return t in (mod.project_id.lower(), mod.name.lower(), mod.key.lower())


def cmd_add(args, ws: Workspace) -> int:
    store, _, _ = _setup(ws)
    jar_path = Path(args.jar)
    if not jar_path.is_file():
        print(report.red(f"  no such file: {jar_path}"))
        return 1
    profiles = _targets(ws, [args.profile] if args.profile else [])
    all_matches = [(p, m) for p in profiles for m in p.mods if _mod_matches(m, args.mod)]
    manual = [(p, m) for (p, m) in all_matches if m.source == "manual"]
    if not all_matches:
        where = "that profile" if args.profile else "any profile"
        print(report.red(f"  no mod matching '{args.mod}' found in {where}."))
        return 1
    if not manual:
        print(report.red(f"  '{args.mod}' is not a manual mod — it's downloaded "
                         "automatically. Only manual mods need `add`."))
        return 1

    print(report.header("Add manual mod"))
    rc = 0
    for profile, mod in manual:
        try:
            saved, sides, swept = engine.capture_manual(profile, mod, jar_path, store, ws)
        except EngineError as e:
            rc = 1
            print(report.red(f"  {e}"))
            continue
        print(report.green(f"  Added {mod.name} ") + report.dim(f"({jar_path.name}) ")
              + report.dim(f"[{profile.id}]"))
        print(report.format_apply(ApplyResult(lock=saved, sides=sides, swept=swept),
                                  profile))
    return rc


def cmd_verify(args, ws: Workspace) -> int:
    store, _, _ = _setup(ws)
    rc = 0
    for profile in _targets(ws, args.target):
        lk = lock_io.load(profile.id, ws)
        issues = checks.run_all(profile, lk, store)
        print(report.format_issues(profile, issues))
        if any(i.level == "error" for i in issues):
            rc = 1
    return rc


def cmd_save(args, ws: Workspace) -> int:
    profile = config.load_profile(args.profile, ws)
    lk = lock_io.load(profile.id, ws)
    name = history.save(profile.id, lk, args.label or "", ws)
    print(report.green(f"Saved snapshot '{name}' ({len(lk.entries)} mods)."))
    return 0


def cmd_history(args, ws: Workspace) -> int:
    snaps = history.list_snapshots(args.profile, ws)
    if not snaps:
        print(report.dim(f"No snapshots for '{args.profile}'."))
        return 0
    print(report.header(f"snapshots · {args.profile}"))
    for s in snaps:
        label = f"  {report.bold(s.label)}" if s.label else ""
        print(f"  {report.cyan(s.name)}{label}  {report.dim(f'{s.count} mods · {s.created}')}")
    return 0


def cmd_rollback(args, ws: Workspace) -> int:
    store, registry, session = _setup(ws)
    profile = config.load_profile(args.profile, ws)
    lk = lock_io.load(profile.id, ws)
    try:
        target = history.load_snapshot(profile.id, args.snapshot, ws)
    except EngineError as e:
        print(report.red(f"  {e}"))
        return 1

    # check phase: show each snapshot mod's status, then what gets rolled back out
    print(report.rollback_intro(profile, args.snapshot, len(target.entries)))
    pad = max((len(e.name) for e in target.entries.values()), default=10)
    for e in sorted(target.entries.values(), key=lambda e: e.name.lower()):
        print(report.rollback_line(e, lk, store, pad))
    removed = report.rollback_removed(lk, target)
    if removed:
        print(removed)

    # download + apply phase (streamed, alphabetical)
    to_dl = _by_name(e for e in target.entries.values()
                     if e.sha512 and not store.has(e.sha512))
    stream = report.OrderedStream([e.key for e in to_dl], label="downloading")
    if to_dl:
        print(report.header("Downloading"))

    def on_dl(entry, err):
        line = (report.rollback_skip_line(entry) if err
                else report.rollback_download_line(entry))
        stream.submit(entry.key, line)

    with ws.exclusive():
        try:
            result = history.rollback(profile, lk, args.snapshot, registry, store, ws,
                                      session=session, on_download=on_dl)
        except EngineError as e:
            stream.finish()
            print(report.red(f"  {e}"))
            return 1
    stream.finish()
    print(report.green(f"\nRolled back '{profile.id}' to '{args.snapshot}'."))
    print(report.format_apply(result, profile))
    for w in result.warnings:
        print(report.yellow(f"  ! {w}"))
    return 0


def cmd_reset(args, ws: Workspace) -> int:
    store_jars = list(ws.store_dir.glob("*.jar")) if ws.store_dir.is_dir() else []
    mods_exists = ws.mods_dir.is_dir()
    if not store_jars and not mods_exists:
        print(report.dim("Nothing to reset."))
        return 0
    print(report.header("Reset"))
    print(f"  store : {report.yellow(str(len(store_jars)) + ' jar(s)')}  "
          + report.dim(str(ws.store_dir)))
    print(f"  mods  : {report.yellow('all built folders') if mods_exists else 'none'}  "
          + report.dim(str(ws.mods_dir)))
    print(report.dim("  (lockfiles, snapshots and your TOMLs are kept)"))
    if not args.yes and not _confirm("\nWipe the store and every built mod folder? [y/N] "):
        print(report.dim("  Aborted."))
        return 0
    with ws.exclusive():
        shutil.rmtree(ws.store_dir, ignore_errors=True)
        shutil.rmtree(ws.mods_dir, ignore_errors=True)
    print(report.green("  Done. Run `install all` to rebuild from your lockfiles."))
    return 0


# ── parser ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="modman", description="Minecraft modpack manager")
    sub = p.add_subparsers(dest="command", required=True)

    def target_arg(sp):
        sp.add_argument("target", nargs="*", help="profile id(s), or omit for all")

    sp = sub.add_parser("list", help="list mods per profile")
    target_arg(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("check", help="check each mod for updates (no changes)")
    target_arg(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("install", help="apply TOML without bumping locked versions")
    target_arg(sp)
    sp.add_argument("--dry-run", action="store_true", help="preview only, change nothing")
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirm prompt")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("update", help="bump mods to newest (respects pins)")
    sp.add_argument("profile", nargs="?", help="profile id, or omit/all for every profile")
    sp.add_argument("mods", nargs="*", help="specific mods to update (default: all)")
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirm prompt")
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("add", help="register a manually-downloaded jar into the store")
    sp.add_argument("mod", help="manual mod name or id (as in the profile TOML)")
    sp.add_argument("jar", help="path to the .jar (e.g. sitting in the current folder)")
    sp.add_argument("--profile", help="limit to one profile if the mod is in several")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("verify", help="integrity + duplicate/dependency/side checks")
    target_arg(sp)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("save", help="record current state as a known-good snapshot")
    sp.add_argument("profile")
    sp.add_argument("--label", "-l", default="", help="short label for the snapshot")
    sp.set_defaults(func=cmd_save)

    sp = sub.add_parser("history", help="list saved snapshots")
    sp.add_argument("profile")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("rollback", help="restore a saved snapshot")
    sp.add_argument("profile")
    sp.add_argument("snapshot")
    sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("reset", help="wipe the store and all built mod folders")
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirm prompt")
    sp.set_defaults(func=cmd_reset)

    return p


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    ws = Workspace()
    try:
        rc = args.func(args, ws)
    except (ConfigError, EngineError) as e:
        print(report.red(f"error: {e}"), file=sys.stderr)
        rc = 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        rc = 130
    except RuntimeError as e:
        print(report.red(f"error: {e}"), file=sys.stderr)
        rc = 2
    print()  # trailing blank line so output doesn't butt against the next prompt
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

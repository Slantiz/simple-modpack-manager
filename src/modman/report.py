"""Terminal formatting for listings, checks, apply summaries, and verdicts."""

from __future__ import annotations

import sys

from .checks import Issue
from .engine import ADD, CAPTURED, FAIL, HELD, KEEP, MANUAL, REMOVE, REPIN, UPDATE, Plan
from .model import Lock, Profile

_USE_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


# Bright ANSI colors.
def bold(t): return _c("1", t)
def dim(t): return _c("90", t)   # gray
def red(t): return _c("91", t)
def green(t): return _c("92", t)
def yellow(t): return _c("93", t)
def blue(t): return _c("94", t)
def cyan(t): return _c("96", t)


_HEADER_WIDTH = 50


def header(title: str) -> str:
    line = "─" * _HEADER_WIDTH
    return bold(f"\n{line}\n  {title}\n{line}")


_KIND_STYLE = {
    ADD: ("[+]", green, "add"),
    UPDATE: ("[↑]", yellow, "update"),
    REPIN: ("[↑]", cyan, "re-pin"),
    HELD: ("[=]", blue, "held"),
    KEEP: ("[=]", dim, "keep"),
    MANUAL: ("[M]", blue, "manual"),
    CAPTURED: ("[+]", green, "captured"),
    REMOVE: ("[-]", red, "remove"),
    FAIL: ("[!]", red, "FAILED"),
}


def _source_label(source: str | None) -> str:
    return {
        "modrinth": "Modrinth", "curseforge": "CurseForge",
        "url": "URL", "manual": "Manual",
    }.get(source or "", source or "?")


# ── check command output (streamed) ──────────────────────────────────────────

# Per-kind status text shown after "..." in the check line.
_CHECK_STATUS = {
    ADD: "new",
    UPDATE: "update available",
    REPIN: "version pinned",
    HELD: "up to date (pinned)",
    KEEP: "up to date",
    MANUAL: "manual download needed",
    CAPTURED: "found locally",
}


class OrderedStream:
    """Print streamed results in a fixed order, flushing each contiguous prefix as
    it becomes ready — so output order is deterministic even though work finishes
    out of order. On a TTY it also shows a single live progress footer.

    Consumed from a single thread (the as-completed loop), so no locking needed.
    """

    def __init__(self, order_keys, *, label="working"):
        self.order = list(order_keys)
        self.index = {k: i for i, k in enumerate(self.order)}
        self.total = len(self.order)
        self.pos = 0
        self.ready: dict[int, str] = {}
        self.done = 0
        self.label = label
        self.footer = _USE_COLOR  # animate only on a real terminal

    def submit(self, key, text: str) -> None:
        self._clear()
        i = self.index.get(key)
        if i is None:  # untracked — just print it in place
            print(text)
        else:
            self.ready[i] = text
            self.done += 1
            while self.pos in self.ready:  # flush the completed prefix, in order
                print(self.ready.pop(self.pos))
                self.pos += 1
        self._draw()

    def finish(self) -> None:
        self._clear()
        for i in range(self.pos, self.total):
            if i in self.ready:
                print(self.ready[i])
        sys.stdout.flush()

    def _draw(self) -> None:
        if self.footer and self.done < self.total:
            sys.stdout.write(dim(f"  {self.label} {self.done}/{self.total} …"))
            sys.stdout.flush()

    def _clear(self) -> None:
        if self.footer:
            sys.stdout.write("\r\033[K")


def check_intro(profile) -> str:
    return header(
        f"{profile.id}  ·  {profile.game_version} / {profile.loader}  ·  "
        f"{len(profile.mods)} mods"
    )


def check_pad(profile) -> int:
    return max((len(m.name) for m in profile.mods), default=10)


def _artifact_line(glyph_fn, glyph: str, name: str, filename: str | None) -> str:
    # colored [icon] · white name · gray (filename)
    line = f"  {glyph_fn(glyph)} {name}"
    if filename:
        line += " " + dim(f"({filename})")
    return line


def download_line(item) -> str:
    # KEEP/HELD only reach the download phase when their jar is being (re)fetched
    # onto a fresh build, so show them as a green install rather than a dim "keep".
    if item.kind in (KEEP, HELD):
        glyph, color = "[+]", green
    else:
        glyph, color, _ = _KIND_STYLE.get(item.kind, ("[+]", green, ""))
    return _artifact_line(color, glyph, item.name, item.filename)


def rollback_download_line(entry) -> str:
    return _artifact_line(green, "[←]", entry.name, entry.filename)


def rollback_skip_line(entry) -> str:
    return _artifact_line(red, "[!]", entry.name, entry.filename)


def rollback_intro(profile, snapshot: str, n: int) -> str:
    return header(f"rollback {profile.id} → {snapshot}  ·  {n} mods")


def rollback_line(entry, current_lock, store, pad: int) -> str:
    cur = current_lock.entries.get(entry.key)
    name = f"{entry.name:<{pad}}"
    if cur and cur.version_id == entry.version_id:
        glyph, color, status = "[=]", dim, "already active"
    elif store.has(entry.sha512):
        glyph, color, status = "[←]", cyan, "restore from store"
    else:
        glyph, color, status = "[←]", yellow, "restore"
    line = f"  {color(glyph)} {name} ... {color(status)}"
    if entry.filename:
        line += " " + dim(f"({entry.filename})")
    if entry.source:
        line += " " + dim(f"[{_source_label(entry.source)}]")
    return line


def rollback_removed(current_lock, target) -> str:
    removed = [e for k, e in current_lock.entries.items() if k not in target.entries]
    if not removed:
        return ""
    lines = [header("Removed mods (rolled back out)")]
    for e in sorted(removed, key=lambda e: e.name.lower()):
        line = f"  {red('[-]')} {e.name}"
        if e.filename:
            line += " " + dim(f"({e.filename})")
        lines.append(line)
    return "\n".join(lines)


def check_line(item, pad: int) -> str:
    # colored [icon] + status phrase; white mod name; gray (filename) and [source]
    glyph, color, _ = _KIND_STYLE.get(item.kind, ("[?]", dim, item.kind))
    name = f"{item.name:<{pad}}"
    if item.kind == FAIL:
        return f"  {color(glyph)} {name} ... {color(item.error or 'error')}"
    status = _CHECK_STATUS.get(item.kind, item.kind)
    if item.kind == HELD and item.available:
        status = f"held at {item.from_version} ({item.available} available)"
    elif item.kind == MANUAL:
        status = "manual — not present" if item.note == "not present" else "manual"
    line = f"  {color(glyph)} {name} ... {color(status)}"
    if item.filename:
        line += " " + dim(f"({item.filename})")
    if item.source:
        line += " " + dim(f"[{_source_label(item.source)}]")
    return line


def check_removed(plan: Plan) -> str:
    """Section listing mods in the lock but no longer in the TOML."""
    removed = plan.by_kind(REMOVE)
    if not removed:
        return ""
    lines = [header("Removed mods (no longer in config)")]
    for i in sorted(removed, key=lambda x: x.name.lower()):
        line = f"  {red('[-]')} {i.name}"
        if i.existing and i.existing.filename:
            line += " " + dim(f"({i.existing.filename})")
        lines.append(line)
    return "\n".join(lines)


def check_manual(plan: Plan) -> str:
    """Section re-listing manual mods with their download URLs."""
    manual = plan.by_kind(MANUAL)
    if not manual:
        return ""
    lines = [header("Manual downloads")]
    for i in sorted(manual, key=lambda x: x.name.lower()):
        head = f"  {blue('[M]')} {i.name}"
        if i.filename:
            head += " " + dim(f"({i.filename})")
        lines.append(head)
        if i.note == "not present":
            lines.append(red("        (not present)"))
        if i.url:
            lines.append(f"        {dim('url  :')} {blue(i.url)}")
    return "\n".join(lines)


def check_summary(plan: Plan) -> str:
    c = {}
    for i in plan.items:
        c[i.kind] = c.get(i.kind, 0) + 1
    up = c.get(KEEP, 0) + c.get(HELD, 0)
    add = c.get(ADD, 0) + c.get(CAPTURED, 0)
    upd = c.get(UPDATE, 0) + c.get(REPIN, 0)
    rem = c.get(REMOVE, 0)
    man = c.get(MANUAL, 0)
    missing = sum(1 for i in plan.by_kind(MANUAL) if i.note == "not present")
    nf = c.get(FAIL, 0)
    lines = [header("Results")]
    lines.append(dim(f"  Up to date : {up}"))
    lines.append((green if add else dim)(f"  To add     : {add}"))
    lines.append((yellow if upd else dim)(f"  To update  : {upd}"))
    lines.append((red if rem else dim)(f"  To remove  : {rem}"))
    man_line = f"  Manual     : {man}"
    if missing:
        man_line += " " + red(f"({missing} not present)")
    lines.append((blue if man else dim)(man_line))
    lines.append((red if nf else dim)(f"  Not found  : {nf}"))
    return "\n".join(lines)


def _plan_counts(plan: Plan):
    add = len(plan.by_kind(ADD, CAPTURED))
    upd = len(plan.by_kind(UPDATE, REPIN))
    rem = len(plan.by_kind(REMOVE))
    missing = sum(1 for i in plan.by_kind(MANUAL) if i.note == "not present")
    nf = len(plan.by_kind(FAIL))
    return add, upd, rem, missing, nf


def _verdict_line(mark_color, text: str) -> str:
    """A white sentence with a colored ``!`` in front to grab attention."""
    return f"  {mark_color('!')} {text}"


def check_verdict(plan: Plan, not_installed: int = 0, locked_total: int = 0) -> str:
    """One glance-able sentence about whether action is needed (pre-apply).

    ``not_installed`` is the count of locked jars missing from the built folders
    (e.g. after a reset the lock looks current but nothing is on disk)."""
    add, upd, rem, missing, nf = _plan_counts(plan)
    parts = []
    if not_installed:
        if locked_total and not_installed >= locked_total:
            parts.append(_verdict_line(yellow,
                         "Nothing is installed — run `install` or `update`."))
        else:
            parts.append(_verdict_line(yellow, f"{not_installed} mod"
                         f"{'s' if not_installed != 1 else ''} not installed — "
                         "run `install` or `update`."))
    install_notes = []
    if add:
        install_notes.append(f"{add} to install")
    if rem:
        install_notes.append(f"{rem} to remove")
    if install_notes:
        parts.append(_verdict_line(yellow, ", ".join(install_notes) + " — run `install`."))
    if upd:
        parts.append(_verdict_line(yellow, f"{upd} update{'s' if upd != 1 else ''} "
                     "available — run `update`."))
    if missing:
        parts.append(_verdict_line(red, f"{missing} manual mod"
                     f"{'s' if missing != 1 else ''} missing — place the jar(s) by hand."))
    if nf:
        parts.append(_verdict_line(red, f"{nf} mod{'s' if nf != 1 else ''} not found — "
                     "check the source/id in the TOML, or it may not be available for "
                     "this game version/loader."))
    if not parts:
        return _verdict_line(green,
                             "Everything looks up to date and installed — nothing to do.")
    return "\n".join(parts)


def action_verdict(plan: Plan) -> str:
    """Verdict for install/update: only flags what the user must still handle —
    resolve failures and missing manual jars. Version currency is irrelevant here
    (install ignores it; update already bumped)."""
    _, _, _, missing, nf = _plan_counts(plan)
    parts = []
    if missing:
        parts.append(_verdict_line(red, f"{missing} manual mod"
                     f"{'s' if missing != 1 else ''} missing — place the jar(s) by hand."))
    if nf:
        parts.append(_verdict_line(red, f"{nf} mod{'s' if nf != 1 else ''} not found — "
                     "check the source/id in the TOML, or it may not be available for "
                     "this game version/loader."))
    if not parts:
        return _verdict_line(green,
                             f"{plan.profile.id} is in sync — nothing needs your attention.")
    return "\n".join(parts)


def format_apply(result, profile: Profile) -> str:
    lines = [header("Summary")]
    pad = max((len(s.side) for s in result.sides), default=6)
    for side in result.sides:
        lines.append(
            f"  {side.side:<{pad}} : "
            + green(f"{len(side.copied)} written")
            + dim(" · ") + red(f"{len(side.removed)} removed")
            + dim(" · ") + dim(f"{len(side.unmanaged)} unmanaged")
        )
    if result.swept:
        lines.append(dim(f"  store: reclaimed {len(result.swept)} unreferenced jar(s)"))
    return "\n".join(lines)


def format_list(profile: Profile, lock: Lock) -> str:
    lines = [header(f"{profile.id}  ·  {profile.game_version} / {profile.loader}  ·  {len(profile.mods)} mods")]
    groups = {"both": [], "client": [], "server": []}
    for m in profile.mods:
        groups[m.side].append(m)
    for side in ("both", "client", "server"):
        mods = sorted(groups[side], key=lambda m: m.name.lower())
        if not mods:
            continue
        lines.append(f"\n  {bold(side.upper())}  ({len(mods)})")
        pad = max(len(m.name) for m in mods) + 2
        for m in mods:
            entry = lock.entries.get(m.key)
            ver = entry.filename if entry else "(not installed)"
            tags = []
            if not m.enabled:
                tags.append(red("disabled"))
            if m.is_pinned:
                tags.append(cyan(f"pinned {m.pinned_version() or 'current'}"))
            if m.is_datapack:
                tags.append(blue("datapack"))
            tags.append(dim(f"[{_source_label(m.source)}]"))
            suffix = "  " + " ".join(tags)
            name = m.name if m.enabled else dim(m.name)
            lines.append(f"  {name:<{pad}} {dim(str(ver))}{suffix}")
    return "\n".join(lines)


def format_issues(profile: Profile, issues: list[Issue]) -> str:
    lines = [header(f"verify {profile.id}")]
    if not issues:
        lines.append(green("  No issues found."))
        return "\n".join(lines)
    for i in issues:
        tag = red("error") if i.level == "error" else yellow("warn ")
        lines.append(f"  {tag}  {bold(i.mod)}: {i.message}")
    errs = sum(1 for i in issues if i.level == "error")
    warns = len(issues) - errs
    lines.append("")
    lines.append(f"  {red(str(errs) + ' error(s)')} · {yellow(str(warns) + ' warning(s)')}")
    return "\n".join(lines)

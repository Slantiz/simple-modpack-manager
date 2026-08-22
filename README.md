# Modpack Manager

A CLI tool that keeps multiple modded Minecraft profiles in sync from a single
hand-edited config each. It resolves mods from Modrinth and CurseForge, keeps a
deduplicated jar store, builds your client/server folders, and lets you snapshot a
known-good setup and roll back to it. Integrity-first: it never leaves a broken
state, and the TOML you edit is the only source of truth.

## Requirements

- Python 3.11+
- `requests`

## Setup

**1. Install dependencies**
```
pip install -r requirements.txt
```
Optionally `pip install -e .` to get a real `modman` command instead of
`py modman.py`.

**2. Configure a profile**

Each modpack is one file, `profiles/<id>.toml`, that you edit by hand. The filename
is the profile id. Copy the annotated template to start:

```
cp profiles/example.toml profiles/mypack.toml
```

It shows every source and per-mod option. A minimal mod entry looks like:

```toml
[profile]
id = "tectonic"
game_version = "1.21.1"
loader = "neoforge"
# singleplayer = true     # also build a singleplayer/ folder (see below)
# client_dir / server_dir / datapacks_dir / singleplayer_dir are optional and
# default to mods/<id>/{client,server,datapacks,singleplayer}

[[mods]]
name = "Create"
source = "modrinth"     # modrinth | curseforge | manual | url
id = "create"           # slug / project id / (for url) the download URL
side = "both"           # client | server | both
```

Per-mod fields:

| Field | Required | Description |
|---|---|---|
| `name` | yes | Display name (yours to choose) |
| `source` | yes | `modrinth`, `curseforge`, `manual`, or `url` |
| `id` | yes* | Slug / project id on that source (or the URL for `url`). Optional for `manual` |
| `side` | yes | `client`, `server`, or `both` |
| `pin` | no | `"0.6.0"` holds that exact version; `true` freezes the currently locked one |
| `channel` | no | Lowest release type to accept (`release`/`beta`/`alpha`); default: any, newest wins. Tightening it re-resolves on the next `install` |
| `enabled` | no | `false` keeps the mod in config but leaves it out of the built folders |
| `type` | no | `"datapack"` resolves a Modrinth datapack into the `datapacks/` folder |
| `url` | no | For `manual`: the page/download URL, shown by `check` so you can grab newer builds |

**3. (Optional) CurseForge API key**

For mods only on CurseForge:
```
cp .env.example .env         # then paste CURSEFORGE_API_KEY=your-key-here
```
Get a free key at [console.curseforge.com](https://console.curseforge.com). Without
one, CurseForge resolution is disabled.

## Usage

Run everything as `py modman.py <command> …` (or `modman <command> …` if installed).
A `<target>` is one or more profile ids, or omitted / `all` for every profile.

### Everyday loop

```
py modman.py list tectonic             # see the profile and its locked jars
py modman.py install tectonic          # make reality match the TOML (no version bumps)
py modman.py check tectonic            # what would updating change? (read-only)
py modman.py update tectonic           # bump to newest, respecting pins
py modman.py save tectonic -l "1.4 ok" # snapshot a known-good setup
py modman.py rollback tectonic <snap>  # go back if an update breaks things
```

### Commands

| Command | Options | What it does |
|---|---|---|
| `list [target]` | — | Mods per profile, each with its built jar filename and tags. Read-only. |
| `check [target]` | — | Resolve each mod against its source (streamed): up to date / update available / manual / not found, then removed + a results summary and a one-line verdict. Read-only. |
| `install [target]` | `--dry-run`, `-y` | Make the lock **satisfy the TOML without chasing newer versions**: add new mods, prune removed, restore anything missing, honor pins, and re-resolve anything that violates its `channel` or was built for a now-changed `game_version`/`loader` — then build folders. Checks and prompts first (`-y` skips); `--dry-run` previews only. |
| `update [profile] [mods…]` | `-y` | Check for updates, then prompt before bumping to newest (respects pins). Name mods to update only those. `-y` skips the prompt. |
| `add <mod> <jar>` | `--profile` | Register a **manual** jar: ingest it into the store, record it, and build it in. No hardcoded filename needed (see below). |
| `store` | — | List stored jars grouped by mod, then a section of retained manual jars no lock references (kept for rollback). Read-only. |
| `verify [target]` | — | Hash-check the store/folders + duplicate / dependency / side-mismatch checks. Exit 1 on errors. |
| `save <profile>` | `--label`/`-l` | Record the current lock as a known-good snapshot. |
| `history <profile>` | — | List saved snapshots. |
| `rollback <profile> <snapshot>` | — | Restore a snapshot: re-download old versions as needed, rebuild folders (reference-aware). |
| `reset` | `-y` | Wipe the shared `store/` and all built `mods/` folders (keeps lockfiles, snapshots, TOMLs). Prompts unless `-y`. |

**Targets:** `list`, `check`, `install`, and `verify` accept several profile ids
(`… tectonic nature`) or none / `all` for every profile. `update` takes a single
profile as its first argument, then optional mod names.

`install` only changes a version to satisfy a constraint you declared (a pin, a
tightened `channel`, a changed `game_version`) — never to chase "newest." Only
`update` seeks newer builds. Both build the client/server folders as their final step.

## How it works

```
profiles/x.toml → resolve → x.lock.json → store/<sha512>.jar → mods/x/{client,server}
   (desired)     (network)    (intended)      (verified bytes)       (what the game reads)
```

- **Resolve** asks Modrinth / CurseForge (concurrently, with retries) for the right
  version of each mod, capturing its download URL, sha512, and dependencies.
- **The store** holds jars by their sha512 — a file's *name is its hash*, so a
  corrupt or half-downloaded jar can never be mistaken for a good one. It's shared
  across profiles, so a mod common to two packs is stored once.
- **The lockfile** is the resolved snapshot of "what's installed now." It's derived
  and disposable: delete it and `install` rebuilds it.
- **Materialize** mirrors the lock into your client/server folders (and `datapacks/`
  for datapacks). Stale files the tool placed are removed; files it never placed are
  left untouched.
- **Snapshots** are tiny metadata files (no jars) recording which versions you had.
  Rolling back re-secures those jars (re-downloading as needed) and rebuilds.
- **Sweep** runs after every change: any jar not referenced by some profile's lock is
  deleted, so the store never accumulates junk — while jars shared or pinned by
  another profile always survive. **Manual jars are the exception**: they can't be
  re-downloaded, so they're pinned in the store permanently (even old versions) so
  rollback always works. `modman store` lists them.

### Integrity guarantees

- Every run reconciles the **whole** TOML against the lock *and* the disk — no edit
  is ever silently missed.
- Bad TOML aborts before anything is written; a failed download/resolve aborts the
  whole profile and leaves the last good state intact.
- Jars and lockfiles are written atomically (temp file then rename), so an
  interrupted run can't leave a corrupt file behind.

### What `verify` checks

- **integrity** — a locked jar missing from the store or failing its hash;
- **duplicate files** — two mods that would write the same filename to a folder (a crash risk);
- **duplicate projects** — the same mod pulled from two sources;
- **side mismatches** — e.g. a client-only mod placed on the server (from Modrinth metadata);
- **missing dependencies** — a required library absent on that side;
- **drift** — expected jars missing from a built folder, or stale ones left behind.

## Manual mods

Some mods can't be downloaded automatically — the author disabled third-party API
downloads, or you just want to manage the jar yourself. Declare it with
`source = "manual"` (no filename required):

```toml
[[mods]]
name = "Voxy Neoforge Port"
source = "manual"
id = "voxy"                 # optional; defaults to a slug of the name
side = "client"
url = "https://modrinth.com/mod/voxy/versions"   # optional, shown by check
```

Then drop the jar next to where you run commands and register it in one step:

```
py modman.py add voxy voxy-0.2.15-neoforge.jar
```

`add` ingests the jar into the store, records it in the lock (so it's tracked, built
into your folders, and never swept away), and picks the profile automatically — or
pass `--profile <id>` if the same manual mod is in several. `check` shows a manual
mod in red as **not present** until you've added its jar; once added it's treated
like any other locked mod. To swap in a newer build, just `add` the new jar — the
old one stays in the store (manual jars are never swept), so you can always roll back
to it. `modman store` shows every retained jar per mod.

## Minimal clients & singleplayer

To keep the multiplayer client as light as possible, put mods that only need to run
server-side (worldgen, most data mods) on `side = "server"`. They won't land in
`client/`, and in multiplayer they still work because the server drives them.

The catch is singleplayer: a singleplayer world runs its own integrated server, so it
needs those server-side mods on the client too. Rather than bloating the client, set
`singleplayer = true` in `[profile]`. Each build then also produces a `singleplayer/`
folder — the union of your client and server mods (everything supported in
singleplayer; a server-side mod like Noisium belongs here because a solo world runs
the integrated server, and only mods unsupported on *both* sides are left out). Use
`client/` for the multiplayer client and `singleplayer/` for singleplayer worlds:

```
mods/tectonic/
  client/        side: client + both        (minimal multiplayer client)
  server/        side: server + both        (dedicated server)
  singleplayer/  client ∪ server-runnable   (full parity for local worlds)
```

You don't mark individual mods for singleplayer — "needed in singleplayer" is just
"is a server-side mod", which `side` already says.

## Handy scenarios

**"I want everything current except Sodium, which breaks a shader."** Add
`pin = "0.6.0"` under Sodium in the TOML, then `update`. Every other mod moves to its
newest build; Sodium holds at 0.6.0, and the tool still reports `0.6.13 available` so
the hold is never a surprise.

**"I deleted a jar from my mods folder by accident."** Just run `install`. The folder
is rebuilt to exactly match the lock, pulling the jar back from the store — no network
needed if it's the current version.

## Project layout

```
mod-manager/
├─ profiles/            you edit these
│  ├─ tectonic.toml         desired state
│  └─ tectonic.lock.json    resolved state (tool-owned, gitignored)
├─ store/               deduplicated jars by hash (gitignored)
├─ history/             your snapshots
├─ mods/                built folders per profile (gitignored)
│  └─ tectonic/{client,server,datapacks,singleplayer}
├─ src/modman/          the code
│  ├─ config.py             read & validate the TOML
│  ├─ providers/            modrinth · curseforge · manual · url
│  ├─ store.py              content-addressed jar store + sweep
│  ├─ lock.py               lockfile read/write
│  ├─ engine.py             the reconciler (install / update)
│  ├─ builder.py            materialize lock → folders
│  ├─ history.py            save / rollback
│  ├─ checks.py             verify checks
│  ├─ report.py             terminal formatting
│  └─ cli.py                command-line entry point
├─ modman.py            zero-install launcher
└─ tests/               offline integration tests
```

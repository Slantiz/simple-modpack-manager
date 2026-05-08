# Modpack Manager

A CLI tool that checks your Minecraft modpack mods for updates, downloads them, and keeps client and server profiles in sync. Sources mods from Modrinth with CurseForge as a fallback.

## Requirements

- Python 3.11+

## Setup

**1. Clone and install dependencies**
```
pip install -r requirements.txt
```

**2. Configure your mods**

Copy the example config and fill in your mods:
```
cp mods.example.toml mods.toml
```

Edit `mods.toml` — set your game version, loader, and add a `[[mods]]` entry for each mod:

| Field | Required | Description |
|---|---|---|
| `name` | yes | Display name |
| `slug` | yes | Modrinth project slug. Leave empty (`""`) to skip Modrinth |
| `side` | yes | `"client"`, `"server"`, or `"both"` |
| `curseforge_slug` | no | CurseForge slug, used as fallback when Modrinth has no result |

**3. (Optional) Add a CurseForge API key for mods not on Modrinth**

```
cp .env.example .env
```

Edit `.env` and paste your key:
```
CURSEFORGE_API_KEY=your-key-here
```

> **Tips:** Get a free key at [console.curseforge.com](https://console.curseforge.com). Without a key, CurseForge fallback is disabled.

## Usage

### Update mods

```
py main.py --profile client
py main.py --profile server
py main.py --profile client --verbose
```

| Flag | Description |
|---|---|
| `--profile` | Required. Which mod set to update (`client` or `server`) |
| `--verbose` | Print full HTTP response details on errors |

### List installed mods

```
py list_mods.py
```

Prints an alphabetically sorted list of all mods grouped by side (both / client / server), with the cached filename shown for each.

## How it works

- Checks each mod against Modrinth (and CurseForge if configured)
- Skips mods already on the latest version (tracked in `mod_cache_<profile>.json`)
- Distinguishes new mods (To add) from version bumps (To update) in results and summaries
- Detects mods removed from `mods.toml` and offers to delete their files
- Writes a summary to `summaries/` only when something changed or failed

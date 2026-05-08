import requests
from pathlib import Path

HEADERS = {"User-Agent": "create-modpack-manager/1.0"}
CHUNK_SIZE = 8192


def download(url: str, dest_dir: Path, filename: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    response = requests.get(url, headers=HEADERS, stream=True)
    response.raise_for_status()

    with open(dest, "wb") as f:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            f.write(chunk)

    return dest


def remove_old(dest_dir: Path, old_filename: str) -> None:
    old_file = dest_dir / old_filename
    if old_file.exists():
        old_file.unlink()

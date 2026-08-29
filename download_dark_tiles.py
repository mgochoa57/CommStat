# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
download_dark_tiles.py - One-off tool to bake an offline dark-theme tile
cache (tilesPNG2Dark/) that mirrors the existing light-theme offline cache
(tilesPNG2/).

Walks tilesPNG2/ to find the exact set of (z, x, y) tiles already baked
for offline use, then downloads the same z/x/y coverage from Esri's World
Dark Gray Canvas basemap (the same source now used for CommStat's online
dark map — see little_gucci.py _load_map()) and writes them to
tilesPNG2Dark/{z}/{x}/{y}.png using the same local-tile naming convention
so TileSchemeHandler can serve them unmodified.

Esri's tile REST API addresses tiles as {z}/{row}/{col}, i.e. z/y/x (the
reverse of the z/x/y XYZ convention CommStat's local cache uses) — this
script does that translation on download.

Run once from the repo root:
    python download_dark_tiles.py

Safe to re-run: existing files are skipped, so an interrupted run can
just be restarted.
"""

import os
import time
import urllib.request
import urllib.error

SRC_DIR = "tilesPNG2"
DST_DIR = "tilesPNG2Dark"
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"

RETRIES = 3
RETRY_DELAY_SEC = 2
REQUEST_TIMEOUT_SEC = 15


def find_tiles(src_dir: str):
    """Yield (z, x, y) for every tile baked into the existing light cache."""
    for z_name in sorted(os.listdir(src_dir), key=lambda s: int(s)):
        z_dir = os.path.join(src_dir, z_name)
        if not os.path.isdir(z_dir):
            continue
        for x_name in sorted(os.listdir(z_dir), key=lambda s: int(s)):
            x_dir = os.path.join(z_dir, x_name)
            if not os.path.isdir(x_dir):
                continue
            for fname in os.listdir(x_dir):
                if fname.endswith(".png"):
                    yield int(z_name), int(x_name), int(fname[:-4])


def download_tile(z: int, x: int, y: int, dest_path: str) -> bool:
    url = TILE_URL.format(z=z, y=y, x=x)
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CommStat-offline-tile-baker/1.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                data = resp.read()
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(data)
            return True
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                # Tile genuinely doesn't exist at this z/x/y (e.g. edge of
                # coverage) — not worth retrying.
                break
        except Exception as e:
            last_err = e
        if attempt < RETRIES:
            time.sleep(RETRY_DELAY_SEC)
    print(f"  FAILED z{z}/x{x}/y{y}: {last_err}")
    return False


def main() -> None:
    if not os.path.isdir(SRC_DIR):
        print(f"Source tile dir '{SRC_DIR}' not found — run this from the repo root.")
        return

    tiles = list(find_tiles(SRC_DIR))
    print(f"Found {len(tiles)} tiles in {SRC_DIR}/ to mirror into {DST_DIR}/")

    downloaded = skipped = failed = 0
    for i, (z, x, y) in enumerate(tiles, 1):
        dest_path = os.path.join(DST_DIR, str(z), str(x), f"{y}.png")
        if os.path.exists(dest_path):
            skipped += 1
            continue
        ok = download_tile(z, x, y, dest_path)
        if ok:
            downloaded += 1
        else:
            failed += 1
        if i % 50 == 0 or i == len(tiles):
            print(f"  [{i}/{len(tiles)}] downloaded={downloaded} skipped={skipped} failed={failed}")

    print(f"Done. downloaded={downloaded} skipped={skipped} failed={failed}")
    if failed:
        print("Re-run this script to retry failed tiles.")


if __name__ == "__main__":
    main()

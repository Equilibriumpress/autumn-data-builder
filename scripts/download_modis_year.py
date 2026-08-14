from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import earthaccess

BANDS = (
    "Senescence_1",
    "Senescence_2",
    "MidGreendown_1",
    "MidGreendown_2",
    "Dormancy_1",
    "Dormancy_2",
    "EVI_Amplitude_1",
    "EVI_Amplitude_2",
    "QA_Overall_1",
    "QA_Overall_2",
)
TILE_RE = re.compile(r"\.h\d{2}v\d{2}\.", re.IGNORECASE)


def retry(label: str, function, attempts: int = 6):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return function()
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                raise
            delay = min(90, 5 * (2 ** (attempt - 1)))
            print(f"{label}: retry {attempt}/{attempts} in {delay}s after {exc}", file=sys.stderr, flush=True)
            time.sleep(delay)
    raise RuntimeError(f"{label} failed: {last_error}")


def normalized_band_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def gdal_subdatasets(path: Path) -> list[tuple[str, str]]:
    payload = json.loads(subprocess.check_output(["gdalinfo", "-json", str(path)], text=True))
    candidates: list[tuple[str, str]] = []
    metadata = payload.get("metadata", {}).get("SUBDATASETS", {})
    if isinstance(metadata, dict):
        names: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        for key, value in metadata.items():
            match = re.match(r"SUBDATASET_(\d+)_(NAME|DESC)$", key)
            if not match:
                continue
            index, kind = match.groups()
            if kind == "NAME":
                names[index] = str(value)
            else:
                descriptions[index] = str(value)
        for index, name in names.items():
            candidates.append((name, descriptions.get(index, "")))
    top_level = payload.get("subdatasets", [])
    if isinstance(top_level, list):
        for item in top_level:
            if isinstance(item, str):
                candidates.append((item, ""))
            elif isinstance(item, dict):
                name = item.get("name") or item.get("NAME")
                desc = item.get("desc") or item.get("description") or item.get("DESC") or ""
                if name:
                    candidates.append((str(name), str(desc)))
    if not candidates:
        text = subprocess.check_output(["gdalinfo", str(path)], text=True, errors="replace")
        names: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        for line in text.splitlines():
            match = re.match(r"SUBDATASET_(\d+)_(NAME|DESC)=(.*)$", line.strip())
            if not match:
                continue
            index, kind, value = match.groups()
            if kind == "NAME":
                names[index] = value
            else:
                descriptions[index] = value
        for index, name in names.items():
            candidates.append((name, descriptions.get(index, "")))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for name, desc in candidates:
        if name not in seen:
            seen.add(name)
            unique.append((name, desc))
    return unique


def split_cycle_band(band: str) -> tuple[str, int]:
    match = re.fullmatch(r"(.+)_([12])", band)
    if not match:
        raise ValueError(f"Expected a cycle-suffixed MODIS band, got {band}")
    return match.group(1), int(match.group(2))


def resolve_source(subdatasets: list[tuple[str, str]], base_band: str) -> str | None:
    target = normalized_band_name(base_band)
    for name, desc in subdatasets:
        name_tail = name.rsplit(":", 1)[-1]
        if normalized_band_name(name_tail) == target:
            return name
        desc_token = desc.split(" MCD12Q2", 1)[0].split()[-1] if desc else ""
        if normalized_band_name(desc_token) == target:
            return name
    return None


def convert_hdf(path: Path, year: int, output: Path) -> None:
    subdatasets = gdal_subdatasets(path)
    stem = path.stem.replace(".", "_")
    if not subdatasets:
        raise RuntimeError(f"{path.name}: GDAL exposed no subdatasets")
    for band in BANDS:
        base_band, cycle = split_cycle_band(band)
        source = resolve_source(subdatasets, base_band)
        if source is None:
            available = "\n".join(f"  - {name} :: {desc}" if desc else f"  - {name}" for name, desc in subdatasets)
            raise RuntimeError(
                f"{path.name}: missing MODIS subdataset {base_band} for {band}. "
                f"GDAL exposed {len(subdatasets)} subdatasets:\n{available}"
            )
        source_info = json.loads(subprocess.check_output(["gdalinfo", "-json", source], text=True))
        raster_bands = source_info.get("bands", [])
        if len(raster_bands) < cycle:
            raise RuntimeError(
                f"{path.name}: {base_band} exposes {len(raster_bands)} raster bands; cycle {cycle} is required for {band}"
            )
        target = output / f"{year}_{stem}_{band}.tif"
        if not target.exists():
            subprocess.run(
                ["gdal_translate", "-q", "-b", str(cycle), "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", source, str(target)],
                check=True,
            )


def tile_from_path(path: Path) -> str | None:
    match = TILE_RE.search(path.name)
    return match.group(0).strip(".").lower() if match else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and convert one MCD12Q2 year")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"))
    parser.add_argument("--tiles", help="Comma-separated native MODIS hXXvYY tiles")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.bbox) == bool(args.tiles):
        raise SystemExit("Pass exactly one of --bbox or --tiles")
    if not (os.getenv("EARTHDATA_TOKEN") or (os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD"))):
        raise SystemExit("Earthdata credentials are missing")

    retry("Earthdata login", lambda: earthaccess.login(strategy="environment"))
    search_kwargs = {
        "short_name": "MCD12Q2",
        "version": "061",
        "temporal": (f"{args.year}-01-01", f"{args.year}-12-31"),
        "count": -1,
    }
    wanted_tiles: set[str] = set()
    if args.tiles:
        wanted_tiles = {value.strip().lower() for value in args.tiles.split(",") if value.strip()}
        search_kwargs["granule_name"] = [f"MCD12Q2.A{args.year}*.{tile}.061.*" for tile in sorted(wanted_tiles)]
    else:
        search_kwargs["bounding_box"] = tuple(args.bbox)

    results = retry("Earthdata search", lambda: earthaccess.search_data(**search_kwargs))
    if not results:
        raise SystemExit(f"No MCD12Q2 granules found for {args.year}")

    raw = args.output / "raw"
    converted = args.output / str(args.year)
    shutil.rmtree(args.output, ignore_errors=True)
    raw.mkdir(parents=True, exist_ok=True)
    converted.mkdir(parents=True, exist_ok=True)

    downloaded = retry("Earthdata download", lambda: earthaccess.download(results, str(raw), threads=4))
    hdf_files = [Path(item) for item in downloaded if str(item).lower().endswith((".hdf", ".h5", ".nc"))]
    if not hdf_files:
        hdf_files = list(raw.rglob("*.hdf")) + list(raw.rglob("*.h5")) + list(raw.rglob("*.nc"))
    if wanted_tiles:
        hdf_files = [path for path in hdf_files if tile_from_path(path) in wanted_tiles]
        found = {tile_from_path(path) for path in hdf_files}
        missing = sorted(wanted_tiles - {tile for tile in found if tile})
        if missing:
            raise RuntimeError(f"MCD12Q2 {args.year}: missing requested tiles after download: {missing}")
    if not hdf_files:
        raise SystemExit("Earthaccess returned no HDF/H5/NetCDF granules")

    for path in sorted(set(hdf_files)):
        convert_hdf(path, args.year, converted)
    shutil.rmtree(raw, ignore_errors=True)
    print(f"Prepared {len(hdf_files)} MCD12Q2 granules for {args.year}")


if __name__ == "__main__":
    main()

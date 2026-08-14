from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
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
        # MCD12Q2 v6.1 exposes two vegetation cycles as raster bands inside one
        # HDF subdataset, e.g. `Senescence` is [2400x2400x2]. Match the base SDS.
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
            available = "\n".join(
                f"  - {name} :: {desc}" if desc else f"  - {name}"
                for name, desc in subdatasets
            )
            raise RuntimeError(
                f"{path.name}: missing MODIS subdataset {base_band} for {band}. "
                f"GDAL exposed {len(subdatasets)} subdatasets:\n{available}"
            )

        source_info = json.loads(
            subprocess.check_output(["gdalinfo", "-json", source], text=True)
        )
        raster_bands = source_info.get("bands", [])
        if len(raster_bands) < cycle:
            raise RuntimeError(
                f"{path.name}: {base_band} exposes {len(raster_bands)} raster bands; "
                f"cycle {cycle} is required for {band}"
            )

        target = output / f"{year}_{stem}_{band}.tif"
        if not target.exists():
            subprocess.run(
                [
                    "gdal_translate",
                    "-q",
                    "-b",
                    str(cycle),
                    "-co",
                    "TILED=YES",
                    "-co",
                    "COMPRESS=DEFLATE",
                    source,
                    str(target),
                ],
                check=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and convert one MCD12Q2 year for one shard")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not (os.getenv("EARTHDATA_TOKEN") or (os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD"))):
        raise SystemExit("Earthdata credentials are missing")

    earthaccess.login(strategy="environment")
    min_lon, min_lat, max_lon, max_lat = args.bbox
    results = earthaccess.search_data(
        short_name="MCD12Q2",
        version="061",
        temporal=(f"{args.year}-01-01", f"{args.year}-12-31"),
        bounding_box=(min_lon, min_lat, max_lon, max_lat),
        count=-1,
    )
    if not results:
        raise SystemExit(f"No MCD12Q2 granules found for {args.year} and bbox {args.bbox}")

    raw = args.output / "raw"
    converted = args.output / str(args.year)
    shutil.rmtree(args.output, ignore_errors=True)
    raw.mkdir(parents=True, exist_ok=True)
    converted.mkdir(parents=True, exist_ok=True)

    downloaded = earthaccess.download(results, str(raw), threads=4)
    hdf_files = [Path(item) for item in downloaded if str(item).lower().endswith((".hdf", ".h5", ".nc"))]
    if not hdf_files:
        hdf_files = list(raw.rglob("*.hdf")) + list(raw.rglob("*.h5")) + list(raw.rglob("*.nc"))
    if not hdf_files:
        raise SystemExit("Earthaccess returned no HDF/H5/NetCDF granules")

    for path in sorted(set(hdf_files)):
        convert_hdf(path, args.year, converted)

    shutil.rmtree(raw, ignore_errors=True)
    print(f"Prepared {len(hdf_files)} MCD12Q2 granules for {args.year}")


if __name__ == "__main__":
    main()

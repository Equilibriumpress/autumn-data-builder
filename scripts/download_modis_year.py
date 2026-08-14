from __future__ import annotations

import argparse
import json
import os
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


def gdal_subdatasets(path: Path) -> list[str]:
    payload = json.loads(subprocess.check_output(["gdalinfo", "-json", str(path)], text=True))
    metadata = payload.get("metadata", {}).get("SUBDATASETS", {})
    return [value for key, value in metadata.items() if key.endswith("_NAME")]


def convert_hdf(path: Path, year: int, output: Path) -> None:
    subdatasets = gdal_subdatasets(path)
    stem = path.stem.replace(".", "_")
    found = 0
    for band in BANDS:
        source = next((item for item in subdatasets if band.lower() in item.lower()), None)
        if source is None:
            raise RuntimeError(f"{path.name}: missing MODIS subdataset {band}")
        target = output / f"{year}_{stem}_{band}.tif"
        if not target.exists():
            subprocess.run(
                [
                    "gdal_translate",
                    "-q",
                    "-co",
                    "TILED=YES",
                    "-co",
                    "COMPRESS=DEFLATE",
                    source,
                    str(target),
                ],
                check=True,
            )
        found += 1
    if found != len(BANDS):
        raise RuntimeError(f"{path.name}: incomplete band conversion")


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

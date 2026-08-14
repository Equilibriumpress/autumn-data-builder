from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import zstandard as zstd

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def years_from(value: str) -> list[int]:
    if ":" in value:
        start, end = (int(part) for part in value.split(":", 1))
        return list(range(start, end + 1))
    return [int(part) for part in value.split(",") if part.strip()]


def write_config(path: Path, cell_degrees: float) -> None:
    payload = {
        "cell_degrees": cell_degrees,
        "min_tree_cover": 20.0,
        "min_deciduous_signal": 15.0,
        "max_modis_qa": 2,
        "min_years": 4,
        "outlier_days": 35,
    }
    path.write_text(json.dumps(payload, indent=2))


def clip_workspace(workspace: Path, bbox: list[float]) -> int:
    min_lon, min_lat, max_lon, max_lat = bbox
    db = sqlite3.connect(workspace)
    try:
        db.execute(
            """
            DELETE FROM zone_candidates
            WHERE latitude < ? OR latitude >= ? OR longitude < ? OR longitude >= ?
            """,
            (min_lat, max_lat, min_lon, max_lon),
        )
        db.commit()
        return int(db.execute("SELECT COUNT(*) FROM zone_candidates").fetchone()[0])
    finally:
        db.close()


def compress(source: Path, target: Path) -> None:
    compressor = zstd.ZstdCompressor(level=10, threads=-1)
    with source.open("rb") as src, target.open("wb") as dst:
        compressor.copy_stream(src, dst)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build one worldwide foliage shard")
    parser.add_argument("--id", required=True)
    parser.add_argument("--bbox", nargs=4, type=float, required=True)
    parser.add_argument("--years", default="2014:2024")
    parser.add_argument("--cell-degrees", type=float, default=0.2)
    parser.add_argument("--pipeline-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pipeline = args.pipeline_dir.resolve()
    if not (pipeline / "modis_ingest.py").exists():
        raise SystemExit(f"Invalid pipeline directory: {pipeline}")

    work = ROOT / ".work" / args.id
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    args.output.mkdir(parents=True, exist_ok=True)
    workspace = work / "workspace.sqlite"
    config = work / "config.json"
    write_config(config, args.cell_degrees)
    python = sys.executable
    common = ["--workspace", str(workspace), "--config", str(config)]

    # MODIS is processed one year at a time so raw HDF and converted TIFFs never
    # accumulate across the full multi-year baseline on a 14 GB runner disk.
    for year in years_from(args.years):
        modis_dir = work / "modis"
        run(
            [
                python,
                str(ROOT / "scripts" / "download_modis_year.py"),
                "--year",
                str(year),
                "--bbox",
                *map(str, args.bbox),
                "--output",
                str(modis_dir),
            ]
        )
        run(
            [
                python,
                str(pipeline / "modis_ingest.py"),
                "--input",
                str(modis_dir),
                "--years",
                str(year),
                *common,
            ]
        )
        shutil.rmtree(modis_dir, ignore_errors=True)

    run([python, str(pipeline / "historical_baseline.py"), *common])

    landcover = work / "landcover"
    run(
        [
            python,
            str(ROOT / "scripts" / "download_cdse.py"),
            "landcover",
            "--bbox",
            *map(str, args.bbox),
            "--output",
            str(landcover),
        ]
    )
    tree_vrt = work / "tree-cover.vrt"
    forest_vrt = work / "forest-type.vrt"
    tree_files = sorted(landcover.glob("Tree_Cover_Fraction_*.tif"))
    forest_files = sorted(landcover.glob("Forest_Type_*.tif"))
    if not tree_files or not forest_files:
        raise RuntimeError("CDSE land-cover download produced no matching raster files")
    run(["gdalbuildvrt", str(tree_vrt), *map(str, tree_files)])
    run(["gdalbuildvrt", str(forest_vrt), *map(str, forest_files)])
    run(
        [
            python,
            str(pipeline / "copernicus_ingest.py"),
            "--input",
            str(landcover),
            "--tree-cover",
            str(tree_vrt),
            "--forest-type",
            str(forest_vrt),
            "--year",
            "2019",
            *common,
        ]
    )
    run([python, str(pipeline / "forest_filter.py"), *common])
    shutil.rmtree(landcover, ignore_errors=True)
    tree_vrt.unlink(missing_ok=True)
    forest_vrt.unlink(missing_ok=True)

    dem = work / "dem"
    run(
        [
            python,
            str(ROOT / "scripts" / "download_cdse.py"),
            "dem",
            "--bbox",
            *map(str, args.bbox),
            "--output",
            str(dem),
        ]
    )
    run([python, str(pipeline / "elevation_ingest.py"), "--input", str(dem), *common])
    shutil.rmtree(dem, ignore_errors=True)

    run([python, str(pipeline / "generate_zones.py"), *common])
    run([python, str(pipeline / "normalize_seasons.py"), *common])
    run([python, str(pipeline / "robust_statistics.py"), *common])

    zone_count = clip_workspace(workspace, args.bbox)
    if zone_count > 0:
        run([python, str(pipeline / "validate.py"), *common])

    export_dir = work / "export"
    run([python, str(pipeline / "export_ios.py"), "--output", str(export_dir), *common])
    database = export_dir / "foliage.sqlite"
    compressed = args.output / f"{args.id}.sqlite.zst"
    compress(database, compressed)

    manifest = {
        "id": args.id,
        "bbox": args.bbox,
        "years": years_from(args.years),
        "cellDegrees": args.cell_degrees,
        "zoneCount": zone_count,
        "compressedBytes": compressed.stat().st_size,
    }
    (args.output / f"{args.id}.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

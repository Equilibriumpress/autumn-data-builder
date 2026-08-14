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
    path.write_text(
        json.dumps(
            {
                "cell_degrees": cell_degrees,
                "min_tree_cover": 20.0,
                "min_deciduous_signal": 15.0,
                "max_modis_qa": 2,
                "min_years": 4,
                "outlier_days": 35,
            },
            indent=2,
        )
    )


def compact_workspace(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        count = int(db.execute("SELECT COUNT(*) FROM phenology_observations").fetchone()[0])
        for table in ("phenology_yearly", "phenology_baseline", "forest_cells", "elevation_cells", "zone_candidates"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        db.execute("DROP TABLE IF EXISTS eligible_forest_cells")
        db.execute("DROP TABLE IF EXISTS forest_observations")
        db.commit()
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("VACUUM")
        return count
    finally:
        db.close()


def compress(source: Path, target: Path) -> None:
    compressor = zstd.ZstdCompressor(level=6, threads=-1)
    with source.open("rb") as src, target.open("wb") as dst:
        compressor.copy_stream(src, dst)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build one native MODIS observation group")
    parser.add_argument("--id", required=True)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--tiles", required=True)
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

    for year in years_from(args.years):
        modis_dir = work / "modis"
        run(
            [
                python,
                str(ROOT / "scripts" / "download_modis_year.py"),
                "--year",
                str(year),
                "--tiles",
                args.tiles,
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

    observation_count = compact_workspace(workspace)
    compressed = args.output / f"{args.asset}.sqlite.zst"
    compress(workspace, compressed)
    manifest = {
        "id": args.id,
        "asset": args.asset,
        "tiles": [value for value in args.tiles.split(",") if value],
        "years": years_from(args.years),
        "cellDegrees": args.cell_degrees,
        "observationCount": observation_count,
        "compressedBytes": compressed.stat().st_size,
    }
    (args.output / f"{args.asset}.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

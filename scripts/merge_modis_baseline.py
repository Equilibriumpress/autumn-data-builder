from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import zstandard as zstd


def decompress(source: Path, destination: Path) -> None:
    decoder = zstd.ZstdDecompressor()
    with source.open("rb") as src, destination.open("wb") as dst:
        decoder.copy_stream(src, dst)


def compress(source: Path, target: Path) -> None:
    encoder = zstd.ZstdCompressor(level=6, threads=-1)
    with source.open("rb") as src, target.open("wb") as dst:
        encoder.copy_stream(src, dst)


def initialize_observations(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS phenology_observations (
            cell_id TEXT NOT NULL,
            year INTEGER NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            senescence_day REAL NOT NULL,
            midgreendown_day REAL NOT NULL,
            dormancy_day REAL NOT NULL,
            mean_qa REAL NOT NULL,
            mean_amplitude REAL NOT NULL,
            sample_count INTEGER NOT NULL
        )
        """
    )


def merge_observations(shards: list[Path], workspace: Path) -> int:
    workspace.unlink(missing_ok=True)
    db = sqlite3.connect(workspace)
    initialize_observations(db)
    inserted = 0
    try:
        with tempfile.TemporaryDirectory(prefix="modis-observations-") as temp_dir:
            temp = Path(temp_dir)
            for index, archive in enumerate(shards, start=1):
                source = temp / f"{index}.sqlite"
                decompress(archive, source)
                db.execute("ATTACH DATABASE ? AS src", (str(source),))
                before = db.total_changes
                db.execute(
                    """
                    INSERT INTO phenology_observations
                    SELECT cell_id, year, latitude, longitude, senescence_day,
                           midgreendown_day, dormancy_day, mean_qa,
                           mean_amplitude, sample_count
                    FROM src.phenology_observations
                    """
                )
                inserted += db.total_changes - before
                db.commit()
                db.execute("DETACH DATABASE src")
                source.unlink(missing_ok=True)
                print(f"Merged MODIS observation shard {index}/{len(shards)}")
        db.execute("CREATE INDEX IF NOT EXISTS idx_obs_cell_year ON phenology_observations(cell_id, year)")
        db.commit()
    finally:
        db.close()
    return inserted


def create_baseline_shard(source: sqlite3.Connection, target_path: Path, bbox: tuple[float, float, float, float]) -> int:
    min_lon, min_lat, max_lon, max_lat = bbox
    rows = source.execute(
        """
        SELECT cell_id, latitude, longitude, start_day, peak_day, end_day,
               variability_days, sample_years, quality
        FROM phenology_baseline
        WHERE latitude >= ? AND latitude < ?
          AND longitude >= ? AND longitude < ?
        ORDER BY cell_id
        """,
        (min_lat, max_lat, min_lon, max_lon),
    ).fetchall()
    if not rows:
        return 0
    target_path.unlink(missing_ok=True)
    target = sqlite3.connect(target_path)
    try:
        target.execute(
            """
            CREATE TABLE phenology_baseline (
                cell_id TEXT PRIMARY KEY,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                start_day INTEGER NOT NULL,
                peak_day INTEGER NOT NULL,
                end_day INTEGER NOT NULL,
                variability_days REAL NOT NULL,
                sample_years INTEGER NOT NULL,
                quality REAL NOT NULL
            )
            """
        )
        target.executemany(
            """
            INSERT INTO phenology_baseline
            (cell_id, latitude, longitude, start_day, peak_day, end_day,
             variability_days, sample_years, quality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        target.commit()
        target.execute("VACUUM")
    finally:
        target.close()
    return len(rows)


def partition_baseline(workspace: Path, output: Path, step: int, lat_min: int, lat_max: int) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(workspace)
    db.execute("CREATE INDEX IF NOT EXISTS idx_baseline_lat_lon ON phenology_baseline(latitude, longitude)")
    db.commit()
    include = []
    try:
        index = 0
        for min_lat in range(lat_min, lat_max, step):
            max_lat = min(lat_max, min_lat + step)
            for min_lon in range(-180, 180, step):
                max_lon = min(180, min_lon + step)
                temp_db = output / f"baseline_e{index:03d}.sqlite"
                count = create_baseline_shard(db, temp_db, (min_lon, min_lat, max_lon, max_lat))
                if count:
                    asset = f"baseline_e{index:03d}.sqlite.zst"
                    compress(temp_db, output / asset)
                    temp_db.unlink(missing_ok=True)
                    include.append(
                        {
                            "id": f"e{index:03d}",
                            "asset": asset,
                            "min_lon": min_lon,
                            "min_lat": min_lat,
                            "max_lon": max_lon,
                            "max_lat": max_lat,
                            "baselineCount": count,
                        }
                    )
                index += 1
    finally:
        db.close()
    return {"include": include, "shardCount": len(include), "step": step}


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge native MODIS shards and build geographic baseline shards")
    parser.add_argument("--shards", type=Path, required=True)
    parser.add_argument("--pipeline-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--matrix-output", type=Path, required=True)
    parser.add_argument("--step", type=int, default=20)
    parser.add_argument("--lat-min", type=int, default=-60)
    parser.add_argument("--lat-max", type=int, default=80)
    args = parser.parse_args()

    compressed = sorted(args.shards.glob("*.sqlite.zst"))
    if not compressed:
        raise SystemExit("No native MODIS observation shards found")
    args.workspace.parent.mkdir(parents=True, exist_ok=True)
    observations = merge_observations(compressed, args.workspace)

    subprocess.run(
        [
            sys.executable,
            str(args.pipeline_dir / "historical_baseline.py"),
            "--workspace",
            str(args.workspace),
            "--config",
            str(args.config),
        ],
        check=True,
    )

    db = sqlite3.connect(args.workspace)
    try:
        baseline_count = int(db.execute("SELECT COUNT(*) FROM phenology_baseline").fetchone()[0])
        yearly_count = int(db.execute("SELECT COUNT(*) FROM phenology_yearly").fetchone()[0])
    finally:
        db.close()
    if baseline_count == 0:
        raise RuntimeError("Historical baseline contains zero cells")

    matrix = partition_baseline(args.workspace, args.output, args.step, args.lat_min, args.lat_max)
    if not matrix["include"]:
        raise RuntimeError("Baseline partition produced zero enrichment shards")
    args.matrix_output.write_text(json.dumps(matrix, separators=(",", ":")))
    report = {
        "modisShards": len(compressed),
        "observations": observations,
        "yearlyCells": yearly_count,
        "baselineCells": baseline_count,
        "enrichmentShards": matrix["shardCount"],
    }
    (args.output / "baseline-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()

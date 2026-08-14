from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import zstandard as zstd


def initialize(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA page_size=4096")
    db.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE zones (
            pk INTEGER PRIMARY KEY,
            id TEXT NOT NULL UNIQUE,
            lat_e5 INTEGER NOT NULL,
            lon_e5 INTEGER NOT NULL,
            radius_dm INTEGER NOT NULL,
            deciduous_bp INTEGER NOT NULL,
            mixed_bp INTEGER NOT NULL,
            tree_bp INTEGER NOT NULL,
            start_day INTEGER NOT NULL,
            peak_day INTEGER NOT NULL,
            end_day INTEGER NOT NULL,
            variability_tenths INTEGER NOT NULL,
            elevation_m INTEGER NOT NULL,
            hemisphere INTEGER NOT NULL,
            quality_byte INTEGER NOT NULL,
            sample_years INTEGER NOT NULL,
            source_id INTEGER NOT NULL REFERENCES sources(id)
        );
        CREATE INDEX idx_zones_lat_lon ON zones(lat_e5, lon_e5);
        CREATE INDEX idx_zones_peak_quality ON zones(peak_day, quality_byte);
        CREATE VIRTUAL TABLE zone_rtree USING rtree(pk, min_lat, max_lat, min_lon, max_lon);
        """
    )


def decompress(source: Path, destination: Path) -> None:
    decoder = zstd.ZstdDecompressor()
    with source.open("rb") as src, destination.open("wb") as dst:
        decoder.copy_stream(src, dst)


def merge_one(target: sqlite3.Connection, shard_path: Path, source_ids: dict[str, int]) -> int:
    shard = sqlite3.connect(f"file:{shard_path}?mode=ro", uri=True)
    try:
        local_sources = {int(row[0]): str(row[1]) for row in shard.execute("SELECT id, name FROM sources")}
        inserted_before = target.total_changes
        rows = shard.execute(
            """
            SELECT id, lat_e5, lon_e5, radius_dm, deciduous_bp, mixed_bp, tree_bp,
                   start_day, peak_day, end_day, variability_tenths, elevation_m,
                   hemisphere, quality_byte, sample_years, source_id
            FROM zones
            """
        )
        batch = []
        for row in rows:
            source_name = local_sources[int(row[15])]
            global_source_id = source_ids.get(source_name)
            if global_source_id is None:
                global_source_id = len(source_ids) + 1
                source_ids[source_name] = global_source_id
                target.execute("INSERT INTO sources(id, name) VALUES (?, ?)", (global_source_id, source_name))
            batch.append((*row[:15], global_source_id))
            if len(batch) >= 5000:
                target.executemany(
                    """
                    INSERT OR IGNORE INTO zones(
                        id, lat_e5, lon_e5, radius_dm, deciduous_bp, mixed_bp, tree_bp,
                        start_day, peak_day, end_day, variability_tenths, elevation_m,
                        hemisphere, quality_byte, sample_years, source_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                batch.clear()
        if batch:
            target.executemany(
                """
                INSERT OR IGNORE INTO zones(
                    id, lat_e5, lon_e5, radius_dm, deciduous_bp, mixed_bp, tree_bp,
                    start_day, peak_day, end_day, variability_tenths, elevation_m,
                    hemisphere, quality_byte, sample_years, source_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
        return target.total_changes - inserted_before
    finally:
        shard.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge cloud foliage shards into one schema-v2 app database")
    parser.add_argument("--shards", type=Path, required=True)
    parser.add_argument("--pipeline-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cell-degrees", type=float, default=0.2)
    args = parser.parse_args()

    compressed = sorted(args.shards.glob("*.sqlite.zst"))
    if not compressed:
        raise SystemExit("No shard .sqlite.zst files found")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.unlink(missing_ok=True)
    target = sqlite3.connect(args.output)
    initialize(target)
    source_ids: dict[str, int] = {}

    with tempfile.TemporaryDirectory(prefix="autumn-shards-") as temp_dir:
        temp = Path(temp_dir)
        for index, archive in enumerate(compressed, start=1):
            shard_db = temp / f"{index}.sqlite"
            decompress(archive, shard_db)
            count = merge_one(target, shard_db, source_ids)
            shard_db.unlink(missing_ok=True)
            print(f"Merged {archive.name}: {count:,} changes")
            if index % 10 == 0:
                target.commit()

    target.commit()
    zone_count = int(target.execute("SELECT COUNT(*) FROM zones").fetchone()[0])
    if zone_count == 0:
        raise RuntimeError("Merged database contains zero foliage zones")

    target.execute(
        """
        INSERT INTO zone_rtree(pk, min_lat, max_lat, min_lon, max_lon)
        SELECT pk, lat_e5 / 100000.0, lat_e5 / 100000.0,
                   lon_e5 / 100000.0, lon_e5 / 100000.0
        FROM zones
        """
    )

    sys.path.insert(0, str(args.pipeline_dir.resolve()))
    from aggregate_export import build_aggregates

    aggregate_count = build_aggregates(target)
    metadata = {
        "schema_version": "2",
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "cell_degrees": str(args.cell_degrees),
        "zone_count": str(zone_count),
        "aggregate_count": str(aggregate_count),
        "aggregate_levels": "5.0,2.5,1.0",
        "coordinate_scale": "100000",
        "cover_scale": "10000",
        "radius_scale": "10",
        "variability_scale": "10",
        "quality_scale": "255",
        "spatial_index": "rtree",
        "phenology_source": "NASA MODIS MCD12Q2.061",
        "forest_source": "Copernicus Global Dynamic Land Cover 100m v3",
        "elevation_source": "Copernicus DEM GLO-90",
        "peak_definition": "MidGreendown + 35% of interval to Dormancy",
        "builder": "Equilibriumpress/autumn-data-builder",
    }
    target.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items())
    target.commit()
    target.execute("ANALYZE")
    target.execute("VACUUM")
    target.close()

    report = {
        "shards": len(compressed),
        "zones": zone_count,
        "aggregates": aggregate_count,
        "bytes": args.output.stat().st_size,
        "cellDegrees": args.cell_degrees,
    }
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()

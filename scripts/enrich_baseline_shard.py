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


def decompress(source: Path, target: Path) -> None:
    decoder = zstd.ZstdDecompressor()
    with source.open("rb") as src, target.open("wb") as dst:
        decoder.copy_stream(src, dst)


def compress(source: Path, target: Path) -> None:
    encoder = zstd.ZstdCompressor(level=6, threads=-1)
    with source.open("rb") as src, target.open("wb") as dst:
        encoder.copy_stream(src, dst)


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


def eligible_bbox(workspace: Path, padding: float) -> list[float] | None:
    db = sqlite3.connect(workspace)
    try:
        row = db.execute(
            """
            SELECT MIN(p.longitude), MIN(p.latitude), MAX(p.longitude), MAX(p.latitude)
            FROM phenology_baseline p
            JOIN eligible_forest_cells f USING(cell_id)
            """
        ).fetchone()
    finally:
        db.close()
    if not row or row[0] is None:
        return None
    return [
        max(-180.0, float(row[0]) - padding),
        max(-90.0, float(row[1]) - padding),
        min(180.0, float(row[2]) + padding),
        min(90.0, float(row[3]) + padding),
    ]


def table_count(workspace: Path, table: str) -> int:
    db = sqlite3.connect(workspace)
    try:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich one geographic MODIS baseline shard with Copernicus data")
    parser.add_argument("--id", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--bbox", nargs=4, type=float, required=True)
    parser.add_argument("--cell-degrees", type=float, default=0.2)
    parser.add_argument("--pipeline-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pipeline = args.pipeline_dir.resolve()
    work = ROOT / ".work" / args.id
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    args.output.mkdir(parents=True, exist_ok=True)
    workspace = work / "workspace.sqlite"
    decompress(args.baseline, workspace)
    config = work / "config.json"
    write_config(config, args.cell_degrees)
    python = sys.executable
    common = ["--workspace", str(workspace), "--config", str(config)]

    landcover = work / "landcover"
    run([python, str(ROOT / "scripts" / "download_cdse.py"), "landcover", "--bbox", *map(str, args.bbox), "--output", str(landcover)])
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

    forest_bbox = eligible_bbox(workspace, max(0.02, args.cell_degrees / 2.0))
    if forest_bbox is not None:
        dem = work / "dem"
        run([python, str(ROOT / "scripts" / "download_cdse.py"), "dem", "--bbox", *map(str, forest_bbox), "--output", str(dem)])
        run([python, str(pipeline / "elevation_ingest.py"), "--input", str(dem), *common])
        shutil.rmtree(dem, ignore_errors=True)

    run([python, str(pipeline / "generate_zones.py"), *common])
    run([python, str(pipeline / "normalize_seasons.py"), *common])
    run([python, str(pipeline / "robust_statistics.py"), *common])
    zone_count = table_count(workspace, "zone_candidates")
    if zone_count > 0:
        run([python, str(pipeline / "validate.py"), *common])

    export_dir = work / "export"
    run([python, str(pipeline / "export_ios.py"), "--output", str(export_dir), *common])
    database = export_dir / "foliage.sqlite"
    asset = f"zone_{args.id}.sqlite.zst"
    compress(database, args.output / asset)
    manifest = {
        "id": args.id,
        "bbox": args.bbox,
        "demBBox": forest_bbox,
        "baselineCount": table_count(workspace, "phenology_baseline"),
        "zoneCount": zone_count,
        "compressedBytes": (args.output / asset).stat().st_size,
    }
    (args.output / f"zone_{args.id}.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

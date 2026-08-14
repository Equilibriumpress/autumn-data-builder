from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests

STAC = "https://stac.dataspace.copernicus.eu/v1"
S3_ENDPOINT = "https://eodata.dataspace.copernicus.eu"


def discover_collection(title_contains: str) -> str:
    response = requests.get(f"{STAC}/collections", timeout=60)
    response.raise_for_status()
    collections = response.json().get("collections", [])
    matches = [item for item in collections if title_contains.lower() in item.get("title", "").lower()]
    if not matches:
        raise RuntimeError(f"No CDSE STAC collection title contains: {title_contains}")
    if len(matches) > 1:
        exact = [item for item in matches if item.get("title", "").lower() == title_contains.lower()]
        if len(exact) == 1:
            return exact[0]["id"]
        raise RuntimeError(f"Ambiguous CDSE collection {title_contains}: {[m['id'] for m in matches]}")
    return matches[0]["id"]


def search_items(collection: str, bbox: list[float], datetime: str | None = None) -> list[dict]:
    body: dict[str, object] = {"collections": [collection], "bbox": bbox, "limit": 1000}
    if datetime:
        body["datetime"] = datetime
    response = requests.post(f"{STAC}/search", json=body, timeout=120)
    response.raise_for_status()
    payload = response.json()
    features = list(payload.get("features", []))
    while True:
        next_link = next((link for link in payload.get("links", []) if link.get("rel") == "next"), None)
        if not next_link:
            break
        method = next_link.get("method", "GET").upper()
        if method == "POST":
            response = requests.post(next_link["href"], json=next_link.get("body", body), timeout=120)
        else:
            response = requests.get(next_link["href"], timeout=120)
        response.raise_for_status()
        payload = response.json()
        features.extend(payload.get("features", []))
    return features


def asset_text(key: str, asset: dict) -> str:
    bands = " ".join(str(item.get("name", "")) for item in asset.get("bands", []))
    return " ".join([key, str(asset.get("title", "")), bands]).lower()


def find_s3_href(asset: dict) -> str | None:
    candidates = [asset.get("href")]
    for alternate in asset.get("alternate", {}).values():
        if isinstance(alternate, dict):
            candidates.append(alternate.get("href"))
    for href in candidates:
        if not href:
            continue
        if href.startswith("s3://"):
            return href
        if href.startswith("/eodata/"):
            return "s3://eodata/" + href.removeprefix("/eodata/")
    return None


def make_client():
    access = os.getenv("CDSE_S3_ACCESS_KEY")
    secret = os.getenv("CDSE_S3_SECRET_KEY")
    if not access or not secret:
        raise RuntimeError("CDSE_S3_ACCESS_KEY/CDSE_S3_SECRET_KEY are missing")
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="default",
    )


def download_s3(client, href: str, target: Path) -> None:
    parsed = urlparse(href)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise RuntimeError(f"Invalid S3 asset href: {href}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 0:
        return
    client.download_file(bucket, key, str(target))


def download_landcover(bbox: list[float], output: Path) -> None:
    collection = discover_collection("CLMS Land Cover (LC) Global 100m yearly V3 (COG)")
    items = search_items(collection, bbox, "2019-01-01T00:00:00Z/2019-12-31T23:59:59Z")
    if not items:
        raise RuntimeError("No 2019 CLMS land-cover items intersect this shard")
    client = make_client()
    counts = {"Tree_Cover_Fraction": 0, "Forest_Type": 0}
    for item in items:
        item_id = item.get("id", "item").replace("/", "_")
        for band in tuple(counts):
            match = next(
                ((key, asset) for key, asset in item.get("assets", {}).items() if band.lower() in asset_text(key, asset)),
                None,
            )
            if not match:
                continue
            key, asset = match
            href = find_s3_href(asset)
            if not href:
                raise RuntimeError(f"Land-cover asset {key} has no CDSE S3 href")
            target = output / f"{band}_{item_id}.tif"
            download_s3(client, href, target)
            counts[band] += 1
    missing = [name for name, count in counts.items() if count == 0]
    if missing:
        raise RuntimeError(f"Missing land-cover assets: {missing}")
    print(f"Downloaded land cover: {counts}")


def download_dem(bbox: list[float], output: Path) -> None:
    collection = discover_collection("CopDEM COG (90 m)")
    items = search_items(collection, bbox)
    if not items:
        raise RuntimeError("No Copernicus GLO-90 DEM items intersect this shard")
    client = make_client()
    count = 0
    for item in items:
        assets = item.get("assets", {})
        match = next(
            (
                (key, asset)
                for key, asset in assets.items()
                if key.lower() == "data"
                or "geotiff" in str(asset.get("type", "")).lower()
                or "data" in asset.get("roles", [])
            ),
            None,
        )
        if not match:
            continue
        key, asset = match
        href = find_s3_href(asset)
        if not href:
            raise RuntimeError(f"DEM asset {key} has no CDSE S3 href")
        item_id = item.get("id", f"dem-{count}").replace("/", "_")
        download_s3(client, href, output / f"{item_id}.tif")
        count += 1
    if count == 0:
        raise RuntimeError("No downloadable GLO-90 GeoTIFF assets found")
    print(f"Downloaded {count} GLO-90 DEM tiles")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Copernicus data for one Autumn Atlas shard")
    parser.add_argument("kind", choices=["landcover", "dem"])
    parser.add_argument("--bbox", nargs=4, type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.kind == "landcover":
        download_landcover(args.bbox, args.output)
    else:
        download_dem(args.bbox, args.output)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request

CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
TILE_RE = re.compile(r"\.h(\d{2})v(\d{2})\.", re.IGNORECASE)


def request_entries(year: int, lat_min: float, lat_max: float) -> list[dict]:
    params = {
        "short_name": "MCD12Q2",
        "version": "061",
        "temporal": f"{year}-01-01T00:00:00Z,{year}-12-31T23:59:59Z",
        "bounding_box": f"-180,{lat_min},180,{lat_max}",
        "page_size": "2000",
        "page_num": "1",
    }
    entries: list[dict] = []
    while True:
        url = CMR + "?" + urllib.parse.urlencode(params)
        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "autumn-data-builder/2"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    payload = json.load(response)
                break
            except Exception as exc:
                last_error = exc
                if attempt == 5:
                    raise
                delay = min(60, 5 * (2 ** (attempt - 1)))
                print(f"CMR retry {attempt}/5 in {delay}s: {exc}", file=sys.stderr)
                time.sleep(delay)
        else:
            raise RuntimeError(f"CMR request failed: {last_error}")

        page = payload.get("feed", {}).get("entry", [])
        entries.extend(page)
        if len(page) < int(params["page_size"]):
            break
        params["page_num"] = str(int(params["page_num"]) + 1)
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover native MCD12Q2 tile groups from CMR")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--lat-min", type=float, default=-60.0)
    parser.add_argument("--lat-max", type=float, default=80.0)
    parser.add_argument("--group-size", type=int, default=2)
    parser.add_argument("--max-groups", type=int, default=240)
    parser.add_argument("--output")
    args = parser.parse_args()

    entries = request_entries(args.year, args.lat_min, args.lat_max)
    tiles: set[str] = set()
    for entry in entries:
        name = str(entry.get("producer_granule_id") or entry.get("title") or entry.get("id") or "")
        match = TILE_RE.search(name)
        if match:
            tiles.add(f"h{match.group(1)}v{match.group(2)}")
    ordered = sorted(tiles)
    if not ordered:
        raise SystemExit(f"CMR returned no MCD12Q2 tiles for {args.year}")

    group_size = max(1, args.group_size, math.ceil(len(ordered) / args.max_groups))
    include = []
    for index in range(0, len(ordered), group_size):
        group = ordered[index : index + group_size]
        tile_text = ",".join(group)
        digest = hashlib.sha1(tile_text.encode()).hexdigest()[:10]
        include.append(
            {
                "id": f"m{len(include):03d}",
                "tiles": tile_text,
                "asset": f"modis_{digest}",
            }
        )

    payload = {
        "include": include,
        "tileCount": len(ordered),
        "groupCount": len(include),
        "groupSize": group_size,
        "referenceYear": args.year,
    }
    text = json.dumps(payload, separators=(",", ":"))
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(text)
    print(text)


if __name__ == "__main__":
    main()

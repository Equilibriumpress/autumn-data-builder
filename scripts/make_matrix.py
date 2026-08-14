from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lat-min", type=int, default=-60)
    parser.add_argument("--lat-max", type=int, default=80)
    parser.add_argument("--step", type=int, default=20)
    args = parser.parse_args()

    include = []
    index = 0
    for min_lat in range(args.lat_min, args.lat_max, args.step):
        max_lat = min(args.lat_max, min_lat + args.step)
        for min_lon in range(-180, 180, args.step):
            max_lon = min(180, min_lon + args.step)
            include.append(
                {
                    "id": f"s{index:03d}",
                    "min_lat": min_lat,
                    "max_lat": max_lat,
                    "min_lon": min_lon,
                    "max_lon": max_lon,
                }
            )
            index += 1

    print(json.dumps({"include": include}, separators=(",", ":")))


if __name__ == "__main__":
    main()

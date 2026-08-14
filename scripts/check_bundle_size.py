from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail if foliage.sqlite is too large for the configured app bundle budget")
    parser.add_argument("database", type=Path)
    parser.add_argument("--max-mib", type=float, default=95.0)
    args = parser.parse_args()

    size = args.database.stat().st_size
    mib = size / 1024 / 1024
    print(f"foliage.sqlite: {mib:.2f} MiB (limit {args.max_mib:.2f} MiB)")
    if mib > args.max_mib:
        raise SystemExit(
            f"Production database is {mib:.2f} MiB, above the {args.max_mib:.2f} MiB bundle gate. "
            "Increase cell_degrees or keep the database as a downloadable release asset instead of committing it."
        )


if __name__ == "__main__":
    main()

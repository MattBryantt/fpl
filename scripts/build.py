#!/usr/bin/env python3
"""Freeze the board into a directory any static host can serve."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplkit import site, snapshot  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist")
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--horizon", type=int, default=snapshot.SNAPSHOT_HORIZON)
    parser.add_argument("--start-gw", type=int, default=None)
    parser.add_argument("--recency", type=float, default=0.0)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    out = Path(args.out)
    snapshot_path = Path(args.snapshot) if args.snapshot else None
    variants = {}

    if snapshot_path is None or not snapshot_path.exists():
        written, _ = snapshot.write(
            horizon=args.horizon, start_gw=args.start_gw,
            recency=args.recency, force_refresh=args.refresh)
        snapshot_path = Path(written)
        for pct in site.LAST_SEASON_STEPS:
            if pct != 25:
                variants[pct], _ = snapshot.write(
                    path=snapshot_path.with_name(f"snapshot-prev-{pct}.json"),
                    horizon=args.horizon, start_gw=args.start_gw,
                    recency=args.recency, previous=pct / 100)

    counts = site.build(out, snapshot_path, variants)
    print(f"{out}: {counts['pages']} pages, {counts['assets']} assets, "
          f"{counts['players']} players over {counts['gameweeks']} gameweeks")


if __name__ == "__main__":
    main()

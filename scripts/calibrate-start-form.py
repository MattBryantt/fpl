#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplkit.cache import cached_json          # noqa: E402
from fplkit.sources import history            # noqa: E402

MIN_HISTORY = 6
MIN_GAMEWEEKS = 12
LEADS = range(1, 9)


def load(season: str) -> list[np.ndarray]:
    url = f"{history.BASE}/{season}/players_raw.csv"
    raw = pd.read_csv(io.StringIO(cached_json(
        "history", url, lambda: history._fetch_csv(url), history.TTL, False)))

    df = history.gameweek_history(season)
    if df.empty:
        raise SystemExit(f"no archive rows for {season} — is the season published yet?")

    df = df.merge(raw[["code", "team", "element_type"]].drop_duplicates("code"),
                  on="code", how="left")
    df = df[df["team"].notna() & (df["element_type"] != 1)]
    df["started"] = (df["starts"] > 0).astype(float)
    df = df.sort_values(["code", "gw"])
    return [g["started"].to_numpy(dtype=float)
            for _, g in df.groupby("code") if len(g) >= MIN_GAMEWEEKS]


def weighted_rate(past: np.ndarray, half_life: float | None) -> float:
    if half_life is None:
        return float(past.mean())
    w = 0.5 ** (np.arange(len(past))[::-1] / half_life)
    return float((past * w).sum() / w.sum())


def paired(seqs: list[np.ndarray], lead: int,
           half_lives: list[float | None]) -> tuple[dict, np.ndarray]:
    out = {hl: [] for hl in half_lives}
    actual = []
    for s in seqs:
        for i in range(MIN_HISTORY, len(s) - lead + 1):
            past = s[:i]
            for hl in half_lives:
                out[hl].append(weighted_rate(past, hl))
            actual.append(s[i + lead - 1])
    return ({hl: np.array(v) for hl, v in out.items()}, np.array(actual))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default="2025-26")
    args = parser.parse_args()

    seqs = load(args.season)
    print(f"{args.season}: {len(seqs)} outfield players with {MIN_GAMEWEEKS}+ gameweeks\n")

    sweep = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 14.0, None]
    preds, actual = paired(seqs, 1, sweep)
    print("=== one gameweek ahead: Brier score by half-life ===")
    for hl in sweep:
        label = "flat season rate" if hl is None else f"half-life {hl:g}"
        print(f"  {label:>17}: {((preds[hl] - actual) ** 2).mean():.5f}")
    print(f"  ({len(actual)} predictions)\n")

    print("=== best blend weight on the recent rate, by lead ===")
    print("  lead   best w   Brier(blend)   Brier(flat)   improvement")
    grid = np.linspace(0.0, 1.4, 141)
    weights = []
    for lead in LEADS:
        preds, actual = paired(seqs, lead, [4.0, None])
        flat, recent = preds[None], preds[4.0]
        scores = [(((flat + w * (recent - flat)).clip(0, 1) - actual) ** 2).mean()
                  for w in grid]
        best = int(np.argmin(scores))
        flat_brier = ((flat - actual) ** 2).mean()
        weights.append(grid[best])
        print(f"  {lead:>4}   {grid[best]:>6.2f}   {scores[best]:>12.5f}   "
              f"{flat_brier:>11.5f}   {100 * (1 - scores[best] / flat_brier):>9.1f}%")

    w = np.array(weights)
    decay = float((w[-1] / w[0]) ** (1.0 / (len(w) - 1)))
    print(f"\n  fitted:  w_k = {w[0]:.3f} * {decay:.3f} ** (k - 1)")
    print("           START_FORM_WEIGHT      = %.2f" % w[0])
    print("           START_FORM_DECAY       = %.3f" % decay)
    print("           START_FORM_HALF_LIFE   = 4  (see the sweep above, and the note in model.py)")
    predicted = w[0] * decay ** np.arange(len(w))
    print("  residuals vs the measured weights: "
          + " ".join(f"{d:+.3f}" for d in (predicted - w)))

    print("\n=== next-match rate for players with a spotless recent record ===")
    preds, actual = paired(seqs, 1, [4.0])
    recent = preds[4.0]
    for lo in (0.90, 0.95, 0.99, 1.0):
        mask = recent >= lo
        if mask.sum():
            print(f"  recent rate >= {lo:.2f}: started next {actual[mask].mean():.4f}"
                  f"   n={mask.sum()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

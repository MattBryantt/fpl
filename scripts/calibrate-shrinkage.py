"""Measure how much of an attacking rate carries into the next season.

The shrinkage prior decides the model's whole behaviour at the top of the table.
`_shrink` blends a player's own rate with the positional average at weight
`M / (M + k)`, so k is the number that says how much of a season to believe --
and it had been set to 1200 for every rate from a recalled figure rather than
from the three seasons of Understat sitting in the cache.

This fits k directly, by minimising the squared error of `w.own + (1-w).prior`
against what each player actually did the following season. That is not a
diagnostic of the shrinkage; it *is* the shrinkage, solved rather than assumed.

    python scripts/calibrate-shrinkage.py

Re-run it when a season ends. If the numbers have moved, move
NPXG_PRIOR_MINUTES and XA_PRIOR_MINUTES in fplkit/model.py to match -- they are
constants in the code but measurements in origin, and the comment beside them
should keep saying which.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplkit.model import (MOVER_PRIOR_MULTIPLIER, NPXG_PRIOR_MINUTES,  # noqa: E402
                          XA_PRIOR_MINUTES)
from fplkit.sources import understat  # noqa: E402

SEASONS = ("2023", "2024", "2025")
MIN_FIRST = 300     # enough of a first season to be evidence of anything
MIN_SECOND = 900    # enough of a second season to be worth scoring against
BOOTSTRAP = 500


def load() -> pd.DataFrame:
    """Consecutive season pairs for the same player, joined on Understat's id."""
    seasons = {}
    for season in SEASONS:
        df = understat.player_stats(season=season)
        df["npxg_per90"] = df["npxG"] / (df["us_minutes"] / 90.0)
        df["xa_per90"] = df["xA"] / (df["us_minutes"] / 90.0)
        df["shots_per90"] = df["shots"] / (df["us_minutes"] / 90.0)
        df["xg_per_shot"] = df["npxG"] / df["shots"].replace(0, np.nan)
        seasons[season] = df

    pairs = [seasons[a].merge(seasons[b], on="us_id", suffixes=("_1", "_2"))
             for a, b in zip(SEASONS, SEASONS[1:])]
    both = pd.concat(pairs, ignore_index=True)
    return both[(both["us_minutes_1"] >= MIN_FIRST)
                & (both["us_minutes_2"] >= MIN_SECOND)]


def fit_k(own: np.ndarray, nxt: np.ndarray, minutes: np.ndarray) -> float:
    """The prior weight that best predicts next season from this one."""
    prior = float(np.average(own, weights=minutes))

    def loss(k: float) -> float:
        w = minutes / (minutes + max(k, 1.0))
        return float(((w * own + (1 - w) * prior - nxt) ** 2).sum())

    return float(minimize_scalar(loss, bounds=(10, 12000), method="bounded").x)


def report(frame: pd.DataFrame, field: str, current: float) -> None:
    own = frame[f"{field}_1"].to_numpy(float)
    nxt = frame[f"{field}_2"].to_numpy(float)
    minutes = frame["us_minutes_1"].to_numpy(float)
    keep = ~(np.isnan(own) | np.isnan(nxt))
    own, nxt, minutes = own[keep], nxt[keep], minutes[keep]

    point = fit_k(own, nxt, minutes)
    slope = float(np.polyfit(own, nxt, 1)[0])
    rng = np.random.default_rng(5)
    draws = [fit_k(own[i], nxt[i], minutes[i]) for i in
             (rng.integers(0, len(own), len(own)) for _ in range(BOOTSTRAP))]
    lo, hi = np.percentile(draws, [2.5, 97.5])

    full = 3000.0
    print(f"  {field:14s} k = {point:5.0f}  95% CI [{lo:4.0f}, {hi:4.0f}]"
          f"   slope {slope:.2f}"
          f"   weight@3000min {full / (full + point):.2f}"
          + (f"   (model uses {current:.0f} -> {full / (full + current):.2f})"
             if current else ""))


def main() -> int:
    both = load()
    print(f"{len(both)} player-seasons with {MIN_FIRST}+ minutes behind them "
          f"and {MIN_SECOND}+ minutes to score against")
    print(f"seasons: {' '.join(SEASONS)}")
    print()

    print("How much of a rate carries into next season")
    report(both, "npxg_per90", NPXG_PRIOR_MINUTES)
    report(both, "xa_per90", XA_PRIOR_MINUTES)
    print()

    # Splitting npxG into volume x conversion looks promising and is not.
    print("Is npxG/90 worth splitting into shot volume and conversion?")
    report(both, "shots_per90", 0)
    report(both, "xg_per_shot", 0)
    truth = both["npxg_per90_2"].to_numpy(float)
    rate_only = float(np.corrcoef(both["npxg_per90_1"], truth)[0, 1])
    volume = both["shots_per90_1"] * both["xg_per_shot_1"].mean()
    blended = 0.5 * both["npxg_per90_1"] + 0.5 * volume
    print(f"    predicting next season: rate alone r={rate_only:.3f}, "
          f"volume alone r={float(np.corrcoef(volume, truth)[0, 1]):.3f}, "
          f"half and half r={float(np.corrcoef(blended, truth)[0, 1]):.3f}")
    print("    -> conversion is noisy, but the product already carries what it")
    print("       is worth. Splitting them buys nothing, so the model does not.")
    print()

    # The mover penalty, which this sample is too small to settle.
    moved = (both["us_team_list_1"].str[-1] != both["us_team_list_2"].str[-1])
    print(f"Club changers (n={int(moved.sum())}), against a charged "
          f"{MOVER_PRIOR_MULTIPLIER}x prior:")
    for label, subset in (("stayed", both[~moved]), ("moved", both[moved])):
        own = subset["npxg_per90_1"].to_numpy(float)
        nxt = subset["npxg_per90_2"].to_numpy(float)
        minutes = subset["us_minutes_1"].to_numpy(float)
        print(f"  {label:8s} n={len(subset):4d}  k = {fit_k(own, nxt, minutes):5.0f}")
    print("  Too few movers to act on, and Understat's club list is a poor way to")
    print("  spot one. Left alone deliberately -- see MOVER_PRIOR_MULTIPLIER.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Where START_FORM_* came from, and how to check them against a new season.

A start rate over a season answers "what share of matches did he start". The
model needs "does he start the next one", and the two come apart for anyone
whose situation changed mid-season -- which is most players at least once.

This measures the gap honestly. Every prediction for gameweek t is built only
from gameweeks strictly before t, so a half-life is chosen out of sample rather
than fitted to the answer it is being scored against. Two questions:

  1. How much does recency weighting help, one gameweek ahead?
  2. How fast does that help decay as the lead lengthens?

The second is the one that matters for a plan. If the answer were "it doesn't",
the recent rate would simply be better and the long-run one could go. It does
decay, geometrically, and `model.start_form_weight` is that decay written down.

    python scripts/calibrate-start-form.py [--season 2025-26]

Prints a table; changes nothing. The constants live in fplkit/model.py and are
meant to be moved by hand, after looking.
"""
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

# Gameweeks of history a player needs before any of his rows are scored. Below
# this the "recent" and "flat" rates are the same number and the comparison is
# empty rather than favourable.
MIN_HISTORY = 6
MIN_GAMEWEEKS = 12   # a player has to be around long enough to be predicted at
LEADS = range(1, 9)


def load(season: str) -> list[np.ndarray]:
    """One 0/1 started-or-not sequence per outfield player, in gameweek order."""
    url = f"{history.BASE}/{season}/players_raw.csv"
    raw = pd.read_csv(io.StringIO(cached_json(
        "history", url, lambda: history._fetch_csv(url), history.TTL, False)))

    df = history.gameweek_history(season)
    if df.empty:
        raise SystemExit(f"no archive rows for {season} — is the season published yet?")

    df = df.merge(raw[["code", "team", "element_type"]].drop_duplicates("code"),
                  on="code", how="left")
    # Keepers are excluded throughout. A club picks one, essentially never
    # rotates him, and including them would flatter every number here.
    df = df[df["team"].notna() & (df["element_type"] != 1)]
    df["started"] = (df["starts"] > 0).astype(float)
    df = df.sort_values(["code", "gw"])
    return [g["started"].to_numpy(dtype=float)
            for _, g in df.groupby("code") if len(g) >= MIN_GAMEWEEKS]


def weighted_rate(past: np.ndarray, half_life: float | None) -> float:
    """Start rate over `past`, halving the weight every `half_life` gameweeks."""
    if half_life is None:
        return float(past.mean())
    w = 0.5 ** (np.arange(len(past))[::-1] / half_life)
    return float((past * w).sum() / w.sum())


def paired(seqs: list[np.ndarray], lead: int,
           half_lives: list[float | None]) -> tuple[dict, np.ndarray]:
    """Predictions at each half-life, and the outcomes they are scored against."""
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

    # --- 1. one gameweek ahead ---------------------------------------------
    sweep = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 14.0, None]
    preds, actual = paired(seqs, 1, sweep)
    print("=== one gameweek ahead: Brier score by half-life ===")
    for hl in sweep:
        label = "flat season rate" if hl is None else f"half-life {hl:g}"
        print(f"  {label:>17}: {((preds[hl] - actual) ** 2).mean():.5f}")
    print(f"  ({len(actual)} predictions)\n")

    # --- 2. the decay, which is the part the horizon knob rides on ---------
    # At each lead, find the blend weight w in
    #     p = flat + w * (recent - flat)
    # that minimises Brier. w falling with the lead is the measured form of
    # "a nailed starter is more obviously nailed next week than in two months".
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

    # Anchored on the first lead rather than least-squares through all eight.
    # A log-linear fit over the whole range trades accuracy at k=1 for accuracy
    # at k=8, and k=1 is both the largest weight and the lead that most decides
    # a squad -- so the curve is pinned there and the ratio read off the ends.
    w = np.array(weights)
    decay = float((w[-1] / w[0]) ** (1.0 / (len(w) - 1)))
    print(f"\n  fitted:  w_k = {w[0]:.3f} * {decay:.3f} ** (k - 1)")
    print("           START_FORM_WEIGHT      = %.2f" % w[0])
    print("           START_FORM_DECAY       = %.3f" % decay)
    print("           START_FORM_HALF_LIFE   = 4  (see the sweep above, and the note in model.py)")
    predicted = w[0] * decay ** np.arange(len(w))
    print("  residuals vs the measured weights: "
          + " ".join(f"{d:+.3f}" for d in (predicted - w)))

    # --- 3. the ceiling ----------------------------------------------------
    # MAX_P_START is 0.95. This is the out-of-sample check on it: what actually
    # happens next to players whose recent record is spotless. Note it comes in
    # *below* the ceiling -- some of those players are about to get injured, and
    # the model handles that separately with the survival curve, so 0.95 is a
    # cap on the fit-and-selected case rather than on this number.
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

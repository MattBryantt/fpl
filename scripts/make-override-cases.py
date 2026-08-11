"""Score a spread of overrides with the Python model, for the JS port to match.

Deliberately not a handful of round numbers. The cases sweep every overridable
field, both the outright and the `_mult` form, across players from each position
and from both ends of the minutes distribution -- a port can agree on a typical
midfielder and still be wrong for the reserve keeper, the penalty taker or the
player whose p_start override drags the whole minutes family with it. Per-match
overrides get the same treatment, including the combinations where a one-week
opinion contradicts the season-level one it is layered on.

Also asserts that `apply_fields` (single player, used per fixture) agrees with
`apply_overrides` (whole table). Those are two implementations of one rule, and
the per-gameweek path depends on the first.

    python scripts/make-override-cases.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from fplkit import server
from fplkit.model import (OVERRIDABLE, apply_fields, apply_overrides,
                          reproject_player)
from fplkit.snapshot import SNAPSHOT_HORIZON

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scripts" / "override-cases.json"


def check_apply_fields_matches_apply_overrides(players: pd.DataFrame) -> None:
    """The single-player and whole-table override paths must agree exactly."""
    variants = [
        {"p_start": 0.42}, {"exp_minutes": 90.0}, {"npxg_per90_mult": 1.7},
        {"p_start": 0.9, "npxg_per90": 0.8}, {"exp_minutes": 20.0, "p_start": 0.95},
        {"price": 4.0, "penalties_order": 1}, {"saves_per90_mult": 0.3},
        {"p_start": 0.0}, {"dc_per90": 22.0, "bonus_per90_mult": 2.5},
        # The shift, on its own and against each of the other two.
        {"mins_if_start": 60.0}, {"p_start": 0.95, "mins_if_start": 55.0},
        {"mins_if_start": 90.0, "exp_minutes": 40.0}, {"exp_minutes": 45.0},
    ]
    fields = list(OVERRIDABLE) + ["p_sub", "p_play", "p60"]
    problems = []
    for _, row in players.head(40).iterrows():
        for override in variants:
            single = apply_fields(row, override)
            table = apply_overrides(
                row.to_frame().T.infer_objects(),
                pd.DataFrame([{"fpl_id": row["fpl_id"], **override}])).iloc[0]
            for field in fields:
                if field not in row.index:
                    continue
                a, b = float(single[field]), float(table[field])
                if abs(a - b) > 1e-12:
                    problems.append(f"{row['web_name']} {override} {field}: {a} vs {b}")
    if problems:
        raise SystemExit("apply_fields disagrees with apply_overrides:\n  "
                         + "\n  ".join(problems[:10]))
    print(f"apply_fields == apply_overrides over {40 * len(variants)} combinations")


def main() -> None:
    projection = server._get_projection(None, SNAPSHOT_HORIZON)
    players = projection.players
    check_apply_fields_matches_apply_overrides(players)

    # One representative per position from each of three minutes bands, so the
    # sweep covers nailed starters, rotation risks and players with no history.
    picks: list[int] = []
    for pos in ("GKP", "DEF", "MID", "FWD"):
        group = players[players["pos"] == pos]
        for lo, hi in ((0.7, 1.01), (0.2, 0.7), (-0.01, 0.2)):
            band = group[(group["p_play"] >= lo) & (group["p_play"] < hi)]
            if len(band):
                picks.append(int(band.iloc[0]["fpl_id"]))
    # And the designated penalty takers, who take a branch nobody else does.
    takers = players[players["penalties_order"] == 1]
    picks += [int(i) for i in takers["fpl_id"].head(3)]

    gameweeks = projection.horizon
    first, mid, last = gameweeks[0], gameweeks[len(gameweeks) // 2], gameweeks[-1]

    cases = []
    for fpl_id in dict.fromkeys(picks):
        row = players[players["fpl_id"] == fpl_id].iloc[0]
        variants: list[dict] = [{}]
        for field, (low, high) in OVERRIDABLE.items():
            if field not in row.index:
                continue
            span = high - low
            variants.append({field: round(low + span * 0.25, 4)})
            variants.append({field: round(low + span * 0.75, 4)})
            variants.append({f"{field}_mult": 1.6})
            variants.append({f"{field}_mult": 0.4})
        # Combinations, because the minutes family couples fields together and a
        # field-at-a-time sweep would never catch it.
        variants += [
            {"p_start": 0.95, "npxg_per90": 0.9},
            {"exp_minutes": 90.0, "p_start": 0.1},   # exp_minutes must win
            {"p_start": 0.0},                        # every scenario probability zero
            # The three minutes fields against each other. exp_minutes is solved
            # against whatever the two before it left, and it prefers to spend
            # the shift before it touches the start probability -- so the first
            # of these must move mins_if_start alone and the second must run out
            # of shift and fall back to raising p_start.
            {"p_start": 0.9, "exp_minutes": 45.0},
            {"p_start": 0.05, "exp_minutes": 75.0},
            {"mins_if_start": 55.0},                 # nailed, but hooked on the hour
            {"mins_if_start": 90.0, "p_start": 0.2},  # rare starter, full shift
            {"mins_if_start": 90.0, "exp_minutes": 30.0},  # exp_minutes still wins
            {"penalties_order": 1, "p_start": 1.0},
            {"saves_per90": 6.0, "dc_per90": 20.0},
            {"price": 3.5, "bonus_per90_mult": 3.0},  # _mult must clip at the cap
        ]
        # Per-match opinions, including ones that contradict the season-level
        # override they sit on top of.
        variants += [
            {"gw": {str(first): {"p_start": 0.0}}},                     # rested
            {"gw": {str(mid): {"p_start": 1.0, "npxg_per90": 1.2}}},    # one big week
            {"gw": {str(first): {"p_start": 0.0}, str(last): {"p_start": 1.0}}},
            {"p_start": 0.1, "gw": {str(mid): {"p_start": 0.95}}},      # match beats season
            {"p_start": 0.95, "gw": {str(mid): {"exp_minutes": 30.0}}},
            {"npxg_per90_mult": 1.5, "gw": {str(last): {"npxg_per90_mult": 0.5}}},
            {"gw": {str(gw): {"p_start": 0.5} for gw in gameweeks}},    # every week
        ]

        for overrides in variants:
            result = reproject_player(projection, fpl_id, overrides)
            cases.append({
                "fpl_id": fpl_id,
                "overrides": overrides,
                "gw": [round(v, 6) for v in result["gw"]],
                "inputs": {k: round(v, 6) for k, v in result["inputs"].items()
                           if v is not None},
            })

    OUT.write_text(json.dumps(cases))
    per_match = sum(1 for c in cases if "gw" in c["overrides"])
    print(f"{OUT}  {len(cases)} cases over {len(set(c['fpl_id'] for c in cases))} "
          f"players ({per_match} per-match)")


if __name__ == "__main__":
    main()

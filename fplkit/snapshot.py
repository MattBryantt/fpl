"""Freeze a projection into a file the browser can work from with nothing behind it.

The board's front end already did most of its arithmetic locally -- best XI,
legality, the budget meter, every chart -- because a projection is expensive and
a click is not. This takes that the rest of the way: everything the optimiser
and the stat editor need travels in one JSON file, so the page can pick a squad,
recompute an override and re-solve the MILP on a phone with the laptop shut.

What cannot travel is the projection itself. It needs pandas, three network
sources and a Nelder-Mead fit per fixture, and its inputs change when odds move.
So the split is: the laptop projects, the phone consumes. `generated_at` is
carried so the board can say how stale it is rather than quietly implying it is
live.

Two things are deliberately *not* frozen at export time:

  * **Horizon.** The snapshot carries per-gameweek points out to its full
    horizon and the browser sums however many it wants. Shortening a horizon is
    a truncation, not a re-projection.
  * **Half-life.** The decay and survival curves are cheap closed forms, so the
    browser applies them. Only `hazard` has to come along, because it needs a
    birth date the browser has no reason to hold.

Recency weighting is the exception that cannot be deferred: it changes the
underlying rates, so a snapshot is taken at one recency setting and the board
disables the slider until the next sync.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import config
from .config import (
    CHIP_HOLD_VALUE,
    CHIP_LABELS,
    CHIPS,
    DEFAULT_BENCH_SLOT_WEIGHTS,
    DEFAULT_BUDGET,
    MAX_PER_CLUB,
    OUT_DIR,
    SQUAD_BY_POS,
    SQUAD_SIZE,
    XI_MAX_BY_POS,
    XI_MIN_BY_POS,
    XI_SIZE,
)
from . import model
from .model import ESTABLISHED_SHARE, OVERRIDABLE, project
from .planning import (DEFAULT_HALF_LIFE, apply_plan_weighting, injury_hazard,
                       weighted_points)
from .sources import fpl_api
from .transfers import (
    CAPTAIN_CANDIDATES,
    FT_VALUE,
    FT_VALUE_BY_STATE,
    IDLE_MOVE_PENALTY,
    POOL_BY_POS,
    TRANSFER_HALF_LIFE,
)

SNAPSHOT_PATH = OUT_DIR / "snapshot.json"

# The horizon to freeze at. Longer than anyone plans to, because the browser can
# only ever truncate: a snapshot taken at 8 can never answer a question about 12.
SNAPSHOT_HORIZON = 12

# Every input `_player_fixture_points` reads. Wider than OVERRIDABLE, which is
# only the subset a user may argue with -- p_sub and p60 are derived, never
# typed, but the scoring code needs them and the browser cannot re-derive p_sub
# from anything it holds.
SCORING_FIELDS = [
    "p_start", "mins_if_start", "p_sub", "p_play", "p60", "exp_minutes",
    "npxg_per90", "xa_per90", "dc_per90", "bonus_per90", "saves_per90",
    "yellow_per90", "penalties_order", "price", "fpl_evidence_weight",
]

# The unshrunk rate each of these started from -- last season's actual per-90
# numbers, before shrinkage pulled them toward the positional average. Only
# the rate stats have one; there is no "raw" price or penalty order.
RAW_FIELDS = [
    "npxg_per90", "xa_per90", "dc_per90", "bonus_per90", "saves_per90",
    "yellow_per90",
]


def _rules() -> dict[str, Any]:
    """The scoring constants, read out of config so the JS cannot drift from it.

    Hand-copying these into JavaScript would work exactly until the first time
    somebody changed a scoring rule in one place. Shipping them means a rule
    change reaches the phone through the next sync instead of through memory.
    """
    return {
        "GOAL_POINTS": config.GOAL_POINTS,
        "CLEAN_SHEET_POINTS": config.CLEAN_SHEET_POINTS,
        "ASSIST_POINTS": config.ASSIST_POINTS,
        "APPEARANCE_POINTS": config.APPEARANCE_POINTS,
        "APPEARANCE_60_POINTS": config.APPEARANCE_60_POINTS,
        "SAVE_POINTS": config.SAVE_POINTS,
        "YELLOW_CARD_POINTS": config.YELLOW_CARD_POINTS,
        "PENALTY_MISS_POINTS": config.PENALTY_MISS_POINTS,
        "DEF_CONTRIB_POINTS": config.DEF_CONTRIB_POINTS,
        "DEF_CONTRIB_THRESHOLD": config.DEF_CONTRIB_THRESHOLD,
        "DC_DISPERSION": config.DC_DISPERSION,
        "DC_DISPERSION_FLOOR": config.DC_DISPERSION_FLOOR,
        "PENALTY_GOAL_SHARE": config.PENALTY_GOAL_SHARE,
        "PENALTY_CONVERSION": config.PENALTY_CONVERSION,
        "ASSUMED_START_MINUTES": config.ASSUMED_START_MINUTES,
        "ASSUMED_SUB_MINUTES": config.ASSUMED_SUB_MINUTES,
        "P60_GIVEN_START": config.P60_GIVEN_START,
        # The curve that turns a shift length into P(reaches 60 | starts), so
        # the phone can price a hooked-on-the-hour starter the same way.
        "P60_MIDPOINT_MINUTES": config.P60_MIDPOINT_MINUTES,
        "P60_SLOPE_MINUTES": config.P60_SLOPE_MINUTES,
        # The minutes pool, so the browser can put a club back to eleven
        # starters after an override the same way the model does.
        "MAX_P_START": model.MAX_P_START,
        "MAX_MINS_IF_START": model.MAX_MINS_IF_START,
        "MAX_MINUTES_SCALE": model.MAX_MINUTES_SCALE,
        "XI_OUTFIELD": model.XI_OUTFIELD,
        "OVERRIDABLE": {k: list(v) for k, v in OVERRIDABLE.items()},
        "SQUAD_BY_POS": SQUAD_BY_POS,
        "XI_MIN_BY_POS": XI_MIN_BY_POS,
        "XI_MAX_BY_POS": XI_MAX_BY_POS,
        "SQUAD_SIZE": SQUAD_SIZE,
        "XI_SIZE": XI_SIZE,
        "BENCH_SLOT_PROFILE": {str(k): v for k, v in config.BENCH_SLOT_PROFILE.items()},
        "MAX_PER_CLUB": MAX_PER_CLUB,
        "DEFAULT_BUDGET": DEFAULT_BUDGET,
        "DEFAULT_BENCH_SLOT_WEIGHTS": {str(k): v
                                       for k, v in DEFAULT_BENCH_SLOT_WEIGHTS.items()},
        "CHIPS": list(CHIPS),
        "CHIP_LABELS": dict(CHIP_LABELS),
        "CHIP_HOLD_VALUE": dict(CHIP_HOLD_VALUE),
        "TRANSFER_HALF_LIFE": TRANSFER_HALF_LIFE,
        # The transfer-and-chip planner's own constants -- see transfers.py's
        # module docstring for why each exists. Carried the same way as every
        # other rule here: nothing hand-copied into transfers.js.
        "POOL_BY_POS": dict(POOL_BY_POS),
        "CAPTAIN_CANDIDATES": CAPTAIN_CANDIDATES,
        "FT_VALUE": FT_VALUE,
        "FT_VALUE_BY_STATE": {str(k): v for k, v in FT_VALUE_BY_STATE.items()},
        "IDLE_MOVE_PENALTY": IDLE_MOVE_PENALTY,
        "MAX_FREE_TRANSFERS": config.MAX_FREE_TRANSFERS,
        "HIT_COST": config.HIT_COST,
        "TRANSFER_FRICTION": config.TRANSFER_FRICTION,
        "BANK_VALUE": config.BANK_VALUE,
        "FREE_TRANSFERS_PER_GW": config.FREE_TRANSFERS_PER_GW,
    }


# Display fields are rounded hard -- nobody reads the seventh decimal of an
# ownership percentage, and there are thousands of them. The handful of numbers
# the scoring model actually computes *from* are not: rounding a lambda or a
# per-90 rate puts a floor on how closely the browser can reproduce the
# projection, and at 6dp that floor was ~3e-6 points per gameweek. Ten decimals
# costs about 2 KB gzipped over the whole file and drops it below 1e-9.
DISPLAY_DP = 6
INPUT_DP = 10


def _num(value: Any, default: float | None = 0.0, dp: int = DISPLAY_DP) -> Any:
    """JSON has no NaN. Anything that is not a finite number becomes `default`."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if np.isnan(number) or np.isinf(number):
        return default
    return round(number, dp)


def _teams(force_refresh: bool = False) -> dict[str, Any]:
    """Club name -> short name and FPL club code.

    The code is the one the shirt images are filed under, and it is not the team
    id: Arsenal are team 1 and code 3, and the two diverge again every time a
    club is promoted. Keyed by the club name the player rows already carry, so
    the pitch can find a shirt without a second join.
    """
    return {
        str(team["name"]): {"short": str(team["short_name"]),
                            "code": int(team["code"])}
        for team in fpl_api.bootstrap(force_refresh)["teams"]
    }


def build(horizon: int = SNAPSHOT_HORIZON, start_gw: int | None = None,
          recency: float = 0.0, force_refresh: bool = False) -> dict:
    """Run the projection and reduce it to what the browser needs."""
    projection = project(horizon=horizon, start_gw=start_gw,
                         recency_half_life=recency or None,
                         force_refresh=force_refresh)

    basis = projection.basis
    players = apply_plan_weighting(projection, DEFAULT_HALF_LIFE)
    raw, _ = weighted_points(projection, DEFAULT_HALF_LIFE)
    gameweeks = [int(gw) for gw in raw.columns]
    hazard = injury_hazard(players)

    # Expected clean sheets per gameweek, not just summed over the horizon. The
    # board lets you shorten the horizon without a sync, and that has to be a
    # truncation of something -- a single total for 12 gameweeks cannot answer
    # what the first 6 are worth.
    clean_sheets = (projection.per_fixture
                    .pivot_table(index="fpl_id", columns="gw",
                                 values="exp_clean_sheets", aggfunc="sum")
                    .reindex(columns=gameweeks).fillna(0.0))

    # Opponent labels for the tooltips, joined on a double gameweek.
    per_fixture = projection.per_fixture.copy()
    per_fixture["label"] = np.where(
        per_fixture["was_home"], per_fixture["opponent"] + " (H)",
        per_fixture["opponent"] + " (A)")
    opponents = (per_fixture.groupby(["fpl_id", "gw"])["label"]
                 .agg(lambda s: " + ".join(s)).unstack().reindex(columns=gameweeks))

    rows = []
    for position, player in enumerate(players.to_dict("records")):
        fpl_id = int(player["fpl_id"])
        if fpl_id in raw.index:
            per_gw = [_num(v, 0.0) for v in raw.loc[fpl_id, gameweeks]]
            cs_gw = [_num(v, 0.0) for v in clean_sheets.loc[fpl_id, gameweeks]]
        else:
            per_gw = [0.0 for _ in gameweeks]
            cs_gw = [0.0 for _ in gameweeks]
        labels = ([("" if pd.isna(v) else str(v))
                   for v in opponents.loc[fpl_id, gameweeks]]
                  if fpl_id in opponents.index else ["" for _ in gameweeks])

        row = {
            "id": fpl_id,
            "name": str(player["web_name"]),
            "full_name": str(player["full_name"]),
            "pos": str(player["pos"]),
            "team": str(player["team"]),
            "team_short": str(player["team_short"]),
            "owned": _num(pd.to_numeric(player["selected_by_percent"], errors="coerce")),
            # Last completed season's points per game, straight from the FPL
            # API. Pre-season that is exactly what bootstrap-static serves, and
            # it is the number most people actually carry in their heads -- so
            # the board shows it next to the model's xPPG rather than asking you
            # to hold one of them in memory while reading the other.
            "ppg": _num(player.get("points_per_game")),
            "minutes_last": _num(player.get("minutes"), dp=0),
            # The season total as well, because PPG alone cannot be put on the
            # same footing as xPPG: one divides by appearances, the other by
            # fixtures, and only total/38 bridges them.
            "pts_last": _num(player.get("total_points"), dp=0),
            "price_change": _num(player.get("exp_price_change"), None),
            "confidence": str(player.get("confidence", "")),
            "recency": _num(player.get("recency"), None),
            # The two halves p_start was blended from. Not used in scoring -- the
            # blend already happened -- but shown in the editor, because "the
            # model has him at 0.41 and he has started the last six" is the
            # single most useful thing to know before overriding him.
            "start_long_run": _num(player.get("start_long_run"), None, dp=INPUT_DP),
            "start_recent": _num(player.get("start_recent"), None, dp=INPUT_DP),
            "moved": bool(player.get("moved_club", False)),
            "previous_club": str(player.get("previous_club", "") or ""),
            "status": str(player["status"]),
            "news": str(player["news"] or ""),
            "gw": per_gw,
            "cs": cs_gw,
            "opp": labels,
            "hazard": _num(hazard.iloc[position], dp=INPUT_DP),
        }
        # The scoring inputs, flat on the row: the browser treats a player as the
        # thing you feed to playerFixturePoints, exactly as the Python does.
        for field in SCORING_FIELDS:
            row[field] = _num(player.get(field), dp=INPUT_DP)
        for field in RAW_FIELDS:
            row[f"raw_{field}"] = _num(player.get(f"raw_{field}"), None, dp=INPUT_DP)
        rows.append(row)

    fixtures = [
        {"gw": int(f["gw"]),
         "home_team": str(f["home_team"]), "away_team": str(f["away_team"]),
         "lam_home": _num(f["lam_home"], dp=INPUT_DP),
         "lam_away": _num(f["lam_away"], dp=INPUT_DP)}
        for _, f in projection.fixtures.iterrows()
        if int(f["gw"]) in gameweeks
    ]

    strength = {
        str(row["team"]): {"npxg_per_match": _num(row["npxg_per_match"], dp=INPUT_DP),
                           "xgc_per_match": _num(row["xgc_per_match"], dp=INPUT_DP)}
        for _, row in projection.strength.iterrows()
    }

    priced = [int(gw) for gw in gameweeks
              if bool(projection.fixtures.loc[projection.fixtures["gw"] == gw,
                                              "has_odds"].any())]

    # The single half-season set that applies to this window -- not the union
    # of both halves, which would erase the mid-season expiry that makes chip
    # timing urgent. The wildcard's start_event of 2 is the load-bearing entry:
    # it is what keeps a wildcard out of a gameweek-one plan.
    chip_windows = {chip: list(window) for chip, window in
                    fpl_api.chip_windows(gameweeks[0], force_refresh).items()}

    return {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "gameweeks": gameweeks,
        "players": rows,
        "fixtures": fixtures,
        "strength": strength,
        "teams": _teams(force_refresh),
        "rules": _rules(),
        "meta": {
            "start_gw": gameweeks[0],
            "horizon": len(gameweeks),
            "recency": recency,
            "odds_coverage": round(projection.odds_coverage, 3),
            "odds_note": projection.odds_note,
            "priced_gws": priced,
            "total_managers": fpl_api.total_managers(),
            # How far into the season the totals behind this snapshot are. The
            # board greys a thin PPG, and "thin" is a share of a full workload
            # rather than a fixed 900 minutes -- otherwise the moment a season
            # rolls over the board greys every player in the league and keeps
            # doing it until Christmas.
            "season_minutes": _num(basis.fpl_minutes if basis else None, None, dp=0),
            "preseason": bool(basis.preseason) if basis else None,
            "established_share": ESTABLISHED_SHARE,
            "chip_windows": chip_windows,
        },
    }


def write(path: Path | None = None, **kwargs) -> tuple[Path, int]:
    target = Path(path) if path else SNAPSHOT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build(**kwargs)
    target.write_text(json.dumps(payload, separators=(",", ":")))
    return target, target.stat().st_size

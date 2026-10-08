
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
    PRICE_POINT_CANDIDATES,
    TRANSFER_HALF_LIFE,
)

SNAPSHOT_PATH = OUT_DIR / "snapshot.json"

SNAPSHOT_HORIZON = 12

SCORING_FIELDS = [
    "p_start", "mins_if_start", "p_sub", "p_play", "p60", "exp_minutes",
    "npxg_per90", "xa_per90", "dc_per90", "bonus_per90", "saves_per90",
    "yellow_per90", "penalties_order", "price",
]

RAW_FIELDS = [
    "npxg_per90", "xa_per90", "dc_per90", "bonus_per90", "saves_per90",
    "yellow_per90",
]


def _rules() -> dict[str, Any]:
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
        "PENALTY_GOAL_SHARE": config.PENALTY_GOAL_SHARE,
        "PENALTY_CONVERSION": config.PENALTY_CONVERSION,
        "ASSUMED_START_MINUTES": config.ASSUMED_START_MINUTES,
        "ASSUMED_SUB_MINUTES": config.ASSUMED_SUB_MINUTES,
        "P60_GIVEN_START": config.P60_GIVEN_START,
        "P60_MIDPOINT_MINUTES": config.P60_MIDPOINT_MINUTES,
        "P60_SLOPE_MINUTES": config.P60_SLOPE_MINUTES,
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
        "POOL_BY_POS": dict(POOL_BY_POS),
        "PRICE_POINT_CANDIDATES": PRICE_POINT_CANDIDATES,
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


DISPLAY_DP = 6
INPUT_DP = 10


def _num(value: Any, default: float | None = 0.0, dp: int = DISPLAY_DP) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if np.isnan(number) or np.isinf(number):
        return default
    return round(number, dp)


def _per90(total: Any, minutes: Any) -> float | None:
    minutes = _num(minutes, 0.0)
    return None if not minutes else _num(_num(total, 0.0) / minutes * 90.0, None, dp=3)


def _seasons(player: dict, basis: Any) -> dict[str, Any]:
    understat = "npxG" if _num(player.get("us_minutes"), 0.0) > 0 else None
    def block(minutes, starts, matches, npxg, xa, us_minutes, dc, saves, bonus):
        minutes = _num(minutes, 0.0)
        return None if minutes <= 0 and _num(matches, 0.0) <= 0 else {
            "minutes": _num(minutes, dp=0), "starts": _num(starts, dp=0),
            "matches": _num(matches, dp=0),
            "npxg_per90": _per90(npxg, us_minutes), "xa_per90": _per90(xa, us_minutes),
            "dc_per90": _per90(dc, minutes), "saves_per90": _per90(saves, minutes),
            "bonus_per90": _per90(bonus, minutes),
        }
    last = block(player.get("prev_minutes"), player.get("prev_starts"),
                 player.get("prev_matches"), player.get("npxG"), player.get("xA"),
                 player.get("us_minutes"), player.get("prev_defensive_contribution"),
                 player.get("prev_saves"), player.get("prev_bonus"))
    matches = (basis.club_matches.get(player.get("team"), 0.0)
               if basis is not None and not basis.preseason else 0.0)
    us_now = _num(player.get("cur_us_minutes"), 0.0)
    now = block(player.get("minutes"), player.get("starts"), matches,
                player.get("cur_npxG") if us_now else
                _num(player.get("expected_goals"), 0.0) * (1 - config.PENALTY_GOAL_SHARE),
                player.get("cur_xA") if us_now else player.get("expected_assists"),
                us_now or player.get("minutes"),
                player.get("defensive_contribution"), player.get("saves"), player.get("bonus"))
    if basis is not None and basis.preseason:
        last = block(player.get("minutes"), player.get("starts"), 38,
                     player.get("npxG"), player.get("xA"), player.get("us_minutes"),
                     player.get("defensive_contribution"), player.get("saves"),
                     player.get("bonus"))
        now = None
    return {"now": now, "prev": last, "attack_source": understat and "understat" or "fpl"}


def _teams(force_refresh: bool = False) -> dict[str, Any]:
    return {
        str(team["name"]): {"short": str(team["short_name"]),
                            "code": int(team["code"])}
        for team in fpl_api.bootstrap(force_refresh)["teams"]
    }


def build(horizon: int = SNAPSHOT_HORIZON, start_gw: int | None = None,
          recency: float = 0.0, previous: float = 0.25,
          force_refresh: bool = False) -> dict:
    projection = project(horizon=horizon, start_gw=start_gw,
                         recency_half_life=recency or None,
                         previous_weight=previous,
                         force_refresh=force_refresh)

    basis = projection.basis
    players = apply_plan_weighting(projection, DEFAULT_HALF_LIFE)
    raw, _ = weighted_points(projection, DEFAULT_HALF_LIFE)
    gameweeks = [int(gw) for gw in raw.columns]
    hazard = injury_hazard(players)

    clean_sheets = (projection.per_fixture
                    .pivot_table(index="fpl_id", columns="gw",
                                 values="exp_clean_sheets", aggfunc="sum")
                    .reindex(columns=gameweeks).fillna(0.0))

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
            "code": int(player["code"]),
            "name": str(player["web_name"]),
            "full_name": str(player["full_name"]),
            "pos": str(player["pos"]),
            "team": str(player["team"]),
            "team_short": str(player["team_short"]),
            "owned": _num(pd.to_numeric(player["selected_by_percent"], errors="coerce")),
            "ppg": _num(player.get("points_per_game")),
            "minutes_last": _num(player.get("minutes"), dp=0),
            "pts_last": _num(player.get("total_points"), dp=0),
            "price_change": _num(player.get("exp_price_change"), None),
            "confidence": str(player.get("confidence", "")),
            "recency": _num(player.get("recency"), None),
            "start_long_run": _num(player.get("start_long_run"), None, dp=INPUT_DP),
            "start_recent": _num(player.get("start_recent"), None, dp=INPUT_DP),
            "moved": bool(player.get("moved_club", False)),
            "previous_club": str(player.get("previous_club", "") or ""),
            "status": str(player["status"]),
            "news": str(player["news"] or ""),
            "seasons": _seasons(player, basis),
            "gw": per_gw,
            "cs": cs_gw,
            "opp": labels,
            "hazard": _num(hazard.iloc[position], dp=INPUT_DP),
        }
        for field in SCORING_FIELDS:
            row[field] = _num(player.get(field), dp=INPUT_DP)
        for field in RAW_FIELDS:
            row[f"raw_{field}"] = _num(player.get(f"raw_{field}"), None, dp=INPUT_DP)
        rows.append(row)

    fixtures = [
        {"gw": int(f["gw"]),
         "home_team": str(f["home_team"]), "away_team": str(f["away_team"]),
         "lam_home": _num(f["lam_home"], dp=INPUT_DP),
         "lam_away": _num(f["lam_away"], dp=INPUT_DP),
         "lam_home_uncalibrated": _num(f["lam_home_uncalibrated"], dp=INPUT_DP),
         "lam_away_uncalibrated": _num(f["lam_away_uncalibrated"], dp=INPUT_DP),
         "source": str(f["lam_source"])}
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
            "previous": previous,
            "previous_weight": _num(basis.previous_weight if basis else None, None),
            "odds_coverage": round(projection.odds_coverage, 3),
            "odds_note": projection.odds_note,
            "notes": list(projection.notes),
            "priced_gws": priced,
            "total_managers": fpl_api.total_managers(),
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

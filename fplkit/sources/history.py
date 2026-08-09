"""Per-gameweek history, for weighting recent form above early-season form.

Neither of the live sources can do this. The FPL API's `element-summary`
history is wiped when a new season starts, and its `history_past` is
season-level totals; Understat's endpoint returns whole-season aggregates and
ignores date parameters. So a season's match-by-match detail simply is not
available from either once that season is over.

This fills the gap from the vaastav/Fantasy-Premier-League archive, a
long-running community mirror that snapshots the FPL API every gameweek and
publishes it as CSV. It is a **third-party** source -- not official, and it can
lag or break -- which is why recency weighting is opt-in and the model falls
back to flat season rates without it.

Element ids are reassigned every season, so joining last season's rows to this
season's players goes through `code`, the stable per-player identifier that
survives across seasons and transfers.
"""

from __future__ import annotations

import io

import pandas as pd
import requests

from ..cache import cached_json

BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
TTL = 12 * 3600
TIMEOUT = 60

# Columns worth carrying: the counting stats the model derives rates from.
KEEP = ["code", "gw", "minutes", "expected_goals", "expected_assists",
        "expected_goals_conceded", "defensive_contribution", "saves", "bonus",
        "clean_sheets", "goals_conceded", "starts", "total_points"]


def _fetch_csv(url: str) -> str:
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def _cached_csv(namespace: str, url: str, force_refresh: bool = False) -> pd.DataFrame:
    text = cached_json(namespace, url, lambda: _fetch_csv(url), TTL, force_refresh)
    return pd.read_csv(io.StringIO(text))


def gameweek_history(season: str = "2025-26",
                     force_refresh: bool = False) -> pd.DataFrame:
    """One row per player per gameweek, keyed by the stable player `code`.

    Returns an empty frame rather than raising if the archive is unreachable --
    recency weighting is an enhancement, and losing it should not take the whole
    projection down with it.
    """
    try:
        merged = _cached_csv("history", f"{BASE}/{season}/gws/merged_gw.csv", force_refresh)
        players = _cached_csv("history", f"{BASE}/{season}/players_raw.csv", force_refresh)
    except Exception:
        return pd.DataFrame(columns=KEEP)

    if "element" not in merged or "code" not in players:
        return pd.DataFrame(columns=KEEP)

    codes = players[["id", "code"]].rename(columns={"id": "element"})
    df = merged.merge(codes, on="element", how="inner")
    df = df.rename(columns={"round": "gw"})

    for column in KEEP:
        if column not in df.columns:
            df[column] = 0.0
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    return df[KEEP]


def recency_multipliers(history: pd.DataFrame, half_life_matches: float,
                        clip: tuple[float, float] = (0.6, 1.6),
                        min_minutes: float = 270.0) -> pd.DataFrame:
    """How much better or worse a player looked late in the season than overall.

    Deliberately a *multiplier* on the rates the model already has, not a
    replacement for them. The archive carries the FPL API's `expected_goals`,
    which includes penalties, while the model's attacking rate is Understat's
    non-penalty xG -- swapping one for the other would quietly reintroduce the
    penalty contamination that is modelled separately elsewhere. A ratio of
    recent-weighted to season-long rate carries the trend without touching the
    basis.

    Weights halve every `half_life_matches` gameweeks going backwards, so a
    half-life of 10 leaves the final third of the season carrying most of the
    signal. Players with too little weighted playing time get 1.0 -- no opinion.
    """
    columns = ["expected_goals", "expected_assists", "defensive_contribution",
               "saves", "bonus"]
    if history.empty or not half_life_matches:
        return pd.DataFrame(columns=["code"] + [f"{c}_mult" for c in columns])

    df = history.copy()
    latest = df["gw"].max()
    df["w"] = 0.5 ** ((latest - df["gw"]) / float(half_life_matches))

    out = []
    for code, group in df.groupby("code"):
        season_minutes = group["minutes"].sum()
        weighted_minutes = (group["minutes"] * group["w"]).sum()
        if season_minutes < min_minutes or weighted_minutes <= 0:
            continue
        row = {"code": int(code)}
        for column in columns:
            season_rate = group[column].sum() / season_minutes
            recent_rate = ((group[column] * group["w"]).sum() / weighted_minutes)
            if season_rate <= 0:
                row[f"{column}_mult"] = 1.0
            else:
                row[f"{column}_mult"] = float(
                    min(clip[1], max(clip[0], recent_rate / season_rate)))
        out.append(row)

    if not out:
        return pd.DataFrame(columns=["code"] + [f"{c}_mult" for c in columns])
    return pd.DataFrame(out)

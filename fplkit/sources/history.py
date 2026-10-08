
from __future__ import annotations

import datetime
import io

import numpy as np
import pandas as pd
import requests

from ..cache import cached_json

BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
TTL = 12 * 3600
TIMEOUT = 60


def season_folder(start_year: int) -> str:
    return f"{start_year}-{str(start_year + 1)[2:]}"


def current_season(today: datetime.date | None = None) -> str:
    today = today or datetime.date.today()
    return season_folder(today.year if today.month >= 8 else today.year - 1)


ARCHIVE_MAX_LAG = 2

PREVIOUS_TOTALS = ["minutes", "starts", "bonus", "yellow_cards", "saves",
                   "defensive_contribution", "expected_goals", "expected_assists",
                   "expected_goals_conceded"]


def season_totals(season: str, force_refresh: bool = False) -> pd.DataFrame:
    try:
        players = _cached_csv("history", f"{BASE}/{season}/players_raw.csv", force_refresh)
        teams = _cached_csv("history", f"{BASE}/{season}/teams.csv", force_refresh)
    except Exception:
        return pd.DataFrame(columns=["code", "prev_team", "prev_matches"]
                            + [f"prev_{c}" for c in PREVIOUS_TOTALS])
    out = pd.DataFrame({"code": pd.to_numeric(players["code"], errors="coerce")})
    for column in PREVIOUS_TOTALS:
        out[f"prev_{column}"] = pd.to_numeric(players.get(column), errors="coerce").fillna(0.0)
    names = dict(zip(teams["id"], teams["name"]))
    out["prev_team"] = players["team"].map(names)
    out["prev_matches"] = 38.0
    return out.dropna(subset=["code"]).astype({"code": int}).drop_duplicates("code")

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


def gameweek_history(season: str | None = None,
                     force_refresh: bool = False) -> pd.DataFrame:
    season = season or current_season()
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


def _weighted(history: pd.DataFrame, half_life_matches: float,
              latest: int | None) -> pd.DataFrame | None:
    if history.empty or not half_life_matches:
        return None
    archive_latest = int(history["gw"].max())
    if latest is None:
        latest = archive_latest
    elif archive_latest < latest - ARCHIVE_MAX_LAG:
        return None
    df = history.copy()
    df["w"] = 0.5 ** ((latest - df["gw"]) / float(half_life_matches))
    return df


def start_form(history: pd.DataFrame, half_life_matches: float = 4.0,
               latest: int | None = None) -> pd.DataFrame:
    df = _weighted(history, half_life_matches, latest)
    if df is None:
        return pd.DataFrame(columns=["code", "recent_start_rate", "recent_matches"])
    df["started"] = (pd.to_numeric(df["starts"], errors="coerce").fillna(0.0) > 0).astype(float)

    grouped = df.groupby("code")
    weight = grouped["w"].sum()
    rate = grouped.apply(lambda g: (g["started"] * g["w"]).sum(), include_groups=False)
    out = pd.DataFrame({
        "code": weight.index.astype(int),
        "recent_start_rate": (rate / weight.replace(0.0, pd.NA)).astype(float).values,
        "recent_matches": weight.values,
    })
    return out.dropna(subset=["recent_start_rate"]).reset_index(drop=True)


def minutes_form(history: pd.DataFrame, half_life_matches: float = 4.0,
                 latest: int | None = None) -> pd.DataFrame:
    columns = ["code", "recent_mins_if_start", "recent_start_matches", "recent_mins_std"]
    df = _weighted(history, half_life_matches, latest)
    if df is None:
        return pd.DataFrame(columns=columns)
    df = df[pd.to_numeric(df["starts"], errors="coerce").fillna(0.0) > 0]
    if df.empty:
        return pd.DataFrame(columns=columns)

    grouped = df.groupby("code")
    weight = grouped["w"].sum()
    mins = grouped.apply(lambda g: (g["minutes"] * g["w"]).sum(), include_groups=False)
    mean = mins / weight.replace(0.0, pd.NA)
    variance = grouped.apply(
        lambda g: (g["w"] * (g["minutes"] - mean.loc[g.name]) ** 2).sum(),
        include_groups=False) / weight.replace(0.0, pd.NA)
    out = pd.DataFrame({
        "code": weight.index.astype(int),
        "recent_mins_if_start": mean.astype(float).values,
        "recent_start_matches": weight.values,
        "recent_mins_std": np.sqrt(variance.astype(float)).values,
    })
    return out.dropna(subset=["recent_mins_if_start"]).reset_index(drop=True)


def recency_multipliers(history: pd.DataFrame, half_life_matches: float,
                        clip: tuple[float, float] = (0.6, 1.6),
                        min_minutes: float = 270.0,
                        latest: int | None = None) -> pd.DataFrame:
    columns = ["expected_goals", "expected_assists", "defensive_contribution",
               "saves", "bonus"]
    df = _weighted(history, half_life_matches, latest)
    if df is None:
        return pd.DataFrame(columns=["code"] + [f"{c}_mult" for c in columns])

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

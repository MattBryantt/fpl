
from __future__ import annotations

import pandas as pd
import requests

from ..cache import cached_json
from ..config import UNDERSTAT_SEASON

URL = "https://understat.com/main/getPlayersStats/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
}
TTL = 24 * 3600

NUMERIC = ["games", "time", "goals", "xG", "npg", "npxG", "assists", "xA",
           "shots", "key_passes", "yellow_cards", "red_cards",
           "xGChain", "xGBuildup"]


def player_stats(season: str | None = None, force_refresh: bool = False) -> pd.DataFrame:
    if season is None:
        from . import fpl_api
        season = UNDERSTAT_SEASON or str(fpl_api.season_start_year() - 1)

    def fetch():
        response = requests.post(
            URL, headers=HEADERS, data={"league": "EPL", "season": season}, timeout=30
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"Understat returned success=false for season {season}")
        return payload["players"]

    rows = cached_json("understat", f"players-{season}", fetch, TTL, force_refresh)
    df = pd.DataFrame(rows)
    for column in NUMERIC:
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)

    df = df.rename(columns={"player_name": "us_name", "team_title": "us_team",
                            "id": "us_id", "time": "us_minutes"})

    per90 = df["us_minutes"].replace(0, pd.NA) / 90.0
    df["npxg_per90"] = (df["npxG"] / per90).fillna(0.0)
    df["xa_per90"] = (df["xA"] / per90).fillna(0.0)
    df["xgchain_per90"] = (df["xGChain"] / per90).fillna(0.0)
    df["shots_per90"] = (df["shots"] / per90).fillna(0.0)
    df["key_passes_per90"] = (df["key_passes"] / per90).fillna(0.0)

    totals = df.groupby("us_id", as_index=False)[
        ["us_minutes", "npxG", "xA", "xGChain", "shots", "key_passes", "goals", "assists"]
    ].sum()
    identity = (df.sort_values("us_minutes", ascending=False)
                  .drop_duplicates("us_id")[["us_id", "us_name", "us_team", "position"]])
    merged = totals.merge(identity, on="us_id", how="left")

    per90 = merged["us_minutes"].replace(0, pd.NA) / 90.0
    for source, target in [("npxG", "npxg_per90"), ("xA", "xa_per90"),
                           ("xGChain", "xgchain_per90"), ("shots", "shots_per90"),
                           ("key_passes", "key_passes_per90")]:
        merged[target] = (merged[source] / per90).fillna(0.0)

    merged["us_team_list"] = (merged["us_team"].fillna("")
                              .apply(lambda s: [t.strip() for t in s.split(",") if t.strip()]))
    merged["moved_clubs"] = merged["us_team_list"].apply(len) > 1
    return merged


def team_rates(stats: pd.DataFrame) -> pd.DataFrame:
    single_club = stats[~stats["moved_clubs"]].copy()
    single_club["club"] = single_club["us_team_list"].str[0]

    grouped = single_club.groupby("club", as_index=False)[["npxG", "xA", "us_minutes"]].sum()
    grouped["matches"] = (grouped["us_minutes"] / (11 * 90)).clip(lower=1)
    grouped["team_npxg_per_match"] = grouped["npxG"] / grouped["matches"]
    grouped["team_xa_per_match"] = grouped["xA"] / grouped["matches"]
    return grouped.rename(columns={"club": "us_team"})

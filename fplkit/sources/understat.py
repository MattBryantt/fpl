"""Understat per-player xG data.

Understat stopped embedding its datasets in the league page HTML, but the AJAX
endpoint the page calls is still open:

    POST https://understat.com/main/getPlayersStats/   league=EPL&season=2025

which returns per-player season totals including npxG, xA, xGChain and xGBuildup.
Team-level rates are aggregated from the player rows rather than scraped
separately, since Understat exposes no equivalent team endpoint.
"""

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
    """One row per player for the given Understat season (start year)."""
    season = season or UNDERSTAT_SEASON

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

    # A player can appear on two rows if he moved club mid-season; keep the club
    # he played the most minutes for, and sum the underlying totals.
    #
    # Grouped on Understat's own player id rather than on the name. Two players
    # can share a display name -- the league carries a few most seasons -- and
    # grouping by name welds them into one row holding the pooled xG of both,
    # which then goes to whichever of them the join happens to reach first. The
    # id is what actually identifies a player, so it is what groups him.
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

    # A player who moved mid-season carries a comma-joined team_title, e.g.
    # "Bournemouth,Manchester City". Keep every club he played for so the player
    # join can find him under whichever one the FPL API now lists him at.
    merged["us_team_list"] = (merged["us_team"].fillna("")
                              .apply(lambda s: [t.strip() for t in s.split(",") if t.strip()]))
    merged["moved_clubs"] = merged["us_team_list"].apply(len) > 1
    return merged


def team_rates(stats: pd.DataFrame) -> pd.DataFrame:
    """Per-match attacking rates for each club, aggregated from its players.

    Only players who stayed at one club all season are counted. A transferred
    player's xG cannot be split between his two clubs from this endpoint, and
    attributing all of it to either one badly distorts that club's rate. Since
    the denominator is the players' own minutes rather than a fixed 38 matches,
    dropping them leaves the resulting per-match rate unbiased.
    """
    single_club = stats[~stats["moved_clubs"]].copy()
    single_club["club"] = single_club["us_team_list"].str[0]

    grouped = single_club.groupby("club", as_index=False)[["npxG", "xA", "us_minutes"]].sum()
    grouped["matches"] = (grouped["us_minutes"] / (11 * 90)).clip(lower=1)
    grouped["team_npxg_per_match"] = grouped["npxG"] / grouped["matches"]
    grouped["team_xa_per_match"] = grouped["xA"] / grouped["matches"]
    return grouped.rename(columns={"club": "us_team"})

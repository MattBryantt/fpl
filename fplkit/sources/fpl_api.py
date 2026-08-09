"""Official Fantasy Premier League API.

Endpoints used:
  bootstrap-static/  -> players, teams, gameweeks
  fixtures/          -> the full 380-fixture list with gameweek assignment
"""

from __future__ import annotations

import pandas as pd
import requests

from ..cache import cached_json
from ..config import POSITIONS

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
TTL = 3 * 3600


def _get(path: str, force_refresh: bool = False):
    def fetch():
        response = requests.get(f"{BASE}/{path}", headers=HEADERS, timeout=30)
        response.raise_for_status()
        return response.json()

    return cached_json("fpl", path, fetch, TTL, force_refresh)


def bootstrap(force_refresh: bool = False) -> dict:
    return _get("bootstrap-static/", force_refresh)


def fixtures_raw(force_refresh: bool = False) -> list[dict]:
    return _get("fixtures/", force_refresh)


def teams(force_refresh: bool = False) -> pd.DataFrame:
    df = pd.DataFrame(bootstrap(force_refresh)["teams"])
    return df[["id", "name", "short_name"]].rename(columns={"id": "team_id"})


def players(force_refresh: bool = False) -> pd.DataFrame:
    """Player table with the raw fields the model needs, lightly normalised.

    Note: totals here are last completed season's, which is what the FPL API
    serves before gameweek 1 of a new season.
    """
    data = bootstrap(force_refresh)
    df = pd.DataFrame(data["elements"])
    team_names = {t["id"]: t["name"] for t in data["teams"]}
    team_short = {t["id"]: t["short_name"] for t in data["teams"]}

    numeric = [
        "now_cost", "minutes", "starts", "total_points", "bonus", "bps",
        "goals_scored", "assists", "clean_sheets", "goals_conceded", "saves",
        "yellow_cards", "red_cards", "own_goals", "penalties_missed",
        "penalties_saved", "expected_goals", "expected_assists",
        "expected_goals_conceded", "expected_goals_per_90",
        "expected_assists_per_90", "expected_goals_conceded_per_90",
        "saves_per_90", "defensive_contribution", "defensive_contribution_per_90",
        "clearances_blocks_interceptions", "recoveries", "tackles",
        "selected_by_percent", "points_per_game", "form",
    ]
    for column in numeric:
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)

    out = pd.DataFrame({
        "fpl_id": df["id"],
        # Stable across seasons and transfers; the join key for archived history.
        "code": df["code"],
        "web_name": df["web_name"],
        "full_name": (df["first_name"] + " " + df["second_name"]).str.strip(),
        "pos": df["element_type"].map(POSITIONS),
        "team_id": df["team"],
        "team": df["team"].map(team_names),
        "team_short": df["team"].map(team_short),
        "price": df["now_cost"] / 10.0,
        "status": df["status"],
        "chance_next": pd.to_numeric(df["chance_of_playing_next_round"], errors="coerce"),
        "news": df["news"],
        "birth_date": pd.to_datetime(df.get("birth_date"), errors="coerce"),
        "transfers_in_event": pd.to_numeric(df["transfers_in_event"], errors="coerce").fillna(0),
        "transfers_out_event": pd.to_numeric(df["transfers_out_event"], errors="coerce").fillna(0),
        "penalties_order": pd.to_numeric(df["penalties_order"], errors="coerce"),
        "set_piece_order": pd.to_numeric(df["corners_and_indirect_freekicks_order"], errors="coerce"),
        "selected_by_percent": df["selected_by_percent"],
    })
    for column in numeric:
        if column not in ("now_cost",):
            out[column] = df[column]
    return out


def fixtures(force_refresh: bool = False) -> pd.DataFrame:
    """Unplayed-or-played fixture list, one row per fixture."""
    df = pd.DataFrame(fixtures_raw(force_refresh))
    df = df[["id", "event", "team_h", "team_a", "kickoff_time", "finished",
             "team_h_difficulty", "team_a_difficulty"]]
    df["kickoff_time"] = pd.to_datetime(df["kickoff_time"], errors="coerce", utc=True)
    return df.rename(columns={"id": "fixture_id", "event": "gw"})


def chip_windows(gameweek: int | None = None,
                 force_refresh: bool = False) -> dict[str, tuple[int, int]]:
    """Earliest and latest gameweek each chip can be played.

    Chips come in two sets, one per half of the season, so each name appears
    twice with different windows. Unioning them would erase the mid-season
    expiry that makes chip timing urgent, so this returns the single set that
    applies to `gameweek` (defaulting to the first set). The wildcard's
    start_event of 2 is the load-bearing detail: the initial squad cannot be
    rebuilt in gameweek one.
    """
    by_name: dict[str, list[tuple[int, int]]] = {}
    for chip in bootstrap(force_refresh).get("chips", []):
        name = chip.get("name")
        start, stop = chip.get("start_event"), chip.get("stop_event")
        if not name or start is None or stop is None:
            continue
        by_name.setdefault(name, []).append((int(start), int(stop)))

    windows: dict[str, tuple[int, int]] = {}
    for name, spans in by_name.items():
        spans.sort()
        if gameweek is not None:
            match = next((s for s in spans if s[0] <= gameweek <= s[1]), None)
            if match:
                windows[name] = match
                continue
        windows[name] = spans[0]
    return windows


def total_managers(force_refresh: bool = False) -> int:
    return int(bootstrap(force_refresh).get("total_players", 0))


def next_gameweek(force_refresh: bool = False) -> int:
    """The gameweek whose deadline has not yet passed."""
    events = bootstrap(force_refresh)["events"]
    for event in events:
        if event.get("is_next"):
            return int(event["id"])
    for event in events:
        if not event.get("finished"):
            return int(event["id"])
    return int(events[-1]["id"])

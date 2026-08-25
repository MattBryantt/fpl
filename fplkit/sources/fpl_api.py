"""Official Fantasy Premier League API.

Endpoints used:
  bootstrap-static/                    -> players, teams, gameweeks
  fixtures/                            -> the full 380-fixture list with gameweek assignment
  entry/{id}/                          -> a manager's public profile
  entry/{id}/event/{gw}/picks/         -> a manager's squad for one gameweek
  entry/{id}/history/                  -> a manager's chips used and past-season totals
  entry/{id}/transfers/                -> a manager's full transfer log

The entry/* endpoints are public and need no login -- a manager's team id
(visible in the URL of their own "Points" page) is all that identifies them.
There is deliberately no path here to FPL's authenticated `/my-team/{id}/`
endpoint, which is the only one that carries per-player selling prices: this
tool never asks for a password, so anywhere a selling price would matter it
falls back to the player's current listed price instead (see live_squad()).
"""

from __future__ import annotations

import pandas as pd
import requests

from ..cache import cached_json
from ..config import POSITIONS

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
TTL = 3 * 3600
# A manager's own picks/bank/chips change as they play, unlike the player pool
# -- caching it as long as bootstrap-static would show a stale squad for
# hours after a real transfer.
LIVE_TTL = 5 * 60


def _get(path: str, force_refresh: bool = False, ttl: int | None = None):
    def fetch():
        response = requests.get(f"{BASE}/{path}", headers=HEADERS, timeout=30)
        response.raise_for_status()
        return response.json()

    return cached_json("fpl", path, fetch, TTL if ttl is None else ttl, force_refresh)


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


# --------------------------------------------------------------------------- #
# A manager's own team
# --------------------------------------------------------------------------- #

def entry(team_id: int, force_refresh: bool = False) -> dict:
    """A manager's public profile: name, overall points and rank."""
    return _get(f"entry/{team_id}/", force_refresh, ttl=LIVE_TTL)


def entry_picks(team_id: int, gw: int, force_refresh: bool = False) -> dict:
    """A manager's squad for one gameweek.

    `picks` is 15 `{element, position, multiplier, is_captain,
    is_vice_captain, element_type}` rows -- `element` is this season's
    `fpl_id`, always valid at fetch time regardless of past rollovers.
    `entry_history` carries that gameweek's `bank`, `value` (squad value,
    both in tenths of a million), `event_transfers` and
    `event_transfers_cost`. Confirmed live (2026-08-24): there is no
    `selling_price` anywhere on this endpoint -- see the module docstring.
    """
    return _get(f"entry/{team_id}/event/{gw}/picks/", force_refresh, ttl=LIVE_TTL)


def entry_history(team_id: int, force_refresh: bool = False) -> dict:
    """A manager's season so far: `current` (one row per gameweek, same shape
    as `entry_picks`'s `entry_history`), `past` (previous seasons) and
    `chips` (`[{name, event, time}]`, one row per chip played)."""
    return _get(f"entry/{team_id}/history/", force_refresh, ttl=LIVE_TTL)


def entry_transfers(team_id: int, force_refresh: bool = False) -> list[dict]:
    """A manager's full transfer log, each row tagged with the `event` it was
    made in. Empty before a manager has made their first in-season transfer
    -- preseason squad-building is not logged here at all."""
    return _get(f"entry/{team_id}/transfers/", force_refresh, ttl=LIVE_TTL)


def live_squad(team_id: int, gw: int | None = None, force_refresh: bool = False) -> dict:
    """Everything the board needs to act on a manager's real team, in one call.

    `squad_ids` are `element` values straight off the picks endpoint --
    this season's `fpl_id`s, already valid against the current player table,
    with no `code` translation needed here. That translation matters only at
    the point a squad gets *persisted* across a season boundary
    (`localStorage`/sync), which the browser already handles at its own
    save/load boundary; a value fetched live is never stale by definition.

    `sell_prices_by_code` is deliberately absent: without the authenticated
    `/my-team/` endpoint there is no per-player selling price to offer, so
    callers should fall back to each player's current listed price -- the
    same approximation `plan_transfers()` already makes when its own
    `sell_prices` argument is omitted. `budget_total`, by contrast, does not
    need per-player prices at all: `value + bank` is exactly what FPL itself
    would use as a wildcard/free-hit rebuild budget, already net of every
    player's real sell-on fee on their end.
    """
    gw = gw or (next_gameweek(force_refresh) - 1) or 1
    picks = entry_picks(team_id, gw, force_refresh)
    history = entry_history(team_id, force_refresh)

    squad_ids = [p["element"] for p in picks["picks"]]
    captain_id = next((p["element"] for p in picks["picks"] if p["is_captain"]), None)

    eh = picks["entry_history"]
    bank = eh["bank"] / 10.0
    value = eh["value"] / 10.0

    chips_used = [c["name"] for c in history.get("chips", [])]

    # Imported here, not at module load: transfers.py -> model.py -> fpl_api.py
    # is already a cycle, so importing transfers at the top of this file would
    # invert it.
    from .. import transfers

    ft = 1  # the first gameweek after preseason always opens on exactly one
    for row in sorted(history.get("current", []), key=lambda r: r["event"]):
        if row["event"] < 2 or row["event"] > gw:
            continue
        freehit = any(c["event"] == row["event"] and c["name"] == "freehit"
                      for c in history.get("chips", []))
        ft = transfers.next_free_transfers(
            ft, row["event_transfers"], played_freehit=freehit)

    return {
        "gw": gw,
        "squad_ids": squad_ids,
        "captain_id": captain_id,
        "bank": bank,
        "value": value,
        "budget_total": round(bank + value, 1),
        "chips_used": chips_used,
        "active_chip": picks.get("active_chip"),
        "free_transfers": ft,
    }

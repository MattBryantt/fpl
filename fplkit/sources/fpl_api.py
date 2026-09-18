"""Official Fantasy Premier League API.

Endpoints used:
  bootstrap-static/                    -> players, teams, gameweeks
  fixtures/                            -> the full 380-fixture list with gameweek assignment
  entry/{id}/                          -> a manager's public profile
  entry/{id}/event/{gw}/picks/         -> a manager's squad for one gameweek
  entry/{id}/history/                  -> a manager's chips used and past-season totals
  entry/{id}/transfers/                -> a manager's full transfer log
  element-summary/{id}/                -> one player's per-gameweek rows this season
  event/{gw}/live/                     -> every player's stats for one gameweek

The entry/* endpoints are public and need no login -- a manager's team id
(visible in the URL of their own "Points" page) is all that identifies them.
There is deliberately no path here to FPL's authenticated `/my-team/{id}/`
endpoint, which is the only one that carries per-player selling prices: this
tool never asks for a password. Selling prices are rebuilt instead from what
the public endpoints do say -- what each player was bought for -- see
purchase_prices() and live_squad().
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


def season_start_year(force_refresh: bool = False) -> int:
    """The year the season the API is serving kicked off in, off gameweek 1's
    deadline. The game for a new season goes live in July, so from then until
    the following June this names that season -- which is what every "this
    season" and "last season" in the model is relative to."""
    first = bootstrap(force_refresh)["events"][0]
    return int(str(first["deadline_time"])[:4])


def event_live(gw: int, force_refresh: bool = False) -> dict:
    """Every player's stats for one gameweek, in one call. A finished
    gameweek's rows never change, so they are cached for the day rather than
    the three hours the rest of the API gets."""
    return _get(f"event/{gw}/live/", force_refresh, ttl=24 * 3600)


def gameweek_history(through_gw: int, force_refresh: bool = False) -> pd.DataFrame:
    """One row per player per finished gameweek this season, keyed by the
    stable `code` -- the same shape history.gameweek_history() reads from the
    community archive, from the API itself, so it can never be behind it.

    A row is kept only for players whose club actually played that gameweek:
    the endpoint lists everyone every week, and a blank week is not a benching.
    """
    from .history import KEEP  # the columns the model reads
    elements = bootstrap(force_refresh)["elements"]
    code = {int(e["id"]): int(e["code"]) for e in elements}
    club = {int(e["id"]): int(e["team"]) for e in elements}
    played = fixtures(force_refresh)
    played = played[played["finished"].fillna(False).astype(bool)]
    active = {int(gw): set(pd.concat([rows["team_h"], rows["team_a"]]).astype(int))
              for gw, rows in played.groupby("gw")}

    rows = []
    for gw in range(1, int(through_gw) + 1):
        for element in event_live(gw, force_refresh).get("elements", []):
            fpl_id = int(element["id"])
            if fpl_id not in code or club[fpl_id] not in active.get(gw, set()):
                continue
            stats = element.get("stats", {})
            rows.append({"code": code[fpl_id], "gw": gw,
                         **{k: stats.get(k, 0.0) for k in KEEP if k not in ("code", "gw")}})
    df = pd.DataFrame(rows, columns=KEEP)
    for column in KEEP:
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    return df


def element_summary(element_id: int, force_refresh: bool = False) -> dict:
    """One player's season: `history` (one row per gameweek played, with the
    price he carried that week as `value`, in tenths) and `history_past`."""
    return _get(f"element-summary/{element_id}/", force_refresh)


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


# Chips that suspend the transfer accounting for their gameweek: the moves made
# under them are free and unlimited, cost nothing from the bank of free
# transfers, and earn none either.
TRANSFER_CHIPS = ("freehit", "wildcard")


def purchase_prices(squad_ids: list[int], transfer_log: list[dict],
                    freehit_gws: set[int], force_refresh: bool = False) -> dict[int, float]:
    """What each owned player was bought for, in millions.

    The transfer log carries the price of every in-season purchase; the
    latest one for a player still owned is the one his sell price is built
    from (bought, sold, bought back: the second purchase). A free-hit week's
    moves are skipped because they were handed back. Whoever is left was in
    the opening squad and cost what he was listed at in gameweek 1, which his
    own gameweek history still says. A player with neither falls back to his
    current price, which is the honest "no profit known".
    """
    bought: dict[int, float] = {}
    for move in sorted(transfer_log, key=lambda t: (t["event"], t.get("time", ""))):
        if move["event"] in freehit_gws:
            continue
        bought[move["element_in"]] = move["element_in_cost"] / 10.0

    out = {}
    for fpl_id in squad_ids:
        if fpl_id in bought:
            out[fpl_id] = bought[fpl_id]
            continue
        rows = element_summary(fpl_id, force_refresh).get("history", [])
        opening = next((r for r in rows if r.get("round") == 1), None)
        if opening is not None:
            out[fpl_id] = opening["value"] / 10.0
    return out


def live_squad(team_id: int, gw: int | None = None, force_refresh: bool = False) -> dict:
    """Everything the board needs to act on a manager's real team, in one call.

    `squad_ids` are `element` values straight off the picks endpoint --
    this season's `fpl_id`s, already valid against the current player table,
    with no `code` translation needed here. That translation matters only at
    the point a squad gets *persisted* across a season boundary
    (`localStorage`/sync), which the browser already handles at its own
    save/load boundary; a value fetched live is never stale by definition.

    `purchase_prices` and `sell_prices` are rebuilt from the public transfer
    log and gameweek-1 prices (see purchase_prices()), since the endpoint that
    states them outright needs a login. `budget_total` is what a wildcard or
    free hit has to spend: the squad's selling value plus the bank. FPL's own
    `value` is passed through for reference; it is the team value the site
    shows, bank included, so it is not added to the bank again here.
    """
    gw = gw or (next_gameweek(force_refresh) - 1) or 1
    picks = entry_picks(team_id, gw, force_refresh)
    history = entry_history(team_id, force_refresh)

    squad_ids = [p["element"] for p in picks["picks"]]
    captain_id = next((p["element"] for p in picks["picks"] if p["is_captain"]), None)

    eh = picks["entry_history"]
    bank = eh["bank"] / 10.0
    value = eh["value"] / 10.0

    chips_played = {c["event"]: c["name"] for c in history.get("chips", [])}
    chips_used = list(chips_played.values())

    # Imported here, not at module load: transfers.py -> model.py -> fpl_api.py
    # is already a cycle, so importing transfers at the top of this file would
    # invert it.
    from .. import transfers

    ft = 1  # the first gameweek after preseason always opens on exactly one
    for row in sorted(history.get("current", []), key=lambda r: r["event"]):
        if row["event"] < 2 or row["event"] > gw:
            continue
        chip = chips_played.get(row["event"]) in TRANSFER_CHIPS
        ft = transfers.next_free_transfers(
            ft, 0 if chip else row["event_transfers"], played_chip=chip)

    freehit_gws = {e for e, name in chips_played.items() if name == "freehit"}
    bought = purchase_prices(squad_ids, entry_transfers(team_id, force_refresh),
                             freehit_gws, force_refresh)
    now = {int(p["id"]): p["now_cost"] / 10.0 for p in bootstrap(force_refresh)["elements"]}
    sell = {i: transfers.sell_price(bought.get(i, now[i]), now[i]) for i in squad_ids}

    return {
        "gw": gw,
        "squad_ids": squad_ids,
        "captain_id": captain_id,
        "bank": bank,
        "value": value,
        "purchase_prices": bought,
        "sell_prices": sell,
        "budget_total": round(bank + sum(sell.values()), 1),
        "chips_used": chips_used,
        "active_chip": picks.get("active_chip"),
        "free_transfers": ft,
    }

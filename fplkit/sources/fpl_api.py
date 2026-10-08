
from __future__ import annotations

import pandas as pd
import requests

from ..cache import cached_json
from ..config import POSITIONS

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
TTL = 3 * 3600
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
    df = pd.DataFrame(fixtures_raw(force_refresh))
    df = df[["id", "event", "team_h", "team_a", "kickoff_time", "finished",
             "team_h_difficulty", "team_a_difficulty"]]
    df["kickoff_time"] = pd.to_datetime(df["kickoff_time"], errors="coerce", utc=True)
    return df.rename(columns={"id": "fixture_id", "event": "gw"})


def chip_windows(gameweek: int | None = None,
                 force_refresh: bool = False) -> dict[str, tuple[int, int]]:
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
    first = bootstrap(force_refresh)["events"][0]
    return int(str(first["deadline_time"])[:4])


def event_live(gw: int, force_refresh: bool = False) -> dict:
    return _get(f"event/{gw}/live/", force_refresh, ttl=24 * 3600)


def gameweek_history(through_gw: int, force_refresh: bool = False) -> pd.DataFrame:
    from .history import KEEP
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
    return _get(f"element-summary/{element_id}/", force_refresh)


def total_managers(force_refresh: bool = False) -> int:
    return int(bootstrap(force_refresh).get("total_players", 0))


def next_gameweek(force_refresh: bool = False) -> int:
    events = bootstrap(force_refresh)["events"]
    for event in events:
        if event.get("is_next"):
            return int(event["id"])
    for event in events:
        if not event.get("finished"):
            return int(event["id"])
    return int(events[-1]["id"])


def entry(team_id: int, force_refresh: bool = False) -> dict:
    return _get(f"entry/{team_id}/", force_refresh, ttl=LIVE_TTL)


def entry_picks(team_id: int, gw: int, force_refresh: bool = False) -> dict:
    return _get(f"entry/{team_id}/event/{gw}/picks/", force_refresh, ttl=LIVE_TTL)


def entry_history(team_id: int, force_refresh: bool = False) -> dict:
    return _get(f"entry/{team_id}/history/", force_refresh, ttl=LIVE_TTL)


def entry_transfers(team_id: int, force_refresh: bool = False) -> list[dict]:
    return _get(f"entry/{team_id}/transfers/", force_refresh, ttl=LIVE_TTL)


TRANSFER_CHIPS = ("freehit", "wildcard")


def purchase_prices(squad_ids: list[int], transfer_log: list[dict],
                    freehit_gws: set[int], force_refresh: bool = False) -> dict[int, float]:
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

    from .. import transfers

    ft = 1
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

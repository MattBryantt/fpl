"""Solve synthetic transfer-and-chip scenarios with CBC, for transfers.js to match.

Same synthetic-league approach as scripts/verify-transfer-rules.py -- a small,
fully-controlled fixture list rather than real projection data, so both CBC
and the WASM solver finish in seconds and the case file is reproducible. That
script proves the *rules* hold; this one proves the *port* agrees with the
Python it was translated from, case for case, the same relationship
scripts/make-solver-cases.py has to scripts/verify-solver-port.mjs.

The pool and points handed to each solve are captured and dumped verbatim --
not rebuilt from a JS port of candidate_pool -- so a disagreement can only mean
the LP itself was translated wrong, not that the two sides picked a different
150-odd players to begin with.

    python scripts/make-transfer-cases.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplkit.config import (  # noqa: E402
    BANK_VALUE, CHIP_HOLD_VALUE, CHIP_LABELS, DEFAULT_BENCH_SLOT_WEIGHTS,
    FREE_TRANSFERS_PER_GW, HIT_COST, MAX_FREE_TRANSFERS, MAX_PER_CLUB,
    SQUAD_BY_POS, SQUAD_SIZE, TRANSFER_FRICTION, XI_MAX_BY_POS, XI_MIN_BY_POS,
    XI_SIZE,
)
from fplkit.model import Projection  # noqa: E402
from fplkit import transfers  # noqa: E402
from fplkit.transfers import (  # noqa: E402
    CAPTAIN_CANDIDATES, IDLE_MOVE_PENALTY, POOL_BY_POS, TRANSFER_HALF_LIFE,
)

ROOT = Path(__file__).resolve().parent.parent
CASES_OUT = ROOT / "scripts" / "transfer-cases.json"

CLUBS = [f"Club {chr(ord('A') + i)}" for i in range(6)]
SHAPE = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
HORIZON = [1, 2, 3]


def league(points, price_override=None, missing=None) -> tuple[Projection, pd.DataFrame]:
    """A small synthetic projection -- see verify-transfer-rules.py's own
    `league` for the full reasoning; this is the same shape, fewer clubs."""
    prices = lambda pos, rank: {"GKP": 4.5, "DEF": 4.5,  # noqa: E731
                                "MID": 5.0, "FWD": 5.5}[pos] + rank * 0.5
    rows, fixture_rows = [], []
    player_id = 0
    for club in CLUBS:
        for pos, count in SHAPE.items():
            for rank in range(count):
                player_id += 1
                rows.append({
                    "fpl_id": player_id, "web_name": f"{club[-1]}{pos[0]}{rank}",
                    "full_name": f"{club} {pos} {rank}", "pos": pos,
                    "team": club, "team_short": club[-1],
                    "price": float((price_override or {}).get(player_id, prices(pos, rank))),
                    "p_play": 0.95, "p_start": 0.9, "status": "a",
                    "birth_date": pd.Timestamp("1998-01-01"),
                    "selected_by_percent": 5.0, "rank": rank,
                })
    players = pd.DataFrame(rows)

    per_fixture = []
    for _, player in players.iterrows():
        for gw in HORIZON:
            if (player["team"], gw) in set(missing or []):
                continue
            per_fixture.append({
                "fpl_id": int(player["fpl_id"]), "gw": gw,
                "xpts": float(points(int(player["fpl_id"]), player["pos"],
                                     int(player["rank"]), gw)),
                "was_home": True, "opponent": "opp",
            })

    for gw in HORIZON:
        playing = [c for c in CLUBS if (c, gw) not in set(missing or [])]
        for home, away in zip(playing[::2], playing[1::2]):
            fixture_rows.append({"gw": gw, "home_team": home, "away_team": away, "has_odds": True})

    projection = Projection(players=players, per_fixture=pd.DataFrame(per_fixture),
                            fixtures=pd.DataFrame(fixture_rows), horizon=list(HORIZON),
                            odds_coverage=1.0, odds_note="")
    return projection, players


def flat(base: dict[str, float]):
    return lambda pid, pos, rank, gw: base[pos] + rank * 0.6


def starting_fifteen(players: pd.DataFrame) -> list[int]:
    squad, per_club = [], {}
    for pos, count in SQUAD_BY_POS.items():
        taken = 0
        for _, player in players[players["pos"] == pos].sort_values("price").iterrows():
            if taken == count:
                break
            if per_club.get(player["team"], 0) >= MAX_PER_CLUB:
                continue
            squad.append(int(player["fpl_id"]))
            per_club[player["team"]] = per_club.get(player["team"], 0) + 1
            taken += 1
    return squad


CHIP_KEYS = ("wildcard", "freehit", "bboost", "3xc")


def dump_case(name: str, projection: Projection, players: pd.DataFrame, **kwargs) -> dict:
    """Solve one scenario and capture exactly the pool/points/opt transfers.js needs."""
    gameweeks = list(projection.horizon)[:kwargs.get("horizon", len(HORIZON))]
    squad = kwargs.get("squad") or []
    points = transfers.expected_points(projection, gameweeks)
    pool = transfers.candidate_pool(
        players, points, keep=list(squad) + list(kwargs.get("include") or []),
        min_minutes_prob=kwargs.get("min_minutes_prob", 0.3),
        exclude=kwargs.get("exclude"))

    chip_windows = kwargs.get("chip_windows") or {}
    chips_used = kwargs.get("chips_used")
    chips = transfers.chip_slots(chip_windows, gameweeks, chips_used)
    variation = transfers.fixture_variation(projection, gameweeks)
    skipped = {}
    if "freehit" in chips and not variation:
        chips.pop("freehit")
        skipped["freehit"] = "no blank or double to hit"

    captain_ids = pool.nlargest(CAPTAIN_CANDIDATES, "window_points")["fpl_id"].astype(int).tolist()

    plan = transfers.plan_transfers(projection, players, pool=pool, points=points, **kwargs)

    js_pool = [{
        "id": int(row["fpl_id"]), "pos": str(row["pos"]), "team": str(row["team"]),
        "price": float(row["price"]),
        "pts": [float(points.at[int(row["fpl_id"]), gw])
               if int(row["fpl_id"]) in points.index else 0.0 for gw in gameweeks],
    } for _, row in pool.iterrows()]

    ft_worth = {str(k): v for k, v in transfers.free_transfer_value().items()}
    hold_value = dict(CHIP_HOLD_VALUE)
    hold_value.update(kwargs.get("chip_hold") or {})

    opt = {
        "gameweeks": gameweeks,
        "budget": kwargs.get("budget", 100.0),
        "squad": list(squad),
        "bank": kwargs.get("bank", 0.0),
        "freeTransfers": kwargs.get("free_transfers", 1),
        "chips": {k: list(v) for k, v in chips.items()},
        "captainPool": captain_ids,
        "slotWeight": {str(k): v for k, v in DEFAULT_BENCH_SLOT_WEIGHTS.items()},
        "squadByPos": dict(SQUAD_BY_POS),
        "xiMinByPos": dict(XI_MIN_BY_POS),
        "xiMaxByPos": dict(XI_MAX_BY_POS),
        "squadSize": SQUAD_SIZE, "xiSize": XI_SIZE, "maxPerClub": MAX_PER_CLUB,
        "include": list(kwargs.get("include") or []), "exclude": list(kwargs.get("exclude") or []),
        "halfLife": TRANSFER_HALF_LIFE, "holdValue": hold_value,
        "friction": kwargs.get("friction", TRANSFER_FRICTION), "ftWorth": ft_worth,
        "maxFreeTransfers": MAX_FREE_TRANSFERS, "hitCost": HIT_COST,
        "bankValue": BANK_VALUE, "freeTransfersPerGw": FREE_TRANSFERS_PER_GW,
        "idleMovePenalty": IDLE_MOVE_PENALTY,
    }

    return {
        "name": name,
        "pool": js_pool,
        "opt": opt,
        "objective": round(plan.objective, 4),
        "squads": {str(gw): sorted(ids) for gw, ids in plan.squads.items()},
        "chipByGw": {str(int(row["gw"])): CHIP_LABELS_REVERSE[row["chip"]]
                    for _, row in plan.ledger.iterrows() if row["chip"]},
    }


CHIP_LABELS_REVERSE = {v: k for k, v in CHIP_LABELS.items()}


def main() -> None:
    cases = []

    # A hand-picked squad (cheapest legal XV) is not a squad the model would
    # ever choose: it is deliberately excluded from the captain pool (the top
    # 40 by window points), and with no cash spare an "owned, not preseason"
    # solve has no way to reach one of them either -- so it is infeasible
    # rather than merely suboptimal. Real squads don't have this problem
    # (nobody's real fifteen is the fifteen cheapest legal players), so every
    # scenario below starts from what the model itself would pick preseason,
    # same as scripts/verify-transfer-rules.py's oscillation and hit scenarios
    # do (`owned = solve(...).squads[HORIZON[0]]`).
    base = flat({"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2})
    settled, players = league(base)
    owned = transfers.plan_transfers(settled, players, horizon=3, seconds=30,
                                     min_minutes_prob=0.0).squads[1]
    cases.append(dump_case("hold — nothing to gain", settled, players,
                           squad=owned, free_transfers=1, bank=0.0, horizon=3, seconds=30))

    # A clean upgrade at two positions, well worth a hit.
    held = players[players["fpl_id"].isin(owned)]
    targets = []
    for _, player in held.iterrows():
        match = players[(players["pos"] == player["pos"]) & (players["price"] == player["price"])
                        & (~players["fpl_id"].isin(owned)) & (~players["fpl_id"].isin(targets))]
        if len(match):
            targets.append(int(match.iloc[0]["fpl_id"]))
        if len(targets) == 2:
            break

    def hit_points(pid, pos, rank, gw):
        return base(pid, pos, rank, gw) + (6.0 if pid in targets else 0.0)
    hit_proj, hit_players = league(hit_points)
    cases.append(dump_case("hit worth taking", hit_proj, hit_players,
                           squad=owned, free_transfers=1, bank=0.0, horizon=3, seconds=30))

    def small_points(pid, pos, rank, gw):
        return base(pid, pos, rank, gw) + (0.2 if pid in targets else 0.0)
    small_proj, small_players = league(small_points)
    cases.append(dump_case("upgrade too small to hit", small_proj, small_players,
                           squad=owned, free_transfers=1, bank=0.0, horizon=3, seconds=30))

    # Bench boost: one gameweek where the bench is worth a lot.
    def bboost_points(pid, pos, rank, gw):
        return base(pid, pos, rank, gw) + (9.0 if gw == 2 else 0.0)
    bb_proj, bb_players = league(bboost_points)
    cases.append(dump_case("bench boost worth it", bb_proj, bb_players,
                           squad=owned, free_transfers=1, bank=2.0, horizon=3, seconds=30,
                           chip_windows={"bboost": (1, 19)}, chip_hold={"bboost": 0.0}))

    # Triple captain: one huge scorer in gameweek one.
    star = int(held[held["pos"] == "MID"].iloc[0]["fpl_id"])

    def tc_points(pid, pos, rank, gw):
        return (30.0 if pid == star and gw == 1 else base(pid, pos, rank, gw))
    tc_proj, tc_players = league(tc_points)
    cases.append(dump_case("triple captain worth it", tc_proj, tc_players,
                           squad=owned, free_transfers=1, bank=0.0, horizon=3, seconds=30,
                           chip_windows={"3xc": (1, 19)}, chip_hold={"3xc": 0.0}))

    # Free hit: most of the league blanks in gameweek two. The owned squad is
    # the model's own preseason pick for the *undisturbed* calendar, so it is
    # a real squad that the blank then catches out.
    blank = [(club, 2) for club in CLUBS[:4]]
    fh_proj, fh_players = league(base, missing=blank)
    fh_owned = transfers.plan_transfers(settled, fh_players, horizon=3, seconds=30,
                                        min_minutes_prob=0.0).squads[1]
    cases.append(dump_case("free hit worth it", fh_proj, fh_players,
                           squad=fh_owned, free_transfers=1, bank=0.0, horizon=3, seconds=30,
                           chip_windows={"freehit": (1, 19)}, chip_hold={"freehit": 0.0}))

    # Wildcard: half the league jumps from gameweek two onward. The owned
    # squad is deliberately hand-picked from the *other* half -- cheap, and
    # walled off from the boost no run of free transfers can fully chase --
    # which is the scenario, not a bug; it needs bank to have room to act at
    # all, same as scripts/verify-transfer-rules.py's own wildcard scenario.
    def wc_points(pid, pos, rank, gw):
        return base(pid, pos, rank, gw) + (4.0 if pid % 2 == 0 and gw >= 2 else 0.0)
    wc_proj, wc_players = league(wc_points)
    wc_owned = starting_fifteen(wc_players[wc_players["fpl_id"] % 2 == 1])
    cases.append(dump_case("wildcard worth it", wc_proj, wc_players,
                           squad=wc_owned, free_transfers=1, bank=10.0, horizon=3, seconds=30,
                           chip_windows={"wildcard": (2, 19)}, chip_hold={"wildcard": 0.0}))

    # All four chips available at once, defaults -- exercises everything
    # together without any one chip being an obviously forced choice.
    all_windows = {"wildcard": (2, 19), "freehit": (2, 19), "bboost": (1, 19), "3xc": (1, 19)}
    cases.append(dump_case("all chips, defaults", settled, players,
                           squad=owned, free_transfers=1, bank=3.0, horizon=3, seconds=30,
                           chip_windows=all_windows))

    CASES_OUT.write_text(json.dumps(cases, separators=(",", ":")))
    print(f"{CASES_OUT}  {len(cases)} cases")


if __name__ == "__main__":
    main()

"""Proves the transfer plan obeys the transfer rules, on projections it cannot game.

The transfer model is the one part of this tool whose output cannot be sanity
checked by looking at it. A squad you can eyeball; a six-gameweek path through
the free-transfer state machine, with hits and chips, you cannot -- and the
failure mode is not a crash, it is a plan that is quietly illegal or quietly
oscillating.

So the checks run against synthetic projections built here, where the right
answer is known by construction. Each scenario is designed so that exactly one
behaviour is correct and the wrong behaviour is attractive:

  * a squad already optimal, so the only right move is to roll;
  * two players whose fixtures alternate, which is what produced the
    swap-and-swap-back schedule this model replaced -- checked both ways, since
    the same scenario solved without the transfer pricing has to bring the
    oscillation back or it was not testing anything;
  * an upgrade big enough to be worth four points, and one just too small;
  * half the league improving at once, which no run of free transfers can chase
    and a wildcard can;
  * a bench that outscores the starters, which only a bench boost can field;
  * a gameweek in which most of the squad has no fixture, which is a free hit;
  * a flat calendar, where every chip should be held.

    python scripts/verify-transfer-rules.py

Takes about ninety seconds: there are seventeen mixed-integer solves in here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplkit.config import (CHIP_HOLD_VALUE, CHIP_LABELS, HIT_COST,  # noqa: E402
                           MAX_FREE_TRANSFERS, MAX_PER_CLUB, SQUAD_BY_POS,
                           SQUAD_SIZE, XI_SIZE)
from fplkit.model import Projection  # noqa: E402
from fplkit import transfers  # noqa: E402

CLUBS = [f"Club {chr(ord('A') + i)}" for i in range(10)]
SHAPE = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
HORIZON = [1, 2, 3, 4, 5, 6]

# The reserve prices are keyed by the API's chip names; the plan reports the
# readable ones.
CHIP_HOLD_VALUE_BY_LABEL = {CHIP_LABELS[chip]: value
                            for chip, value in CHIP_HOLD_VALUE.items()}

failures: list[str] = []


def check(name: str, condition: bool, detail: str) -> None:
    print(f"{'ok  ' if condition else 'FAIL'}  {name}: {detail}")
    if not condition:
        failures.append(name)


# --------------------------------------------------------------------------- #
# A league the tests can reason about
# --------------------------------------------------------------------------- #

def league(points, prices=None, missing=None,
           price_override=None) -> tuple[Projection, pd.DataFrame]:
    """A synthetic projection: ten identical clubs, priced on a fixed ladder.

    `points(player_id, position, rank, gw)` returns the expected points for one
    player in one gameweek, which is the only thing a scenario has to define.
    `missing` names (team, gw) pairs that have no fixture, which is how blanks
    and doubles get onto the calendar.
    """
    prices = prices or (lambda pos, rank: {"GKP": 4.5, "DEF": 4.5,
                                           "MID": 5.0, "FWD": 5.5}[pos] + rank * 0.5)
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
                    "price": float((price_override or {}).get(player_id,
                                                              prices(pos, rank))),
                    "p_play": 0.95,
                    "p_start": 0.9, "status": "a", "birth_date": pd.Timestamp("1998-01-01"),
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
            fixture_rows.append({"gw": gw, "home_team": home, "away_team": away,
                                 "has_odds": True})

    projection = Projection(
        players=players, per_fixture=pd.DataFrame(per_fixture),
        fixtures=pd.DataFrame(fixture_rows), horizon=list(HORIZON),
        odds_coverage=1.0, odds_note="")
    return projection, players


def flat(base: dict[str, float]):
    """Every player scores by position and rank, the same every gameweek."""
    return lambda pid, pos, rank, gw: base[pos] + rank * 0.6


def solve(projection, players, **kwargs) -> transfers.TransferPlan:
    defaults = dict(horizon=len(HORIZON), budget=100.0, min_minutes_prob=0.0,
                    chip_windows={}, seconds=60)
    return transfers.plan_transfers(projection, players, **{**defaults, **kwargs})


# --------------------------------------------------------------------------- #
# Rule conformance
# --------------------------------------------------------------------------- #

def check_legality(plan: transfers.TransferPlan, players: pd.DataFrame,
                   label: str) -> None:
    """Every gameweek's squad is one FPL would let you register."""
    by_id = players.set_index("fpl_id")
    sizes, shapes, clubs = set(), set(), []
    for gw, squad in plan.squads.items():
        sizes.add(len(squad))
        held = by_id.loc[squad]
        shapes.add(tuple(sorted(held["pos"].value_counts().to_dict().items())))
        clubs.append(int(held["team"].value_counts().max()))

    check(f"{label}: squad size", sizes == {SQUAD_SIZE}, f"sizes seen {sorted(sizes)}")
    check(f"{label}: positions", shapes == {tuple(sorted(SQUAD_BY_POS.items()))},
          f"{len(shapes)} distinct shape(s), all {'legal' if len(shapes) == 1 else 'NOT legal'}")
    check(f"{label}: club limit", max(clubs) <= MAX_PER_CLUB,
          f"at most {max(clubs)} from one club (limit {MAX_PER_CLUB})")

    xi = plan.lineups["starting_xi"].str.split(", ").apply(len)
    expected = [SQUAD_SIZE if chip == "Bench Boost" else XI_SIZE
                for chip in plan.lineups["chip"]]
    check(f"{label}: XI size", list(xi) == expected,
          f"fielded {list(xi)}, expected {expected}")


def check_ledger(plan: transfers.TransferPlan, opening: int, label: str) -> None:
    """The free-transfer balance follows the rule, gameweek by gameweek.

    ft(w+1) = clamp(ft(w) - transfers + 1, 1, 5), and a wildcard or free hit
    cancels the +1 while leaving what was banked alone.
    """
    ledger = plan.ledger
    check(f"{label}: opening balance", int(ledger["free"].iloc[0]) == opening,
          f"starts on {int(ledger['free'].iloc[0])} free transfer(s)")

    ok, detail = True, []
    for step in range(len(ledger) - 1):
        row, nxt = ledger.iloc[step], ledger.iloc[step + 1]
        chip = row["chip"] in ("Wildcard", "Free Hit")
        spent = 0 if row["chip"] == "Wildcard" else int(row["transfers"])
        raw = int(row["free"]) - spent + (0 if chip else 1)
        expect = min(max(raw, 1), MAX_FREE_TRANSFERS)
        if int(nxt["free"]) != expect:
            ok = False
            detail.append(f"GW{int(nxt['gw'])} is {int(nxt['free'])}, rule says {expect}")
    check(f"{label}: free-transfer recursion", ok,
          "; ".join(detail) if detail else "every gameweek follows the rule")

    check(f"{label}: cap", ledger["free"].max() <= MAX_FREE_TRANSFERS,
          f"peaks at {int(ledger['free'].max())} (cap {MAX_FREE_TRANSFERS})")

    hits_ok = all(
        int(row["hits"]) == max(0, (0 if row["chip"] == "Wildcard"
                                    else int(row["transfers"])) - int(row["free"]))
        for _, row in ledger.iterrows())
    check(f"{label}: hits charged", hits_ok,
          f"{int(ledger['hits'].sum())} hit(s), costing {HIT_COST * int(ledger['hits'].sum()):.0f}")

    paid_for = all(row["chip"] == "Wildcard"
                   or int(row["transfers"]) <= int(row["free"]) + int(row["hits"])
                   for _, row in ledger.iterrows())
    check(f"{label}: every move is paid for", paid_for,
          "each transfer comes out of a free one, a hit, or a wildcard")


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_settled() -> None:
    """Nothing to gain, so nothing should be bought."""
    projection, players = league(flat({"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}))
    plan = solve(projection, players)
    check_legality(plan, players, "settled")
    check_ledger(plan, 0, "settled")
    check("settled: no churn", plan.moves.empty,
          f"{len(plan.moves)} transfer(s) in a window where every swap is a wash")
    check("settled: banks to the cap",
          int(plan.ledger["free"].iloc[-1]) == min(len(HORIZON) - 1, MAX_FREE_TRANSFERS),
          f"ends holding {int(plan.ledger['free'].iloc[-1])}")


def scenario_oscillation() -> None:
    """The regression this whole model exists to fix.

    Two defenders whose fixtures alternate: whichever you own, the other one is
    better next week. A model that does not charge for spending a transfer will
    swap between them every gameweek forever. A model that does will own one of
    them and leave it alone.
    """
    # Two premium defenders at different clubs, priced so the budget fits one
    # and not both. That last part is the whole scenario: given the choice a
    # squad owns both and rotates them, which is a better answer than either
    # oscillating or sitting still, and the failure this is looking for cannot
    # happen. It is the week you cannot afford both that a transfer plan has to
    # be able to refuse.
    left, right, budget = 7, 22, 85.0

    def points(pid, pos, rank, gw):
        base = {"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}[pos] + rank * 0.6
        if pid == left:
            return base + 6.0 + (0.5 if gw % 2 else 0.0)
        if pid == right:
            return base + 6.0 + (0.0 if gw % 2 else 0.5)
        return base
    projection, players = league(points, price_override={left: 14.0, right: 14.0})

    owned = solve(projection, players, budget=budget).squads[HORIZON[0]]
    check("alternating: the budget forces a choice",
          len({left, right} & set(owned)) == 1,
          f"owns {len({left, right} & set(owned))} of the two premiums at £{budget:.0f}m")
    plan = solve(projection, players, squad=owned, free_transfers=1, budget=budget)
    check_legality(plan, players, "alternating")
    both_ways = set(plan.moves["in"]) & set(plan.moves["out"])
    check("alternating: nobody is bought and sold in the same window", not both_ways,
          f"{len(plan.moves)} transfer(s), {len(both_ways)} player(s) traded both ways "
          f"— the swing is 1.0 a gameweek, less than a free transfer is worth")

    # And the same projection, solved the way the deleted schedule was solved:
    # holding a transfer is worth nothing and acting costs nothing. If the
    # oscillation does not come back here, this scenario was not testing
    # anything and the check above is worthless.
    naive = solve(projection, players, squad=owned, free_transfers=1,
                  budget=budget, friction=0.0, ft_value_scale=0.0)
    churn = set(naive.moves["in"]) & set(naive.moves["out"])
    check("alternating: unpriced, the swap-and-swap-back comes straight back",
          bool(churn),
          f"{len(naive.moves)} transfer(s), {len(churn)} player(s) traded both ways "
          f"once a banked transfer is worth nothing and acting is free"
          if churn else "no churn even unpriced — the scenario proves nothing")


def scenario_hit() -> None:
    """A hit is taken when, and only when, it clears four points.

    The squad going in is the one this model itself picks when nothing is
    special about anyone, so there is nothing else to fix and the only question
    on the table is the upgrade. Then two players who are *not* owned -- at the
    same positions and the same prices as two who are, so no money changes
    hands -- become better. Run it once where they are worth far more than four
    points over the window and once where they are worth about one, and the
    answer should differ.
    """
    base = flat({"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2})
    settled, players = league(base)
    owned = solve(settled, players).squads[HORIZON[0]]

    held = players[players["fpl_id"].isin(owned)]
    targets = []
    for _, player in held.iterrows():
        match = players[(players["pos"] == player["pos"])
                        & (players["price"] == player["price"])
                        & (~players["fpl_id"].isin(owned))
                        & (~players["fpl_id"].isin(targets))]
        if len(match):
            targets.append(int(match.iloc[0]["fpl_id"]))
        if len(targets) == 2:
            break

    for bonus, expect_hit in ((6.0, True), (0.2, False)):
        def points(pid, pos, rank, gw, bonus=bonus):
            return base(pid, pos, rank, gw) + (bonus if pid in targets else 0.0)

        projection, pool = league(points)
        plan = solve(projection, pool, squad=owned, free_transfers=1, bank=0.0)
        check_legality(plan, pool, f"hit@{bonus}")
        check_ledger(plan, 1, f"hit@{bonus}")
        took = int(plan.ledger["hits"].sum()) > 0
        check(f"hit@{bonus}: {'takes' if expect_hit else 'refuses'} the hit",
              took == expect_hit,
              f"{int(plan.ledger['hits'].sum())} hit(s) for two upgrades worth "
              f"{bonus:.1f} a gameweek each "
              f"({bonus * len(HORIZON):.1f} over the window, against {HIT_COST:.0f} a hit)")


def scenario_wildcard() -> None:
    """A wildcard is fifteen free transfers that leave the bank untouched."""
    def points(pid, pos, rank, gw):
        base = {"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}[pos] + rank * 0.6
        # Half the league becomes far better from gameweek three onward, which
        # is more than any sequence of free transfers can chase.
        return base + (4.0 if pid % 2 == 0 and gw >= 3 else 0.0)

    projection, players = league(points)
    owned = _starting_fifteen(projection, players, only_odd=True)
    plan = solve(projection, players, squad=owned, free_transfers=1, bank=10.0,
                 chip_windows={"wildcard": (2, 19)},
                 chip_hold={"wildcard": 0.0})
    check_legality(plan, players, "wildcard")
    check_ledger(plan, 1, "wildcard")

    played = plan.ledger[plan.ledger["chip"] == "Wildcard"]
    check("wildcard: played", len(played) == 1,
          f"played in GW{int(played['gw'].iloc[0])}" if len(played) else "not played")
    if len(played):
        row = played.iloc[0]
        check("wildcard: costs no hits", int(row["hits"]) == 0,
              f"{int(row['transfers'])} transfers, {int(row['hits'])} hits")
        step = list(plan.ledger["gw"]).index(row["gw"])
        after = plan.ledger.iloc[step + 1] if step + 1 < len(plan.ledger) else None
        if after is not None:
            check("wildcard: banked transfers survive it",
                  int(after["free"]) == int(row["free"]),
                  f"held {int(row['free'])} before, {int(after['free'])} after")


def scenario_bench_boost() -> None:
    """A bench worth more than the bench weights say is only reachable with the chip."""
    def points(pid, pos, rank, gw):
        base = {"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}[pos] + rank * 0.6
        return base + (9.0 if gw == 4 else 0.0)

    projection, players = league(points)
    plan = solve(projection, players, chip_windows={"bboost": (1, 19)},
                 chip_hold={"bboost": 0.0})
    check_legality(plan, players, "bench boost")
    played = plan.lineups[plan.lineups["chip"] == "Bench Boost"]
    check("bench boost: fields all fifteen", len(played) == 1
          and len(played["starting_xi"].iloc[0].split(", ")) == SQUAD_SIZE,
          f"{len(played)} boosted gameweek(s)")


def scenario_free_hit() -> None:
    """Eight of ten clubs blank in one gameweek, which is what a free hit is for."""
    blank = [(club, 4) for club in CLUBS[:8]]
    projection, players = league(
        flat({"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}), missing=blank)
    plan = solve(projection, players, chip_windows={"freehit": (2, 19)},
                 chip_hold={"freehit": 0.0})
    check_legality(plan, players, "free hit")
    played = plan.ledger[plan.ledger["chip"] == "Free Hit"]
    check("free hit: played into the blank",
          len(played) == 1 and int(played["gw"].iloc[0]) == 4,
          f"played in GW{int(played['gw'].iloc[0])}" if len(played) else "not played")
    check("free hit: the squad comes back",
          len(played) == 0 or plan.squads[4] == plan.squads[3],
          "the fifteen owned either side of the chip are the same fifteen")


def scenario_chip_hold() -> None:
    """With nothing to time a chip against, the plan should hold all four.

    This is the behaviour the old heuristic hard-coded as a refusal. Here it
    falls out of an arithmetic comparison against what the chip is worth held,
    which means it also knows when to stop refusing.
    """
    projection, players = league(flat({"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}))
    windows = {"wildcard": (2, 19), "freehit": (2, 19),
               "bboost": (1, 19), "3xc": (1, 19)}

    dear = solve(projection, players, chip_windows=windows,
                 chip_hold={chip: 100.0 for chip in windows})
    check("chip hold: a reserve nothing can clear holds every chip",
          (dear.ledger["chip"] == "").all(),
          "all four held" if (dear.ledger["chip"] == "").all()
          else f"played {sorted(set(dear.ledger['chip']) - {''})}")

    free = solve(projection, players, chip_windows=windows,
                 chip_hold={chip: 0.0 for chip in windows})
    check("chip hold: a reserve of nothing spends them",
          (free.ledger["chip"] != "").sum() >= 2,
          f"{int((free.ledger['chip'] != '').sum())} chip(s) played once holding "
          f"is worth nothing")

    # And in between: a chip is played only when it out-earns its own reserve.
    default = solve(projection, players, chip_windows=windows)
    worth = default.chips.set_index("chip")["worth"].to_dict()
    respected = all(
        pd.isna(worth.get(row["chip"])) or worth[row["chip"]] >= CHIP_HOLD_VALUE_BY_LABEL[row["chip"]]
        for _, row in default.ledger.iterrows() if row["chip"])
    played = sorted(set(default.ledger["chip"]) - {""})
    check("chip hold: what is played clears its own reserve", respected,
          f"played {played or 'nothing'}" + (
              f", worth {', '.join(f'{c} {worth[c]:.1f} vs {CHIP_HOLD_VALUE_BY_LABEL[c]:.0f}' for c in played if not pd.isna(worth.get(c)))}"
              if played else ""))


def _starting_fifteen(projection, players, exclude=None, only_odd=False) -> list[int]:
    """A legal fifteen to own going in, chosen without the transfer model."""
    pool = players[~players["fpl_id"].isin(exclude or [])]
    if only_odd:
        pool = pool[pool["fpl_id"] % 2 == 1]
    squad, per_club = [], {}
    for pos, count in SQUAD_BY_POS.items():
        taken = 0
        for _, player in pool[pool["pos"] == pos].sort_values("price").iterrows():
            if taken == count:
                break
            if per_club.get(player["team"], 0) >= MAX_PER_CLUB:
                continue
            squad.append(int(player["fpl_id"]))
            per_club[player["team"]] = per_club.get(player["team"], 0) + 1
            taken += 1
    return squad


def main() -> int:
    for scenario in (scenario_settled, scenario_oscillation, scenario_hit,
                     scenario_wildcard, scenario_bench_boost, scenario_free_hit,
                     scenario_chip_hold):
        print(f"\n{scenario.__name__.replace('scenario_', '').replace('_', ' ')}")
        print("-" * 72)
        scenario()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
  * a bench that outscores the starters in the *middle* of the window, which
    only a bench boost can field and only in that gameweek;
  * a league where the cheap bench fodder scores nothing, so a bench boost is
    only worth playing if the fifteen bought weeks earlier was bought with it
    in mind -- the check that a chip is a squad decision and not a lineup one;
  * one player with one enormous gameweek, likewise not the first, which is a
    triple captain;
  * a gameweek in which most of the squad has no fixture, which is a free hit;
  * a flat calendar, where every chip should be held;
  * the same flat calendar with two chips forced, where they should be played
    anyway and the reserve that was holding them should stop applying.

Three of those put the gameweek a chip wants somewhere other than the first,
which is the check that a chip is weighed in every gameweek of the horizon
rather than only the one in front of it. The discount makes GW1 the cheapest
week to play anything in, so a model that is not really looking ahead lands
there and these scenarios catch it.

    python scripts/verify-transfer-rules.py

Takes a couple of minutes: there are twenty-one mixed-integer solves in here.
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

    ft(w+1) = clamp(ft(w) - transfers + 1, 1, 5), and a free hit cancels the +1
    while leaving what was banked alone.
    """
    ledger = plan.ledger
    check(f"{label}: opening balance", int(ledger["free"].iloc[0]) == opening,
          f"starts on {int(ledger['free'].iloc[0])} free transfer(s)")

    ok, detail = True, []
    for step in range(len(ledger) - 1):
        row, nxt = ledger.iloc[step], ledger.iloc[step + 1]
        raw = (int(row["free"]) - int(row["transfers"])
               + (0 if row["chip"] == "Free Hit" else 1))
        expect = min(max(raw, 1), MAX_FREE_TRANSFERS)
        if int(nxt["free"]) != expect:
            ok = False
            detail.append(f"GW{int(nxt['gw'])} is {int(nxt['free'])}, rule says {expect}")
    check(f"{label}: free-transfer recursion", ok,
          "; ".join(detail) if detail else "every gameweek follows the rule")

    check(f"{label}: cap", ledger["free"].max() <= MAX_FREE_TRANSFERS,
          f"peaks at {int(ledger['free'].max())} (cap {MAX_FREE_TRANSFERS})")

    hits_ok = all(
        int(row["hits"]) == max(0, int(row["transfers"]) - int(row["free"]))
        for _, row in ledger.iterrows())
    check(f"{label}: hits charged", hits_ok,
          f"{int(ledger['hits'].sum())} hit(s), costing {HIT_COST * int(ledger['hits'].sum()):.0f}")

    paid_for = all(int(row["transfers"]) <= int(row["free"]) + int(row["hits"])
                   for _, row in ledger.iterrows())
    check(f"{label}: every move is paid for", paid_for,
          "each transfer comes out of a free one or a hit")


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


BOOST_GW = 4
STAR_GW = 5


def scenario_bench_boost() -> None:
    """A bench worth more than the bench weights say is only reachable with the chip.

    The bump is in the middle of the window rather than the first gameweek, so
    the gameweek it lands in is itself a check: every gameweek carries its own
    `use_bboost` binary, and if only the first were really in play the plan
    would take GW1 -- which the discount makes the cheapest week to play
    anything in, and which is therefore where a chip model that is not looking
    ahead ends up.
    """
    def points(pid, pos, rank, gw):
        base = {"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}[pos] + rank * 0.6
        return base + (9.0 if gw == BOOST_GW else 0.0)

    projection, players = league(points)
    plan = solve(projection, players, chip_windows={"bboost": (1, 19)},
                 chip_hold={"bboost": 0.0})
    check_legality(plan, players, "bench boost")
    played = plan.lineups[plan.lineups["chip"] == "Bench Boost"]
    check("bench boost: fields all fifteen", len(played) == 1
          and len(played["starting_xi"].iloc[0].split(", ")) == SQUAD_SIZE,
          f"{len(played)} boosted gameweek(s)")
    check("bench boost: waits for the gameweek worth boosting",
          len(played) == 1 and int(played["gw"].iloc[0]) == BOOST_GW,
          f"played in GW{int(played['gw'].iloc[0])}, the big bench week is "
          f"GW{BOOST_GW}" if len(played) else "not played")


def scenario_bench_boost_reshapes_the_squad() -> None:
    """The chip changes which fifteen you buy, not just who you field.

    This is the load-bearing claim in transfers.py's docstring -- that a chip is
    a squad decision taken weeks earlier -- and it is the one that separates
    solving chips inside the transfer model from bolting a chip report onto the
    side of it. Everything above would still pass if the chip only ever changed
    a lineup.

    The league is built so the two answers are visibly different. The cheapest
    tier scores nothing at all: he is the £4.0m defender who never plays, and on
    an ordinary bench he is close to free, because a benched player is only
    scored at his slot's weight. Under a bench boost he is a hole in the eleven,
    so the chip should pay £0.5m a man to replace him -- out of the budget the
    starting eleven was going to use.

    The boost is pinned to GW2, and the plan opens preseason with no banked
    transfer, so it cannot buy its way to a better bench at GW2 without taking
    hits. If the squad is really solved around the chip, the GW1 fifteen has to
    differ. If it is not, GW1 is identical and only the GW2 lineup moves.
    """
    ladder_points = [0.0, 4.0, 6.0, 8.0, 10.0]
    ladder_price = [4.0, 4.5, 5.5, 7.0, 9.0]
    horizon, boost_gw = 3, 2
    window = HORIZON[:horizon]

    projection, players = league(lambda pid, pos, rank, gw: ladder_points[rank],
                                 prices=lambda pos, rank: ladder_price[rank])
    pts = transfers.expected_points(projection, window)
    by_pos = {int(k): str(v) for k, v in players.set_index("fpl_id")["pos"].items()}

    def bench_worth(plan):
        series = transfers.chip_payout_series(plan.squads, pts, window, by_pos)
        return series["bboost"][boost_gw]

    plain = solve(projection, players, horizon=horizon, chip_windows={})
    boosted = solve(projection, players, horizon=horizon,
                    chip_windows={"bboost": (boost_gw, boost_gw)},
                    force_chips=["bboost"])
    check_legality(boosted, players, "bench boost squad")

    before = len(set(boosted.squads[HORIZON[0]]) ^ set(plain.squads[HORIZON[0]])) // 2
    check("bench boost squad: the fifteen is bought differently before the chip",
          before > 0,
          f"{before} of 15 differ in GW{HORIZON[0]} for a chip played in "
          f"GW{boost_gw}")

    plain_bench, boosted_bench = bench_worth(plain), bench_worth(boosted)
    check("bench boost squad: and the bench it buys is worth fielding",
          boosted_bench > plain_bench + 1.0,
          f"bench worth {boosted_bench:.1f} in the boosted plan against "
          f"{plain_bench:.1f} in the plan that never gets the chip")


def scenario_triple_captain() -> None:
    """One player has one enormous gameweek, and it is not the first one.

    Same argument as the bench boost above, on the other chip whose payout is a
    single gameweek's points: the answer is only right if all six `use_3xc`
    binaries are live and each is scored against that gameweek's own captain.
    """
    def points(pid, pos, rank, gw):
        base = {"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}[pos] + rank * 0.6
        return 30.0 if pid == 1 and gw == STAR_GW else base

    projection, players = league(points)
    plan = solve(projection, players, chip_windows={"3xc": (1, 19)},
                 chip_hold={"3xc": 0.0})
    check_legality(plan, players, "triple captain")
    played = plan.ledger[plan.ledger["chip"] == "Triple Captain"]
    check("triple captain: waits for the gameweek worth tripling",
          len(played) == 1 and int(played["gw"].iloc[0]) == STAR_GW,
          f"played in GW{int(played['gw'].iloc[0])}, the 30-point week is "
          f"GW{STAR_GW}" if len(played) else "not played")
    if len(played):
        captain = plan.lineups.loc[plan.lineups["gw"] == STAR_GW, "captain"].iloc[0]
        check("triple captain: the armband goes on the right player",
              captain.endswith("(3x)"), f"captained {captain}")


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


FLAT_WINDOWS = {"freehit": (2, 19), "bboost": (1, 19), "3xc": (1, 19)}


def scenario_chip_hold() -> None:
    """With nothing to time a chip against, the plan should hold every one.

    This is the behaviour the old heuristic hard-coded as a refusal. Here it
    falls out of an arithmetic comparison against what the chip is worth held,
    which means it also knows when to stop refusing.
    """
    projection, players = league(flat({"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}))
    windows = FLAT_WINDOWS

    dear = solve(projection, players, chip_windows=windows,
                 chip_hold={chip: 100.0 for chip in windows})
    check("chip hold: a reserve nothing can clear holds every chip",
          (dear.ledger["chip"] == "").all(),
          "all held" if (dear.ledger["chip"] == "").all()
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


def scenario_forced_chips() -> None:
    """Forcing a chip overrules the reserve that was holding it, and only that.

    Same flat calendar the scenario above holds every chip on, so anything
    played here is played because it was forced. The gameweek is still the
    solver's to pick, and the third check is what says the reserve is dropped
    rather than merely out-earned: a reserve still in the objective would be
    cheapest in the last gameweek of the window, because the discount shrinks
    it, and a forced chip would drift there regardless of the points.
    """
    projection, players = league(flat({"GKP": 3.0, "DEF": 3.5, "MID": 4.0, "FWD": 4.2}))
    forced = ["bboost", "3xc"]

    plan = solve(projection, players, chip_windows=FLAT_WINDOWS, force_chips=forced)
    check_legality(plan, players, "forced chips")
    # Preseason, so the opening balance is zero: the first free transfer is
    # earned for the second gameweek, not handed out before the first.
    check_ledger(plan, 0, "forced chips")

    played = sorted(set(plan.ledger["chip"]) - {""})
    check("forced chips: both are played", played == ["Bench Boost", "Triple Captain"],
          f"played {played or 'nothing'} on a calendar that holds everything by default")

    boosted = plan.lineups[plan.lineups["chip"] == "Bench Boost"]
    check("forced chips: the bench boost still fields fifteen",
          len(boosted) == 1
          and len(boosted["starting_xi"].iloc[0].split(", ")) == SQUAD_SIZE,
          f"{len(boosted)} boosted gameweek(s)")

    weeks = [int(row["gw"]) for _, row in plan.ledger.iterrows() if row["chip"]]
    check("forced chips: not dumped in the last gameweek to dodge the reserve",
          all(gw != HORIZON[-1] for gw in weeks) or len(set(weeks)) > 1,
          f"played in GW{', GW'.join(str(gw) for gw in weeks)} of "
          f"GW{HORIZON[0]}-GW{HORIZON[-1]}")

    verdicts = plan.chips[plan.chips["gw"].notna()]["verdict"]
    check("forced chips: the report says they were forced",
          len(verdicts) and all(v.startswith("forced") for v in verdicts),
          "; ".join(verdicts) if len(verdicts) else "no chip rows")

    try:
        solve(projection, players, chip_windows=FLAT_WINDOWS,
              force_chips=["bboost"], forbid_chips=["bboost"])
    except ValueError as error:
        check("forced chips: forcing and forbidding the same chip is refused",
              "forced and forbidden" in str(error), str(error))
    else:
        check("forced chips: forcing and forbidding the same chip is refused",
              False, "the solve was attempted anyway")

    try:
        solve(projection, players, chip_windows=FLAT_WINDOWS, chips_used=["3xc"],
              force_chips=["3xc"])
    except ValueError as error:
        check("forced chips: forcing a spent chip is refused",
              "cannot force" in str(error), str(error))
    else:
        check("forced chips: forcing a spent chip is refused",
              False, "the solve was attempted anyway")


def main() -> int:
    for scenario in (scenario_settled, scenario_oscillation, scenario_hit,
                     scenario_bench_boost, scenario_bench_boost_reshapes_the_squad,
                     scenario_triple_captain, scenario_free_hit,
                     scenario_chip_hold, scenario_forced_chips):
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

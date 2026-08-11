"""Transfers and chips as one decision, solved over the whole window.

There used to be a transfer schedule here, and it was deleted for a good
reason. It produced this:

    gw 4   transfer   Guéhi   -> Lacroix   +0.60
    gw 5   transfer   Lacroix -> Guéhi     +0.40
    gw 6   transfer   Guéhi   -> Lacroix   +0.59

Two free transfers burned to end up where you started. The diagnosis at the
time was that you cannot know your gameweek-six transfer in gameweek one, and
that part is still true. But it was the wrong diagnosis of *that* output. Those
three lines are not a model being over-confident about the future; they are a
model that does not know a transfer is a resource. Each week it re-asked "what
is the best squad for this week?" and paid whatever it cost to get there,
because nothing in the objective charged it for spending.

Three things have to be in the objective before a transfer plan means anything,
and all three are mechanical rather than predictive:

  1. **Transfers are a stock, not a flow.** One a gameweek, banked to a maximum
     of five, and everything past the allowance costs four points. That is an
     inventory problem with a hard cap, and it is the reason "hold this week and
     do two next week" is a real move rather than a delay.
  2. **A banked transfer is worth points you have not scored yet.** Holding
     looks free to a solver that only counts points on the pitch, so it will
     always spend. Pricing the bank -- and pricing it with diminishing returns,
     because the fifth one can only be used in a week you also use the other
     four -- is what makes rolling a decision the model can reach.
  3. **Acting has to cost something.** Between two players a tenth of a point
     apart, the model's own error is an order of magnitude larger than the gap.
     A flat friction charge on every move buys nothing except a refusal to
     trade on noise, which is precisely what the oscillating schedule was.

With those in, the swap-and-swap-back disappears without being banned, which is
the test that the fix is the right one rather than a patch over the symptom.

The chips belong in the same problem rather than in a report next to it. Every
one of them is a statement about the squad: a bench boost is worth playing only
if the bench is worth fielding, and whether the bench is worth fielding is a
transfer decision made three gameweeks earlier. A wildcard is a week with
fifteen free transfers, so it competes directly against the transfers either
side of it. Solving them separately gets both wrong.

What has *not* changed is the honesty about the horizon. The plan is re-decided
every week with information this run does not have, so only the first
gameweek's move is a decision; the rest is the shape that move is part of.
`value_of_acting` puts a number on exactly that first decision by re-solving
with this week banned, and the chip report says plainly when there is nothing
in the fixture list to time a chip against.

Formulation follows the FPL solver community's standard multi-period model
(`sertalpbilal/FPL-Optimization-Tools`), including the free-transfer state
machine and the friction and bank terms; the free-transfer values are its
published defaults. Two things here differ deliberately, both noted at the
constants below: the discount is gentler, and the horizon's final free-transfer
balance is credited rather than transfers being banned in the last gameweek.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pulp

from .config import (
    BANK_VALUE,
    CHIP_HOLD_VALUE,
    CHIP_LABELS,
    CHIPS,
    DEFAULT_BENCH_SLOT_WEIGHTS,
    DEFAULT_BUDGET,
    FREE_TRANSFERS_PER_GW,
    FT_VALUE,
    FT_VALUE_BY_STATE,
    HIT_COST,
    MAX_FREE_TRANSFERS,
    MAX_PER_CLUB,
    SQUAD_BY_POS,
    SQUAD_SIZE,
    TRANSFER_FRICTION,
    XI_MAX_BY_POS,
    XI_MIN_BY_POS,
    XI_SIZE,
)
from .model import Projection
from .planning import _best_xi_ids, fixture_counts, survival_curve

# The plan's own half-life is 3 gameweeks, and three of the reasons it is that
# short are optionality: a bad fixture in five gameweeks is not one you are
# locked into, because you will have made transfers by then. This model makes
# that optionality explicit -- it *is* the transfers -- so discounting at 3
# again would charge for it twice, and the plan would refuse to look past the
# fortnight it can already see.
#
# What is left to discount is genuine information decay: injuries, form and
# fixtures that are not priced yet. 6.5 gameweeks is a per-gameweek factor of
# 0.90, which is where the solver community's discount sits (0.84-0.90 across
# the two most used implementations) once optionality is modelled rather than
# assumed.
TRANSFER_HALF_LIFE = 6.5

# How far ahead to plan. Long enough that banking a transfer for a fixture swing
# is representable, short enough that the last gameweek is not pure fiction.
DEFAULT_TRANSFER_HORIZON = 6

# The candidate pool. Every player-gameweek is a handful of binaries, so the
# pool size sets the solve time; these caps keep it to a few thousand and still
# leave every plausible pick in. Bench fodder survives on value rather than
# points, which is why the pool is a union of two rankings.
POOL_BY_POS = {"GKP": 12, "DEF": 45, "MID": 45, "FWD": 25}

# Captaincy is not a free choice in practice -- it goes to a premium attacker
# almost every week -- so only the best players get a captain binary. Generous
# enough that the constraint never binds on anything the model would pick.
CAPTAIN_CANDIDATES = 40

# A transfer that changes nothing is free under a wildcard, so the solver is
# indifferent between rebuilding thirteen players and rebuilding two and will
# return whichever it reached first. This is too small to outweigh any real
# gain and large enough to break the tie toward leaving the squad alone.
IDLE_MOVE_PENALTY = 0.01

SOLVER_SECONDS = 120


@dataclass
class TransferPlan:
    """A squad path: what to own each gameweek, and what it costs to get there."""

    gameweeks: list[int]
    squads: dict[int, list[int]]  # gameweek -> the 15 owned (free-hit weeks excepted)
    moves: pd.DataFrame  # one row per transfer: gw, out, in, prices
    ledger: pd.DataFrame  # per gameweek: transfers, free, hits, chip, bank, xpts
    lineups: pd.DataFrame  # per gameweek: XI, bench order, captain, chip
    chips: pd.DataFrame  # chip, gw, what it is worth, whether that is signal
    objective: float
    status: str
    horizon_points: float  # undiscounted XI points over the window, chips included
    notes: list[str] = field(default_factory=list)

    @property
    def this_week(self) -> pd.DataFrame:
        """Only the first gameweek's moves -- the part that is a decision."""
        if self.moves.empty:
            return self.moves
        return self.moves[self.moves["gw"] == self.gameweeks[0]]


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

def decay_factors(gameweeks: list[int],
                  half_life: float | None = TRANSFER_HALF_LIFE) -> dict[int, float]:
    """Geometric discount per gameweek, keyed by gameweek."""
    if half_life is None or math.isinf(half_life):
        return {gw: 1.0 for gw in gameweeks}
    return {gw: 0.5 ** (step / half_life) for step, gw in enumerate(gameweeks)}


def expected_points(projection: Projection,
                    gameweeks: list[int] | None = None) -> pd.DataFrame:
    """Per-player, per-gameweek points, discounted only for availability.

    This is the raw projection multiplied by the survival curve, and nothing
    else. The time discount is applied in the objective a gameweek at a time,
    because the objective also has to discount hits and banked transfers, and
    those are not per-player quantities.
    """
    raw = (projection.per_fixture
           .pivot_table(index="fpl_id", columns="gw", values="xpts", aggfunc="sum"))
    gameweeks = gameweeks or sorted(projection.per_fixture["gw"].unique())
    raw = raw.reindex(columns=gameweeks).fillna(0.0)

    players = projection.players.set_index("fpl_id").loc[raw.index].reset_index()
    survival = survival_curve(players, gameweeks)
    survival.index = raw.index
    return raw * survival


def candidate_pool(players: pd.DataFrame, points: pd.DataFrame,
                   keep: list[int] | None = None,
                   min_minutes_prob: float = 0.0,
                   exclude: list[int] | None = None,
                   caps: dict[str, int] | None = None) -> pd.DataFrame:
    """Shrink the league to the players a plan could plausibly want.

    Two rankings, unioned: total points over the window, and points per million.
    The first finds the players you build around, the second finds the £4.0m
    defender who never plays but has to be somewhere. Ranking on either alone
    produces a pool that cannot field a legal squad inside the budget.

    Anyone already owned is kept unconditionally, whatever he now looks like --
    a plan that cannot see your own squad cannot tell you to sell it.
    """
    caps = caps or POOL_BY_POS
    keep = set(keep or [])

    pool = players[players["fpl_id"].isin(points.index)].copy()
    pool["window_points"] = pool["fpl_id"].map(points.sum(axis=1)).fillna(0.0)
    pool["window_value"] = pool["window_points"] / pool["price"].replace(0, np.nan)

    eligible = pool["fpl_id"].isin(keep)
    if min_minutes_prob > 0 and "p_play" in pool.columns:
        eligible |= pool["p_play"] >= min_minutes_prob
    else:
        eligible |= True
    pool = pool[eligible]
    if exclude:
        pool = pool[pool["fpl_id"].isin(keep) | ~pool["fpl_id"].isin(exclude)]

    chosen: list[pd.DataFrame] = []
    for position, cap in caps.items():
        block = pool[pool["pos"] == position]
        by_points = block.nlargest(cap, "window_points")
        by_value = block.nlargest(max(cap // 2, 6), "window_value")
        forced = block[block["fpl_id"].isin(keep)]
        chosen.append(pd.concat([by_points, by_value, forced]))

    return (pd.concat(chosen)
            .drop_duplicates(subset="fpl_id")
            .reset_index(drop=True))


def free_transfer_value() -> dict[int, float]:
    """Cumulative worth of holding s banked transfers, for s in 0..5.

    The marginal values are the community solver's published defaults: the
    second banked transfer is the most valuable one to have (it is what turns
    a single move into a pair), and the fifth is worth least because it can
    only ever be spent in a week the other four are spent too.
    """
    value, running = {0: 0.0}, 0.0
    for state in range(1, MAX_FREE_TRANSFERS + 1):
        running += FT_VALUE_BY_STATE.get(state, FT_VALUE)
        value[state] = running
    return value


# --------------------------------------------------------------------------- #
# Chip windows
# --------------------------------------------------------------------------- #

def chip_slots(windows: dict[str, tuple[int, int]], gameweeks: list[int],
               already_used: list[str] | None = None) -> dict[str, list[int]]:
    """Gameweeks in the horizon where each chip may legally be played.

    A chip with no legal gameweek is dropped entirely rather than carried as a
    variable that can only take one value; the wildcard's `start_event` of 2 is
    what removes it from a gameweek-one plan.
    """
    used = set(already_used or [])
    slots = {}
    for chip, (start, stop) in windows.items():
        if chip in used or chip not in CHIPS:
            continue
        allowed = [gw for gw in gameweeks if start <= gw <= stop]
        if allowed:
            slots[chip] = allowed
    return slots


def fixture_variation(projection: Projection, gameweeks: list[int]) -> dict[int, str]:
    """Which gameweeks in the window contain a double or a blank.

    Chip value comes overwhelmingly from these, and they do not exist on the
    calendar until cup rounds are drawn and games are postponed. A window with
    none of them cannot rank one gameweek above another for a chip on anything
    but noise, and this is how the plan knows to say so.
    """
    counts = fixture_counts(projection).reindex(columns=gameweeks, fill_value=0)
    marks = {}
    for gw in gameweeks:
        doubles = int((counts[gw] >= 2).sum())
        blanks = int((counts[gw] == 0).sum())
        if doubles or blanks:
            marks[gw] = f"{doubles} double, {blanks} blank"
    return marks


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #

def plan_transfers(
    projection: Projection,
    players: pd.DataFrame,
    *,
    horizon: int = DEFAULT_TRANSFER_HORIZON,
    budget: float = DEFAULT_BUDGET,
    squad: list[int] | None = None,
    bank: float = 0.0,
    free_transfers: int = 1,
    sell_prices: dict[int, float] | None = None,
    chip_windows: dict[str, tuple[int, int]] | None = None,
    chips_used: list[str] | None = None,
    chip_hold: dict[str, float] | None = None,
    half_life: float | None = TRANSFER_HALF_LIFE,
    min_minutes_prob: float = 0.3,
    include: list[int] | None = None,
    exclude: list[int] | None = None,
    bench_weights: dict[object, float] | None = None,
    hit_limit: int | None = None,
    friction: float | None = None,
    ft_value_scale: float = 1.0,
    no_transfer_gws: list[int] | None = None,
    ban_first_gw_transfers: bool = False,
    forbid_chips: list[str] | None = None,
    seconds: int = SOLVER_SECONDS,
    pool: pd.DataFrame | None = None,
    points: pd.DataFrame | None = None,
) -> TransferPlan:
    """Solve the squad path, the transfers along it and the chip timing together.

    Args:
        squad: the fifteen you own now. `None` means preseason -- the first
            gameweek's squad is chosen freely, because before the opening
            deadline transfers are unlimited and free.
        bank: money not in the squad, in millions.
        free_transfers: how many you have available for the first gameweek.
        sell_prices: what each owned player sells for, if that differs from his
            listed price. FPL takes half of any rise back, rounded down to
            £0.1m, so this matters the moment a squad has been held a while.
        chip_hold: per-chip reservation price, overriding CHIP_HOLD_VALUE. A
            chip is played only when this gameweek beats what it is worth held
            for a well-timed one later. All zeroes turns the question back into
            "when in this window is each chip best?", which is a different and
            much weaker question.
        friction: per-transfer charge, overriding TRANSFER_FRICTION.
        ft_value_scale: scales what a banked free transfer is worth. Zero makes
            holding free, which is the state the old oscillating schedule was
            solved in.
        ban_first_gw_transfers: force a roll this week. Used to price the
            alternative to acting rather than to make a plan.
        forbid_chips: chips the solve may not use, for pricing what a chip is
            worth by taking it away.

    Returns a `TransferPlan`. `objective` is the discounted total the solver
    ranked on and is comparable only against another solve of the same window;
    `horizon_points` is the plain undiscounted XI total, which is not what was
    maximised and should not be used to compare two plans.
    """
    gameweeks = list(projection.horizon)[:horizon]
    if not gameweeks:
        raise ValueError("no gameweeks in the projection horizon")
    if not 0 <= free_transfers <= MAX_FREE_TRANSFERS:
        raise ValueError(f"free transfers must be 0-{MAX_FREE_TRANSFERS}, "
                         f"got {free_transfers}")

    points = expected_points(projection, gameweeks) if points is None else points[gameweeks]
    owned = list(squad or [])
    preseason = not owned

    if pool is None:
        pool = candidate_pool(players, points, keep=owned + list(include or []),
                              min_minutes_prob=min_minutes_prob, exclude=exclude)
    pool = pool.reset_index(drop=True)

    missing = set(owned) - set(pool["fpl_id"])
    if missing:
        raise ValueError(f"owned players are not in the projection: {sorted(missing)}")

    index = list(pool.index)
    by_id = {int(pool.at[i, "fpl_id"]): i for i in index}
    price = {i: float(pool.at[i, "price"]) for i in index}
    sell = {i: float((sell_prices or {}).get(int(pool.at[i, "fpl_id"]), price[i]))
            for i in index}
    position = {i: str(pool.at[i, "pos"]) for i in index}
    club = {i: str(pool.at[i, "team"]) for i in index}

    xpts = {(i, gw): float(points.at[int(pool.at[i, "fpl_id"]), gw])
            if int(pool.at[i, "fpl_id"]) in points.index else 0.0
            for i in index for gw in gameweeks}

    decay = decay_factors(gameweeks, half_life)
    slot_weight = dict(DEFAULT_BENCH_SLOT_WEIGHTS)
    slot_weight.update(bench_weights or {})
    slots = list(slot_weight)

    chips = chip_slots(chip_windows or {}, gameweeks, chips_used)
    for chip in (forbid_chips or []):
        chips.pop(chip, None)
    # A free hit needs somewhere to hit. With no blanks or doubles on the
    # calendar it is a week of unlimited transfers you have to hand back, which
    # is worth approximately nothing and costs a full second squad's worth of
    # binaries to discover.
    variation = fixture_variation(projection, gameweeks)
    skipped = {}
    if "freehit" in chips and not variation:
        chips.pop("freehit")
        skipped["freehit"] = "no blank or double to hit"

    problem = pulp.LpProblem("fpl_transfer_plan", pulp.LpMaximize)
    first, last = gameweeks[0], gameweeks[-1]
    terminal = last + 1

    # --- variables ---------------------------------------------------------
    in_squad = pulp.LpVariable.dicts("squad", (index, gameweeks), cat="Binary")
    in_xi = pulp.LpVariable.dicts("xi", (index, gameweeks), cat="Binary")
    in_slot = pulp.LpVariable.dicts("bench", (index, gameweeks, slots), cat="Binary")

    captain_pool = list(pool["window_points"].nlargest(CAPTAIN_CANDIDATES).index) \
        if "window_points" in pool else index
    is_captain = pulp.LpVariable.dicts("capt", (captain_pool, gameweeks), cat="Binary")

    bought = pulp.LpVariable.dicts("in", (index, gameweeks), cat="Binary")
    sold = pulp.LpVariable.dicts("out", (index, gameweeks), cat="Binary")

    in_bank = pulp.LpVariable.dicts("bank", gameweeks, lowBound=0)
    ft = pulp.LpVariable.dicts("ft", gameweeks + [terminal], lowBound=0,
                               upBound=MAX_FREE_TRANSFERS, cat="Integer")
    ft_state = pulp.LpVariable.dicts("ftstate", (gameweeks + [terminal],
                                                 range(MAX_FREE_TRANSFERS + 1)),
                                     cat="Binary")
    spent = pulp.LpVariable.dicts("spent", gameweeks, lowBound=0,
                                  upBound=SQUAD_SIZE, cat="Integer")
    paid = pulp.LpVariable.dicts("hits", gameweeks, lowBound=0,
                                 upBound=SQUAD_SIZE, cat="Integer")
    over = pulp.LpVariable.dicts("ftover", gameweeks, cat="Binary")
    under = pulp.LpVariable.dicts("ftunder", gameweeks, cat="Binary")

    use = {chip: pulp.LpVariable.dicts(f"use_{chip}", allowed, cat="Binary")
           for chip, allowed in chips.items()}

    def played(chip: str, gw: int):
        """1 if `chip` is played in `gw`, as an expression usable anywhere."""
        return use[chip][gw] if chip in use and gw in use[chip] else 0

    triple = pulp.LpVariable.dicts("tc", (captain_pool, gameweeks), cat="Binary") \
        if "3xc" in chips else None
    free_hit_squad = pulp.LpVariable.dicts("fhsquad", (index, gameweeks), cat="Binary") \
        if "freehit" in chips else None

    # --- squad legality ----------------------------------------------------
    for gw in gameweeks:
        problem += pulp.lpSum(in_squad[i][gw] for i in index) == SQUAD_SIZE
        for pos, count in SQUAD_BY_POS.items():
            members = [i for i in index if position[i] == pos]
            problem += pulp.lpSum(in_squad[i][gw] for i in members) == count
        for team in set(club.values()):
            members = [i for i in index if club[i] == team]
            problem += pulp.lpSum(in_squad[i][gw] for i in members) <= MAX_PER_CLUB

        for fpl_id in include or []:
            if fpl_id not in by_id:
                raise ValueError(f"player {fpl_id} was forced in but is not in the pool")
            problem += in_squad[by_id[fpl_id]][gw] == 1
        for fpl_id in exclude or []:
            if fpl_id in by_id:
                problem += in_squad[by_id[fpl_id]][gw] == 0

    # --- the free-hit squad ------------------------------------------------
    # A separate fifteen that exists for one gameweek and is handed back. It has
    # to be legal and affordable on its own, out of what selling the real squad
    # that week would raise, and it disappears entirely when the chip is not
    # played.
    if free_hit_squad is not None:
        for gw in chips["freehit"]:
            flag = use["freehit"][gw]
            problem += pulp.lpSum(free_hit_squad[i][gw] for i in index) == SQUAD_SIZE * flag
            for pos, count in SQUAD_BY_POS.items():
                members = [i for i in index if position[i] == pos]
                problem += pulp.lpSum(free_hit_squad[i][gw] for i in members) == count * flag
            for team in set(club.values()):
                members = [i for i in index if club[i] == team]
                problem += pulp.lpSum(free_hit_squad[i][gw] for i in members) <= MAX_PER_CLUB * flag
            problem += (pulp.lpSum(price[i] * free_hit_squad[i][gw] for i in index)
                        <= pulp.lpSum(sell[i] * in_squad[i][gw] for i in index) + in_bank[gw])
        for gw in gameweeks:
            for i in index:
                problem += free_hit_squad[i][gw] <= played("freehit", gw)

    # --- lineup ------------------------------------------------------------
    for gw in gameweeks:
        boost = played("bboost", gw)
        hit = played("freehit", gw)
        problem += pulp.lpSum(in_xi[i][gw] for i in index) == XI_SIZE + (SQUAD_SIZE - XI_SIZE) * boost

        for slot in slots:
            eligible = [i for i in index if (position[i] == "GKP") == (slot == "GKP")]
            # Bench boost fields the whole squad, so there is no bench to order.
            problem += pulp.lpSum(in_slot[i][gw][slot] for i in eligible) == 1 - boost
            for i in index:
                if i not in eligible:
                    problem += in_slot[i][gw][slot] == 0

        for i in index:
            benched = pulp.lpSum(in_slot[i][gw][s] for s in slots)
            problem += in_xi[i][gw] <= in_squad[i][gw] + hit
            problem += benched <= in_squad[i][gw] + hit
            if free_hit_squad is not None:
                # Under a free hit the eleven come from the borrowed squad
                # instead; either way exactly one of the two bounds binds.
                problem += in_xi[i][gw] <= free_hit_squad[i][gw] + (1 - hit)
                problem += benched <= free_hit_squad[i][gw] + (1 - hit)
            problem += in_xi[i][gw] + benched <= 1

        for pos in ("GKP", "DEF", "MID", "FWD"):
            members = [i for i in index if position[i] == pos]
            problem += pulp.lpSum(in_xi[i][gw] for i in members) >= XI_MIN_BY_POS[pos]
            problem += pulp.lpSum(in_xi[i][gw] for i in members) <= XI_MAX_BY_POS[pos] + boost

        problem += pulp.lpSum(is_captain[i][gw] for i in captain_pool) == 1
        for i in captain_pool:
            problem += is_captain[i][gw] <= in_xi[i][gw]
        if triple is not None:
            problem += (pulp.lpSum(triple[i][gw] for i in captain_pool)
                        == played("3xc", gw))
            for i in captain_pool:
                problem += triple[i][gw] <= is_captain[i][gw]

    # --- transfers ---------------------------------------------------------
    for step, gw in enumerate(gameweeks):
        hit = played("freehit", gw)
        for i in index:
            previous = (in_squad[i][gameweeks[step - 1]] if step
                        else (1 if int(pool.at[i, "fpl_id"]) in owned else 0))
            if preseason and step == 0:
                # Before the opening deadline the squad is a free choice, so
                # there is nothing to transfer from and nothing to charge for.
                problem += bought[i][gw] == 0
                problem += sold[i][gw] == 0
                continue
            problem += in_squad[i][gw] == previous + bought[i][gw] - sold[i][gw]
            problem += bought[i][gw] <= 1 - hit
            problem += sold[i][gw] <= 1 - hit
            # Selling a player and buying him straight back is a null move that
            # costs nothing under a wildcard, so it has to be ruled out rather
            # than priced out.
            problem += bought[i][gw] + sold[i][gw] <= 1

    banned = set(no_transfer_gws or [])
    if ban_first_gw_transfers:
        banned.add(first)
    for gw in banned:
        if gw in gameweeks:
            problem += pulp.lpSum(bought[i][gw] for i in index) == 0

    # --- money -------------------------------------------------------------
    for step, gw in enumerate(gameweeks):
        raised = pulp.lpSum(sell[i] * sold[i][gw] for i in index)
        outlay = pulp.lpSum(price[i] * bought[i][gw] for i in index)
        if preseason and step == 0:
            problem += in_bank[gw] == budget - pulp.lpSum(price[i] * in_squad[i][gw]
                                                          for i in index)
        elif step == 0:
            problem += in_bank[gw] == bank + raised - outlay
        else:
            problem += in_bank[gw] == in_bank[gameweeks[step - 1]] + raised - outlay

    # --- the free-transfer state machine -----------------------------------
    # `spent` is what comes out of the allowance. A wildcard is fifteen free
    # transfers, so it takes nothing from the allowance and leaves the balance
    # where it was.
    for gw in gameweeks:
        moves = pulp.lpSum(bought[i][gw] for i in index)
        card = played("wildcard", gw)
        problem += spent[gw] >= moves - SQUAD_SIZE * card
        problem += spent[gw] <= SQUAD_SIZE * (1 - card)
        problem += spent[gw] <= moves
        problem += paid[gw] >= spent[gw] - ft[gw]

    problem += ft[first] == (0 if preseason else int(free_transfers))
    if preseason:
        # No transfer was rolled through the opening deadline, so gameweek two
        # opens on exactly one -- which the recursion below produces from zero.
        pass

    big_m = 2 * MAX_FREE_TRANSFERS + SQUAD_SIZE
    for step, gw in enumerate(gameweeks):
        nxt = gameweeks[step + 1] if step + 1 < len(gameweeks) else terminal
        # Playing a wildcard or free hit costs you that gameweek's new free
        # transfer, but since 2024/25 it no longer burns the ones you banked.
        earned = FREE_TRANSFERS_PER_GW - played("wildcard", gw) - played("freehit", gw)
        raw = ft[gw] - spent[gw] + earned

        problem += raw >= (MAX_FREE_TRANSFERS + 1) - big_m * (1 - over[gw])
        problem += raw <= MAX_FREE_TRANSFERS + big_m * over[gw]
        problem += raw <= big_m * (1 - under[gw])
        problem += raw >= 1 - big_m * under[gw]
        problem += over[gw] + under[gw] <= 1

        problem += ft[nxt] <= MAX_FREE_TRANSFERS + big_m * (1 - over[gw])
        problem += ft[nxt] >= MAX_FREE_TRANSFERS - big_m * (1 - over[gw])
        problem += ft[nxt] <= 1 + big_m * (1 - under[gw])
        problem += ft[nxt] >= 1 - big_m * (1 - under[gw])
        problem += ft[nxt] - raw <= big_m * (over[gw] + under[gw])
        problem += raw - ft[nxt] <= big_m * (over[gw] + under[gw])

    for gw in gameweeks + [terminal]:
        problem += pulp.lpSum(ft_state[gw][s] for s in range(MAX_FREE_TRANSFERS + 1)) == 1
        problem += ft[gw] == pulp.lpSum(s * ft_state[gw][s]
                                        for s in range(MAX_FREE_TRANSFERS + 1))

    # --- chips -------------------------------------------------------------
    for chip, allowed in chips.items():
        problem += pulp.lpSum(use[chip][gw] for gw in allowed) <= 1
    for gw in gameweeks:
        # One chip a gameweek, from the rules.
        active = [played(chip, gw) for chip in chips]
        if active:
            problem += pulp.lpSum(active) <= 1
    if hit_limit is not None:
        problem += pulp.lpSum(paid[gw] for gw in gameweeks) <= hit_limit

    # --- objective ---------------------------------------------------------
    hold_value = dict(CHIP_HOLD_VALUE)
    hold_value.update(chip_hold or {})
    # Both of these exist to be turned off. The claim this module makes is that
    # pricing the bank and charging for acting are what stop a transfer plan
    # oscillating, and a claim you cannot switch off is not a claim you have
    # tested -- see scripts/verify-transfer-rules.py, which switches them off
    # and watches the swap-and-swap-back come back.
    charge = TRANSFER_FRICTION if friction is None else friction
    ft_worth = {state: value * ft_value_scale
                for state, value in free_transfer_value().items()}
    banked = {gw: pulp.lpSum(ft_worth[s] * ft_state[gw][s]
                             for s in range(MAX_FREE_TRANSFERS + 1))
              for gw in gameweeks + [terminal]}
    opening = ft_worth[0 if preseason else int(free_transfers)]

    total = []
    for step, gw in enumerate(gameweeks):
        scored = pulp.lpSum(
            xpts[i, gw] * (in_xi[i][gw]
                           + pulp.lpSum(slot_weight[s] * in_slot[i][gw][s] for s in slots))
            for i in index)
        # Captain scores twice, and a third time under the triple-captain chip.
        scored += pulp.lpSum(xpts[i, gw] * is_captain[i][gw] for i in captain_pool)
        if triple is not None:
            scored += pulp.lpSum(xpts[i, gw] * triple[i][gw] for i in captain_pool)

        # What playing a chip here gives up: the same chip, well timed, later in
        # its window. Charged at this gameweek's discount so that the comparison
        # is a clean "does this week beat a good week?" rather than a race
        # between two different discount factors.
        forgone = pulp.lpSum(hold_value.get(chip, 0.0) * played(chip, gw)
                             for chip in chips)

        previous = banked[gameweeks[step - 1]] if step else opening
        week = (scored
                - HIT_COST * paid[gw]
                - charge * spent[gw]
                - IDLE_MOVE_PENALTY * pulp.lpSum(bought[i][gw] for i in index)
                - forgone
                + (banked[gw] - previous)
                + BANK_VALUE * in_bank[gw])
        total.append(decay[gw] * week)

    # Whatever is banked when the window closes is worth having, and crediting
    # it is what lets the last gameweek be a normal one. The alternative -- the
    # reference implementation's -- is to ban transfers in the final gameweeks
    # so the missing credit cannot be exploited, which throws away a real move.
    total.append(decay[last] * (banked[terminal] - banked[last]))

    problem += pulp.lpSum(total)

    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=seconds)
    status = problem.solve(solver)
    label = pulp.LpStatus[status]
    if label not in ("Optimal", "Not Solved") or problem.objective.value() is None:
        raise RuntimeError(
            f"no legal transfer plan found (solver status: {label}). "
            "Budget too low, squad illegal, or too many players excluded?")

    return _read_solution(
        pool=pool, points=points, gameweeks=gameweeks, index=index,
        in_squad=in_squad, in_xi=in_xi, in_slot=in_slot, slots=slots,
        is_captain=is_captain, captain_pool=captain_pool, triple=triple,
        bought=bought, sold=sold, free_hit_squad=free_hit_squad,
        ft=ft, spent=spent, paid=paid, in_bank=in_bank, chips=chips, use=use,
        price=price, sell=sell, decay=decay, variation=variation,
        skipped=skipped,
        objective=float(pulp.value(problem.objective)), status=label,
        preseason=preseason,
    )


# --------------------------------------------------------------------------- #
# Reading the solution back
# --------------------------------------------------------------------------- #

def _read_solution(*, pool, points, gameweeks, index, in_squad, in_xi, in_slot,
                   slots, is_captain, captain_pool, triple, bought, sold,
                   free_hit_squad, ft, spent, paid, in_bank, chips, use,
                   price, sell, decay, variation, skipped, objective, status,
                   preseason) -> TransferPlan:
    """Turn solver variables into the tables a person reads."""
    name = {i: str(pool.at[i, "web_name"]) for i in index}
    team = {i: str(pool.at[i, "team_short"]) for i in index}
    position = {i: str(pool.at[i, "pos"]) for i in index}
    fpl_id = {i: int(pool.at[i, "fpl_id"]) for i in index}

    def on(var) -> bool:
        return var is not None and var.value() is not None and var.value() > 0.5

    chip_by_gw = {}
    for chip, allowed in chips.items():
        for gw in allowed:
            if on(use[chip][gw]):
                chip_by_gw[gw] = chip

    squads, moves, ledger, lineups = {}, [], [], []
    horizon_points = 0.0

    for gw in gameweeks:
        chip = chip_by_gw.get(gw)
        held = [i for i in index if on(in_squad[i][gw])]
        squads[gw] = [fpl_id[i] for i in held]

        fielded = held if chip != "freehit" else [i for i in index
                                                  if on(free_hit_squad[i][gw])]
        starters = [i for i in index if on(in_xi[i][gw])]
        bench = [(s, i) for i in fielded for s in slots if on(in_slot[i][gw][s])]
        bench.sort(key=lambda pair: (pair[0] == "GKP", slots.index(pair[0])))
        captain = next((i for i in captain_pool if on(is_captain[i][gw])), None)
        tripled = triple is not None and any(on(triple[i][gw]) for i in captain_pool)
        vice = max((i for i in starters if i != captain),
                   key=lambda i: points.at[fpl_id[i], gw], default=None)

        multiplier = 3 if tripled else 2
        week_points = sum(points.at[fpl_id[i], gw] for i in starters)
        if captain is not None:
            week_points += (multiplier - 1) * points.at[fpl_id[captain], gw]
        horizon_points += week_points

        formation = "-".join(str(sum(1 for i in starters if position[i] == p))
                             for p in ("DEF", "MID", "FWD"))

        lineups.append({
            "gw": gw,
            "chip": CHIP_LABELS.get(chip, "") if chip else "",
            "formation": formation,
            "captain": name.get(captain, "") + (" (3x)" if tripled else ""),
            "vice_captain": name.get(vice, ""),
            "starting_xi": ", ".join(name[i] for i in starters),
            "bench_order": ", ".join(name[i] for _, i in bench),
            "xi_points": round(float(week_points), 1),
        })

        # Which sale funded which purchase is not something the solver decides
        # -- it moves a set of players out and a set in, and the budget is
        # pooled. Pairing them up is presentation, so pair inside a position
        # where that is possible, which is the only pairing that reads as a
        # transfer rather than as an accident of sort order.
        # A transfer is only worth what it earns from the gameweek it is made
        # onward. Summing the whole window would charge the incoming player for
        # gameweeks you did not own him in, and credit the outgoing one for the
        # same, which reliably makes a good transfer look like a bad one.
        window = points.loc[:, [g for g in gameweeks if g >= gw]]
        arrived = [i for i in index if on(bought[i][gw])]
        for out_player, in_player in _pair_moves(
                [i for i in index if on(sold[i][gw])], arrived, position):
            moves.append({
                "gw": gw,
                "out": name[out_player] if out_player is not None else "",
                "out_price": sell[out_player] if out_player is not None else np.nan,
                "in": name[in_player] if in_player is not None else "",
                "in_price": price[in_player] if in_player is not None else np.nan,
                "in_team": team[in_player] if in_player is not None else "",
                "pos": position[in_player if in_player is not None else out_player],
                "gain": round(
                    float((window.loc[fpl_id[in_player]].sum() if in_player is not None else 0.0)
                          - (window.loc[fpl_id[out_player]].sum() if out_player is not None else 0.0)),
                    2),
            })

        hits = int(round(paid[gw].value() or 0))
        ledger.append({
            "gw": gw,
            "chip": CHIP_LABELS.get(chip, "") if chip else "",
            "transfers": len(arrived),
            "free": int(round(ft[gw].value() or 0)),
            "hits": hits,
            "cost": -HIT_COST * hits,
            "bank": round(float(in_bank[gw].value() or 0), 1),
            "xi_xpts": round(float(week_points), 1),
            "weight": round(decay[gw], 2),
        })

    chip_rows = _chip_report(chips, chip_by_gw, squads, points, gameweeks,
                             {fpl_id[i]: position[i] for i in index}, variation,
                             skipped)

    notes = []
    if preseason:
        notes.append("preseason: the opening squad is a free choice, and the "
                     "first free transfer arrives for the second gameweek")
    if variation:
        notes.append("doubles/blanks in the window: "
                     + "; ".join(f"GW{gw} ({what})" for gw, what in variation.items()))
    else:
        notes.append("no doubles or blanks are on the calendar in this window, so "
                     "nothing distinguishes one gameweek from another for a chip")
    if status == "Not Solved":
        notes.append("solver hit its time limit; this is the best plan it had, "
                     "not a proven optimum")

    return TransferPlan(
        gameweeks=gameweeks,
        squads=squads,
        moves=pd.DataFrame(moves, columns=["gw", "out", "out_price", "in",
                                           "in_price", "in_team", "pos", "gain"]),
        ledger=pd.DataFrame(ledger),
        lineups=pd.DataFrame(lineups),
        chips=chip_rows,
        objective=objective,
        status=status,
        horizon_points=float(horizon_points),
        notes=notes,
    )


def _pair_moves(out_players: list, in_players: list,
                position: dict) -> list[tuple]:
    """Match sales to purchases within a position, then whatever is left over."""
    pairs, leftover_out, remaining = [], [], list(in_players)
    for out_player in sorted(out_players):
        match = next((i for i in remaining if position[i] == position[out_player]), None)
        if match is None:
            leftover_out.append(out_player)
            continue
        remaining.remove(match)
        pairs.append((out_player, match))
    while leftover_out or remaining:
        pairs.append((leftover_out.pop(0) if leftover_out else None,
                      remaining.pop(0) if remaining else None))
    return pairs


def _chip_report(chips, chip_by_gw, squads, points, gameweeks, positions,
                 variation, skipped) -> pd.DataFrame:
    """What each chip is worth, and whether the gameweek it wants is a real choice.

    The payout is measured against the squad the plan actually holds that
    gameweek, and it is measured the same way in every gameweek: what the four
    who would have been benched are worth, and what a third helping of the best
    starter is worth. Reading it off the solved bench instead would break in
    exactly the gameweek that matters, because a bench-boosted week has no
    bench.

    The second number is the honest one -- how much better the chosen gameweek
    is than the median gameweek in the window. When that gap is small the chip
    is being timed against noise, which is what happens all season until the cup
    draws put doubles and blanks on the calendar.
    """
    payouts: dict[str, dict[int, float]] = {"bboost": {}, "3xc": {}}
    for gw in gameweeks:
        held = [i for i in squads.get(gw, []) if i in points.index]
        by_pos = {pos: [i for i in held if positions.get(i) == pos]
                  for pos in ("GKP", "DEF", "MID", "FWD")}
        column = points[gw]
        starters, _ = _best_xi_ids(by_pos, column)
        payouts["bboost"][gw] = float(column[held].sum() - column[starters].sum())
        payouts["3xc"][gw] = float(column[starters].max()) if starters else 0.0

    rows = [{"chip": CHIP_LABELS[chip], "gw": pd.NA, "worth": np.nan,
             "edge": np.nan, "verdict": why}
            for chip, why in (skipped or {}).items()]
    for chip, allowed in chips.items():
        gw = next((g for g, c in chip_by_gw.items() if c == chip), None)
        series = payouts.get(chip, {})
        window = [series[g] for g in allowed if g in series]

        if gw is None:
            rows.append({"chip": CHIP_LABELS[chip], "gw": pd.NA, "worth": np.nan,
                         "edge": np.nan,
                         "verdict": "hold — beaten by keeping it"})
            continue

        if not series:
            # A wildcard or free hit has no payout of its own -- it pays
            # through the squad it lets you buy, which only shows up as the
            # difference between two whole plans. `chip_values` measures it.
            rows.append({"chip": CHIP_LABELS[chip], "gw": gw, "worth": np.nan,
                         "edge": np.nan,
                         "verdict": "structural — use --chip-value"})
            continue

        worth = series.get(gw, np.nan)
        edge = worth - float(np.median(window)) if window else np.nan
        if not variation:
            verdict = "hold — nothing to time against"
        elif np.isnan(edge) or edge < 1.0:
            verdict = "weak — no better than any other week"
        else:
            verdict = "timed against a double or blank"
        rows.append({"chip": CHIP_LABELS[chip], "gw": gw, "worth": worth,
                     "edge": edge, "verdict": verdict})

    return pd.DataFrame(rows, columns=["chip", "gw", "worth", "edge", "verdict"])


# --------------------------------------------------------------------------- #
# Pricing the decision
# --------------------------------------------------------------------------- #

def value_of_acting(projection: Projection, players: pd.DataFrame,
                    plan: TransferPlan | None = None, **kwargs) -> dict:
    """What this week's move is worth against rolling the transfer instead.

    Solving twice -- once freely, once with this gameweek's transfers banned --
    is the only comparison that charges the move for what it consumes. The gain
    already nets off the four points a hit costs, the friction, and the banked
    transfer the move spends, because both solves are scored on the same
    objective.

    This is the number to act on. Everything past the first gameweek is a shape,
    not an instruction: it will be re-solved next week with team news, price
    moves and a fixture list that this run cannot see.

    `plan` is the free solve, if the caller already has one.
    """
    acting = plan or plan_transfers(projection, players, **kwargs)
    holding = plan_transfers(projection, players,
                             **{**kwargs, "ban_first_gw_transfers": True})
    return {
        "plan": acting,
        "hold": holding,
        "gain": acting.objective - holding.objective,
        "moves": acting.this_week,
    }


def chip_values(projection: Projection, players: pd.DataFrame,
                chip_windows: dict[str, tuple[int, int]] | None = None,
                **kwargs) -> pd.DataFrame:
    """Each chip's worth, measured by taking it away and re-solving.

    A chip's value is not its payout. Playing a bench boost changes which
    fifteen you buy in the weeks before it, and a plan that never gets the chip
    would have bought a different fifteen -- cheaper bench, better XI. The only
    honest measure is the difference between the best plan that has the chip and
    the best plan that does not, which is what this computes, one chip at a time.

    Expensive: one solve per chip plus a baseline.
    """
    kwargs.pop("forbid_chips", None)
    full = plan_transfers(projection, players, chip_windows=chip_windows, **kwargs)
    available = list(chip_slots(chip_windows or {}, full.gameweeks,
                                kwargs.get("chips_used")))

    rows = []
    for chip in available:
        without = plan_transfers(projection, players, chip_windows=chip_windows,
                                 forbid_chips=[chip], **kwargs)
        played = full.chips.loc[full.chips["chip"] == CHIP_LABELS[chip], "gw"]
        rows.append({
            "chip": CHIP_LABELS[chip],
            "gw": played.iloc[0] if len(played) else pd.NA,
            "worth": round(full.objective - without.objective, 2),
        })

    if available:
        none = plan_transfers(projection, players, chip_windows=chip_windows,
                              forbid_chips=available, **kwargs)
        rows.append({"chip": "all chips", "gw": pd.NA,
                     "worth": round(full.objective - none.objective, 2)})
    return pd.DataFrame(rows, columns=["chip", "gw", "worth"])

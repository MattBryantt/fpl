"""Squad selection as a mixed-integer program.

Picking a squad is a knapsack with side constraints, which PuLP solves exactly
in well under a second at this size. Doing it exactly is what makes the
"is he worth the money?" question answerable: force a player in, re-optimise
everything around him, and compare total squad output. That price the model
puts on him already accounts for what the freed-up money would otherwise buy.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pulp

from .config import (
    BENCH_SLOT_PROFILE,
    DEFAULT_BENCH_WEIGHT,
    DEFAULT_BUDGET,
    MAX_PER_CLUB,
    SQUAD_BY_POS,
    SQUAD_SIZE,
    XI_MAX_BY_POS,
    XI_MIN_BY_POS,
    XI_SIZE,
)


@dataclass
class Squad:
    players: pd.DataFrame  # 15 rows, with `starting` and `is_captain` flags
    total_cost: float
    xi_points: float  # starting XI, captain already doubled
    bench_points: float
    objective: float  # xi + bench_weight * bench (+ ownership tilt, if enabled)

    # `objective` is the only number that is comparable between two solves.
    # xi_points is a component of it, so a constrained solve can post a higher
    # xi_points than an unconstrained one by trading bench quality away -- the
    # solver would never make that trade freely, but the component can still
    # rise. Always rank squads on `objective`.

    @property
    def starting(self) -> pd.DataFrame:
        return self.players[self.players["starting"]]

    @property
    def bench(self) -> pd.DataFrame:
        return self.players[~self.players["starting"]]

    @property
    def captain(self) -> pd.Series:
        return self.players[self.players["is_captain"]].iloc[0]


def optimise(
    players: pd.DataFrame,
    budget: float = DEFAULT_BUDGET,
    bench_weight: float = DEFAULT_BENCH_WEIGHT,
    include: list[int] | None = None,
    exclude: list[int] | None = None,
    min_minutes_prob: float = 0.0,
    max_per_club: int = MAX_PER_CLUB,
    points_column: str = "xpts",
    ownership_weight: float = 0.0,
    formation: dict[str, int] | None = None,
    bench_slot_weights: dict[object, float] | None = None,
) -> Squad:
    """Maximise starting-XI expected points subject to the FPL squad rules.

    Args:
        players: projection output; needs fpl_id, pos, team, price, `points_column`.
        budget: squad budget in millions.
        bench_weight: how much bench points count in the objective. Scales the
            BENCH_SLOT_PROFILE, so the four slots stay weighted relative to each
            other -- first sub worth roughly six times the third, reserve keeper
            worth least. 0 makes the solver pick the four cheapest bodies.
            Ignored when `bench_slot_weights` is given.
        bench_slot_weights: absolute weight per bench slot, keyed "GKP", 1, 2, 3.
            Overrides bench_weight × BENCH_SLOT_PROFILE entirely, so a caller who
            disagrees with the shape of the default profile -- not just its
            overall size -- can say so. Missing slots fall back to the scaled
            default for that slot.
        include: fpl_ids that must be in the squad.
        exclude: fpl_ids that must not be.
        min_minutes_prob: drop players below this probability of appearing,
            which keeps injured and fringe players out of the "free" bench slots.
        formation: pin the starting XI shape, e.g. {"DEF": 3, "MID": 4, "FWD": 3}.
            Omit to let the solver choose whichever legal shape scores best.
        ownership_weight: how much to favour widely owned players. FPL is scored
            on rank, so a player owned by half the field is partly insurance:
            not owning him is a risk even when the raw points say he is
            replaceable. Zero maximises expected points and ignores the field
            entirely; raising it buys protection against a template player
            hauling, at the cost of expected points. 0.15 is a mild tilt.
    """
    pool = players.copy()
    if min_minutes_prob > 0 and "p_play" in pool.columns:
        keep = pool["p_play"] >= min_minutes_prob
        if include:
            keep |= pool["fpl_id"].isin(include)
        pool = pool[keep]
    if exclude:
        pool = pool[~pool["fpl_id"].isin(exclude)]

    pool = pool.reset_index(drop=True)
    if include:
        missing = set(include) - set(pool["fpl_id"])
        if missing:
            raise ValueError(f"Forced-in players not in the pool: {sorted(missing)}")

    problem = pulp.LpProblem("fpl_squad", pulp.LpMaximize)
    index = list(pool.index)

    in_squad = pulp.LpVariable.dicts("squad", index, cat="Binary")
    in_xi = pulp.LpVariable.dicts("xi", index, cat="Binary")
    is_captain = pulp.LpVariable.dicts("capt", index, cat="Binary")

    points = pool[points_column].fillna(0.0).to_dict()
    price = pool["price"].to_dict()

    ownership = {i: 0.0 for i in index}
    if ownership_weight and "selected_by_percent" in pool.columns:
        owned = pd.to_numeric(pool["selected_by_percent"], errors="coerce").fillna(0.0)
        ownership = (owned / 100.0 * pool[points_column].fillna(0.0)).to_dict()

    # Bench slots, ordered. Without these the four substitutes are an unordered
    # set and every one of them carries the same weight, which is wrong: the
    # first outfield sub comes on whenever a starter blanks, the third almost
    # never does. Giving each slot its own variable lets the solver decide who
    # is worth putting first, and stops it paying first-sub money for a body who
    # will sit at the bottom of the bench all season.
    slots = list(BENCH_SLOT_PROFILE)
    in_slot = pulp.LpVariable.dicts("bench", (index, slots), cat="Binary")

    # One number per slot, however the caller chose to express it.
    slot_weight = {s: bench_weight * BENCH_SLOT_PROFILE[s] for s in slots}
    for slot, weight in (bench_slot_weights or {}).items():
        if slot in slot_weight and weight is not None:
            slot_weight[slot] = float(weight)

    problem += pulp.lpSum(
        points[i] * in_xi[i]
        + points[i] * is_captain[i]  # captain scores twice
        + ownership_weight * ownership[i] * in_squad[i]
        + pulp.lpSum(slot_weight[s] * points[i] * in_slot[i][s] for s in slots)
        for i in index
    )

    for slot in slots:
        eligible = [i for i in index
                    if (pool.at[i, "pos"] == "GKP") == (slot == "GKP")]
        problem += pulp.lpSum(in_slot[i][slot] for i in eligible) == 1
        for i in index:
            if i not in eligible:
                problem += in_slot[i][slot] == 0
    for i in index:
        # A player is on exactly one bench slot if and only if he is in the
        # squad and not in the XI.
        problem += pulp.lpSum(in_slot[i][s] for s in slots) == in_squad[i] - in_xi[i]

    problem += pulp.lpSum(in_squad[i] for i in index) == SQUAD_SIZE
    problem += pulp.lpSum(price[i] * in_squad[i] for i in index) <= budget
    problem += pulp.lpSum(in_xi[i] for i in index) == XI_SIZE
    problem += pulp.lpSum(is_captain[i] for i in index) == 1

    for i in index:
        problem += in_xi[i] <= in_squad[i]
        problem += is_captain[i] <= in_xi[i]

    for position, count in SQUAD_BY_POS.items():
        members = [i for i in index if pool.at[i, "pos"] == position]
        problem += pulp.lpSum(in_squad[i] for i in members) == count
        if formation and position in formation:
            # Pin the starting XI shape exactly, rather than letting the solver
            # pick whichever legal shape scores best.
            problem += pulp.lpSum(in_xi[i] for i in members) == formation[position]
        else:
            problem += pulp.lpSum(in_xi[i] for i in members) >= XI_MIN_BY_POS[position]
            problem += pulp.lpSum(in_xi[i] for i in members) <= XI_MAX_BY_POS[position]

    for team in pool["team"].unique():
        members = [i for i in index if pool.at[i, "team"] == team]
        problem += pulp.lpSum(in_squad[i] for i in members) <= max_per_club

    for fpl_id in include or []:
        members = [i for i in index if pool.at[i, "fpl_id"] == fpl_id]
        problem += pulp.lpSum(in_squad[i] for i in members) == 1

    status = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(
            f"No legal squad found (solver status: {pulp.LpStatus[status]}). "
            "Budget too low, or too many players excluded?"
        )

    chosen = [i for i in index if in_squad[i].value() > 0.5]
    squad = pool.loc[chosen].copy()
    squad["starting"] = [in_xi[i].value() > 0.5 for i in chosen]
    squad["is_captain"] = [is_captain[i].value() > 0.5 for i in chosen]
    squad["bench_slot"] = [
        next((s for s in slots if in_slot[i][s].value() > 0.5), None) for i in chosen
    ]
    squad = squad.sort_values(
        ["starting", "pos", points_column],
        ascending=[False, True, False],
        key=lambda col: col.map({"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3})
        if col.name == "pos" else col,
    )

    xi_points = float(squad.loc[squad["starting"], points_column].sum()
                      + squad.loc[squad["is_captain"], points_column].sum())
    bench_points = float(squad.loc[~squad["starting"], points_column].sum())

    return Squad(
        players=squad.reset_index(drop=True),
        total_cost=float(squad["price"].sum()),
        xi_points=xi_points,
        bench_points=bench_points,
        objective=float(pulp.value(problem.objective)),
    )


def marginal_value(players: pd.DataFrame, fpl_id: int, **kwargs) -> dict:
    """What the whole squad is worth with this player forced in, versus without.

    The difference between the two is the honest answer to "is he worth it":
    it charges him for the money he consumes, because the alternative squad got
    to spend that money on somebody else.

    Both sides are scored on the solver's objective rather than on XI points
    alone, so that the comparison is between the two things the solver actually
    ranked. Scoring on XI points would let a squad with a gutted bench look
    better than one the solver strictly preferred.
    """
    with_player = optimise(players, include=[fpl_id], **kwargs)
    without_player = optimise(players, exclude=[fpl_id], **kwargs)
    return {
        "fpl_id": fpl_id,
        "squad_xpts_with": with_player.objective,
        "squad_xpts_without": without_player.objective,
        "delta": with_player.objective - without_player.objective,
        "xi_with": with_player.xi_points,
        "xi_without": without_player.xi_points,
        "squad_with": with_player,
        "squad_without": without_player,
    }

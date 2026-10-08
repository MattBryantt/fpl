
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

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
    players: pd.DataFrame
    total_cost: float
    xi_points: float
    bench_points: float
    objective: float


    @property
    def starting(self) -> pd.DataFrame:
        return self.players[self.players["starting"]]

    @property
    def bench(self) -> pd.DataFrame:
        return self.players[~self.players["starting"]]

    @property
    def captain(self) -> pd.Series:
        return self.players[self.players["is_captain"]].iloc[0]


def eligible_pool(
    players: pd.DataFrame,
    min_minutes_prob: float = 0.0,
    exclude: list[int] | None = None,
    include: list[int] | None = None,
) -> pd.DataFrame:
    pool = players.copy()
    if min_minutes_prob > 0 and "p_play" in pool.columns:
        keep = pool["p_play"] >= min_minutes_prob
        if include:
            keep |= pool["fpl_id"].isin(include)
        pool = pool[keep]
    if exclude:
        pool = pool[~pool["fpl_id"].isin(exclude)]
    return pool.reset_index(drop=True)


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
    captain_multiplier: float = 2.0,
) -> Squad:
    pool = eligible_pool(players, min_minutes_prob=min_minutes_prob,
                         exclude=exclude, include=include)
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

    slots = list(BENCH_SLOT_PROFILE)
    in_slot = pulp.LpVariable.dicts("bench", (index, slots), cat="Binary")

    slot_weight = {s: bench_weight * BENCH_SLOT_PROFILE[s] for s in slots}
    for slot, weight in (bench_slot_weights or {}).items():
        if slot in slot_weight and weight is not None:
            slot_weight[slot] = float(weight)

    problem += pulp.lpSum(
        points[i] * in_xi[i]
        + (captain_multiplier - 1) * points[i] * is_captain[i]
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

    xi_points = float(
        squad.loc[squad["starting"], points_column].sum()
        + (captain_multiplier - 1) * squad.loc[squad["is_captain"], points_column].sum())
    bench_points = float(squad.loc[~squad["starting"], points_column].sum())

    return Squad(
        players=squad.reset_index(drop=True),
        total_cost=float(squad["price"].sum()),
        xi_points=xi_points,
        bench_points=bench_points,
        objective=float(pulp.value(problem.objective)),
    )


def near_miss_candidates(
    players: pd.DataFrame,
    in_squad: set[int] | list[int],
    points_column: str = "xpts",
    per_club: bool = False,
    min_minutes_prob: float = 0.0,
    exclude: list[int] | None = None,
) -> list[int]:
    pool = eligible_pool(players, min_minutes_prob=min_minutes_prob, exclude=exclude)
    pool = pool[~pool["fpl_id"].isin(list(in_squad))]
    if pool.empty:
        return []

    keys = ["pos", "team"] if per_club else ["pos"]
    chosen: list[int] = []
    for _, group in pool.groupby(keys, sort=True):
        ordered = group.sort_values(["price", points_column], ascending=[True, False])
        best = -math.inf
        for fpl_id, points in zip(ordered["fpl_id"], ordered[points_column].fillna(0.0)):
            if points > best:
                chosen.append(int(fpl_id))
                best = float(points)
    return chosen


def near_misses(
    players: pd.DataFrame,
    base: Squad | None = None,
    include: list[int] | None = None,
    per_club: bool = False,
    points_column: str = "xpts",
    progress: Callable[[int, int, int], None] | None = None,
    **kwargs,
) -> pd.DataFrame:
    if base is None:
        base = optimise(players, include=include, points_column=points_column, **kwargs)
    held = set(base.players["fpl_id"].astype(int))

    candidates = near_miss_candidates(
        players, held, points_column=points_column, per_club=per_club,
        min_minutes_prob=kwargs.get("min_minutes_prob", 0.0),
        exclude=kwargs.get("exclude"),
    )

    names = players.set_index("fpl_id")["web_name"].to_dict()
    positions = players.set_index("fpl_id")["pos"].to_dict()
    rows = []
    for done, fpl_id in enumerate(candidates, start=1):
        try:
            forced = optimise(players, include=[*(include or []), fpl_id],
                              points_column=points_column, **kwargs)
        except (RuntimeError, ValueError):
            rows.append({"fpl_id": fpl_id, "gap": float("nan"),
                         "role": "no legal squad", "replaces": ""})
        else:
            got = forced.players
            his = got[got["fpl_id"] == fpl_id].iloc[0]
            dropped = held - set(got["fpl_id"].astype(int))
            out = [names.get(i, str(i)) for i in
                   sorted(dropped, key=lambda i: (positions.get(i) != his["pos"],
                                                  names.get(i, str(i))))]
            rows.append({
                "fpl_id": fpl_id,
                "gap": max(0.0, base.objective - forced.objective),
                "role": "XI (C)" if his["is_captain"] else ("XI" if his["starting"] else "bench"),
                "replaces": ", ".join(out),
            })
        if progress:
            progress(done, len(candidates), fpl_id)

    if not rows:
        return pd.DataFrame(columns=["fpl_id", "gap", "role", "replaces"])
    frame = pd.DataFrame(rows).merge(players, on="fpl_id", how="left")
    return frame.sort_values("gap", na_position="last").reset_index(drop=True)


def marginal_value(players: pd.DataFrame, fpl_id: int, **kwargs) -> dict:
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

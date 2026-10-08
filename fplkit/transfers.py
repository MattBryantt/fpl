
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
    SELL_ON_FEE,
    SQUAD_BY_POS,
    SQUAD_SIZE,
    TRANSFER_FRICTION,
    XI_MAX_BY_POS,
    XI_MIN_BY_POS,
    XI_SIZE,
)
from .model import Projection
from .planning import _best_xi_ids, fixture_counts, survival_curve

TRANSFER_HALF_LIFE = 6.5

DEFAULT_TRANSFER_HORIZON = 6

POOL_BY_POS = {"GKP": 12, "DEF": 45, "MID": 45, "FWD": 25}

PRICE_POINT_CANDIDATES = 3

CAPTAIN_CANDIDATES = 40

IDLE_MOVE_PENALTY = 0.01

SOLVER_SECONDS = 120


@dataclass
class TransferPlan:

    gameweeks: list[int]
    squads: dict[int, list[int]]
    moves: pd.DataFrame
    ledger: pd.DataFrame
    lineups: pd.DataFrame
    chips: pd.DataFrame
    chip_options: pd.DataFrame
    objective: float
    status: str
    horizon_points: float
    notes: list[str] = field(default_factory=list)

    @property
    def this_week(self) -> pd.DataFrame:
        if self.moves.empty:
            return self.moves
        return self.moves[self.moves["gw"] == self.gameweeks[0]]


def decay_factors(gameweeks: list[int],
                  half_life: float | None = TRANSFER_HALF_LIFE) -> dict[int, float]:
    if half_life is None or math.isinf(half_life):
        return {gw: 1.0 for gw in gameweeks}
    return {gw: 0.5 ** (step / half_life) for step, gw in enumerate(gameweeks)}


def expected_points(projection: Projection,
                    gameweeks: list[int] | None = None) -> pd.DataFrame:
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
                   caps: dict[str, int] | None = None,
                   price_point_candidates: int = PRICE_POINT_CANDIDATES) -> pd.DataFrame:
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
        by_price = (block.sort_values("window_points", ascending=False)
                         .groupby("price", group_keys=False)
                         .head(price_point_candidates))
        forced = block[block["fpl_id"].isin(keep)]
        chosen.append(pd.concat([by_points, by_value, by_price, forced]))

    return (pd.concat(chosen)
            .drop_duplicates(subset="fpl_id")
            .reset_index(drop=True))


def next_free_transfers(ft: int, spent: int, played_chip: bool = False) -> int:
    earned = FREE_TRANSFERS_PER_GW - (1 if played_chip else 0)
    raw = ft - spent + earned
    return max(1, min(MAX_FREE_TRANSFERS, raw))


def sell_price(bought: float, now: float) -> float:
    profit = round((now - bought) * 10)
    if profit <= 0:
        return round(now, 1)
    return round(bought + math.floor(profit * (1 - SELL_ON_FEE)) / 10, 1)


def free_transfer_value() -> dict[int, float]:
    value, running = {0: 0.0}, 0.0
    for state in range(1, MAX_FREE_TRANSFERS + 1):
        running += FT_VALUE_BY_STATE.get(state, FT_VALUE)
        value[state] = running
    return value


def chip_slots(windows: dict[str, tuple[int, int]], gameweeks: list[int],
               already_used: list[str] | None = None) -> dict[str, list[int]]:
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
    counts = fixture_counts(projection).reindex(columns=gameweeks, fill_value=0)
    marks = {}
    for gw in gameweeks:
        doubles = int((counts[gw] >= 2).sum())
        blanks = int((counts[gw] == 0).sum())
        if doubles or blanks:
            marks[gw] = f"{doubles} double, {blanks} blank"
    return marks


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
    force_chips: list[str] | None = None,
    seconds: int = SOLVER_SECONDS,
    pool: pd.DataFrame | None = None,
    points: pd.DataFrame | None = None,
) -> TransferPlan:
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

    forced = list(dict.fromkeys(force_chips or []))
    clash = sorted(set(forced) & set(forbid_chips or []))
    if clash:
        raise ValueError(f"chips cannot be both forced and forbidden: {clash}")

    chips = chip_slots(chip_windows or {}, gameweeks, chips_used)
    for chip in (forbid_chips or []):
        chips.pop(chip, None)
    variation = fixture_variation(projection, gameweeks)
    skipped = {}
    if "freehit" in chips and not variation and "freehit" not in forced:
        chips.pop("freehit")
        skipped["freehit"] = "no blank or double to hit"

    unplayable = [chip for chip in forced if chip not in chips]
    if unplayable:
        raise ValueError(
            f"cannot force {sorted(unplayable)}: not playable in GW{gameweeks[0]}"
            f"-GW{gameweeks[-1]}. Already used, outside the chip's window, or "
            f"not a chip this tool models (known chips: {', '.join(CHIPS)})")

    problem = pulp.LpProblem("fpl_transfer_plan", pulp.LpMaximize)
    first, last = gameweeks[0], gameweeks[-1]
    terminal = last + 1

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
        return use[chip][gw] if chip in use and gw in use[chip] else 0

    triple = pulp.LpVariable.dicts("tc", (captain_pool, gameweeks), cat="Binary") \
        if "3xc" in chips else None
    free_hit_squad = pulp.LpVariable.dicts("fhsquad", (index, gameweeks), cat="Binary") \
        if "freehit" in chips else None

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

    for gw in gameweeks:
        boost = played("bboost", gw)
        hit = played("freehit", gw)
        problem += pulp.lpSum(in_xi[i][gw] for i in index) == XI_SIZE + (SQUAD_SIZE - XI_SIZE) * boost

        for slot in slots:
            eligible = [i for i in index if (position[i] == "GKP") == (slot == "GKP")]
            problem += pulp.lpSum(in_slot[i][gw][slot] for i in eligible) == 1 - boost
            for i in index:
                if i not in eligible:
                    problem += in_slot[i][gw][slot] == 0

        for i in index:
            benched = pulp.lpSum(in_slot[i][gw][s] for s in slots)
            problem += in_xi[i][gw] <= in_squad[i][gw] + hit
            problem += benched <= in_squad[i][gw] + hit
            if free_hit_squad is not None:
                problem += in_xi[i][gw] <= free_hit_squad[i][gw] + (1 - hit)
                problem += benched <= free_hit_squad[i][gw] + (1 - hit)
            problem += in_xi[i][gw] + benched <= 1

        for pos in ("GKP", "DEF", "MID", "FWD"):
            members = [i for i in index if position[i] == pos]
            problem += pulp.lpSum(in_xi[i][gw] for i in members) >= XI_MIN_BY_POS[pos]
            relax = SQUAD_BY_POS[pos] - XI_MAX_BY_POS[pos]
            problem += (pulp.lpSum(in_xi[i][gw] for i in members)
                        <= XI_MAX_BY_POS[pos] + relax * boost)

        problem += pulp.lpSum(is_captain[i][gw] for i in captain_pool) == 1
        for i in captain_pool:
            problem += is_captain[i][gw] <= in_xi[i][gw]
        if triple is not None:
            problem += (pulp.lpSum(triple[i][gw] for i in captain_pool)
                        == played("3xc", gw))
            for i in captain_pool:
                problem += triple[i][gw] <= is_captain[i][gw]

    for step, gw in enumerate(gameweeks):
        hit = played("freehit", gw)
        for i in index:
            previous = (in_squad[i][gameweeks[step - 1]] if step
                        else (1 if int(pool.at[i, "fpl_id"]) in owned else 0))
            if preseason and step == 0:
                problem += bought[i][gw] == 0
                problem += sold[i][gw] == 0
                continue
            problem += in_squad[i][gw] == previous + bought[i][gw] - sold[i][gw]
            problem += bought[i][gw] <= 1 - hit
            problem += sold[i][gw] <= 1 - hit
            problem += bought[i][gw] + sold[i][gw] <= 1

    banned = set(no_transfer_gws or [])
    if ban_first_gw_transfers:
        banned.add(first)
    for gw in banned:
        if gw in gameweeks:
            problem += pulp.lpSum(bought[i][gw] for i in index) == 0

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

    for gw in gameweeks:
        problem += spent[gw] == pulp.lpSum(bought[i][gw] for i in index)
        problem += paid[gw] >= spent[gw] - ft[gw]

    problem += ft[first] == int(free_transfers)

    big_m = 2 * MAX_FREE_TRANSFERS + SQUAD_SIZE
    for step, gw in enumerate(gameweeks):
        nxt = gameweeks[step + 1] if step + 1 < len(gameweeks) else terminal
        earned = FREE_TRANSFERS_PER_GW - played("freehit", gw)
        if preseason and step == 0:
            earned = 0
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

    for chip, allowed in chips.items():
        times = pulp.lpSum(use[chip][gw] for gw in allowed)
        problem += (times == 1) if chip in forced else (times <= 1)
    for gw in gameweeks:
        active = [played(chip, gw) for chip in chips]
        if active:
            problem += pulp.lpSum(active) <= 1
    if hit_limit is not None:
        problem += pulp.lpSum(paid[gw] for gw in gameweeks) <= hit_limit

    hold_value = dict(CHIP_HOLD_VALUE)
    hold_value.update(chip_hold or {})
    for chip in forced:
        hold_value[chip] = 0.0
    charge = TRANSFER_FRICTION if friction is None else friction
    ft_worth = {state: value * ft_value_scale
                for state, value in free_transfer_value().items()}
    banked = {gw: pulp.lpSum(ft_worth[s] * ft_state[gw][s]
                             for s in range(MAX_FREE_TRANSFERS + 1))
              for gw in gameweeks + [terminal]}
    opening = ft_worth[int(free_transfers)]

    total = []
    for step, gw in enumerate(gameweeks):
        scored = pulp.lpSum(
            xpts[i, gw] * (in_xi[i][gw]
                           + pulp.lpSum(slot_weight[s] * in_slot[i][gw][s] for s in slots))
            for i in index)
        scored += pulp.lpSum(xpts[i, gw] * is_captain[i][gw] for i in captain_pool)
        if triple is not None:
            scored += pulp.lpSum(xpts[i, gw] * triple[i][gw] for i in captain_pool)

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
        slot_weight=slot_weight,
        is_captain=is_captain, captain_pool=captain_pool, triple=triple,
        bought=bought, sold=sold, free_hit_squad=free_hit_squad,
        ft=ft, spent=spent, paid=paid, in_bank=in_bank, chips=chips, use=use,
        price=price, sell=sell, decay=decay, variation=variation,
        skipped=skipped, forced=forced, hold_value=hold_value,
        objective=float(pulp.value(problem.objective)), status=label,
        preseason=preseason,
    )


def _read_solution(*, pool, points, gameweeks, index, in_squad, in_xi, in_slot,
                   slots, slot_weight, is_captain, captain_pool, triple, bought, sold,
                   free_hit_squad, ft, spent, paid, in_bank, chips, use,
                   price, sell, decay, variation, skipped, forced, hold_value,
                   objective, status, preseason) -> TransferPlan:
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

    chip_rows, chip_options = _chip_report(
        chips, chip_by_gw, squads, points, gameweeks,
        {fpl_id[i]: position[i] for i in index}, variation, skipped, forced,
        hold_value, slot_weight)

    notes = []
    if forced:
        notes.append("forced: " + ", ".join(CHIP_LABELS[c] for c in forced)
                     + " — this is the best plan that plays "
                     + ("them" if len(forced) > 1 else "it")
                     + ", not the best plan")
    if preseason:
        notes.append("from scratch: the opening fifteen is a free choice -- a "
                     "wildcard, or the preseason squad -- and no transfer is "
                     "earned for that week")
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
        chip_options=chip_options,
        objective=objective,
        status=status,
        horizon_points=float(horizon_points),
        notes=notes,
    )


def _pair_moves(out_players: list, in_players: list,
                position: dict) -> list[tuple]:
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


def chip_payout_series(squads: dict[int, list[int]], points: pd.DataFrame,
                       gameweeks: list[int], positions: dict[int, str],
                       slot_weight: dict[object, float] | None = None,
                       ) -> dict[str, dict[int, float]]:
    weights = dict(DEFAULT_BENCH_SLOT_WEIGHTS)
    weights.update(slot_weight or {})
    outfield = [s for s in weights if s != "GKP"]

    payouts: dict[str, dict[int, float]] = {"bboost": {}, "3xc": {}}
    for gw in gameweeks:
        held = [i for i in squads.get(gw, []) if i in points.index]
        by_pos = {pos: [i for i in held if positions.get(i) == pos]
                  for pos in ("GKP", "DEF", "MID", "FWD")}
        column = points[gw]
        starters, _ = _best_xi_ids(by_pos, column)

        benched = [i for i in held if i not in set(starters)]
        spare_gk = [i for i in benched if positions.get(i) == "GKP"]
        rest = sorted((i for i in benched if positions.get(i) != "GKP"),
                      key=lambda i: -float(column[i]))
        earned = sum(weights["GKP"] * float(column[i]) for i in spare_gk)
        earned += sum(weights[slot] * float(column[i])
                      for slot, i in zip(outfield, rest))

        payouts["bboost"][gw] = float(column[benched].sum()) - earned
        payouts["3xc"][gw] = float(column[starters].max()) if starters else 0.0
    return payouts


def _chip_report(chips, chip_by_gw, squads, points, gameweeks, positions,
                 variation, skipped, forced=(), hold_value=None,
                 slot_weight=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    payouts = chip_payout_series(squads, points, gameweeks, positions, slot_weight)

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

        lead = "forced" if chip in forced else "play"

        if not series:
            rows.append({"chip": CHIP_LABELS[chip], "gw": gw, "worth": np.nan,
                         "edge": np.nan,
                         "verdict": f"{lead} — structural, use --chip-value"})
            continue

        worth = series.get(gw, np.nan)
        edge = worth - float(np.median(window)) if window else np.nan
        if not variation:
            timing = "nothing to time against"
        elif np.isnan(edge) or edge < 1.0:
            timing = "no better than any other week"
        else:
            timing = "timed on a double or blank"
        rows.append({"chip": CHIP_LABELS[chip], "gw": gw, "worth": worth,
                     "edge": edge, "verdict": f"{lead} — {timing}"})

    reserve = dict(CHIP_HOLD_VALUE)
    reserve.update(hold_value or {})
    options = []
    for chip, allowed in chips.items():
        series = payouts.get(chip)
        if not series:
            continue
        row = {"chip": CHIP_LABELS[chip]}
        row.update({f"GW{gw}": round(series[gw], 1) if gw in allowed else np.nan
                    for gw in gameweeks})
        row["reserve"] = 0.0 if chip in forced else round(reserve.get(chip, 0.0), 1)
        row["played"] = next((f"GW{g}" for g, c in chip_by_gw.items() if c == chip),
                             "—")
        options.append(row)

    return (pd.DataFrame(rows, columns=["chip", "gw", "worth", "edge", "verdict"]),
            pd.DataFrame(options,
                         columns=(["chip"] + [f"GW{gw}" for gw in gameweeks]
                                  + ["reserve", "played"])))


def value_of_acting(projection: Projection, players: pd.DataFrame,
                    plan: TransferPlan | None = None, **kwargs) -> dict:
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
    kwargs.pop("forbid_chips", None)
    forced = list(kwargs.pop("force_chips", None) or [])
    full = plan_transfers(projection, players, chip_windows=chip_windows,
                          force_chips=forced, **kwargs)
    available = list(chip_slots(chip_windows or {}, full.gameweeks,
                                kwargs.get("chips_used")))

    rows = []
    for chip in available:
        without = plan_transfers(projection, players, chip_windows=chip_windows,
                                 forbid_chips=[chip],
                                 force_chips=[c for c in forced if c != chip],
                                 **kwargs)
        played = full.chips.loc[full.chips["chip"] == CHIP_LABELS[chip], "gw"]
        rows.append({
            "chip": CHIP_LABELS[chip],
            "gw": played.iloc[0] if len(played) else pd.NA,
            "worth": round(full.objective - without.objective, 2),
        })

    if available:
        none = plan_transfers(projection, players, chip_windows=chip_windows,
                              forbid_chips=available, force_chips=[], **kwargs)
        rows.append({"chip": "all chips", "gw": pd.NA,
                     "worth": round(full.objective - none.objective, 2)})
    return pd.DataFrame(rows, columns=["chip", "gw", "worth"])

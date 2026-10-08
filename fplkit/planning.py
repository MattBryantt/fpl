
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .config import XI_MAX_BY_POS, XI_MIN_BY_POS, XI_SIZE
from .model import Projection
from .optimise import Squad, optimise

DEFAULT_HALF_LIFE = 3.0

BASE_HAZARD = 0.030
AGE_HAZARD_SLOPE = 0.08
AGE_HAZARD_FROM = 29.0
DOUBTFUL_HAZARD_MULTIPLIER = 2.0

FALL_OWNERSHIP_AMPLIFIER = 1.5
RISE_OWNERSHIP_DAMPING = 0.5


@dataclass
class Plan:
    squad: Squad
    players: pd.DataFrame
    per_gw: pd.DataFrame
    lineups: pd.DataFrame
    timeline: pd.DataFrame
    windows: pd.DataFrame
    exposure: pd.DataFrame
    coverage: dict
    core: pd.DataFrame
    horizon: list[int]
    half_life: float | None
    bank: float
    notes: list[str] = field(default_factory=list)


def decay_weights(gameweeks: list[int],
                  half_life: float | None = DEFAULT_HALF_LIFE) -> pd.Series:
    steps = np.arange(len(gameweeks))
    if half_life is None or math.isinf(half_life):
        return pd.Series(1.0, index=gameweeks, name="decay")
    return pd.Series(0.5 ** (steps / half_life), index=gameweeks, name="decay")


def injury_hazard(players: pd.DataFrame, base: float = BASE_HAZARD) -> pd.Series:
    today = pd.Timestamp(datetime.now().date())
    birth = pd.to_datetime(players.get("birth_date"), errors="coerce")
    age = (today - birth).dt.days / 365.25

    hazard = pd.Series(base, index=players.index, dtype=float)
    older = (age - AGE_HAZARD_FROM).clip(lower=0).fillna(0.0)
    hazard = hazard * (1 + AGE_HAZARD_SLOPE * older)
    hazard = hazard.where(players["status"] != "d", hazard * DOUBTFUL_HAZARD_MULTIPLIER)
    return hazard.clip(0.005, 0.25)


def survival_curve(players: pd.DataFrame, gameweeks: list[int]) -> pd.DataFrame:
    hazard = injury_hazard(players).to_numpy()[:, None]
    steps = np.arange(len(gameweeks))[None, :]
    return pd.DataFrame((1 - hazard) ** steps,
                        index=players["fpl_id"].to_numpy(), columns=gameweeks)


def weighted_points(projection: Projection,
                    half_life: float | None = DEFAULT_HALF_LIFE) -> tuple[pd.DataFrame, pd.DataFrame]:
    gameweeks = sorted(projection.per_fixture["gw"].unique())
    raw = (projection.per_fixture
           .pivot_table(index="fpl_id", columns="gw", values="xpts", aggfunc="sum")
           .reindex(columns=gameweeks)
           .fillna(0.0))

    players = projection.players.set_index("fpl_id").loc[raw.index].reset_index()
    survival = survival_curve(players, gameweeks)
    survival.index = raw.index

    decay = decay_weights(gameweeks, half_life)
    weighted = raw * survival * decay
    return raw, weighted


def apply_plan_weighting(projection: Projection,
                         half_life: float | None = DEFAULT_HALF_LIFE) -> pd.DataFrame:
    raw, weighted = weighted_points(projection, half_life)
    players = projection.players.copy()

    plan_points = weighted.sum(axis=1)
    raw_points = raw.sum(axis=1)
    first_gw = raw.columns[0]

    players["xpts_plan"] = players["fpl_id"].map(plan_points).fillna(0.0)
    players["xpts_raw"] = players["fpl_id"].map(raw_points).fillna(0.0)
    players["xpts_gw1"] = players["fpl_id"].map(raw[first_gw]).fillna(0.0)
    players["xpts_plan_per_m"] = players["xpts_plan"] / players["price"]

    with np.errstate(divide="ignore", invalid="ignore"):
        share = players["xpts_gw1"] * len(raw.columns) / players["xpts_raw"].replace(0, np.nan)
    players["frontloaded"] = share.fillna(0.0)
    return players


def price_forecast(players: pd.DataFrame, n_gameweeks: int,
                   total_managers: int = 0) -> pd.DataFrame:
    df = players.copy()
    net = df["transfers_in_event"] - df["transfers_out_event"]
    owned = pd.to_numeric(df["selected_by_percent"], errors="coerce").fillna(0.0)

    if net.abs().sum() > 0 and total_managers > 0:
        holders = (owned / 100.0 * total_managers).clip(lower=1000)
        pressure = (net / holders).rank(pct=True) - 0.5
        basis = "net transfers"
        confidence = "medium"
    else:
        value = df["xpts_plan_per_m"] if "xpts_plan_per_m" in df else df["xpts_per_m"]
        pressure = value.rank(pct=True) - 0.5
        basis = "value proxy (no transfer data yet)"
        confidence = "low"

    ownership_weight = (owned / 100.0).clip(0.0, 0.6)
    falling = pressure < 0
    amplifier = np.where(falling,
                         1.0 + FALL_OWNERSHIP_AMPLIFIER * ownership_weight,
                         1.0 - RISE_OWNERSHIP_DAMPING * ownership_weight)

    df["rise_score"] = pressure + 0.5
    df["ownership_pct"] = owned
    df["exp_price_change"] = (pressure * amplifier * 0.4
                              * (n_gameweeks / 5.0)).clip(-0.4, 0.4).round(2)
    df.attrs["price_basis"] = basis
    df.attrs["price_confidence"] = confidence
    return df


def field_exposure(players: pd.DataFrame, squad_ids: list[int],
                   points_column: str = "xpts_plan", limit: int = 12) -> pd.DataFrame:
    owned = pd.to_numeric(players["selected_by_percent"], errors="coerce").fillna(0.0) / 100.0
    df = players.assign(
        ownership_pct=owned * 100,
        exposure=owned * players[points_column],
        in_squad=players["fpl_id"].isin(squad_ids),
    )
    missing = df[~df["in_squad"]].nlargest(limit, "exposure")
    return missing[["web_name", "pos", "team_short", "price", "ownership_pct",
                    points_column, "exposure"]].reset_index(drop=True)


def coverage(players: pd.DataFrame, squad_ids: list[int],
             points_column: str = "xpts_plan") -> dict:
    owned = pd.to_numeric(players["selected_by_percent"], errors="coerce").fillna(0.0) / 100.0
    weighted = owned * players[points_column]
    total = float(weighted.sum())
    held = float(weighted[players["fpl_id"].isin(squad_ids)].sum())
    return {
        "field_total": total,
        "covered": held,
        "covered_share": held / total if total else 0.0,
        "exposed": total - held,
    }


def fixture_counts(projection: Projection) -> pd.DataFrame:
    fixtures = projection.fixtures
    home = fixtures.groupby(["home_team", "gw"]).size().rename("n")
    away = fixtures.groupby(["away_team", "gw"]).size().rename("n")
    home.index.names = away.index.names = ["team", "gw"]
    counts = pd.concat([home, away]).groupby(["team", "gw"]).sum().unstack(fill_value=0)
    return counts.reindex(columns=projection.horizon, fill_value=0)


def _legal_formations() -> list[tuple[int, int, int]]:
    return [
        (d, m, f)
        for d in range(XI_MIN_BY_POS["DEF"], XI_MAX_BY_POS["DEF"] + 1)
        for m in range(XI_MIN_BY_POS["MID"], XI_MAX_BY_POS["MID"] + 1)
        for f in range(XI_MIN_BY_POS["FWD"], XI_MAX_BY_POS["FWD"] + 1)
        if d + m + f == XI_SIZE - 1
    ]


FORMATIONS = _legal_formations()


def best_xi_matrix(points: np.ndarray, positions: np.ndarray,
                   captain: bool = True) -> np.ndarray:
    if points.size == 0:
        return np.zeros(0)
    n_gws = points.shape[1]

    cumulative: dict[str, np.ndarray] = {}
    for position in ("GKP", "DEF", "MID", "FWD"):
        block = points[positions == position]
        if block.size == 0:
            cumulative[position] = np.zeros((1, n_gws))
            continue
        ordered = -np.sort(-block, axis=0)
        cumulative[position] = np.vstack([np.zeros((1, n_gws)),
                                          np.cumsum(ordered, axis=0)])

    if cumulative["GKP"].shape[0] < 2:
        return np.zeros(n_gws)
    keeper = cumulative["GKP"][1]

    best = np.full(n_gws, -np.inf)
    for defenders, midfielders, forwards in FORMATIONS:
        if (cumulative["DEF"].shape[0] <= defenders
                or cumulative["MID"].shape[0] <= midfielders
                or cumulative["FWD"].shape[0] <= forwards):
            continue
        total = (keeper + cumulative["DEF"][defenders]
                 + cumulative["MID"][midfielders] + cumulative["FWD"][forwards])
        best = np.maximum(best, total)

    best = np.where(np.isfinite(best), best, 0.0)
    if captain:
        best = best + points.max(axis=0)
    return best


def squad_points_by_gw(squad_ids: list[int], players: pd.DataFrame,
                       points: pd.DataFrame, captain: bool = True) -> pd.Series:
    positions = players.set_index("fpl_id")["pos"]
    ids = [i for i in squad_ids if i in points.index]
    if not ids:
        return pd.Series(0.0, index=points.columns, name="xi_points")
    sub = points.loc[ids]
    totals = best_xi_matrix(sub.to_numpy(float),
                            positions.reindex(ids).to_numpy(), captain)
    return pd.Series(totals, index=points.columns, name="xi_points")


def _best_xi_ids(ids_by_pos: dict[str, list[int]], pts: pd.Series) -> tuple[list[int], tuple]:
    gkp = sorted(ids_by_pos["GKP"], key=lambda i: -pts[i])[:1]
    best_total, best_ids, best_formation = -np.inf, None, None
    for defenders, midfielders, forwards in FORMATIONS:
        if (not gkp or len(ids_by_pos["DEF"]) < defenders
                or len(ids_by_pos["MID"]) < midfielders
                or len(ids_by_pos["FWD"]) < forwards):
            continue
        defs = sorted(ids_by_pos["DEF"], key=lambda i: -pts[i])[:defenders]
        mids = sorted(ids_by_pos["MID"], key=lambda i: -pts[i])[:midfielders]
        fwds = sorted(ids_by_pos["FWD"], key=lambda i: -pts[i])[:forwards]
        chosen = gkp + defs + mids + fwds
        total = float(pts[chosen].sum())
        if total > best_total:
            best_total, best_ids, best_formation = total, chosen, (defenders, midfielders, forwards)
    return best_ids or [], best_formation or (0, 0, 0)


def gw_lineups(squad_ids: list[int], players: pd.DataFrame,
              raw: pd.DataFrame) -> pd.DataFrame:
    positions = players.set_index("fpl_id")["pos"]
    names = players.set_index("fpl_id")["web_name"]
    ids = [i for i in squad_ids if i in raw.index]
    ids_by_pos = {pos: [i for i in ids if positions.get(i) == pos]
                 for pos in ("GKP", "DEF", "MID", "FWD")}

    rows = []
    for gw in raw.columns:
        pts = raw.loc[ids, gw]
        starters, formation = _best_xi_ids(ids_by_pos, pts)
        bench = sorted((i for i in ids if i not in starters), key=lambda i: -pts[i])
        captain_id = max(starters, key=lambda i: pts[i]) if starters else None
        vice_id = max((i for i in starters if i != captain_id), key=lambda i: pts[i], default=None)
        xi_points = float(pts[starters].sum() + (pts[captain_id] if captain_id is not None else 0.0))
        rows.append({
            "gw": gw,
            "formation": "-".join(str(n) for n in formation),
            "captain": names.get(captain_id, ""),
            "vice_captain": names.get(vice_id, ""),
            "starting_xi": ", ".join(names[i] for i in starters),
            "bench_order": ", ".join(names[i] for i in bench),
            "xi_points": round(xi_points, 1),
        })
    return pd.DataFrame(rows)


def fixture_timeline(squad_ids: list[int], players: pd.DataFrame,
                     raw: pd.DataFrame, projection: Projection) -> pd.DataFrame:
    positions = players.set_index("fpl_id")
    gameweeks = list(raw.columns)

    opponents = (projection.per_fixture
                 .assign(label=lambda d: np.where(d["was_home"], d["opponent"].str.upper(),
                                                  d["opponent"].str.lower()))
                 .groupby(["fpl_id", "gw"])["label"]
                 .agg(lambda s: "+".join(s)))

    rows = []
    for fpl_id in squad_ids:
        if fpl_id not in raw.index:
            continue
        series = raw.loc[fpl_id]
        baseline = series.mean()
        row = {
            "web_name": positions.at[fpl_id, "web_name"],
            "pos": positions.at[fpl_id, "pos"],
            "team_short": positions.at[fpl_id, "team_short"],
            "price": positions.at[fpl_id, "price"],
        }
        for gw in gameweeks:
            value = float(series[gw])
            if baseline <= 0:
                marker = "·"
            elif value >= baseline * 1.15:
                marker = "+"
            elif value <= baseline * 0.85:
                marker = "-"
            else:
                marker = "="
            row[f"gw{gw}"] = f"{marker}{value:.1f}"
        row["swing"] = float(series.max() - series.min())
        worst = series.idxmin()
        row["worst_gw"] = int(worst)
        row["worst_vs"] = opponents.get((fpl_id, worst), "")
        rows.append(row)

    timeline = pd.DataFrame(rows)
    return timeline.sort_values("swing", ascending=False).reset_index(drop=True)


def sell_windows(timeline: pd.DataFrame, gameweeks: list[int],
                 threshold: float = 0.85) -> pd.DataFrame:
    rows = []
    for _, player in timeline.iterrows():
        run: list[int] = []
        for gw in gameweeks:
            cell = player.get(f"gw{gw}", "")
            if isinstance(cell, str) and cell.startswith("-"):
                run.append(gw)
                continue
            if len(run) >= 2:
                rows.append({"web_name": player["web_name"], "pos": player["pos"],
                             "from_gw": run[0], "to_gw": run[-1], "length": len(run)})
            run = []
        if len(run) >= 2:
            rows.append({"web_name": player["web_name"], "pos": player["pos"],
                         "from_gw": run[0], "to_gw": run[-1], "length": len(run)})
    if not rows:
        return pd.DataFrame(columns=["web_name", "pos", "from_gw", "to_gw", "length"])
    return pd.DataFrame(rows).sort_values(["from_gw", "length"],
                                          ascending=[True, False]).reset_index(drop=True)


def horizon_sensitivity(projection: Projection, budget: float, bench_weight: float,
                        min_minutes_prob: float,
                        half_life: float | None = DEFAULT_HALF_LIFE,
                        max_horizon: int | None = None) -> pd.DataFrame:
    raw, weighted = weighted_points(projection, half_life)
    gameweeks = list(raw.columns)
    max_horizon = min(max_horizon or len(gameweeks), len(gameweeks))

    players = projection.players.copy()
    plan_points = weighted.sum(axis=1)
    players["xpts_plan"] = players["fpl_id"].map(plan_points).fillna(0.0)
    plan_squad = optimise(players, budget=budget, bench_weight=bench_weight,
                          min_minutes_prob=min_minutes_prob, points_column="xpts_plan")
    plan_ids = set(plan_squad.players["fpl_id"])

    rows, previous_ids = [], None
    for horizon in range(1, max_horizon + 1):
        window = gameweeks[:horizon]
        players["xpts_h"] = players["fpl_id"].map(raw[window].sum(axis=1)).fillna(0.0)
        squad = optimise(players, budget=budget, bench_weight=bench_weight,
                         min_minutes_prob=min_minutes_prob, points_column="xpts_h")
        ids = set(squad.players["fpl_id"])
        rows.append({
            "horizon": horizon,
            "last_gw": window[-1],
            "changed_vs_prev": (len(ids - previous_ids) if previous_ids else np.nan),
            "shared_with_plan": len(ids & plan_ids),
        })
        previous_ids = ids

    return pd.DataFrame(rows)


def build_plan(projection: Projection, budget: float, bench_weight: float,
               min_minutes_prob: float,
               half_life: float | None = DEFAULT_HALF_LIFE,
               total_managers: int = 0, include: list[int] | None = None,
               exclude: list[int] | None = None,
               ownership_weight: float = 0.0) -> Plan:
    players = apply_plan_weighting(projection, half_life)
    players = price_forecast(players, len(projection.horizon), total_managers)
    raw, weighted = weighted_points(projection, half_life)

    squad = optimise(players, budget=budget, bench_weight=bench_weight,
                     include=include, exclude=exclude,
                     min_minutes_prob=min_minutes_prob, points_column="xpts_plan",
                     ownership_weight=ownership_weight)
    squad_ids = list(squad.players["fpl_id"])
    bank = budget - squad.total_cost

    per_gw = pd.DataFrame({
        "gw": raw.columns,
        "xi_xpts": squad_points_by_gw(squad_ids, players, raw).to_numpy(),
        "decay": decay_weights(list(raw.columns), half_life).to_numpy(),
    })
    per_gw["odds_priced"] = [
        bool(projection.fixtures.loc[projection.fixtures["gw"] == gw, "has_odds"].any())
        for gw in raw.columns
    ]

    lineups = gw_lineups(squad_ids, players, raw)
    timeline = fixture_timeline(squad_ids, players, raw, projection)
    windows = sell_windows(timeline, list(raw.columns))
    exposure = field_exposure(players, squad_ids)
    cover = coverage(players, squad_ids)

    core_ids = set(squad_ids)
    for candidate_half_life in (1.0, 6.0):
        alternative = apply_plan_weighting(projection, candidate_half_life)
        other = optimise(alternative, budget=budget, bench_weight=bench_weight,
                         include=include, exclude=exclude,
                         min_minutes_prob=min_minutes_prob, points_column="xpts_plan")
        core_ids &= set(other.players["fpl_id"])
    core = players[players["fpl_id"].isin(core_ids)].copy()

    notes = []
    if not any(per_gw["odds_priced"]):
        notes.append("no fixtures in this window are priced by the bookmakers yet")
    else:
        priced = int(per_gw["odds_priced"].sum())
        notes.append(f"{priced} of {len(per_gw)} gameweeks are bookmaker-priced; "
                     "the rest come from xG ratings")
    notes.append(f"price forecast basis: {players.attrs.get('price_basis', 'n/a')} "
                 f"(confidence {players.attrs.get('price_confidence', 'n/a')})")
    movers = int(players.loc[players["fpl_id"].isin(squad_ids), "moved_club"].sum()) \
        if "moved_club" in players else 0
    if movers:
        notes.append(f"{movers} of the 15 changed club this summer; their rates are "
                     "adjusted for the new team and shrunk harder (see `movers`)")

    return Plan(squad=squad, players=players, per_gw=per_gw, lineups=lineups,
                timeline=timeline, windows=windows, exposure=exposure, coverage=cover,
                core=core, horizon=list(raw.columns),
                half_life=half_life, bank=bank, notes=notes)

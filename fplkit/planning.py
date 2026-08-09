"""Turning a projection into a plan.

A projection answers "how many points over the next N gameweeks?", and that
answer is uncomfortably sensitive to N -- pick 3 and you get a fixture-chasing
squad, pick 8 and you get a squad built for games you will never actually field
it in, because you will have made six transfers by then.

The fix is to stop treating the horizon as a cliff. Three things all decay with
distance, and modelling them removes most of the horizon sensitivity:

  1. **Optionality.** You get a free transfer every week. A bad fixture in five
     gameweeks is not a cost you are locked into, so it should not weigh as much
     as this week's fixture, which you are.
  2. **Availability.** Players get injured, suspended and dropped. The chance
     that today's nailed starter is still a nailed starter in gameweek 8 is
     meaningfully below one.
  3. **Model confidence.** The bookmakers have priced roughly the next
     fortnight. Beyond that the fixture projections come from ratings alone.

All three point the same way, so the plan discounts future gameweeks
geometrically and multiplies by a survival curve. The result barely moves when
you change the horizon, which is the property we actually want.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .config import XI_MAX_BY_POS, XI_MIN_BY_POS, XI_SIZE
from .model import Projection
from .optimise import Squad, optimise

# Points this many gameweeks out are worth half of this gameweek's. Three is a
# deliberate choice: with one free transfer a week you can turn over a third of
# the squad inside three gameweeks, so that is roughly the distance at which
# "who I own now" stops constraining "who I will field".
DEFAULT_HALF_LIFE = 3.0

# Per-gameweek probability that an available player becomes unavailable, before
# the age adjustment. Over a 38-game season this compounds to roughly the
# fraction of the year a typical starter loses to injury and suspension.
BASE_HAZARD = 0.030
AGE_HAZARD_SLOPE = 0.08  # extra hazard per year over the threshold
AGE_HAZARD_FROM = 29.0
DOUBTFUL_HAZARD_MULTIPLIER = 2.0

# How much a player's ownership amplifies a price move already in progress.
# Falls are amplified because only owners can sell; rises are damped because
# the rise threshold scales with ownership and most likely buyers already own
# him. See price_forecast for the reasoning.
FALL_OWNERSHIP_AMPLIFIER = 1.5
RISE_OWNERSHIP_DAMPING = 0.5

# Chip windows come straight from the API's `chips` block, but these are the
# defaults if it cannot be read.
FIRST_HALF_END = 19


@dataclass
class Plan:
    squad: Squad
    players: pd.DataFrame  # projection + xpts_plan, survival, price forecast
    per_gw: pd.DataFrame  # what the squad is projected to score each gameweek
    lineups: pd.DataFrame  # starting XI, bench order and captain, picked fresh per gameweek
    timeline: pd.DataFrame  # when each player's fixtures turn good or bad
    windows: pd.DataFrame  # runs of bad fixtures worth banking a transfer for
    exposure: pd.DataFrame  # high-ownership players not owned
    coverage: dict  # share of the field's expected points the squad covers
    chips: pd.DataFrame  # suggested chip timing
    core: pd.DataFrame  # players the plan keeps at every horizon
    horizon: list[int]
    half_life: float | None  # None = no decay: every gameweek at full value
    bank: float
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Weighting: decay and survival
# --------------------------------------------------------------------------- #

def decay_weights(gameweeks: list[int],
                  half_life: float | None = DEFAULT_HALF_LIFE) -> pd.Series:
    """Geometric discount on future gameweeks, indexed by gameweek.

    A `half_life` of None -- or infinity, which is the same statement written
    as a number -- is no discount at all: every gameweek in the horizon counts
    its full value. That is a real setting rather than a degenerate one. It is
    what "rank on total points over the next N gameweeks" means, and it is the
    top stop on the board's fixture-decay slider.
    """
    steps = np.arange(len(gameweeks))
    if half_life is None or math.isinf(half_life):
        return pd.Series(1.0, index=gameweeks, name="decay")
    return pd.Series(0.5 ** (steps / half_life), index=gameweeks, name="decay")


def injury_hazard(players: pd.DataFrame, base: float = BASE_HAZARD) -> pd.Series:
    """Per-gameweek probability of dropping out, by player.

    Age is the one durable, observable risk factor available here. A player
    already flagged doubtful carries roughly double the baseline risk of the
    problem recurring, on top of the chance_of_playing discount already applied
    to his start probability.
    """
    today = pd.Timestamp(datetime.now().date())
    birth = pd.to_datetime(players.get("birth_date"), errors="coerce")
    age = (today - birth).dt.days / 365.25

    hazard = pd.Series(base, index=players.index, dtype=float)
    older = (age - AGE_HAZARD_FROM).clip(lower=0).fillna(0.0)
    hazard = hazard * (1 + AGE_HAZARD_SLOPE * older)
    hazard = hazard.where(players["status"] != "d", hazard * DOUBTFUL_HAZARD_MULTIPLIER)
    return hazard.clip(0.005, 0.25)


def survival_curve(players: pd.DataFrame, gameweeks: list[int]) -> pd.DataFrame:
    """P(still available) for each player in each gameweek, as players x gameweeks.

    The first gameweek is not discounted: current availability is already in the
    player's start probability, and this curve only describes what might go
    wrong between now and later.
    """
    hazard = injury_hazard(players).to_numpy()[:, None]
    steps = np.arange(len(gameweeks))[None, :]
    return pd.DataFrame((1 - hazard) ** steps,
                        index=players["fpl_id"].to_numpy(), columns=gameweeks)


def weighted_points(projection: Projection,
                    half_life: float | None = DEFAULT_HALF_LIFE) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-player, per-gameweek points, raw and plan-weighted.

    Returns (raw, weighted), both players x gameweeks. Two fixtures in one
    gameweek are summed, so double gameweeks fall out for free.
    """
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
    """Add xpts_plan (and its components) to the projection's player table."""
    raw, weighted = weighted_points(projection, half_life)
    players = projection.players.copy()

    plan_points = weighted.sum(axis=1)
    raw_points = raw.sum(axis=1)
    first_gw = raw.columns[0]

    players["xpts_plan"] = players["fpl_id"].map(plan_points).fillna(0.0)
    players["xpts_raw"] = players["fpl_id"].map(raw_points).fillna(0.0)
    players["xpts_gw1"] = players["fpl_id"].map(raw[first_gw]).fillna(0.0)
    players["xpts_plan_per_m"] = players["xpts_plan"] / players["price"]

    # How front-loaded a player is: high means his value is in the next couple
    # of gameweeks (a fixture-swing punt), low means it is spread out (a keeper).
    with np.errstate(divide="ignore", invalid="ignore"):
        share = players["xpts_gw1"] * len(raw.columns) / players["xpts_raw"].replace(0, np.nan)
    players["frontloaded"] = share.fillna(0.0)
    return players


# --------------------------------------------------------------------------- #
# Price changes
# --------------------------------------------------------------------------- #

def price_forecast(players: pd.DataFrame, n_gameweeks: int,
                   total_managers: int = 0) -> pd.DataFrame:
    """Rank players by how likely they are to rise in price.

    In-season this is driven by net transfers, which is the actual mechanism:
    FPL raises a player's price once net transfers in clear a threshold that
    scales with his ownership.

    Before the season starts there are no transfer counts at all, so this falls
    back to a proxy -- players who are both good value and already popular are
    the ones that get bought -- and the result is a ranking, not a calibrated
    price prediction. Treat it as a tie-breaker between similar players, never
    as a reason to pick a worse one.
    """
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

    # Ownership does not push a price in a direction -- it amplifies whichever
    # direction the player is already going, and it does so asymmetrically.
    #
    # A fall needs sellers, and only owners can sell, so a heavily owned player
    # going badly drops fast: there are millions of people able to leave. A rise
    # needs buyers relative to a threshold that itself scales with ownership, so
    # a heavily owned player rises more slowly -- most of the people who would
    # buy him already have him.
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


# --------------------------------------------------------------------------- #
# Rank risk: what the rest of the field owns
# --------------------------------------------------------------------------- #

def field_exposure(players: pd.DataFrame, squad_ids: list[int],
                   points_column: str = "xpts_plan", limit: int = 12) -> pd.DataFrame:
    """The players you do not own, ranked by how much they cost you if they hit.

    FPL is scored on rank, not on points, so the relevant question is not "how
    many points will I score" but "how many will I score relative to everyone
    else". A player owned by half the field who returns big costs you half his
    haul in relative terms even though your own total is unaffected. That is why
    not owning Haaland can hurt more than owning a mediocre midfielder.

    Exposure here is ownership x projected points: roughly the points the
    average rival banks from him that you do not. It is a risk measure, not a
    recommendation -- covering every high-ownership player is how you guarantee
    finishing exactly average.
    """
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
    """How much of the field's expected points your squad actually covers."""
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


# --------------------------------------------------------------------------- #
# Fixture shape: double and blank gameweeks
# --------------------------------------------------------------------------- #

def fixture_counts(projection: Projection) -> pd.DataFrame:
    """Fixtures per team per gameweek: 2 is a double, 0 is a blank."""
    fixtures = projection.fixtures
    home = fixtures.groupby(["home_team", "gw"]).size().rename("n")
    away = fixtures.groupby(["away_team", "gw"]).size().rename("n")
    home.index.names = away.index.names = ["team", "gw"]
    counts = pd.concat([home, away]).groupby(["team", "gw"]).sum().unstack(fill_value=0)
    return counts.reindex(columns=projection.horizon, fill_value=0)


# --------------------------------------------------------------------------- #
# Best XI within a fixed squad
# --------------------------------------------------------------------------- #

def _legal_formations() -> list[tuple[int, int, int]]:
    """(DEF, MID, FWD) counts that make a legal ten outfield players."""
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
    """Best legal XI total for every gameweek at once.

    `points` is players x gameweeks for one squad. Sorting each position block
    descending and taking cumulative sums turns "best d defenders" into a single
    array lookup, so every formation is scored across every gameweek with a
    handful of vector operations. This sits in the innermost loop of the
    transfer search, which is why it is worth doing this way.
    """
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
    """Best-XI points for a fixed squad in each gameweek, captain doubled."""
    positions = players.set_index("fpl_id")["pos"]
    ids = [i for i in squad_ids if i in points.index]
    if not ids:
        return pd.Series(0.0, index=points.columns, name="xi_points")
    sub = points.loc[ids]
    totals = best_xi_matrix(sub.to_numpy(float),
                            positions.reindex(ids).to_numpy(), captain)
    return pd.Series(totals, index=points.columns, name="xi_points")


def _best_xi_ids(ids_by_pos: dict[str, list[int]], pts: pd.Series) -> tuple[list[int], tuple]:
    """Best legal XI (as player ids) for one gameweek's points, plus its formation."""
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
    """Starting XI, bench order and captain for a fixed squad, picked fresh each gameweek.

    The 15-man squad does not change without a transfer, but who starts and who
    captains is a weekly decision made with that week's own fixture, not the
    plan's decayed weighting -- a squad player whose fixture swings that week
    should rotate in and take the armband even though nobody was bought or
    sold. This is what turns `best_xi_matrix`'s per-gameweek totals into an
    actual, actionable selection: which player, not just how many points.
    """
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


# --------------------------------------------------------------------------- #
# Transfer path
# --------------------------------------------------------------------------- #

def fixture_timeline(squad_ids: list[int], players: pd.DataFrame,
                     raw: pd.DataFrame, projection: Projection) -> pd.DataFrame:
    """When each squad player's fixtures turn good or bad.

    This deliberately replaces a predicted transfer schedule. Rolling a squad
    forward on projections alone produces transfers that undo each other -- swap
    out for one gameweek's fixture, swap back for the next -- because the search
    has no idea that a transfer is a spent resource and that in three weeks you
    will know things you do not know now. Two free transfers burned to end up
    where you started is strictly worse than holding.

    What survives contact with reality is the fixture shape: which gameweeks a
    player is worth owning through, and which ones he is not. Decide the actual
    transfer when it arrives, with form and injury news you do not have yet.
    """
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
            # Mark relative to the player's own average, so this reads as
            # "good week for him" rather than "good player".
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
    """Runs of consecutive bad gameweeks, as candidate windows to move a player on.

    A single poor fixture is rarely worth a transfer. Two or more in a row is a
    window, and knowing when it starts is what lets you bank a transfer for it
    rather than spending one now.
    """
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


# --------------------------------------------------------------------------- #
# Chips
# --------------------------------------------------------------------------- #

def chip_advice(squad: Squad, players: pd.DataFrame, raw: pd.DataFrame,
                counts: pd.DataFrame, chip_windows: dict[str, tuple[int, int]],
                ) -> pd.DataFrame:
    """Where in the horizon each chip looks best, and how confident that is.

    Chip timing is mostly decided by double and blank gameweeks, which the
    fixture list does not know about until the cup rounds are drawn and games
    get postponed. Early in the season every team plays exactly once every
    gameweek, so these recommendations carry very little signal -- they are
    marked accordingly rather than dressed up.
    """
    squad_ids = list(squad.players["fpl_id"])
    gameweeks = [gw for gw in raw.columns]
    positions = players.set_index("fpl_id")["pos"]
    names = players.set_index("fpl_id")["web_name"]
    teams = players.set_index("fpl_id")["team"]

    doubles = {gw: sorted(counts.index[counts[gw] >= 2]) for gw in gameweeks}
    blanks = {gw: sorted(counts.index[counts[gw] == 0]) for gw in gameweeks}
    any_variation = any(doubles[gw] or blanks[gw] for gw in gameweeks)

    rows = []

    def in_window(chip: str, gw: int) -> bool:
        start, stop = chip_windows.get(chip, (1, 38))
        return start <= gw <= stop

    # Triple captain: the single biggest one-player gameweek.
    best_gw, best_id, best_value = None, None, -1.0
    for gw in gameweeks:
        if not in_window("3xc", gw):
            continue
        column = raw.loc[raw.index.intersection(squad_ids), gw]
        if column.empty:
            continue
        if column.max() > best_value:
            best_value, best_id, best_gw = float(column.max()), column.idxmax(), gw
    if best_gw:
        rows.append({
            "chip": "Triple Captain", "gw": best_gw,
            "detail": f"{names[best_id]} ({best_value:.1f} xPts)",
            "confidence": "medium" if doubles.get(best_gw) else "low",
        })

    # Bench boost: the gameweek where the four bench players score most.
    bench_ids = list(squad.bench["fpl_id"])
    best_gw, best_value = None, -1.0
    for gw in gameweeks:
        if not in_window("bboost", gw):
            continue
        value = float(raw.loc[raw.index.intersection(bench_ids), gw].sum())
        if value > best_value:
            best_value, best_gw = value, gw
    if best_gw:
        rows.append({
            "chip": "Bench Boost", "gw": best_gw,
            "detail": f"bench projects {best_value:.1f} xPts",
            "confidence": "medium" if doubles.get(best_gw) else "low",
        })

    # Free hit: the gameweek where most of the squad has no fixture.
    best_gw, worst_available = None, XI_SIZE + 1
    for gw in gameweeks:
        if not in_window("freehit", gw):
            continue
        playing = sum(counts.loc[teams[i], gw] > 0 for i in squad_ids
                      if teams[i] in counts.index)
        if playing < worst_available:
            worst_available, best_gw = playing, gw
    if best_gw and worst_available < len(squad_ids):
        rows.append({
            "chip": "Free Hit", "gw": best_gw,
            "detail": f"only {worst_available}/15 have a fixture",
            "confidence": "medium",
        })

    # Wildcard: where the squad drifts furthest from what you would pick fresh.
    best_gw, worst_gap = None, -1.0
    for gw in gameweeks:
        if not in_window("wildcard", gw):
            continue
        held = squad_points_by_gw(squad_ids, players, raw[[gw]], captain=False).iloc[0]

        # The benchmark is the best legal XI available anywhere in the league
        # that gameweek, ignoring budget. Both sides of the comparison have to
        # be an XI for the gap to mean anything.
        ranked = {
            position: raw.loc[[i for i in raw.index if positions.get(i) == position],
                              gw].nlargest(XI_MAX_BY_POS.get(position, 5)).to_numpy()
            for position in ("GKP", "DEF", "MID", "FWD")
        }
        ideal = 0.0
        for defenders, midfielders, forwards in FORMATIONS:
            if (ranked["GKP"].size < 1 or ranked["DEF"].size < defenders
                    or ranked["MID"].size < midfielders
                    or ranked["FWD"].size < forwards):
                continue
            ideal = max(ideal, float(ranked["GKP"][0]
                                     + ranked["DEF"][:defenders].sum()
                                     + ranked["MID"][:midfielders].sum()
                                     + ranked["FWD"][:forwards].sum()))
        gap = ideal - held
        if gap > worst_gap:
            worst_gap, best_gw = gap, gw
    if best_gw:
        rows.append({
            "chip": "Wildcard", "gw": best_gw,
            "detail": "largest gap to a freshly picked squad",
            "confidence": "low",
        })

    chips = pd.DataFrame(rows)

    # Chip value comes overwhelmingly from double gameweeks and from covering
    # blanks, and neither exists on the calendar until cup rounds are drawn and
    # games are postponed. With a flat fixture list the "best" gameweek for a
    # chip is whichever one noise favours, so say nothing rather than dress that
    # up as a recommendation. The first-half set does not expire until GW19,
    # which is a long time to wait for real information.
    if not chips.empty and not any_variation:
        window_end = min((stop for _, stop in chip_windows.values()), default=FIRST_HALF_END)
        chips["gw"] = pd.NA
        chips["detail"] = ("hold — no doubles or blanks scheduled in this window; "
                           f"the first-half set does not expire until GW{window_end}")
        chips["confidence"] = "n/a (nothing to time against yet)"
    return chips


# --------------------------------------------------------------------------- #
# Horizon sensitivity
# --------------------------------------------------------------------------- #

def horizon_sensitivity(projection: Projection, budget: float, bench_weight: float,
                        min_minutes_prob: float,
                        half_life: float | None = DEFAULT_HALF_LIFE,
                        max_horizon: int | None = None) -> pd.DataFrame:
    """How much the recommended squad changes as the horizon is extended.

    Solves the squad for every horizon from one gameweek up, on raw summed
    points, and measures the overlap with both the previous horizon and the
    decay-weighted plan. If the plan weighting is doing its job, its squad sits
    in the middle of the range and stops moving well before the raw squads do.
    """
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


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def build_plan(projection: Projection, budget: float, bench_weight: float,
               min_minutes_prob: float,
               half_life: float | None = DEFAULT_HALF_LIFE,
               chip_windows: dict[str, tuple[int, int]] | None = None,
               total_managers: int = 0, include: list[int] | None = None,
               exclude: list[int] | None = None,
               ownership_weight: float = 0.0) -> Plan:
    """Pick an initial squad on plan-weighted points and describe where it goes."""
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

    counts = fixture_counts(projection)
    chips = chip_advice(squad, players, raw, counts, chip_windows or {})

    lineups = gw_lineups(squad_ids, players, raw)
    timeline = fixture_timeline(squad_ids, players, raw, projection)
    windows = sell_windows(timeline, list(raw.columns))
    exposure = field_exposure(players, squad_ids)
    cover = coverage(players, squad_ids)

    # Players the plan keeps regardless of how far ahead you look: solve the
    # squad at a short and a long horizon and intersect.
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
                chips=chips, core=core, horizon=list(raw.columns),
                half_life=half_life, bank=bank, notes=notes)

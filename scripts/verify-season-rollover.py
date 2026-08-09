"""Proves the model still works once the FPL API stops serving last season.

Every rate in the projection is a season total divided by something, and the
FPL API serves *season-to-date* totals. Before a ball is kicked those totals are
last season's completed 38 matches, which is the only state this model could be
tested in for eight months of the year -- and the only state a hard-coded 38 is
correct for. A week into a new season the same field means three matches, and
nothing in a preseason run can tell you what the model does then.

So this simulates the rollover rather than waiting for it: take the real cached
data, scale each player's FPL totals down to `N` matches' worth, and tell the
model that `N` matches have been played. Understat is left alone, because it is
a completed season and stays one -- that divergence is the whole difficulty.

The old behaviour is reachable as `basis=None`, which falls back to the
hard-coded 38, so each check runs both ways and the contrast is the point.

    python scripts/verify-season-rollover.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplkit.model import (SeasonBasis, _shrink, attach_rates,  # noqa: E402
                          detect_movers, minutes_model, season_basis,
                          team_strength)
from fplkit.matching import match_players, match_team  # noqa: E402
from fplkit.sources import fpl_api, understat  # noqa: E402

MATCHES_IN = 6  # how far into the simulated season to stand
FULL_SEASON = 38

failures: list[str] = []


def check(name: str, condition: bool, detail: str) -> None:
    print(f"{'ok  ' if condition else 'FAIL'}  {name}: {detail}")
    if not condition:
        failures.append(name)


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, str]]:
    fpl_players = fpl_api.players()
    fpl_teams = fpl_api.teams()
    all_fixtures = fpl_api.fixtures()
    us_stats = understat.player_stats()

    team_names = fpl_teams["name"].tolist()
    us_clubs = sorted({c for clubs in us_stats["us_team_list"] for c in clubs})
    team_map = {club: match_team(club, team_names) for club in us_clubs}
    players = detect_movers(match_players(fpl_players, us_stats, team_map), team_map)
    return players, us_stats, all_fixtures, team_map


def mid_season(players: pd.DataFrame) -> pd.DataFrame:
    """The same squads, `MATCHES_IN` matches into a season instead of before one.

    Only the FPL columns are scaled. Understat is a different, completed season
    and does not shrink because this one is young -- which is exactly the state
    that used to collapse every attacking rate.
    """
    df = players.copy()
    share = MATCHES_IN / FULL_SEASON
    for column in ("minutes", "starts", "bonus", "yellow_cards", "saves",
                   "defensive_contribution"):
        if column in df:
            df[column] = df[column] * share
    df["starts"] = df["starts"].round()
    return df


def main() -> int:
    players, us_stats, all_fixtures, team_map = load()
    clubs = sorted(players["team"].dropna().unique())

    # --- the basis itself reads the calendar, not a constant ------------------
    preseason = season_basis(all_fixtures, dict(enumerate(clubs, start=1)), us_stats)
    check("preseason basis", preseason.preseason and preseason.fpl_matches == 38.0,
          f"no matches finished -> totals treated as a full {preseason.fpl_matches:.0f}")

    played = all_fixtures.copy()
    played["finished"] = played["gw"] <= MATCHES_IN
    id_to_name = dict(zip(fpl_api.teams()["team_id"], fpl_api.teams()["name"]))
    read_back = season_basis(played, id_to_name, us_stats)
    check("in-season basis", (not read_back.preseason)
          and read_back.fpl_matches == float(MATCHES_IN),
          f"{MATCHES_IN} gameweeks finished -> basis {read_back.fpl_matches:.0f}")

    basis = SeasonBasis(club_matches=pd.Series(float(MATCHES_IN), index=clubs),
                        understat_matches=float(FULL_SEASON), preseason=False)
    young = mid_season(players)

    # --- minutes: p_start still tracks who actually starts --------------------
    # Per-club normalisation hides a lot of this: whatever the denominator does
    # to the raw share, `_normalise_to` rescales each club back to eleven
    # starters, so the totals look right either way. What it cannot restore is
    # the *blend*. Understating the raw share by 38/6 crushes the evidence term,
    # the price-based prior fills the gap, and p_start quietly stops describing
    # who starts and starts describing who is expensive.
    strength = team_strength(young, us_stats, team_map, basis)
    fixed = minutes_model(young, basis=basis)
    stale = minutes_model(young)  # the hard-coded 38

    per_club = fixed.groupby("team")["p_start"].sum()
    check("clubs still field eleven", abs(per_club.mean() - 11.0) < 0.01,
          f"mean {per_club.mean():.2f}, min {per_club.min():.2f}, max {per_club.max():.2f}")

    evidenced = young["minutes"] >= 0.26 * MATCHES_IN * 90
    observed = (young.loc[evidenced, "starts"] / MATCHES_IN).clip(0, 1)
    tracks = fixed.loc[evidenced, "p_start"].corr(observed)
    drifts = stale.loc[evidenced, "p_start"].corr(observed)
    check("p_start tracks starts, not price", tracks > drifts + 0.05,
          f"correlation with observed start rate {tracks:.3f}, "
          f"hard-coded 38 gives {drifts:.3f}")

    price_pull = fixed.loc[evidenced, "p_start"].corr(young.loc[evidenced, "price"])
    stale_pull = stale.loc[evidenced, "p_start"].corr(young.loc[evidenced, "price"])
    check("and leans less on price", price_pull < stale_pull,
          f"correlation with price {price_pull:.3f}, "
          f"hard-coded 38 gives {stale_pull:.3f}")

    # --- rates: last season's xG keeps the weight last season's minutes earned -
    rated = attach_rates(young, strength, None, basis)

    established = rated["us_minutes"].fillna(0) >= 0.26 * FULL_SEASON * 90
    forwards = established & (rated["pos"] == "FWD")
    check("positional prior is not empty", int(forwards.sum()) > 0,
          f"{int(forwards.sum())} established forwards found by Understat minutes")

    # Understat did not change, so neither should anything derived from it. This
    # is the property the whole fix exists to hold: a rate is shrunk against the
    # minutes that produced it, and six matches of a *different* season are not
    # evidence about last season's xG one way or the other.
    full = attach_rates(players, team_strength(players, us_stats, team_map), None)
    kept = rated.loc[forwards, "npxg_per90"].mean()
    drift = abs(kept - full.loc[forwards, "npxg_per90"].mean())
    check("attacking rates survive the rollover", drift < 0.005,
          f"established forwards npxg/90 {kept:.3f}, "
          f"|Δ| {drift:.4f} against the same players preseason")

    # What weighting last season's xG by this season's minutes would have done:
    # six matches against a 1,200-minute prior is barely a third of the weight,
    # so every forward converges on the positional average and the model loses
    # the ability to tell them apart at all.
    prior = pd.Series(full.loc[forwards, "npxg_per90"].mean(), index=rated.index)
    crushed = _shrink(rated.loc[forwards, "raw_npxg_per90"],
                      young.loc[forwards, "minutes"], prior)
    spread, flattened = rated.loc[forwards, "npxg_per90"].std(), crushed.std()
    check("rather than collapsing them onto the prior", spread > 1.5 * flattened,
          f"spread across forwards {spread:.3f}, against {flattened:.3f} "
          f"if shrunk by this season's minutes")

    print()
    if failures:
        print(f"FAIL — {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("PASS — the model reads the calendar, not the constant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

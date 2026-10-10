
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import poisson as ps
from .config import (
    APPEARANCE_60_POINTS,
    APPEARANCE_POINTS,
    ASSIST_POINTS,
    ASSUMED_START_MINUTES,
    ASSUMED_SUB_MINUTES,
    CLEAN_SHEET_POINTS,
    DEF_CONTRIB_POINTS,
    DC_DISPERSION,
    DEF_CONTRIB_THRESHOLD,
    GOAL_POINTS,
    HOME_ADVANTAGE,
    LEAGUE_MEAN_GOALS,
    MATCHES_PER_SEASON,
    P60_MIDPOINT_MINUTES,
    P60_SLOPE_MINUTES,
    PENALTY_CONVERSION,
    PENALTY_GOAL_SHARE,
    PENALTY_MISS_POINTS,
    SAVE_POINTS,
    STATUS_AVAILABILITY,
    YELLOW_CARD_POINTS,
)
from .matching import match_players, match_team
from .sources import fpl_api, history, understat
from .sources import odds as odds_source
from .config import UNDERSTAT_SEASON

PROMOTED_ATTACK = 0.80
PROMOTED_DEFENCE = 1.25

ODDS_CALIBRATION_PRIOR_MATCHES = 2.0
PROMOTED_ODDS_CALIBRATION_PRIOR_MATCHES = 0.4
ODDS_CALIBRATION_MAX_LOG = 0.4

PLAYER_PRIOR_MINUTES = 1200

NPXG_PRIOR_MINUTES = 550
XA_PRIOR_MINUTES = 1000.0
DC_PRIOR_MINUTES = 300
TEAM_PRIOR_MATCHES = 8.0

MOVER_PRIOR_MULTIPLIER = 1.8

XI_OUTFIELD = 10

MAX_P_START = 0.95
SUBS_PER_MATCH = 6.0

MAX_MINS_IF_START = 90.0

START_PRIOR_MINUTES = 700.0

FRINGE_SHARE = 0.15
FRINGE_COLLAPSE_STRENGTH = 0.85

START_FORM_HALF_LIFE = 4.0
START_FORM_WEIGHT = 1.09
START_FORM_DECAY = 0.828
MAX_START_FORM_WEIGHT = 1.25
START_FORM_PRIOR_MATCHES = 3.0

MINUTES_FORM_PRIOR_MATCHES = 2.0

MINS_VOLATILE_CV = 0.35
MINS_FLAG_MIN_MATCHES = MINUTES_FORM_PRIOR_MATCHES

MAX_MINUTES_SCALE = 2.5
MAX_RATE_SCALE = 2.5

ASSISTS_PER_GOAL = 0.924
BONUS_PER_TEAM_MATCH = 2.78

ASSISTS_PER_OPEN_PLAY_GOAL = ASSISTS_PER_GOAL / (1 - PENALTY_GOAL_SHARE)

TRANSFER_CONTEXT_ALPHA = 0.5


PREVIOUS_SEASON_MATCHES = float(MATCHES_PER_SEASON)


ESTABLISHED_SHARE = 0.26
BLINDSPOT_SHARE = 0.13
THIN_SHARE = 0.08


@dataclass(frozen=True)
class SeasonBasis:

    club_matches: pd.Series
    understat_matches: float
    preseason: bool
    understat_current_matches: float = 0.0
    previous_scale: float = 1.0

    @property
    def fpl_matches(self) -> float:
        if not len(self.club_matches):
            return float(MATCHES_PER_SEASON)
        return float(self.club_matches.max())

    @property
    def fpl_minutes(self) -> float:
        return self.fpl_matches * 90.0

    @property
    def understat_minutes(self) -> float:
        return self.understat_matches * 90.0

    @property
    def previous_weight(self) -> float:
        if self.preseason:
            return 0.0
        fade = float(np.clip(1.0 - self.fpl_matches / PREVIOUS_SEASON_MATCHES, 0.0, 1.0))
        return fade * float(np.clip(self.previous_scale, 0.0, 1.0))

    @property
    def understat_weight(self) -> float:
        return 1.0 if self.preseason else self.previous_weight

    @property
    def pooled_fpl_minutes(self) -> float:
        return self.fpl_minutes + self.previous_weight * PREVIOUS_SEASON_MATCHES * 90.0

    @property
    def pooled_understat_minutes(self) -> float:
        return (self.understat_weight * self.understat_minutes
                + self.understat_current_matches * 90.0)


def _understat_matches(us_stats: pd.DataFrame | None) -> float:
    if us_stats is None or "us_minutes" not in us_stats:
        return 0.0
    us_minutes = pd.to_numeric(us_stats["us_minutes"], errors="coerce")
    return float(us_minutes.max()) / 90.0 if len(us_minutes) and us_minutes.max() > 0 else 0.0


def season_basis(all_fixtures: pd.DataFrame, id_to_name: dict[int, str],
                 us_stats: pd.DataFrame,
                 us_current: pd.DataFrame | None = None,
                 previous_scale: float = 1.0) -> SeasonBasis:
    finished = all_fixtures[all_fixtures["finished"].fillna(False).astype(bool)]
    played = (pd.concat([finished["team_h"], finished["team_a"]])
              .map(id_to_name).value_counts()
              .reindex(list(id_to_name.values())).fillna(0.0).astype(float))

    us_matches = _understat_matches(us_stats) or float(MATCHES_PER_SEASON)
    current = _understat_matches(us_current)

    if played.sum() == 0:
        return SeasonBasis(
            club_matches=pd.Series(float(MATCHES_PER_SEASON), index=played.index),
            understat_matches=us_matches, preseason=True,
            understat_current_matches=current, previous_scale=previous_scale)
    return SeasonBasis(club_matches=played.clip(lower=1.0),
                       understat_matches=us_matches, preseason=False,
                       understat_current_matches=current, previous_scale=previous_scale)


@dataclass
class Projection:

    players: pd.DataFrame
    per_fixture: pd.DataFrame
    fixtures: pd.DataFrame
    horizon: list[int]
    odds_coverage: float
    odds_note: str
    strength: pd.DataFrame | None = None
    basis: SeasonBasis | None = None
    notes: list[str] = field(default_factory=list)


def _team_xgc(players: pd.DataFrame, minutes_col: str, xgc_col: str,
              team_col: str) -> pd.Series:
    minutes = pd.to_numeric(players[minutes_col], errors="coerce").fillna(0.0)
    xgc = pd.to_numeric(players[xgc_col], errors="coerce").fillna(0.0)
    frame = pd.DataFrame({"team": players[team_col], "minutes": minutes, "xgc": xgc})
    frame = frame[(frame["minutes"] > 0) & frame["team"].notna()]
    totals = frame.groupby("team")[["minutes", "xgc"]].sum()
    return (totals["xgc"] / totals["minutes"] * 90.0).where(totals["minutes"] > 0)


def team_strength(players: pd.DataFrame, us_stats: pd.DataFrame,
                  team_map: dict[str, str],
                  basis: SeasonBasis | None = None,
                  us_current: pd.DataFrame | None = None,
                  team_map_current: dict[str, str] | None = None) -> pd.DataFrame:
    us_teams = understat.team_rates(us_stats)
    us_teams["fpl_team"] = us_teams["us_team"].map(team_map)
    attack = us_teams.set_index("fpl_team")["team_npxg_per_match"].to_dict()
    us_weight = basis.understat_weight if basis else 1.0
    us_matches = basis.understat_matches if basis else float(MATCHES_PER_SEASON)
    cur_matches = basis.understat_current_matches if basis else 0.0
    attack_now: dict[str, float] = {}
    if us_current is not None and len(us_current) and cur_matches > 0:
        now_teams = understat.team_rates(us_current)
        now_teams["fpl_team"] = now_teams["us_team"].map(team_map_current or {})
        attack_now = now_teams.set_index("fpl_team")["team_npxg_per_match"].to_dict()

    def played_here_last_season(row, team: str) -> bool:
        clubs = row.get("us_team_list")
        return isinstance(clubs, list) and any(team_map.get(c) == team for c in clubs)

    prev_weight = basis.previous_weight if basis else 0.0
    if basis is None or basis.preseason:
        here = players[players.apply(lambda r: played_here_last_season(r, r["team"]), axis=1)]
        this_season = _team_xgc(here, "minutes", "expected_goals_conceded", "team")
    else:
        this_season = _team_xgc(players, "minutes", "expected_goals_conceded", "team")
    if prev_weight > 0 and "prev_team" in players:
        last_season = _team_xgc(players, "prev_minutes",
                                "prev_expected_goals_conceded", "prev_team")
    else:
        last_season = pd.Series(dtype=float)

    rows = []
    for team in sorted(players["team"].dropna().unique()):
        club_matches = float(basis.club_matches.get(team, MATCHES_PER_SEASON)) if basis else float(MATCHES_PER_SEASON)
        att_parts = [(us_weight * us_matches, attack.get(team)),
                     (cur_matches, attack_now.get(team))]
        def_parts = [(club_matches, this_season.get(team)),
                     (prev_weight * PREVIOUS_SEASON_MATCHES, last_season.get(team))]

        def pooled(parts):
            parts = [(m, r) for m, r in parts if m > 0 and r is not None and not pd.isna(r)]
            matches = sum(m for m, _ in parts)
            return ((sum(m * r for m, r in parts) / matches, matches)
                    if matches > 0 else (np.nan, 0.0))

        npxg, att_m = pooled(att_parts)
        xgc, def_m = pooled(def_parts)
        rows.append({"team": team, "npxg_per_match": npxg, "xgc_per_match": xgc,
                     "attack_matches": att_m, "defence_matches": def_m,
                     "is_promoted": team not in attack})

    df = pd.DataFrame(rows)
    league_attack = df["npxg_per_match"].mean(skipna=True) or LEAGUE_MEAN_GOALS
    league_defence = df["xgc_per_match"].mean(skipna=True) or LEAGUE_MEAN_GOALS

    df.loc[df["is_promoted"] & df["npxg_per_match"].isna(), "npxg_per_match"] = \
        league_attack * PROMOTED_ATTACK
    df.loc[df["is_promoted"] & df["xgc_per_match"].isna(), "xgc_per_match"] = \
        league_defence * PROMOTED_DEFENCE
    df["npxg_per_match"] = df["npxg_per_match"].fillna(league_attack)
    df["xgc_per_match"] = df["xgc_per_match"].fillna(league_defence)

    attack_weight = df["attack_matches"] / (df["attack_matches"] + TEAM_PRIOR_MATCHES)
    defence_weight = df["defence_matches"] / (df["defence_matches"] + TEAM_PRIOR_MATCHES)

    df["npxg_per_match"] = (attack_weight * df["npxg_per_match"]
                            + (1 - attack_weight) * league_attack)
    df["xgc_per_match"] = (defence_weight * df["xgc_per_match"]
                           + (1 - defence_weight) * league_defence)

    df["attack_rating"] = df["npxg_per_match"] / league_attack
    df["defence_rating"] = df["xgc_per_match"] / league_defence

    open_play = LEAGUE_MEAN_GOALS * (1 - PENALTY_GOAL_SHARE)
    if df["npxg_per_match"].mean() > 0:
        df["npxg_per_match"] *= open_play / df["npxg_per_match"].mean()
    if df["xgc_per_match"].mean() > 0:
        df["xgc_per_match"] *= LEAGUE_MEAN_GOALS / df["xgc_per_match"].mean()

    df["league_npxg"] = league_attack
    df["league_xgc"] = league_defence
    return df


ODDS_KICKOFF_TOLERANCE = pd.Timedelta(days=4)


def _nearest_priced_match(candidates: list, kickoff) -> Any | None:
    if pd.isna(kickoff):
        return candidates[0] if len(candidates) == 1 else None
    best, best_gap = None, None
    for row in candidates:
        gap = abs(row["commence_time"] - kickoff)
        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    return best if best_gap is not None and best_gap <= ODDS_KICKOFF_TOLERANCE else None


def _attach_odds(fixtures: pd.DataFrame, teams: list[str]) -> pd.DataFrame:
    fixtures = fixtures.copy()
    for column in ("p_home", "p_draw", "p_away", "p_over", "totals_line"):
        fixtures[column] = np.nan
    fixtures["has_odds"] = False

    try:
        market = odds_source.match_odds()
    except odds_source.OddsUnavailable as error:
        return fixtures.assign(odds_note=str(error))
    except Exception as error:
        return fixtures.assign(odds_note=f"odds fetch failed: {error}")

    market["home_fpl"] = market["home_team_odds"].map(lambda n: match_team(n, teams))
    market["away_fpl"] = market["away_team_odds"].map(lambda n: match_team(n, teams))
    lookup: dict[tuple[str, str], list] = {}
    for _, row in market.iterrows():
        if row["home_fpl"] and row["away_fpl"]:
            lookup.setdefault((row["home_fpl"], row["away_fpl"]), []).append(row)

    for index, fixture in fixtures.iterrows():
        candidates = lookup.get((fixture["home_team"], fixture["away_team"]))
        hit = (_nearest_priced_match(candidates, fixture.get("kickoff_time"))
               if candidates else None)
        if hit is None:
            continue
        fixtures.loc[index, ["p_home", "p_draw", "p_away", "p_over", "totals_line"]] = [
            hit["p_home"], hit["p_draw"], hit["p_away"], hit["p_over"], hit["totals_line"]
        ]
        fixtures.loc[index, "has_odds"] = True

    return fixtures.assign(odds_note="")


def _rating_lambdas(home: str, away: str, ratings: pd.DataFrame,
                    attack_calib: dict[str, float],
                    defence_calib: dict[str, float]) -> tuple[float, float]:
    league = LEAGUE_MEAN_GOALS
    lh = (league * ratings.loc[home, "attack_rating"] * attack_calib.get(home, 1.0)
          * ratings.loc[away, "defence_rating"] * defence_calib.get(away, 1.0)
          * HOME_ADVANTAGE)
    la = (league * ratings.loc[away, "attack_rating"] * attack_calib.get(away, 1.0)
          * ratings.loc[home, "defence_rating"] * defence_calib.get(home, 1.0)
          / HOME_ADVANTAGE)
    return lh, la


def _odds_calibration(fixtures: pd.DataFrame, ratings: pd.DataFrame
                      ) -> tuple[dict[str, float], dict[str, float]]:
    attack_errors: dict[str, list[float]] = {}
    defence_errors: dict[str, list[float]] = {}

    def add(team: str, bucket: dict[str, list[float]], error: float) -> None:
        bucket.setdefault(team, []).append(error)

    for _, fixture in fixtures[fixtures["has_odds"]].iterrows():
        home, away = fixture["home_team"], fixture["away_team"]
        if home not in ratings.index or away not in ratings.index:
            continue
        rating_lh, rating_la = _rating_lambdas(home, away, ratings, {}, {})
        if rating_lh <= 0 or rating_la <= 0:
            continue
        err_home = np.clip(np.log(fixture["lam_home"] / rating_lh),
                           -ODDS_CALIBRATION_MAX_LOG, ODDS_CALIBRATION_MAX_LOG)
        err_away = np.clip(np.log(fixture["lam_away"] / rating_la),
                           -ODDS_CALIBRATION_MAX_LOG, ODDS_CALIBRATION_MAX_LOG)
        add(home, attack_errors, err_home / 2)
        add(away, defence_errors, err_home / 2)
        add(away, attack_errors, err_away / 2)
        add(home, defence_errors, err_away / 2)

    def shrunk_multipliers(errors: dict[str, list[float]]) -> dict[str, float]:
        out = {}
        for team, values in errors.items():
            n = len(values)
            promoted = bool(ratings.loc[team, "is_promoted"]) if team in ratings.index else False
            prior_matches = (PROMOTED_ODDS_CALIBRATION_PRIOR_MATCHES if promoted
                             else ODDS_CALIBRATION_PRIOR_MATCHES)
            weight = n / (n + prior_matches)
            out[team] = float(np.exp(weight * float(np.mean(values))))
        return out

    return shrunk_multipliers(attack_errors), shrunk_multipliers(defence_errors)


def fixture_lambdas(fixtures: pd.DataFrame, strength: pd.DataFrame,
                    teams: list[str], calibrate: bool = True) -> pd.DataFrame:
    fixtures = _attach_odds(fixtures, teams)
    ratings = strength.set_index("team")

    lam_home, lam_away, source = [], [], []
    lam_home_raw, lam_away_raw = [], []
    for _, fixture in fixtures.iterrows():
        home, away = fixture["home_team"], fixture["away_team"]
        if fixture["has_odds"]:
            lh, la = ps.lambdas_from_odds(
                fixture["p_home"], fixture["p_draw"], fixture["p_away"],
                None if pd.isna(fixture["p_over"]) else fixture["p_over"],
                None if pd.isna(fixture["totals_line"]) else fixture["totals_line"],
            )
            origin = "odds"
            raw_lh, raw_la = lh, la
        else:
            raw_lh, raw_la = _rating_lambdas(home, away, ratings, {}, {})
            lh, la = raw_lh, raw_la
            origin = "xg"
        lam_home.append(lh)
        lam_away.append(la)
        lam_home_raw.append(raw_lh)
        lam_away_raw.append(raw_la)
        source.append(origin)

    fixtures["lam_home"] = lam_home
    fixtures["lam_away"] = lam_away
    fixtures["lam_source"] = source
    fixtures["lam_home_uncalibrated"] = lam_home_raw
    fixtures["lam_away_uncalibrated"] = lam_away_raw

    if calibrate:
        attack_calib, defence_calib = _odds_calibration(fixtures, ratings)
        is_xg = fixtures["lam_source"] == "xg"
        for index, fixture in fixtures[is_xg].iterrows():
            home, away = fixture["home_team"], fixture["away_team"]
            lh, la = _rating_lambdas(home, away, ratings, attack_calib, defence_calib)
            fixtures.loc[index, "lam_home"] = lh
            fixtures.loc[index, "lam_away"] = la

    return fixtures


def _start_prior(df: pd.DataFrame) -> pd.Series:
    floor = df.groupby(["team", "is_keeper"])["price"].transform("min")
    return ((df["price"] - floor) + 0.5) ** 2


def start_form_weight(horizon: int) -> float:
    n = max(1, int(horizon))
    if START_FORM_DECAY >= 1.0:
        mean_decay = 1.0
    else:
        mean_decay = (1 - START_FORM_DECAY ** n) / (n * (1 - START_FORM_DECAY))
    return float(min(MAX_START_FORM_WEIGHT, START_FORM_WEIGHT * mean_decay))


def minutes_model(players: pd.DataFrame,
                  overrides: pd.DataFrame | None = None,
                  basis: SeasonBasis | None = None,
                  horizon: int = 1) -> pd.DataFrame:
    df = players.copy()

    availability = df["status"].map(STATUS_AVAILABILITY)
    df["availability"] = (availability
                          .fillna(df["chance_next"].fillna(50.0) / 100.0)
                          .clip(0.0, 1.0))
    df["is_keeper"] = df["pos"] == "GKP"

    club_matches = (df["team"].map(basis.club_matches) if basis
                    else pd.Series(float(MATCHES_PER_SEASON), index=df.index))
    club_matches = club_matches.fillna(float(MATCHES_PER_SEASON)).clip(lower=1.0)

    carry = _previous_weight(df, basis, same_club=True)
    starts = df["starts"].astype(float) + carry * _prev(df, "starts")
    minutes = df["minutes"].astype(float) + carry * _prev(df, "minutes")
    matches = club_matches + carry * _prev(df, "matches")

    raw_start = (starts / matches).clip(0.0, 1.0)
    sub_minutes = (minutes - starts * ASSUMED_START_MINUTES).clip(lower=0.0)
    sub_appearances = sub_minutes / ASSUMED_SUB_MINUTES
    non_start_matches = (matches - starts).clip(lower=1.0)
    raw_sub = (sub_appearances / non_start_matches).clip(0.0, 1.0)

    weight = minutes / (minutes + START_PRIOR_MINUTES)
    df["evidence_minutes"] = minutes
    prior = _start_prior(df)
    prior_share = prior / prior.groupby([df["team"], df["is_keeper"]]).transform("sum")
    prior_start = (prior_share * np.where(df["is_keeper"], 1.0, XI_OUTFIELD)).clip(
        upper=MAX_P_START)

    long_run = weight * raw_start + (1 - weight) * prior_start

    if "recent_start_rate" not in df:
        tilted = long_run
        recent = long_run
    else:
        recent = pd.to_numeric(df["recent_start_rate"], errors="coerce")
        recent_matches = pd.to_numeric(
            df.get("recent_matches", pd.Series(0.0, index=df.index)), errors="coerce")
        recent = recent.fillna(long_run)
        recent_matches = recent_matches.fillna(0.0).clip(lower=0.0)
        believed = (recent_matches / START_FORM_PRIOR_MATCHES).clip(0.0, 1.0)
        recent = long_run + believed * (recent - long_run)
        tilted = long_run + start_form_weight(horizon) * (recent - long_run)

    tilted = tilted.clip(0.0, 1.0)

    blended = tilted * df["availability"]
    blended_sub = (weight * raw_sub + (1 - weight) * prior_share) * df["availability"]

    involvement = blended + (1 - blended) * blended_sub
    thin = (1 - weight).clip(0.0, 1.0)
    fringe_pull = thin * FRINGE_COLLAPSE_STRENGTH * (1 - involvement / FRINGE_SHARE).clip(0.0, 1.0)
    blended = blended * (1 - fringe_pull)
    blended_sub = blended_sub * (1 - fringe_pull)

    df["p_start"] = _normalise_to(blended, df, {True: 1.0, False: float(XI_OUTFIELD)})
    df["start_long_run"] = long_run.clip(0.0, 1.0)
    df["start_recent"] = recent.clip(0.0, 1.0)

    outfield_subs = ((1 - df["p_start"]) * blended_sub).where(~df["is_keeper"], 0.0)
    scale = _club_scale(outfield_subs, df["team"], SUBS_PER_MATCH)
    df["p_sub"] = np.where(df["is_keeper"], blended_sub,
                           (blended_sub * df["team"].map(scale)).clip(0.0, 1.0))

    if "recent_mins_if_start" not in df:
        df["mins_if_start"] = float(ASSUMED_START_MINUTES)
    else:
        recent_mins = pd.to_numeric(df["recent_mins_if_start"], errors="coerce")
        recent_starts = pd.to_numeric(
            df.get("recent_start_matches", pd.Series(0.0, index=df.index)),
            errors="coerce").fillna(0.0).clip(lower=0.0)
        believed = (recent_starts / MINUTES_FORM_PRIOR_MATCHES).clip(0.0, 1.0)
        df["mins_if_start"] = (float(ASSUMED_START_MINUTES)
                               + believed.fillna(0.0) * (recent_mins.fillna(ASSUMED_START_MINUTES)
                                                          - ASSUMED_START_MINUTES)
                               ).clip(0.0, MAX_MINS_IF_START)

    df["mins_flags"] = _minutes_flags(df)
    _derive_minutes(df)
    return df


def _minutes_flags(df: pd.DataFrame) -> pd.Series:
    columns = {}

    if "recent_mins_std" in df and "recent_mins_if_start" in df:
        matches = pd.to_numeric(
            df.get("recent_start_matches", pd.Series(0.0, index=df.index)),
            errors="coerce").fillna(0.0)
        mean = pd.to_numeric(df["recent_mins_if_start"], errors="coerce")
        std = pd.to_numeric(df["recent_mins_std"], errors="coerce")
        cv = (std / mean.replace(0.0, pd.NA)).astype(float)
        volatile = ((matches >= MINS_FLAG_MIN_MATCHES) & (cv > MINS_VOLATILE_CV)).fillna(False)
        columns["volatile"] = np.where(volatile, "volatile minutes", "")

    if "moved_club" in df:
        columns["moved"] = np.where(df["moved_club"].fillna(False), "changed club", "")

    if "status" in df:
        unavailable = df["status"].fillna("a") != "a"
        columns["status"] = np.where(unavailable, "status: " + df["status"].astype(str), "")

    if not columns:
        return pd.Series("", index=df.index)
    reasons = pd.DataFrame(columns, index=df.index)
    return reasons.apply(lambda row: ", ".join(value for value in row if value), axis=1)


def _p60_given_start(mins_if_start):
    minutes = np.asarray(mins_if_start, dtype=float)
    return 1.0 / (1.0 + np.exp(-(minutes - P60_MIDPOINT_MINUTES) / P60_SLOPE_MINUTES))


def _mins_if_start(player) -> float:
    try:
        value = float(player.get("mins_if_start", ASSUMED_START_MINUTES))
    except (TypeError, ValueError):
        return float(ASSUMED_START_MINUTES)
    return float(ASSUMED_START_MINUTES) if np.isnan(value) else value


def _derive_minutes(df: pd.DataFrame) -> None:
    p_start, p_sub = df["p_start"], df["p_sub"]
    mins = df["mins_if_start"]
    df["p_play"] = p_start + (1 - p_start) * p_sub
    df["p60"] = p_start * _p60_given_start(mins)
    df["exp_minutes"] = (p_start * mins
                         + (1 - p_start) * p_sub * ASSUMED_SUB_MINUTES)


def conserve_team_output(players: pd.DataFrame,
                         strength: pd.DataFrame | None) -> pd.DataFrame:
    if strength is None or "exp_minutes" not in players:
        return players

    df = players.copy()
    share = df["exp_minutes"] / 90.0
    team_npxg = df["team"].map(strength.set_index("team")["npxg_per_match"])

    for column, target in (
        ("npxg_per90", team_npxg),
        ("xa_per90", team_npxg * ASSISTS_PER_OPEN_PLAY_GOAL),
        ("bonus_per90", pd.Series(BONUS_PER_TEAM_MATCH, index=df.index)),
    ):
        if column not in df:
            continue
        weight = df.get(EVIDENCE_WEIGHT.get(column, ""))
        if weight is None:
            weight = pd.Series(0.0, index=df.index)
        df[column] = _conserve_column(df[column], share, weight.fillna(0.0),
                                      target, df["team"])

    return df


EVIDENCE_WEIGHT = {
    "npxg_per90": "attack_evidence_weight",
    "xa_per90": "xa_evidence_weight",
    "bonus_per90": "fpl_evidence_weight",
}


def _conserve_column(rate: pd.Series, share: pd.Series, weight: pd.Series,
                     target: pd.Series, team: pd.Series) -> pd.Series:
    out = rate.copy()
    exponent = (1.0 - weight).clip(0.0, 1.0)

    for club, index in rate.groupby(team).groups.items():
        r = rate.loc[index].to_numpy(dtype=float)
        s = share.loc[index].to_numpy(dtype=float)
        a = exponent.loc[index].to_numpy(dtype=float)
        goal = float(pd.Series(target).loc[index].iloc[0])
        if not len(r) or goal <= 0:
            continue

        def produced(lam: float) -> float:
            return float((r * np.power(lam, a) * s).sum())

        if produced(1.0) <= 0:
            continue

        if produced(MAX_RATE_SCALE) <= goal:
            lam = MAX_RATE_SCALE
        else:
            lo, hi = 0.0, MAX_RATE_SCALE
            for _ in range(60):
                mid = (lo + hi) / 2
                if produced(mid) < goal:
                    lo = mid
                else:
                    hi = mid
            lam = (lo + hi) / 2

        scaled = r * np.power(lam, a)
        total = float((scaled * s).sum())
        if total > 0:
            scaled *= min(goal / total, MAX_RATE_SCALE)
        out.loc[index] = scaled

    return out


def _club_scale(value: pd.Series, team: pd.Series, target: float) -> pd.Series:
    total = value.groupby(team).sum()
    return (target / total.replace(0.0, np.nan)).clip(upper=MAX_MINUTES_SCALE).fillna(1.0)


def _normalise_to(value: pd.Series, df: pd.DataFrame,
                  targets: dict[bool, float]) -> pd.Series:
    out = value.clip(0.0, MAX_P_START)
    for is_keeper, target in targets.items():
        mask = df["is_keeper"] == is_keeper
        for team, group in value[mask].groupby(df.loc[mask, "team"]):
            if not len(group):
                continue
            v = group.to_numpy(dtype=float)

            def fielded(lam: float) -> float:
                return float(np.minimum(MAX_P_START,
                                        v * min(lam, MAX_MINUTES_SCALE)).sum())

            if fielded(MAX_MINUTES_SCALE) <= target:
                lam = MAX_MINUTES_SCALE
            else:
                lo, hi = 0.0, MAX_MINUTES_SCALE
                for _ in range(50):
                    mid = (lo + hi) / 2
                    if fielded(mid) < target:
                        lo = mid
                    else:
                        hi = mid
                lam = (lo + hi) / 2
            out.loc[group.index] = np.minimum(MAX_P_START, v * lam)
    return out


OVERRIDABLE = {
    "p_start": (0.0, 1.0),
    "mins_if_start": (0.0, MAX_MINS_IF_START),
    "p_sub": (0.0, 1.0),
    "exp_minutes": (0.0, 90.0),
    "npxg_per90": (0.0, 3.0),
    "xa_per90": (0.0, 3.0),
    "dc_per90": (0.0, 40.0),
    "bonus_per90": (0.0, 3.0),
    "saves_per90": (0.0, 12.0),
    "yellow_per90": (0.0, 1.0),
    "penalties_order": (0.0, 5.0),
    "price": (3.5, 20.0),
}


def _solve_exp_minutes(exp_minutes: float, p_start: float,
                       p_sub: float) -> tuple[float, float]:
    if p_start > 0.0:
        shift = (exp_minutes - (1.0 - p_start) * p_sub * ASSUMED_SUB_MINUTES) / p_start
        if shift <= MAX_MINS_IF_START:
            return p_start, float(np.clip(shift, 0.0, MAX_MINS_IF_START))

    denom = MAX_MINS_IF_START - p_sub * ASSUMED_SUB_MINUTES
    solved = (exp_minutes - p_sub * ASSUMED_SUB_MINUTES) / denom
    return float(np.clip(solved, 0.0, MAX_P_START)), MAX_MINS_IF_START


def _recompute_minutes(df: pd.DataFrame, mask) -> None:
    subset = df.loc[mask, ["p_start", "p_sub", "mins_if_start"]].copy()
    _derive_minutes(subset)
    for column in ("p_play", "p60", "exp_minutes"):
        df.loc[mask, column] = subset[column]


def apply_fields(player: Mapping[str, Any], fields: Mapping[str, Any]) -> dict:
    out = dict(player)
    if not fields:
        return out

    touched, minutes_touched, explicit_minutes = [], False, False
    for field, (low, high) in OVERRIDABLE.items():
        if field not in out:
            continue
        value = fields.get(field)
        multiplier = fields.get(f"{field}_mult")

        if value is not None and not pd.isna(value):
            out[field] = float(np.clip(float(value), low, high))
            touched.append(field)
        elif multiplier is not None and not pd.isna(multiplier):
            out[field] = float(np.clip(float(out[field]) * float(multiplier), low, high))
            touched.append(f"{field}×{float(multiplier):g}")
        else:
            continue

        if field in ("p_start", "mins_if_start", "p_sub"):
            minutes_touched = True
        elif field == "exp_minutes":
            out["p_start"], out["mins_if_start"] = _solve_exp_minutes(
                float(out["exp_minutes"]), float(out["p_start"]), float(out["p_sub"]))
            minutes_touched = explicit_minutes = True

    if minutes_touched:
        pinned = out["exp_minutes"] if explicit_minutes else None
        p_start, p_sub = float(out["p_start"]), float(out["p_sub"])
        mins = _mins_if_start(out)
        out["p_play"] = p_start + (1 - p_start) * p_sub
        out["p60"] = p_start * float(_p60_given_start(mins))
        out["exp_minutes"] = (p_start * mins
                              + (1 - p_start) * p_sub * ASSUMED_SUB_MINUTES)
        if pinned is not None:
            out["exp_minutes"] = pinned
    if touched:
        existing = str(out.get("overridden") or "")
        out["overridden"] = ", ".join(filter(None, [existing, ", ".join(touched)]))
    return out


def gameweek_overrides(overrides: pd.DataFrame | None,
                       players: pd.DataFrame | None = None
                       ) -> dict[tuple[int, int], dict]:
    if overrides is None or not len(overrides) or "gw" not in overrides.columns:
        return {}

    frame = overrides.copy()
    frame.columns = [c.strip().lower() for c in frame.columns]
    known = set(OVERRIDABLE) | {f"{f}_mult" for f in OVERRIDABLE}

    by_name = None
    if players is not None and "web_name" in frame.columns:
        by_name = {str(n).strip().lower(): int(i) for n, i
                   in zip(players["web_name"], players["fpl_id"])}

    out: dict[tuple[int, int], dict] = {}
    for _, row in frame.iterrows():
        if pd.isna(row.get("gw")):
            continue
        fpl_id = None
        if "fpl_id" in frame.columns and not pd.isna(row.get("fpl_id")):
            fpl_id = int(row["fpl_id"])
        elif by_name is not None and not pd.isna(row.get("web_name")):
            fpl_id = by_name.get(str(row["web_name"]).strip().lower())
        if fpl_id is None:
            continue

        fields = {c: row[c] for c in frame.columns
                  if c in known and not pd.isna(row.get(c))}
        if fields:
            out.setdefault((fpl_id, int(row["gw"])), {}).update(fields)
    return out


def apply_overrides(df: pd.DataFrame, overrides: pd.DataFrame) -> pd.DataFrame:
    if overrides is None or not len(overrides):
        return df

    overrides = overrides.copy()
    overrides.columns = [c.strip().lower() for c in overrides.columns]
    df = df.copy()
    if "overridden" not in df:
        df["overridden"] = ""
    if "minutes_pinned" not in df:
        df["minutes_pinned"] = False
    if "mins_if_start" not in df:
        df["mins_if_start"] = float(ASSUMED_START_MINUTES)

    for _, row in overrides.iterrows():
        if "gw" in overrides.columns and not pd.isna(row.get("gw")):
            continue
        if "fpl_id" in overrides.columns and not pd.isna(row.get("fpl_id")):
            mask = df["fpl_id"] == int(row["fpl_id"])
        elif "web_name" in overrides.columns and not pd.isna(row.get("web_name")):
            mask = df["web_name"].str.lower() == str(row["web_name"]).strip().lower()
        else:
            continue
        if not mask.any():
            continue

        touched, minutes_touched, explicit_minutes = [], False, False
        start_moved = False
        for field, (low, high) in OVERRIDABLE.items():
            if field not in df.columns:
                continue
            value = row.get(field)
            multiplier = row.get(f"{field}_mult")

            if value is not None and not pd.isna(value):
                df.loc[mask, field] = float(np.clip(float(value), low, high))
                touched.append(field)
            elif multiplier is not None and not pd.isna(multiplier):
                scaled = df.loc[mask, field].astype(float) * float(multiplier)
                df.loc[mask, field] = scaled.clip(low, high)
                touched.append(f"{field}×{float(multiplier):g}")
            else:
                continue

            if field == "p_start":
                minutes_touched = start_moved = True
            elif field in ("mins_if_start", "p_sub"):
                minutes_touched = True
            elif field == "exp_minutes":
                was = float(df.loc[mask, "p_start"].iloc[0])
                implied_start, implied_mins = _solve_exp_minutes(
                    float(df.loc[mask, "exp_minutes"].iloc[0]), was,
                    float(df.loc[mask, "p_sub"].iloc[0]))
                df.loc[mask, "p_start"] = implied_start
                df.loc[mask, "mins_if_start"] = implied_mins
                minutes_touched = explicit_minutes = True
                start_moved = start_moved or abs(implied_start - was) > 1e-12

        if minutes_touched:
            pinned = df.loc[mask, "exp_minutes"].copy() if explicit_minutes else None
            _recompute_minutes(df, mask)
            if pinned is not None:
                df.loc[mask, "exp_minutes"] = pinned
            if start_moved:
                df.loc[mask, "minutes_pinned"] = True
        for field in touched:
            weight_column = EVIDENCE_WEIGHT.get(field.split("×")[0])
            if weight_column and weight_column in df.columns:
                df.loc[mask, weight_column] = 1.0
        if touched:
            df.loc[mask, "overridden"] = ", ".join(touched)
    return df


def _calibrate_bonus_prior(df: pd.DataFrame, raw: pd.Series, prior: pd.Series,
                           evidence: pd.Series,
                           prior_minutes: pd.Series) -> pd.Series:
    share = pd.to_numeric(df["exp_minutes"], errors="coerce").fillna(0.0) / 90.0
    weight = (evidence / (evidence + prior_minutes)).fillna(0.0)

    target = float(df["team"].nunique()) * BONUS_PER_TEAM_MATCH
    evidenced = float((weight * raw * share).sum())
    assumed = float(((1.0 - weight) * prior * share).sum())
    if assumed <= 0:
        return prior

    multiplier = float(np.clip((target - evidenced) / assumed, 0.0, 3.0))
    return prior * multiplier


def _bisect_scale(values: np.ndarray, target: float) -> np.ndarray:
    if not len(values) or values.sum() <= 0:
        return values.copy()

    def fielded(lam: float) -> float:
        return float(np.minimum(MAX_P_START, values * lam).sum())

    cap = fielded(MAX_MINUTES_SCALE)
    if cap <= target:
        lam = MAX_MINUTES_SCALE
    else:
        lo, hi = 0.0, MAX_MINUTES_SCALE
        for _ in range(60):
            mid = (lo + hi) / 2
            if fielded(mid) < target:
                lo = mid
            else:
                hi = mid
        lam = (lo + hi) / 2
    return np.minimum(MAX_P_START, values * lam)


def renormalise_minutes(players: pd.DataFrame,
                        baseline_p_start: pd.Series | None = None) -> pd.DataFrame:
    if "minutes_pinned" not in players or not players["minutes_pinned"].any():
        return players

    df = players.copy()
    pinned = df["minutes_pinned"].fillna(False).astype(bool)
    baseline = baseline_p_start if baseline_p_start is not None else df["p_start"]

    for team in df.loc[df["is_keeper"], "team"].dropna().unique():
        club = (df["is_keeper"]) & (df["team"] == team)
        free = club & ~pinned
        if not free.any():
            continue
        spoken_for = float(df.loc[club & pinned, "p_start"].sum())
        remaining = max(1.0 - spoken_for, 0.0)
        values = df.loc[free, "p_start"].to_numpy(dtype=float)
        df.loc[free, "p_start"] = _bisect_scale(values, remaining)

    for team in df.loc[~df["is_keeper"], "team"].dropna().unique():
        club = (~df["is_keeper"]) & (df["team"] == team)
        free = club & ~pinned
        if not free.any():
            continue

        spoken_for = float(df.loc[club & pinned, "p_start"].sum())
        remaining = max(float(XI_OUTFIELD) - spoken_for, 0.0)

        claimed = 0.0
        settled = pd.Series(False, index=df.index)
        for pos in df.loc[club & pinned, "pos"].unique():
            pos_free = free & (df["pos"] == pos)
            if not pos_free.any():
                continue
            pinned_pos = club & pinned & (df["pos"] == pos)
            delta = (float(df.loc[pinned_pos, "p_start"].sum())
                     - float(baseline.loc[pinned_pos].sum()))
            values = df.loc[pos_free, "p_start"].to_numpy(dtype=float)
            cap = float(pos_free.sum()) * MAX_P_START
            want = float(np.clip(values.sum() - delta, 0.0, cap))
            df.loc[pos_free, "p_start"] = _bisect_scale(values, want)
            claimed += want
            settled |= pos_free

        other_free = free & ~settled
        if other_free.any():
            remaining_other = max(remaining - claimed, 0.0)
            values = df.loc[other_free, "p_start"].to_numpy(dtype=float)
            df.loc[other_free, "p_start"] = _bisect_scale(values, remaining_other)

    _recompute_minutes(df, ~pinned)
    return df


def _prev(df: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(df.get(f"prev_{column}"), errors="coerce").reindex(df.index).fillna(0.0) \
        if f"prev_{column}" in df else pd.Series(0.0, index=df.index)


def _previous_weight(df: pd.DataFrame, basis: SeasonBasis | None,
                     same_club: bool = False) -> pd.Series:
    weight = basis.previous_weight if basis else 0.0
    if weight <= 0 or "prev_team" not in df:
        return pd.Series(0.0, index=df.index)
    known = df["prev_team"].notna()
    if same_club:
        known &= df["prev_team"] == df["team"]
    return pd.Series(np.where(known, weight, 0.0), index=df.index)


def _pooled_rate(df: pd.DataFrame, column: str, carry: pd.Series,
                 minutes: pd.Series) -> pd.Series:
    total = pd.to_numeric(df[column], errors="coerce").fillna(0.0) + carry * _prev(df, column)
    return (total / minutes.replace(0.0, np.nan) * 90.0).fillna(0.0)


def _shrink(rate: pd.Series, minutes: pd.Series, prior: pd.Series,
            prior_minutes: pd.Series | float = PLAYER_PRIOR_MINUTES) -> pd.Series:
    rate = pd.to_numeric(rate, errors="coerce").fillna(0.0)
    weight = minutes / (minutes + prior_minutes)
    return weight * rate + (1 - weight) * prior


def detect_movers(players: pd.DataFrame, team_map: dict[str, str]) -> pd.DataFrame:
    df = players.copy()

    def clubs_of(row) -> list[str]:
        value = row.get("us_team_list")
        return value if isinstance(value, list) else []

    def moved(row) -> bool:
        clubs = clubs_of(row)
        return bool(clubs) and not any(team_map.get(c) == row["team"] for c in clubs)

    df["moved_club"] = df.apply(moved, axis=1)
    df["previous_club"] = df.apply(
        lambda r: clubs_of(r)[-1] if clubs_of(r) and r["moved_club"] else "", axis=1)
    return df


def attach_rates(players: pd.DataFrame, strength: pd.DataFrame | None = None,
                 us_attack_rating: dict[str, float] | None = None,
                 basis: SeasonBasis | None = None) -> pd.DataFrame:
    df = players.copy()
    carry = _previous_weight(df, basis)
    minutes = df["minutes"].astype(float) + carry * _prev(df, "minutes")
    full_fpl = basis.pooled_fpl_minutes if basis else MATCHES_PER_SEASON * 90.0

    us_weight = basis.understat_weight if basis else 1.0
    us_last = pd.to_numeric(df.get("us_minutes"), errors="coerce").fillna(0.0) * us_weight
    us_now = pd.to_numeric(df.get("cur_us_minutes"), errors="coerce").reindex(df.index).fillna(0.0) \
        if "cur_us_minutes" in df else pd.Series(0.0, index=df.index)
    us_minutes = us_last + us_now
    has_understat = us_minutes > 0

    df["team_context"] = 1.0
    if strength is not None and us_attack_rating and "moved_club" in df:
        new_rating = df["team"].map(strength.set_index("team")["attack_rating"])
        old_rating = df["previous_club"].map(us_attack_rating)
        ratio = (new_rating / old_rating).replace([np.inf, -np.inf], np.nan)
        context = ratio.clip(0.4, 2.5) ** TRANSFER_CONTEXT_ALPHA
        df["team_context"] = np.where(df["moved_club"] & context.notna(),
                                      context.fillna(1.0), 1.0)

    def understat_rate(last: str, now: str) -> pd.Series:
        total = (pd.to_numeric(df.get(last), errors="coerce").reindex(df.index).fillna(0.0)
                 * us_weight * df["team_context"])
        if now in df:
            total = total + pd.to_numeric(df[now], errors="coerce").fillna(0.0)
        return (total / us_minutes.replace(0.0, np.nan) * 90.0).fillna(0.0)

    fpl_xg90 = _pooled_rate(df, "expected_goals", carry, minutes) * (1 - PENALTY_GOAL_SHARE)
    fpl_xa90 = _pooled_rate(df, "expected_assists", carry, minutes)
    raw_xg90 = understat_rate("npxG", "cur_npxG").where(has_understat, fpl_xg90)
    raw_xa90 = understat_rate("xA", "cur_xA").where(has_understat, fpl_xa90)

    attack_minutes = us_minutes.where(has_understat, minutes)
    full_us = basis.pooled_understat_minutes if basis else MATCHES_PER_SEASON * 90.0
    attack_full = pd.Series(np.where(has_understat, full_us, full_fpl), index=df.index)

    df["rate_source"] = np.where(has_understat, "understat", "fpl")
    df.loc[attack_minutes < THIN_SHARE * attack_full, "rate_source"] = "thin"
    df.loc[attack_minutes <= 0, "rate_source"] = "none"

    df["raw_npxg_per90"] = raw_xg90
    df["raw_xa_per90"] = raw_xa90
    df["raw_bonus_per90"] = _pooled_rate(df, "bonus", carry, minutes).clip(0, 3)
    df["raw_yellow_per90"] = _pooled_rate(df, "yellow_cards", carry, minutes).clip(0, 1)
    df["raw_dc_per90"] = _pooled_rate(df, "defensive_contribution", carry, minutes)
    df["raw_saves_per90"] = _pooled_rate(df, "saves", carry, minutes)

    df["recency"] = 1.0
    for column, target in (("expected_goals_mult", "raw_npxg_per90"),
                           ("expected_assists_mult", "raw_xa_per90"),
                           ("defensive_contribution_mult", "raw_dc_per90"),
                           ("saves_mult", "raw_saves_per90"),
                           ("bonus_mult", "raw_bonus_per90")):
        if column in df.columns:
            multiplier = pd.to_numeric(df[column], errors="coerce").fillna(1.0)
            df[target] = df[target] * multiplier
            if column == "expected_goals_mult":
                df["recency"] = multiplier


    def prior_for(base: float) -> pd.Series:
        series = pd.Series(base, index=df.index, dtype=float)
        if "moved_club" in df:
            series = series.where(~df["moved_club"], base * MOVER_PRIOR_MULTIPLIER)
        return series

    rate_prior = {
        "raw_npxg_per90": prior_for(NPXG_PRIOR_MINUTES),
        "raw_xa_per90": prior_for(XA_PRIOR_MINUTES),
        "raw_dc_per90": prior_for(DC_PRIOR_MINUTES),
    }
    default_prior = prior_for(PLAYER_PRIOR_MINUTES)

    default_evidence = (minutes, pd.Series(full_fpl, index=df.index))
    rate_evidence = {
        "raw_npxg_per90": (attack_minutes, attack_full),
        "raw_xa_per90": (attack_minutes, attack_full),
    }

    shrunk = {}
    for raw, target in [("raw_npxg_per90", "npxg_per90"), ("raw_xa_per90", "xa_per90"),
                        ("raw_bonus_per90", "bonus_per90"), ("raw_dc_per90", "dc_per90"),
                        ("raw_saves_per90", "saves_per90"),
                        ("raw_yellow_per90", "yellow_per90")]:
        evidence, full_workload = rate_evidence.get(raw, default_evidence)
        established = evidence >= ESTABLISHED_SHARE * full_workload
        priors = {}
        for position in df["pos"].unique():
            group = established & (df["pos"] == position)
            weights = evidence[group]
            priors[position] = (
                float(np.average(df.loc[group, raw], weights=weights))
                if group.any() and weights.sum() > 0 else 0.0
            )
        prior = df["pos"].map(priors).fillna(0.0)

        prior_minutes = rate_prior.get(raw, default_prior)
        if target == "bonus_per90" and "exp_minutes" in df:
            prior = _calibrate_bonus_prior(df, df[raw], prior, evidence, prior_minutes)

        shrunk[target] = _shrink(df[raw], evidence, prior, prior_minutes)
    for target, series in shrunk.items():
        df[target] = series

    df["attack_evidence_weight"] = (
        attack_minutes / (attack_minutes + rate_prior["raw_npxg_per90"])).fillna(0.0)
    df["xa_evidence_weight"] = (
        attack_minutes / (attack_minutes + rate_prior["raw_xa_per90"])).fillna(0.0)
    df["fpl_evidence_weight"] = (minutes / (minutes + default_prior)).fillna(0.0)

    thin = attack_minutes < ESTABLISHED_SHARE * attack_full
    moved = df.get("moved_club", pd.Series(False, index=df.index))
    df["confidence"] = np.select(
        [attack_minutes <= 0, moved & thin, thin, moved],
        ["none", "very low", "low", "moderate"], default="ok")
    return df


def _minutes_scenarios(player: pd.Series) -> list[tuple[float, float, float]]:
    p_start = float(player["p_start"])
    p_sub_given_no_start = float(player["p_sub"])
    mins_if_start = _mins_if_start(player)
    return [
        (p_start, mins_if_start, float(_p60_given_start(mins_if_start))),
        ((1 - p_start) * p_sub_given_no_start, ASSUMED_SUB_MINUTES, 0.0),
    ]


def _player_fixture_points(player: pd.Series, lam_for: float, lam_against: float,
                           team_npxg: float, team_xgc: float) -> dict[str, float]:
    pos = player["pos"]
    minutes_share = float(player["exp_minutes"]) / 90.0

    lam_openplay = lam_for * (1 - PENALTY_GOAL_SHARE)
    attack_scale = lam_openplay / team_npxg if team_npxg > 0 else 1.0
    defence_scale = lam_against / team_xgc if team_xgc > 0 else 1.0

    exp_goals = float(player["npxg_per90"]) * minutes_share * attack_scale
    exp_assists = float(player["xa_per90"]) * minutes_share * attack_scale

    pen_goals = pen_miss = 0.0
    if player.get("penalties_order") == 1:
        awarded = lam_for * PENALTY_GOAL_SHARE / PENALTY_CONVERSION
        pen_goals = awarded * PENALTY_CONVERSION * minutes_share
        pen_miss = awarded * (1 - PENALTY_CONVERSION) * minutes_share

    appearance = (APPEARANCE_POINTS * float(player["p_play"])
                  + APPEARANCE_60_POINTS * float(player["p60"]))
    goals_pts = GOAL_POINTS[pos] * (exp_goals + pen_goals)
    assists_pts = ASSIST_POINTS * exp_assists
    bonus_pts = float(player["bonus_per90"]) * minutes_share
    cards_pts = YELLOW_CARD_POINTS * float(player["yellow_per90"]) * minutes_share
    pen_miss_pts = PENALTY_MISS_POINTS * pen_miss

    clean_sheet_pts = concede_pts = saves_pts = dc_pts = 0.0
    exp_clean_sheets = 0.0
    threshold = DEF_CONTRIB_THRESHOLD[pos]
    team_cs_prob = ps.clean_sheet_prob(lam_against)

    for probability, minutes, reaches_60 in _minutes_scenarios(player):
        if probability <= 0:
            continue
        share = minutes / 90.0
        lam_on_pitch = lam_against * share


        cs_prob = ps.clean_sheet_prob(lam_on_pitch)

        exp_clean_sheets += probability * reaches_60 * cs_prob

        if CLEAN_SHEET_POINTS[pos]:
            clean_sheet_pts += (probability * reaches_60
                                * CLEAN_SHEET_POINTS[pos] * cs_prob)
        if pos in ("GKP", "DEF"):
            concede_pts -= probability * ps.expected_concession_penalty(lam_on_pitch)
        if pos == "GKP":
            exp_saves = float(player["saves_per90"]) * share * defence_scale
            saves_pts += probability * SAVE_POINTS * ps.expected_save_points(exp_saves)
        if threshold:
            exp_dc = float(player["dc_per90"]) * share
            dc_pts += (probability * DEF_CONTRIB_POINTS
                       * ps.prob_at_least(threshold, exp_dc, DC_DISPERSION))

    total = (appearance + goals_pts + assists_pts + clean_sheet_pts + concede_pts
             + saves_pts + dc_pts + bonus_pts + cards_pts + pen_miss_pts)

    return {
        "xpts": total,
        "xpts_appearance": appearance,
        "xpts_goals": goals_pts,
        "xpts_assists": assists_pts,
        "xpts_clean_sheet": clean_sheet_pts,
        "xpts_conceded": concede_pts,
        "xpts_saves": saves_pts,
        "xpts_defcon": dc_pts,
        "xpts_bonus": bonus_pts,
        "xpts_cards": cards_pts + pen_miss_pts,
        "exp_goals": exp_goals + pen_goals,
        "exp_assists": exp_assists,
        "exp_clean_sheets": exp_clean_sheets,
        "team_cs_prob": team_cs_prob,
    }


def project(horizon: int = 5, start_gw: int | None = None,
            overrides: pd.DataFrame | None = None,
            recency_half_life: float | None = None,
            force_refresh: bool = False,
            calibrate_to_odds: bool = True,
            previous_weight: float = 1.0) -> Projection:
    fpl_players = fpl_api.players(force_refresh)
    fpl_teams = fpl_api.teams(force_refresh)
    all_fixtures = fpl_api.fixtures(force_refresh)
    notes: list[str] = []

    year = fpl_api.season_start_year(force_refresh)
    us_stats = understat.player_stats(UNDERSTAT_SEASON or str(year - 1),
                                      force_refresh=force_refresh)
    us_current = _understat_current(str(year), force_refresh, notes)

    team_names = fpl_teams["name"].tolist()
    id_to_name = dict(zip(fpl_teams["team_id"], fpl_teams["name"]))
    basis = season_basis(all_fixtures, id_to_name, us_stats, us_current, previous_weight)
    start_gw = start_gw or fpl_api.next_gameweek(force_refresh)

    us_clubs = sorted({club for clubs in us_stats["us_team_list"] for club in clubs})
    team_map = {club: match_team(club, team_names) for club in us_clubs}

    players = match_players(fpl_players, us_stats, team_map)
    players = detect_movers(players, team_map)

    team_map_current: dict[str, str] = {}
    if len(us_current):
        now_clubs = sorted({club for clubs in us_current["us_team_list"] for club in clubs})
        team_map_current = {club: match_team(club, team_names) for club in now_clubs}
        matched = match_players(fpl_players, us_current, team_map_current)
        current = matched[["fpl_id", "us_minutes", "npxG", "xA"]].rename(
            columns={"us_minutes": "cur_us_minutes", "npxG": "cur_npxG", "xA": "cur_xA"})
        players = players.merge(current, on="fpl_id", how="left")

    if not basis.preseason:
        previous = history.season_totals(history.season_folder(year - 1), force_refresh)
        if len(previous):
            players = players.merge(previous, on="code", how="left")
        else:
            notes.append("last season's archived totals were unavailable; rates "
                         "rest on this season's alone")

    latest = None if basis.preseason else start_gw - 1
    if basis.preseason:
        gw_history = history.gameweek_history(force_refresh=force_refresh)
    else:
        try:
            gw_history = fpl_api.gameweek_history(latest, force_refresh)
        except Exception as error:
            gw_history = pd.DataFrame(columns=history.KEEP)
            notes.append(f"per-gameweek stats unavailable ({error}): recent-form tilt off")
    if not len(gw_history) and not notes:
        notes.append("per-gameweek history unavailable: recent-form tilt off")

    if recency_half_life:
        multipliers = history.recency_multipliers(gw_history, recency_half_life,
                                                  latest=latest)
        if len(multipliers):
            players = players.merge(multipliers, on="code", how="left")

    form = history.start_form(gw_history, START_FORM_HALF_LIFE, latest=latest)
    if len(form):
        players = players.merge(form, on="code", how="left")

    mins_form = history.minutes_form(gw_history, START_FORM_HALF_LIFE, latest=latest)
    if len(mins_form):
        players = players.merge(mins_form, on="code", how="left")

    strength = team_strength(players, us_stats, team_map, basis,
                             us_current, team_map_current)
    us_teams = understat.team_rates(us_stats)
    league_mean = us_teams["team_npxg_per_match"].mean()
    us_attack_rating = (us_teams.set_index("us_team")["team_npxg_per_match"]
                        / league_mean).to_dict() if league_mean else {}

    players = minutes_model(players, basis=basis, horizon=horizon)
    players = attach_rates(players, strength, us_attack_rating, basis)
    minutes_baseline = players["p_start"].copy()
    players = apply_overrides(players, overrides)
    players = renormalise_minutes(players, minutes_baseline)
    players = conserve_team_output(players, strength)
    per_gameweek = gameweek_overrides(overrides, players)

    gameweeks = list(range(start_gw, start_gw + horizon))

    fixtures = all_fixtures[all_fixtures["gw"].isin(gameweeks)].copy()
    fixtures["home_team"] = fixtures["team_h"].map(id_to_name)
    fixtures["away_team"] = fixtures["team_a"].map(id_to_name)
    fixtures = fixture_lambdas(fixtures, strength, team_names, calibrate_to_odds)

    odds_note = fixtures["odds_note"].dropna().unique()
    odds_note = next((n for n in odds_note if n), "")
    coverage = float(fixtures["has_odds"].mean()) if len(fixtures) else 0.0

    strength_by_team = strength.set_index("team")
    players_indexed = players.set_index("fpl_id", drop=False)

    rows = []
    for _, fixture in fixtures.iterrows():
        for team, opponent, lam_for, lam_against, at_home in (
            (fixture["home_team"], fixture["away_team"],
             fixture["lam_home"], fixture["lam_away"], True),
            (fixture["away_team"], fixture["home_team"],
             fixture["lam_away"], fixture["lam_home"], False),
        ):
            squad = players_indexed[players_indexed["team"] == team]
            team_npxg = float(strength_by_team.loc[team, "npxg_per_match"])
            team_xgc = float(strength_by_team.loc[team, "xgc_per_match"])

            for _, player in squad.iterrows():
                match_fields = per_gameweek.get((int(player["fpl_id"]), int(fixture["gw"])))
                scored = apply_fields(player, match_fields) if match_fields else player
                if scored["p_play"] <= 0:
                    continue
                points = _player_fixture_points(scored, lam_for, lam_against,
                                                team_npxg, team_xgc)
                rows.append({
                    "fpl_id": player["fpl_id"],
                    "gw": int(fixture["gw"]),
                    "fixture_id": int(fixture["fixture_id"]),
                    "opponent": opponent,
                    "was_home": at_home,
                    "lam_for": lam_for,
                    "lam_against": lam_against,
                    "lam_source": fixture["lam_source"],
                    **points,
                })

    per_fixture = pd.DataFrame(rows)
    if per_fixture.empty:
        raise RuntimeError(f"No fixtures found for gameweeks {gameweeks}")

    breakdown_cols = [c for c in per_fixture.columns if c.startswith("xpts")] + \
                     ["exp_goals", "exp_assists", "exp_clean_sheets"]
    totals = per_fixture.groupby("fpl_id", as_index=False)[breakdown_cols].sum()
    totals["n_fixtures"] = per_fixture.groupby("fpl_id").size().values

    summary = players.merge(totals, on="fpl_id", how="left")
    for column in breakdown_cols:
        summary[column] = summary[column].fillna(0.0)

    fixtures_per_team = pd.concat([
        fixtures.groupby("home_team").size(), fixtures.groupby("away_team").size()
    ]).groupby(level=0).sum()
    summary["n_fixtures"] = summary["n_fixtures"].fillna(
        summary["team"].map(fixtures_per_team)).fillna(0)

    summary["xpts_per_game"] = summary["xpts"] / summary["n_fixtures"].clip(lower=1)
    summary["xpts_per_m"] = summary["xpts"] / summary["price"]
    summary["value_rank"] = summary["xpts_per_m"].rank(ascending=False)

    evidence = summary.get("evidence_minutes", summary["minutes"])
    summary["needs_override"] = ((evidence < BLINDSPOT_SHARE * basis.pooled_fpl_minutes)
                                 & (summary["price"] >= 5.0))

    scored = per_fixture.merge(summary[["fpl_id", "team"]], on="fpl_id")
    team_goals = scored.groupby(["team", "fixture_id"])["exp_goals"].sum()
    expected = []
    for _, fixture in fixtures.iterrows():
        for team, lam in ((fixture["home_team"], fixture["lam_home"]),
                          (fixture["away_team"], fixture["lam_away"])):
            expected.append({
                "team": team,
                "attributed": float(team_goals.get((team, fixture["fixture_id"]), 0.0)),
                "expected": float(lam),
            })
    goals_covered = pd.DataFrame(expected).groupby("team")[["attributed", "expected"]].sum()
    goals_covered["ratio"] = (goals_covered["attributed"]
                              / goals_covered["expected"].replace(0, np.nan))
    summary["goal_coverage"] = summary["team"].map(goals_covered["ratio"])

    return Projection(
        players=summary.sort_values("xpts", ascending=False).reset_index(drop=True),
        per_fixture=per_fixture,
        fixtures=fixtures,
        horizon=gameweeks,
        odds_coverage=coverage,
        odds_note=odds_note,
        strength=strength,
        basis=basis,
        notes=notes,
    )


def _understat_current(season: str, force_refresh: bool,
                       notes: list[str]) -> pd.DataFrame:
    try:
        stats = understat.player_stats(season, force_refresh=force_refresh)
    except Exception as error:
        notes.append(f"Understat {season} unavailable ({error}); attacking rates "
                     "rest on last season alone")
        return pd.DataFrame(columns=["us_team_list", "us_minutes", "npxG", "xA"])
    return stats if len(stats) else pd.DataFrame(columns=["us_team_list", "us_minutes",
                                                          "npxG", "xA"])


def reproject_player(projection: Projection, fpl_id: int,
                     overrides: dict[str, Any]) -> dict:
    players = projection.players
    row = players[players["fpl_id"] == fpl_id]
    if row.empty:
        raise KeyError(f"no player with id {fpl_id}")

    per_gameweek = {int(gw): fields
                    for gw, fields in (overrides.get("gw") or {}).items()}
    season = {k: v for k, v in overrides.items() if k != "gw"}
    edited = apply_overrides(row.copy(), pd.DataFrame([{"fpl_id": fpl_id, **season}]))
    player = edited.iloc[0]

    strength = projection.strength.set_index("team")
    team_npxg = float(strength.loc[player["team"], "npxg_per_match"])
    team_xgc = float(strength.loc[player["team"], "xgc_per_match"])

    fixtures = projection.fixtures
    mine = fixtures[(fixtures["home_team"] == player["team"])
                    | (fixtures["away_team"] == player["team"])]

    per_gw: dict[int, float] = {gw: 0.0 for gw in projection.horizon}
    breakdown: dict[str, float] = {}
    for _, fixture in mine.iterrows():
        at_home = fixture["home_team"] == player["team"]
        lam_for = fixture["lam_home"] if at_home else fixture["lam_away"]
        lam_against = fixture["lam_away"] if at_home else fixture["lam_home"]
        gameweek = int(fixture["gw"])
        match_fields = per_gameweek.get(gameweek)
        scored = apply_fields(player, match_fields) if match_fields else player
        points = _player_fixture_points(scored, lam_for, lam_against, team_npxg, team_xgc)
        per_gw[gameweek] = per_gw.get(gameweek, 0.0) + points["xpts"]
        for key, value in points.items():
            breakdown[key] = breakdown.get(key, 0.0) + value

    return {
        "fpl_id": int(fpl_id),
        "gw": [round(per_gw.get(gw, 0.0), 4) for gw in projection.horizon],
        "xpts": round(sum(per_gw.values()), 4),
        "breakdown": {k: round(v, 4) for k, v in breakdown.items()},
        "inputs": {k: (None if pd.isna(player.get(k)) else float(player[k]))
                   for k in OVERRIDABLE if k in player.index},
        "derived": {k: (None if pd.isna(player.get(k)) else float(player[k]))
                    for k in ("p_sub", "p_play", "p60", "exp_minutes")
                    if k in player.index},
        "overridden": str(player.get("overridden", "")),
    }

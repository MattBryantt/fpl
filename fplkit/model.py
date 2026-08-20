"""The expected-points model.

Three layers, each answering one question:

  1. How many goals will each team score in each fixture?
     Bookmaker 1X2 + over/under prices, inverted through a Poisson model.
     Where the books have not priced a fixture yet, fall back to xG-derived
     attack/defence ratings.

  2. How much of that does this player take, and how long is he on the pitch?
     Understat non-penalty xG and xA per 90 give the share; the FPL API's
     starts/minutes give the minutes distribution.

  3. What is that worth in FPL points?
     Apply the scoring rules, computing the non-linear terms (clean sheets,
     goals conceded, saves, defensive contribution) over their full
     distributions rather than plugging in the mean.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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

# Newly promoted clubs have no Premier League xG history. Until they have played
# some games, assume a below-average attack and a leaky defence.
PROMOTED_ATTACK = 0.80
PROMOTED_DEFENCE = 1.25

# How hard a club's own priced fixtures pull its rating before an *unpriced*
# fixture is projected from it. See _odds_calibration(). Low, because the
# evidence is a single bookmaker consensus compared against the model's own
# guess for the same match, not an observed result -- there is no result-luck
# to average away, but nor is there more than one match's worth of "this club
# might be different from its Understat history" behind it. Two matches of
# that kind of evidence earns half weight.
ODDS_CALIBRATION_PRIOR_MATCHES = 2.0
# A rating correction wider than this is more likely a team-matching slip or a
# thin, one-sided market than real signal, so it is clipped rather than taken
# at face value. exp(0.4) =~ 1.5x, i.e. half a goal a match at typical rates.
ODDS_CALIBRATION_MAX_LOG = 0.4

# Shrinkage. A single season of xG is a noisy estimate of a true rate, and the
# noise is worst exactly where it does the most damage: a fringe player with 150
# minutes and one lucky big chance projects as a superstar unless his rate is
# pulled back toward what players like him normally do.
#
# The prior weight is deliberately heavier than pure small-sample correction
# would justify, because the sample is not just small, it is *last season*.
# Year-over-year correlation of npxG/90 runs around 0.6-0.7 even for players
# with a full season behind them, so a complete 3,000-minute campaign should
# carry roughly three-quarters weight, not effectively all of it. At 1,200 that
# is what a full season gets; a half season gets about 55%.
PLAYER_PRIOR_MINUTES = 1200

# The attacking rates get their own priors, because theirs could be measured and
# the rest could not. Three seasons of Understat are in the cache, and the
# question the prior answers -- how much of a rate carries into next season --
# is a regression of one season on the one before it. Fitting k directly, by
# minimising squared error of `w.own + (1-w).prior` with `w = M / (M + k)`:
#
#     npxG/90   k = 342   95% CI [186, 538]   (year-over-year slope 0.85)
#     xA/90     k = 891   95% CI [601, 1191]  (slope 0.68)
#
# over 468 player-seasons with 300+ minutes behind them and a real season to
# score against. Both intervals sit far below the 1200 that had been applied to
# everything: at 3,000 minutes that is a weight of 0.71 where npxG earns 0.90.
# The model was shrinking its best-evidenced attackers about three times harder
# than a season of evidence justifies.
#
# Set at the cautious end of each interval rather than at the point estimate.
# The players who can be measured this way are ones who were established in two
# consecutive seasons, and a rate fitted on those is being read back onto a
# population that includes players who lost their place -- so erring toward the
# prior is erring in the direction the sample is weakest.
NPXG_PRIOR_MINUTES = 550
XA_PRIOR_MINUTES = 1000.0
TEAM_PRIOR_MATCHES = 8.0

# A player who changed club is a worse bet than his minutes suggest: his rate
# was produced by different team-mates, a different system and a different role.
# He gets a heavier prior on top of the team-context adjustment below.
MOVER_PRIOR_MULTIPLIER = 1.8

# --- Minutes normalisation ----------------------------------------------------
# Every club starts eleven and plays 990 minutes, whatever last season's data
# happens to remember about its current squad. See minutes_model().
XI_OUTFIELD = 10  # the eleventh is the keeper, normalised separately

# Nobody is certain to start. Measured, not assumed: last season each club's
# single most-nailed player averaged a 0.966 start rate and first-choice
# keepers 0.921, and those are after-the-fact maxima. 0.95 is what "as nailed
# as it gets" is worth before the season happens.
MAX_P_START = 0.95
# 11 starters x 78 minutes leaves 132 of 990, and 132 / 22 is six appearances.
SUBS_PER_MATCH = 6.0

# A shift cannot be longer than the match. Unlike MAX_P_START there is nothing
# probabilistic to shade here: a player who starts and is never substituted
# plays ninety, and that is the whole of it.
MAX_MINS_IF_START = 90.0

# How much evidence it takes before last season's start rate outweighs the
# price-based prior. Lighter than the rate prior: minutes are a far more direct
# measurement of a role than xG is of finishing ability, so they earn their
# weight faster.
START_PRIOR_MINUTES = 700.0

# A blended figure that is already low is more likely to describe a player who
# simply does not feature for a run of matches than one who plays a token ten
# minutes in every game -- real fringe involvement is lumpy, not a smooth
# trickle. Below this share, and only while there is not yet enough of this
# player's own evidence to say otherwise (see `weight` in minutes_model), both
# his start and sub chances are pulled further toward nothing.
FRINGE_SHARE = 0.15
# How much of that pull is applied at the extreme -- no evidence at all
# (weight 0) and no involvement at all (blended 0). Established evidence turns
# this off entirely regardless of the constant, which is why it can afford to
# be this aggressive: a player who demonstrably does get used off the bench
# every week is not touched by it.
FRINGE_COLLAPSE_STRENGTH = 0.85

# --- Start form: why a season-long start rate is the wrong number -------------
# A start rate over a season answers "what share of matches did he start". The
# question actually being asked is "does he start the next one", and those come
# apart for anyone whose situation changed inside the season. The season rate
# reads a man who missed August to October injured and has started every match
# since as a 0.6 starter. He is not one. He is a 0.95 starter with a bad autumn
# behind him, and next Saturday is the only match the plan is about.
#
# So the model carries two numbers and blends them by *how far ahead it is
# looking*: the long-run rate, and a recency-weighted one from the per-gameweek
# archive. All three constants below were fitted out of sample on 2025-26 --
# every prediction for gameweek t built only from gameweeks before t -- by
# searching, at each lead k, for the blend weight w that minimised Brier score
# against what actually happened. See scripts/calibrate-start-form.py.
#
#     lead k    best w    Brier(blend)   Brier(flat)   improvement
#        1       1.09        0.10184        0.11622        12.4%
#        2       0.82        0.11452        0.12254         6.5%
#        3       0.63        0.12227        0.12704         3.8%
#        4       0.51        0.12796        0.13101         2.3%
#        5       0.42        0.13282        0.13481         1.5%
#        6       0.36        0.13666        0.13811         1.1%
#        7       0.32        0.14002        0.14111         0.8%
#        8       0.29        0.14309        0.14398         0.6%
#
# Two things in that table are the whole feature. The blend beats the flat rate
# at *every* lead, so this is not a trade of near accuracy for far accuracy. And
# w decays geometrically -- 1.09 down to 0.29, a ratio of 0.828 per gameweek --
# which is the measured version of the intuition that a nailed starter is more
# obviously nailed next week than he is in two months.
START_FORM_HALF_LIFE = 4.0
START_FORM_WEIGHT = 1.09    # weight on the recent rate one gameweek out
START_FORM_DECAY = 0.828    # ...falling by this much per gameweek of lead
# w above 1 is not a typo: the fit wants the recent rate *extrapolated past*,
# because a run of starts is a slightly under-confident signal of a settled
# place. Capped so an extrapolation cannot invert the two numbers it sits
# between, which is what an unbounded w would do to a player whose recent rate
# is far below his long-run one.
MAX_START_FORM_WEIGHT = 1.25
# How many weighted recent matches it takes before the recent rate is believed
# over the long-run one. Small, because the evidence is already recency-weighted
# and a player with three recent matches behind him has genuinely told you
# something -- but not zero, or one substitute appearance in a blank fortnight
# would rewrite a season.
START_FORM_PRIOR_MATCHES = 3.0

# Shift length is a steadier trait than start probability -- a manager's team
# selection swings week to week, but whether a player is the type hooked at the
# hour or the type who plays every minute does not -- so it earns belief faster
# than the start rate above, and (unlike start_form_weight) is not shrunk
# further as the horizon lengthens: how long he plays when he starts is not a
# question next month answers differently from next week.
MINUTES_FORM_PRIOR_MATCHES = 2.0

# Above this coefficient of variation, "his average shift is 70 minutes" is
# hiding two different players -- one hooked at the hour every week, one who
# plays 90 half the time and doesn't feature the other half -- and the mean
# alone is a worse answer than the same mean with a warning on it. Not fed back
# into mins_if_start itself: nothing downstream of it models a *distribution*,
# so widening the point estimate would just move the uncertainty somewhere it
# is not accounted for. Flagged instead -- see MINS_FLAG_MIN_MATCHES.
MINS_VOLATILE_CV = 0.35
# Below this much recency-weighted start evidence, a volatile-looking average is
# more likely to be two or three data points than a real pattern, so the flag
# stays off until there is enough behind it to trust.
MINS_FLAG_MIN_MATCHES = MINUTES_FORM_PRIOR_MATCHES

# Ceiling on the per-club correction. A squad the data barely knows would
# otherwise have its two familiar players multiplied into superstars -- a worse
# error than the under-fielding being corrected.
MAX_MINUTES_SCALE = 2.5
MAX_RATE_SCALE = 2.5

# --- Conservation of team output ----------------------------------------------
# Per-90 rates are shares of what a team produces, so they have to add up to it.
# See conserve_team_output(). Both measured from last season rather than assumed:
# 786 assists against 851 goals, and 2115 bonus points over 380 matches (below
# the nominal 6 because a match with fewer than three scorers pays out less).
ASSISTS_PER_GOAL = 0.924
BONUS_PER_TEAM_MATCH = 2.78

# The assist target is applied to a club's *open-play* xG, because that is the
# quantity conserve_team_output() has to hand -- so the ratio has to be per
# open-play goal, not per goal. A penalty carries no assist, so all 786 assists
# were produced by the 91% of goals that were not penalties, and dividing by
# that share is what puts numerator and denominator on the same basis. Left as
# the raw 0.924 against an open-play denominator, every assist in the model came
# out about 9% light.
ASSISTS_PER_OPEN_PLAY_GOAL = ASSISTS_PER_GOAL / (1 - PENALTY_GOAL_SHARE)

# How much of a moved player's output follows the team rather than the player.
# Zero would say a striker leaving a great side for a poor one keeps his rate
# untouched; one would say his output is entirely his team's doing. The truth is
# in between, and the exponent form keeps the adjustment mild for small moves.
TRANSFER_CONTEXT_ALPHA = 0.5


# Thresholds that used to be absolute minute counts, written instead as the
# share of a full workload they stand for: 900 minutes of a 3,420-minute season
# is 26%, 450 is 13%, 270 is 8%. They have to be shares, because "900 minutes"
# means *a regular* in May and *nobody in the league* in September, and a fixed
# number silently reclassifies every player in it the moment a season rolls over.
ESTABLISHED_SHARE = 0.26  # enough history to help set a positional prior
BLINDSPOT_SHARE = 0.13    # below this, a priced player is one the model cannot see
THIN_SHARE = 0.08         # below this, the rate is labelled as barely evidenced


@dataclass(frozen=True)
class SeasonBasis:
    """How many matches stand behind the totals each source is currently serving.

    Every rate in this model is a season total divided by something, and what
    that something is depends on where the calendar is -- not on a constant.
    The FPL API serves season-to-date totals, which means last season's complete
    38 matches right up until the new season's first whistle, and three matches
    a fortnight later. Dividing by a hard-coded 38 is correct for exactly one of
    those. A week into a season it reads a nailed starter's `starts / 38` as
    0.08 and concludes he is a fringe player; it reads every positional prior
    off an "established" group that has nobody in it.

    Understat is counted separately because it is a different season's data
    whenever `UNDERSTAT_SEASON` names a completed one, which is the normal case.
    Weighting last season's xG by this season's minutes is how a genuine 0.6
    npxG/90 striker gets shrunk to the positional average in September.
    """

    club_matches: pd.Series   # per club, matches behind the FPL totals in hand
    understat_matches: float  # matches behind the Understat totals
    preseason: bool           # True while the totals still describe last season

    @property
    def fpl_matches(self) -> float:
        """League-level matches behind the FPL totals."""
        if not len(self.club_matches):
            return float(MATCHES_PER_SEASON)
        return float(self.club_matches.max())

    @property
    def fpl_minutes(self) -> float:
        """What a regular's FPL minutes look like at this point in the season."""
        return self.fpl_matches * 90.0

    @property
    def understat_minutes(self) -> float:
        return self.understat_matches * 90.0


def season_basis(all_fixtures: pd.DataFrame, id_to_name: dict[int, str],
                 us_stats: pd.DataFrame) -> SeasonBasis:
    """Read the season's progress off the fixture list rather than assuming it.

    Before a ball is kicked the FPL totals in hand are last season's completed
    ones, so the basis is a full 38. After that it is however many matches each
    club has actually finished -- which differs between clubs whenever a game is
    postponed, so it is counted per club rather than league-wide.
    """
    finished = all_fixtures[all_fixtures["finished"].fillna(False).astype(bool)]
    played = (pd.concat([finished["team_h"], finished["team_a"]])
              .map(id_to_name).value_counts()
              .reindex(list(id_to_name.values())).fillna(0.0).astype(float))

    # Understat's own workload, measured off its data rather than assumed, so
    # that pointing UNDERSTAT_SEASON at a season in progress degrades honestly
    # instead of overstating how much evidence is behind every attacking rate.
    us_minutes = pd.to_numeric(us_stats.get("us_minutes"), errors="coerce")
    us_matches = (float(us_minutes.max()) / 90.0
                  if us_minutes is not None and len(us_minutes) and us_minutes.max() > 0
                  else float(MATCHES_PER_SEASON))

    if played.sum() == 0:
        return SeasonBasis(
            club_matches=pd.Series(float(MATCHES_PER_SEASON), index=played.index),
            understat_matches=us_matches, preseason=True)
    return SeasonBasis(club_matches=played.clip(lower=1.0),
                       understat_matches=us_matches, preseason=False)


@dataclass
class Projection:
    """Everything a projection run produced, ready for the CLI to render."""

    players: pd.DataFrame  # one row per player, xpts summed over the horizon
    per_fixture: pd.DataFrame  # one row per player-fixture
    fixtures: pd.DataFrame  # one row per fixture with lam_home / lam_away
    horizon: list[int]
    odds_coverage: float  # fraction of fixtures priced by bookmakers
    odds_note: str
    strength: pd.DataFrame | None = None  # per-club attack/defence, for reprojection
    basis: SeasonBasis | None = None  # what season the totals behind this describe


# --------------------------------------------------------------------------- #
# Layer 1: team strength and fixture lambdas
# --------------------------------------------------------------------------- #

def team_strength(players: pd.DataFrame, us_stats: pd.DataFrame,
                  team_map: dict[str, str],
                  basis: SeasonBasis | None = None) -> pd.DataFrame:
    """Per-match attacking and defensive rates for each FPL club.

    Attack comes from Understat (non-penalty xG summed over the squad, divided
    by matches). Defence comes from the FPL API's own expected_goals_conceded
    per 90, taken as a minutes-weighted mean across the squad -- every player on
    the pitch shares the same team concession rate, so weighting by minutes
    recovers it.
    """
    us_teams = understat.team_rates(us_stats)
    us_teams["fpl_team"] = us_teams["us_team"].map(team_map)
    attack = us_teams.set_index("fpl_team")["team_npxg_per_match"].to_dict()

    def played_here_last_season(row, team: str) -> bool:
        clubs = row.get("us_team_list")
        return isinstance(clubs, list) and any(team_map.get(c) == team for c in clubs)

    rows = []
    for team, group in players.groupby("team"):
        # A player's expected_goals_conceded_per_90 describes the defence he
        # played behind last season, which for a new signing is his old club's.
        # Only players who were already here tell us anything about this defence.
        continuing = group[group.apply(played_here_last_season, axis=1, team=team)]
        minutes = continuing["minutes"]
        xgc90 = continuing["expected_goals_conceded_per_90"]
        usable = minutes > 0
        if usable.any() and (minutes[usable] * xgc90[usable]).sum() > 0:
            conceded = float(np.average(xgc90[usable], weights=minutes[usable]))
        else:
            conceded = np.nan
        rows.append({
            "team": team,
            "npxg_per_match": attack.get(team, np.nan),
            "xgc_per_match": conceded,
        })

    df = pd.DataFrame(rows)
    league_attack = df["npxg_per_match"].mean(skipna=True) or LEAGUE_MEAN_GOALS
    league_defence = df["xgc_per_match"].mean(skipna=True) or LEAGUE_MEAN_GOALS

    # A club with no Premier League xG history last season was promoted. Both of
    # its ratings are assumptions, not measurements -- a handful of its players
    # may have FPL minutes, but those describe their previous clubs.
    df["is_promoted"] = df["npxg_per_match"].isna()
    df.loc[df["is_promoted"], "npxg_per_match"] = league_attack * PROMOTED_ATTACK
    df.loc[df["is_promoted"], "xgc_per_match"] = league_defence * PROMOTED_DEFENCE
    df["npxg_per_match"] = df["npxg_per_match"].fillna(league_attack)
    df["xgc_per_match"] = df["xgc_per_match"].fillna(league_defence)

    # Regress one season of team xG toward the league mean. Season-over-season
    # correlation of team xG rates is well short of 1, so taking last year's
    # number at face value overstates how far apart the clubs really are.
    #
    # Each side is regressed by the evidence behind *it*, not by a shared
    # constant: attack is Understat's and is a completed season, defence is the
    # FPL API's and is however far into this one we are. Six matches into a
    # season those are 38 and 6, and treating them alike would let a club's
    # early concession rate move its rating as if a full season stood behind it.
    us_matches = basis.understat_matches if basis else float(MATCHES_PER_SEASON)
    attack_weight = us_matches / (us_matches + TEAM_PRIOR_MATCHES)
    fpl_matches = (df["team"].map(basis.club_matches) if basis
                   else pd.Series(float(MATCHES_PER_SEASON), index=df.index))
    fpl_matches = fpl_matches.fillna(float(MATCHES_PER_SEASON))
    defence_weight = fpl_matches / (fpl_matches + TEAM_PRIOR_MATCHES)

    df["npxg_per_match"] = (attack_weight * df["npxg_per_match"]
                            + (1 - attack_weight) * league_attack)
    df["xgc_per_match"] = (defence_weight * df["xgc_per_match"]
                           + (1 - defence_weight) * league_defence)

    df["attack_rating"] = df["npxg_per_match"] / league_attack
    df["defence_rating"] = df["xgc_per_match"] / league_defence

    # Put the per-match rates on the same scale as the fixture lambdas before
    # they leave this function. Both are used as the denominator of a ratio
    # whose numerator is a lambda -- and lambdas are pinned to LEAGUE_MEAN_GOALS,
    # while these are measured on their own sources' scales. Understat's team
    # aggregate in particular sits ~13% high, because building it from
    # single-club players drops the transferred squad men who drag a team's rate
    # down. Left alone that mismatch silently deflated every player's open-play
    # goals by about the same 13%.
    #
    # Only the level moves; the ratings above are computed first and are
    # unitless, so relative team strength is untouched.
    open_play = LEAGUE_MEAN_GOALS * (1 - PENALTY_GOAL_SHARE)
    if df["npxg_per_match"].mean() > 0:
        df["npxg_per_match"] *= open_play / df["npxg_per_match"].mean()
    if df["xgc_per_match"].mean() > 0:
        df["xgc_per_match"] *= LEAGUE_MEAN_GOALS / df["xgc_per_match"].mean()

    df["league_npxg"] = league_attack
    df["league_xgc"] = league_defence
    return df


# How far a priced match's kickoff may sit from a fixture's before the two are
# taken to be different matches. Comfortably wider than the couple of days a
# fixture moves when television reschedules it, and far narrower than the months
# between a league meeting and its reverse leg.
ODDS_KICKOFF_TOLERANCE = pd.Timedelta(days=4)


def _nearest_priced_match(candidates: list, kickoff) -> Any | None:
    """Pick which of several priced matches is actually this fixture.

    A pair of clubs does not identify a match: they meet twice a season, and a
    postponement can drop the rearranged game into a window where the reverse
    leg is already on the board. Keyed on the pair alone, one match's prices get
    attached to the other's fixture -- silently, and carrying an entirely
    plausible number, which is the kind of error nobody goes looking for.

    When the fixture has no kickoff time yet, a single candidate is accepted and
    an ambiguous one is refused: the fallback is xG-derived ratings, which is a
    worse projection but an honest one.
    """
    if pd.isna(kickoff):
        return candidates[0] if len(candidates) == 1 else None
    best, best_gap = None, None
    for row in candidates:
        gap = abs(row["commence_time"] - kickoff)
        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    return best if best_gap is not None and best_gap <= ODDS_KICKOFF_TOLERANCE else None


def _attach_odds(fixtures: pd.DataFrame, teams: list[str]) -> pd.DataFrame:
    """Join bookmaker probabilities onto the fixture list where they exist."""
    fixtures = fixtures.copy()
    for column in ("p_home", "p_draw", "p_away", "p_over", "totals_line"):
        fixtures[column] = np.nan
    fixtures["has_odds"] = False

    try:
        market = odds_source.match_odds()
    except odds_source.OddsUnavailable as error:
        return fixtures.assign(odds_note=str(error))
    except Exception as error:  # network failure should not kill a run
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
    """The ratings-only projection for one fixture, with any odds calibration
    folded in. Calibration multipliers default to 1.0, so this is exactly the
    old xG-ratings formula when none is available or the toggle is off."""
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
    """Per-team attack/defence multipliers, learned from the gap between this
    fixture list's *priced* matches and what the xG ratings alone would have
    said about them.

    A book prices a match on everything it knows right now -- a summer signing,
    a manager sacked in September, a keeper who will not recover in time for
    the opener -- none of which last season's Understat numbers can see. Where
    a club's priced fixtures consistently disagree with its rating, that
    disagreement is evidence about the club, and it is evidence an *unpriced*
    fixture for the same club currently has no other way to use.

    A single match cannot separate a team's attack from its opponent's
    defence -- the scoreline only reveals their product -- so a fixture's
    whole log-error is split evenly between the two ratings it touches, the
    way one step of iterative proportional fitting would. A club with more
    than one priced match averages its evidence, shrunk toward "no
    correction" by ODDS_CALIBRATION_PRIOR_MATCHES.
    """
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
        # err_home is home-attack * away-defence; err_away is away-attack *
        # home-defence. Neither side of either product is separately known,
        # so each gets half the blame (or credit) in log space.
        add(home, attack_errors, err_home / 2)
        add(away, defence_errors, err_home / 2)
        add(away, attack_errors, err_away / 2)
        add(home, defence_errors, err_away / 2)

    def shrunk_multipliers(errors: dict[str, list[float]]) -> dict[str, float]:
        out = {}
        for team, values in errors.items():
            n = len(values)
            weight = n / (n + ODDS_CALIBRATION_PRIOR_MATCHES)
            out[team] = float(np.exp(weight * float(np.mean(values))))
        return out

    return shrunk_multipliers(attack_errors), shrunk_multipliers(defence_errors)


def fixture_lambdas(fixtures: pd.DataFrame, strength: pd.DataFrame,
                    teams: list[str], calibrate: bool = True) -> pd.DataFrame:
    """Expected goals for each side of each fixture.

    Where a fixture is priced, the odds are used directly. Where it is not,
    the xG ratings project it -- optionally nudged by what this same club's
    *priced* fixtures elsewhere in the list said about it, see
    _odds_calibration(). Both the calibrated and the uncalibrated ratings
    projection are kept on every xG-sourced row so a consumer (the browser
    board's toggle, in particular) can switch between them without a refetch.
    """
    fixtures = _attach_odds(fixtures, teams)
    ratings = strength.set_index("team")
    # Understat is used for relative strength only. Its absolute level is biased
    # upward here (dropping transferred players removes mostly low-output squad
    # men), and the league-wide goals rate is a stable, better-known constant,
    # so the ratings are applied on top of that rather than on top of the
    # measured mean. Where odds exist they override this entirely.

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
    # The xG-only figures, before calibration -- what the ratings alone said,
    # kept even for priced rows so a caller cannot mistake a still-zero column
    # for "no calibration available" on the rows where it actually matters.
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


# --------------------------------------------------------------------------- #
# Layer 2: minutes and share of team output
# --------------------------------------------------------------------------- #

def _start_prior(df: pd.DataFrame) -> pd.Series:
    """Expected start share for a player with no Premier League history.

    Price is the only signal available for a promoted club's squad or a signing
    from abroad, and it is a good one: FPL prices players by the role it expects
    them to have. Squared, because the relationship is not linear -- a £12m
    forward is far more than twice as likely to start as a £6m one -- and taken
    relative to the cheapest player in the same club and group so that it
    describes a pecking order rather than an absolute.
    """
    floor = df.groupby(["team", "is_keeper"])["price"].transform("min")
    return ((df["price"] - floor) + 0.5) ** 2


def start_form_weight(horizon: int) -> float:
    """How much of the recent start rate survives, averaged over the horizon.

    The fitted weight applies to one lead at a time: w_k = W * DECAY^(k-1). A
    projection totals `horizon` gameweeks and reports one number per player, so
    the weight it should carry is the mean of that curve over the leads it
    actually covers -- the closed form of which is the geometric series below.

    That is what makes the horizon control do the right thing without anyone
    having to think about it. Ask for one gameweek and you get w = 1.10, and the
    board fills with the players who are starting *now*. Ask for twelve and you
    get w = 0.40, because who starts in April is a question the last four
    gameweeks cannot answer, and the season-long rate is the better guess.
    """
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
    """Probability of starting, of appearing, of reaching 60 minutes.

    Derived from the starts and minutes the FPL API is currently serving --
    `starts` over the matches his club has played gives the start rate, and the
    minutes left over once starts are accounted for imply how often the player
    came off the bench -- and then **normalised so that each club fields a full
    team**.

    That last step is not a refinement. Taken raw, `starts / 38` summed to 8.25
    starters per club rather than 11, because a squad is not the set of players
    who played for it last season: some retired, some left the league, some
    arrived from abroad with no Premier League record, and a promoted club's
    entire squad has none. Every one of those is a hole that nobody filled, and
    the missing minutes were credited to no player at all -- which put the
    league's projected points at 74% of what a season actually awards, and put
    every promoted club at approximately zero.

    So the raw rate is treated as *evidence about a share*, not as the answer.
    Players with no evidence fall back to a price-based prior, the two are
    blended by how many minutes stand behind the evidence, and the result is
    scaled per club so that eleven players start: one keeper and ten outfield.
    Substitute appearances are scaled the same way, to the six that the 78/22
    minutes split implies once eleven starters are accounted for.

    Availability is applied *before* normalising, on purpose. A club whose first
    choice is injured still starts eleven players, so his share should pass to
    whoever is behind him rather than evaporate.
    """
    df = players.copy()

    # `d` (doubtful) maps to None on purpose, and an unrecognised status falls
    # through to the same place: the API's own chance_of_playing_next_round is a
    # better number for both than any constant this file could pick.
    availability = df["status"].map(STATUS_AVAILABILITY)
    df["availability"] = (availability
                          .fillna(df["chance_next"].fillna(50.0) / 100.0)
                          .clip(0.0, 1.0))
    df["is_keeper"] = df["pos"] == "GKP"

    # Divided by the matches his club has actually played, not by a fixed 38.
    # `starts` is a season-to-date total, so the denominator has to be the same
    # season to date -- 38 before the season starts, when the totals in hand are
    # still last season's complete ones, and six in the middle of September.
    # Against a hard-coded 38 a nailed starter six matches in reads 6/38 = 0.16
    # and the model files him behind whoever last season happened to know.
    club_matches = (df["team"].map(basis.club_matches) if basis
                    else pd.Series(float(MATCHES_PER_SEASON), index=df.index))
    club_matches = club_matches.fillna(float(MATCHES_PER_SEASON)).clip(lower=1.0)

    raw_start = (df["starts"] / club_matches).clip(0.0, 1.0)
    sub_minutes = (df["minutes"] - df["starts"] * ASSUMED_START_MINUTES).clip(lower=0.0)
    sub_appearances = sub_minutes / ASSUMED_SUB_MINUTES
    non_start_matches = (club_matches - df["starts"]).clip(lower=1.0)
    raw_sub = (sub_appearances / non_start_matches).clip(0.0, 1.0)

    # How much the evidence is worth. A full season speaks for itself; 300
    # minutes barely speaks at all, and zero cannot speak.
    minutes = df["minutes"].astype(float)
    weight = minutes / (minutes + START_PRIOR_MINUTES)
    prior = _start_prior(df)
    prior_share = prior / prior.groupby([df["team"], df["is_keeper"]]).transform("sum")
    # Put the prior on the same scale as a start probability before blending, or
    # a 30-man squad's shares would each be tiny next to a real start rate.
    # Clipped to the same ceiling as everything else: an expensive signing's
    # raw share can exceed 1.0 at a rich club, and blending an impossibility
    # into the evidence is how a £9m striker with eight starts behind him came
    # out projected to start every single match.
    prior_start = (prior_share * np.where(df["is_keeper"], 1.0, XI_OUTFIELD)).clip(
        upper=MAX_P_START)

    long_run = weight * raw_start + (1 - weight) * prior_start

    # Then tilt toward who has been starting lately, by how far ahead we are
    # looking. Two shrinkages guard it: the recent rate is itself pulled back
    # toward the long-run one by how many recent matches stand behind it, and
    # the horizon weight decays the whole correction away as the plan lengthens.
    # A player the archive has never heard of has recent_matches 0, which makes
    # both terms vanish and leaves him exactly where the long-run rate put him.
    # `.get` on a missing column returns None, not an empty Series, and
    # pd.to_numeric(None) is a bare nan -- so the column has to be tested for
    # before it is converted, or a frame without the archive raises here.
    if "recent_start_rate" not in df:
        tilted = long_run
        recent = long_run
    else:
        recent = pd.to_numeric(df["recent_start_rate"], errors="coerce")
        recent_matches = pd.to_numeric(
            df.get("recent_matches", pd.Series(0.0, index=df.index)), errors="coerce")
        recent = recent.fillna(long_run)
        recent_matches = recent_matches.fillna(0.0).clip(lower=0.0)
        believed = recent_matches / (recent_matches + START_FORM_PRIOR_MATCHES)
        recent = long_run + believed * (recent - long_run)
        tilted = long_run + start_form_weight(horizon) * (recent - long_run)

    # Clipped before availability rather than after: the extrapolation above can
    # land outside [0, 1], and a negative share would come back through
    # normalisation as a club owing starts to its own bench.
    tilted = tilted.clip(0.0, 1.0)

    blended = tilted * df["availability"]
    blended_sub = (weight * raw_sub + (1 - weight) * prior_share) * df["availability"]

    # Fringe collapse. See FRINGE_SHARE: a player already projected low, with
    # not much of his own evidence behind that projection, is pulled further
    # toward nothing rather than left as a smooth trickle across every fixture.
    # `_normalise_to` below then redistributes what he gave up to the rest of
    # his club the same way any other shortfall is redistributed, so this is a
    # reallocation within the group, not extra minutes invented or destroyed.
    involvement = blended + (1 - blended) * blended_sub
    thin = (1 - weight).clip(0.0, 1.0)
    fringe_pull = thin * FRINGE_COLLAPSE_STRENGTH * (1 - involvement / FRINGE_SHARE).clip(0.0, 1.0)
    blended = blended * (1 - fringe_pull)
    blended_sub = blended_sub * (1 - fringe_pull)

    df["p_start"] = _normalise_to(blended, df, {True: 1.0, False: float(XI_OUTFIELD)})
    # Kept on the frame so the board can redo this blend at a different horizon
    # without the laptop: the browser has p_start at the snapshot's horizon, and
    # these two are what let it recompute one for any other.
    df["start_long_run"] = long_run.clip(0.0, 1.0)
    df["start_recent"] = recent.clip(0.0, 1.0)

    # Six substitute appearances per club per match: 11 starters x 78 minutes
    # leaves 132 of the 990 a team plays, and 132/22 is six. Keepers are excluded
    # -- a reserve keeper coming on is rare enough to leave to his own rate.
    outfield_subs = ((1 - df["p_start"]) * blended_sub).where(~df["is_keeper"], 0.0)
    scale = _club_scale(outfield_subs, df["team"], SUBS_PER_MATCH)
    df["p_sub"] = np.where(df["is_keeper"], blended_sub,
                           (blended_sub * df["team"].map(scale)).clip(0.0, 1.0))

    # How long he plays *when he starts*, which is a different question from how
    # often he starts. Season minutes divided by starts cannot answer it --
    # mixing in substitute cameos undercounts a genuine 90-minute regular the
    # moment he has come off the bench even once -- but the per-gameweek archive
    # can: a row where he started reports minutes from that start alone. Absent
    # that archive, or absent enough of it for a given player, he opens on the
    # league average and the user says otherwise where he knows better. See
    # OVERRIDABLE and _p60_given_start.
    if "recent_mins_if_start" not in df:
        df["mins_if_start"] = float(ASSUMED_START_MINUTES)
    else:
        recent_mins = pd.to_numeric(df["recent_mins_if_start"], errors="coerce")
        recent_starts = pd.to_numeric(
            df.get("recent_start_matches", pd.Series(0.0, index=df.index)),
            errors="coerce").fillna(0.0).clip(lower=0.0)
        believed = recent_starts / (recent_starts + MINUTES_FORM_PRIOR_MATCHES)
        df["mins_if_start"] = (float(ASSUMED_START_MINUTES)
                               + believed.fillna(0.0) * (recent_mins.fillna(ASSUMED_START_MINUTES)
                                                          - ASSUMED_START_MINUTES)
                               ).clip(0.0, MAX_MINS_IF_START)

    df["mins_flags"] = _minutes_flags(df)
    _derive_minutes(df)
    return df


def _minutes_flags(df: pd.DataFrame) -> pd.Series:
    """Reasons the minutes assumption above is worth a manual look, not a fact.

    Three things the model either cannot see or can only half-correct for:
    a shift length so inconsistent that its own average is a poor summary
    (see MINS_VOLATILE_CV), a club move that may have changed his role (his
    current-season minutes archive already reflects the new club once he has
    played there, but a mid-season mover's rows mix both clubs with no way to
    tell them apart), and a fitness/squad status the API itself is unsure
    about. Comma-joined so a CLI table can show it in one column; empty string
    means nothing stood out.
    """
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
    """P(reaches 60 minutes | starts), given how long his shift is.

    A logistic pinned at two points rather than fitted -- an hour-long shift
    reaches the hour half the time by definition, and the league-average shift
    reaches it P60_GIVEN_START of the time. See P60_SLOPE_MINUTES in config.

    Not applied to substitutes. This curve describes the length of a *start*,
    calibrated on starters; a player who came on with twenty minutes left is
    bounded by when he came on, not by how a starter's match tends to end.

    Accepts a scalar or a Series; returns a bare ndarray either way, so a
    caller wanting one number wraps it in float().
    """
    minutes = np.asarray(mins_if_start, dtype=float)
    return 1.0 / (1.0 + np.exp(-(minutes - P60_MIDPOINT_MINUTES) / P60_SLOPE_MINUTES))


def _mins_if_start(player) -> float:
    """One player's shift length, falling back to the league average.

    The column is younger than the rest of the minutes family, so a row that
    predates it -- a stored snapshot, a CSV round-trip -- reads as the 78
    minutes everybody used to be assumed to play, which is exactly what that
    row was scored with when it was written.
    """
    try:
        value = float(player.get("mins_if_start", ASSUMED_START_MINUTES))
    except (TypeError, ValueError):
        return float(ASSUMED_START_MINUTES)
    return float(ASSUMED_START_MINUTES) if np.isnan(value) else value


def _derive_minutes(df: pd.DataFrame) -> None:
    """p_play, p60 and exp_minutes from p_start, p_sub and mins_if_start.

    The one place the forward minutes formula lives. Everything else that moves
    a member of the family -- the pipeline, an override, a club rebalance --
    comes back through here rather than restating it.
    """
    p_start, p_sub = df["p_start"], df["p_sub"]
    mins = df["mins_if_start"]
    df["p_play"] = p_start + (1 - p_start) * p_sub
    df["p60"] = p_start * _p60_given_start(mins)
    df["exp_minutes"] = (p_start * mins
                         + (1 - p_start) * p_sub * ASSUMED_SUB_MINUTES)


def conserve_team_output(players: pd.DataFrame,
                         strength: pd.DataFrame | None) -> pd.DataFrame:
    """Make each club's per-90 rates add up to what the club actually produces.

    A per-90 rate is a share of team output, but nothing until now made the
    shares sum to one. Once every club fields a full eleven that omission stops
    being hidden and starts being wrong in the opposite direction: the squads
    collectively expected 37.8 goals in a gameweek whose own fixture lambdas
    said 29.7, and 74 bonus points in a gameweek where the rules award 60. Those
    are not disagreements with an outside benchmark, they are the model
    contradicting itself.

    Three quantities are fixed by something outside the player rates, so three
    get normalised:

      * **Goals.** The fixture lambdas already decide how many a team scores.
        Scaling npxg_per90 so the squad sums to the club's per-match xG makes
        `attack_scale` conserve them exactly rather than approximately.
      * **Assists.** An assist needs a goal, so the league's assists-per-goal
        ratio pins the total once goals are pinned -- measured per *open-play*
        goal, since that is what the club's npxG target counts.
      * **Bonus.** Three points per match per team, by rule and by arithmetic.

    Everything else -- clean sheets, concessions, saves, defensive contribution
    -- is already anchored to a team-level quantity or is genuinely per-player,
    and is left alone.
    """
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


# Which shrinkage weight stands behind each conserved rate, so the correction
# below can tell an evidenced number from an assumed one.
EVIDENCE_WEIGHT = {
    "npxg_per90": "attack_evidence_weight",
    "xa_per90": "xa_evidence_weight",
    "bonus_per90": "fpl_evidence_weight",
}


def _conserve_column(rate: pd.Series, share: pd.Series, weight: pd.Series,
                     target: pd.Series, team: pd.Series) -> pd.Series:
    """Bring each club's total to `target`, charging it to the least-evidenced rates.

    The old version scaled every player at a club by the same factor, and that
    turned out to be a quiet tax on exactly the players the model knows best.
    Shrinkage pulls a rate toward the positional average from *both* sides, so a
    squad's fringe -- including players with literally no minutes, who are pure
    prior -- gets lifted to something a regular would earn. Their contributions
    add up: Manchester City's squad projected 4.71 bonus a match against the 2.78
    the rules actually pay, and closing that gap uniformly took 41% off Haaland,
    whose own bonus rate is one of the best-evidenced numbers in the league.
    Shrinkage had already cost him 15%; the conservation step then charged him
    for the assumptions made about his reserve goalkeeper.

    So the correction is applied as `lam ** (1 - weight)`: a player whose rate is
    all evidence (weight 1) is untouched, one who is all prior (weight 0) takes
    it in full, and everyone else in between. `lam` is found by bisection --
    the total is monotone in it, so the club still lands exactly on its target,
    which is the whole point of this function and is not negotiable.

    Where protecting the evidenced players cannot get there on its own, the
    remainder is taken uniformly. Conserving the total matters more than who
    pays for it: an unconserved club contradicts its own fixture lambdas.
    """
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
            # A squad the data barely knows: do what is defensible, no more.
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
        # Whatever protecting the evidenced players could not absorb.
        total = float((scaled * s).sum())
        if total > 0:
            scaled *= min(goal / total, MAX_RATE_SCALE)
        out.loc[index] = scaled

    return out


def _club_scale(value: pd.Series, team: pd.Series, target: float) -> pd.Series:
    """Per-club multiplier that brings `value` to `target`, within reason.

    Capped because normalisation is a corrective, not a licence. An unbounded
    factor would take a club whose squad the data barely knows and multiply the
    two players it does know into superstars, which is a worse error than the
    one being fixed.
    """
    total = value.groupby(team).sum()
    return (target / total.replace(0.0, np.nan)).clip(upper=MAX_MINUTES_SCALE).fillna(1.0)


def _normalise_to(value: pd.Series, df: pd.DataFrame,
                  targets: dict[bool, float]) -> pd.Series:
    """Scale within each (club, keeper?) group so the group sums to its target.

    One multiplier per club, found by bisection on

        f(lam) = sum_i min(MAX_P_START, v_i * min(lam, MAX_MINUTES_SCALE))

    which is monotone in lam, so the answer is exact. The shape of this matters
    more than it looks:

      * **One lam, not an iteration.** The first version rescaled repeatedly,
        redistributing what the ceiling rejected -- and each pass compounded the
        previous one, so a player with 20% start evidence could be ratcheted to
        certainty in four steps of 2.5x. A single multiplier preserves the
        *shape* of the evidence: everyone at a club moves together, and the
        pecking order the data supports is the pecking order that comes out.

      * **The ceiling is 0.95, not 1.0.** Nobody is certain to start. Last
        season each club's single most-nailed player averaged a 0.966 start
        rate, first-choice keepers a 0.921 -- and that is the *maximum order
        statistic*, measured after the fact. The first version pinned 26
        players at exactly 1.0 in a league where eight managed 38/38.

      * **The scale is capped at 2.5x total.** Normalising to eleven is a
        correction for players the data cannot see, not a promotion for the
        ones it can. If a club's evidence is so thin that 2.5x still does not
        reach eleven starters, the shortfall is accepted rather than invented.
    """
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
                lam = MAX_MINUTES_SCALE  # thin squad: do what is defensible, no more
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


# Every model input a user may sensibly disagree with, and the range it is
# allowed to take. Each also accepts a `<name>_mult` column, which multiplies
# whatever the model derived instead of replacing it -- usually the more natural
# way to express an opinion ("about 20% better than his old club suggests").
# Order matters for the four minutes fields. They are applied in the order
# listed, and exp_minutes is solved against whatever the three before it left --
# so stating all four means "this start probability, this shift, this bench
# chance, and the minutes I actually want", with the last one arbitrating.
OVERRIDABLE = {
    "p_start": (0.0, 1.0),
    "mins_if_start": (0.0, MAX_MINS_IF_START),
    # p_sub is P(comes on | doesn't start). Left untouched, a benched player's
    # p_play floors at whatever this was estimated at, no matter how far
    # p_start or exp_minutes gets pushed down -- a keeper who is realistically
    # never coming off the bench still needs a way to say so.
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
    """Invert the forward minutes formula. Returns (p_start, mins_if_start).

    There are two ways a player comes to play more minutes -- he starts more
    often, or he stays on longer when he does -- and stating exp_minutes does
    not say which. This prefers the second, because it is the smaller claim:
    lengthening a man's shift says nothing about anyone else, while raising his
    start probability takes a shirt off a team-mate and drags the whole club
    through renormalise_minutes. So holding p_start where the user left it,

        exp_minutes = p_start*mins_if_start + (1-p_start)*p_sub*ASSUMED_SUB_MINUTES

    solved for mins_if_start. Only when that runs out of room -- he cannot play
    more than the ninety, and if he rarely starts even ninety is not enough --
    does the shift go to its maximum and p_start absorb the remainder, which is
    the old behaviour and still the right answer for the case it was written
    for: a player the model has never seen, asserted into the side.

    Clipped to MAX_P_START rather than 1.0 to match the ceiling every other
    minutes path in this file uses -- nobody is nailed on beyond it.
    """
    if p_start > 0.0:
        shift = (exp_minutes - (1.0 - p_start) * p_sub * ASSUMED_SUB_MINUTES) / p_start
        if shift <= MAX_MINS_IF_START:
            return p_start, float(np.clip(shift, 0.0, MAX_MINS_IF_START))

    denom = MAX_MINS_IF_START - p_sub * ASSUMED_SUB_MINUTES
    solved = (exp_minutes - p_sub * ASSUMED_SUB_MINUTES) / denom
    return float(np.clip(solved, 0.0, MAX_P_START)), MAX_MINS_IF_START


def _recompute_minutes(df: pd.DataFrame, mask) -> None:
    """Keep the minutes family consistent after one of its members moves.

    p_start, mins_if_start and exp_minutes are three views of one assumption, so
    setting any of them has to re-derive the rest: the appearance points, the
    60-minute clean-sheet gate and the minutes scaling all read from different
    members of this family, and letting them drift apart produces a player who
    starts every week but plays no minutes.
    """
    subset = df.loc[mask, ["p_start", "p_sub", "mins_if_start"]].copy()
    _derive_minutes(subset)
    for column in ("p_play", "p60", "exp_minutes"):
        df.loc[mask, column] = subset[column]


def apply_fields(player: Mapping[str, Any], fields: Mapping[str, Any]) -> dict:
    """Apply overrides to one player, as a plain mapping.

    The single-player counterpart to `apply_overrides`, which works a column at
    a time across a whole table. Both exist because they are asked different
    questions: the table version answers "what does the pool look like with
    these opinions in it", and this one answers "what is this player worth in
    *this* fixture" -- a question that only arises per gameweek, where building
    a one-row DataFrame per player per fixture would be absurd.

    Kept honest by `test_apply_fields_matches_apply_overrides` in
    scripts/verify-per-gameweek.py, which runs both over the same inputs.
    """
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

        # exp_minutes is solved last and wins: it is the more specific claim,
        # and _solve_exp_minutes reads whatever the other three just set.
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
    """Pull the per-gameweek rows out of an overrides table.

    A row with a `gw` is an opinion about one match -- rested for a cup final,
    back from injury in three weeks, moved up front while the striker is out.
    A row without one is an opinion about the player, and is handled by
    `apply_overrides` as before. Keying on (fpl_id, gw) rather than layering
    frames keeps the two kinds from having to know about each other.
    """
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
    """Replace any modelled input with your own number, by name or by id.

    Last season's rates are an estimate, not a fact, and there are things you
    know that they cannot contain: a new penalty taker, a change of role, a
    player whose xG was produced in a system he has left. This is the hook for
    saying so. Anything listed in OVERRIDABLE can be set outright, or scaled
    with a `_mult` column.

    Overriding a rate deliberately bypasses the shrinkage that raw data goes
    through -- if you assert a number, the model uses that number. Shrinkage
    exists to stop a small sample from speaking too loudly, and an override is
    not a sample.
    """
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
        # A row carrying a `gw` is about one match, not about the player.
        # gameweek_overrides() picks those up; applying them here as well would
        # silently spread a one-week opinion across the whole horizon.
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
        # Whether p_start itself moved, which is the only part of the family the
        # club has to be rebalanced around: lengthening one man's shift takes
        # nothing off a team-mate, so it must not pin him.
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

            # exp_minutes is solved last and wins: it is the more specific
            # claim, and _solve_exp_minutes reads whatever the other three set.
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
                # _recompute_minutes derives exp_minutes from the other two; if
                # the user stated the minutes directly, that stands.
                df.loc[mask, "exp_minutes"] = pinned
            if start_moved:
                # Renormalising a club back to eleven starters has to know whose
                # number is not its to move. See renormalise_minutes().
                df.loc[mask, "minutes_pinned"] = True
        for field in touched:
            # An assertion is the strongest evidence there is, so conservation
            # treats it as fully evidenced and takes its correction from the
            # rates the model was guessing at instead. Without this, stating a
            # rate and then balancing the club's books would quietly scale the
            # stated number -- the one thing an override must never do.
            weight_column = EVIDENCE_WEIGHT.get(field.split("×")[0])
            if weight_column and weight_column in df.columns:
                df.loc[mask, weight_column] = 1.0
        if touched:
            df.loc[mask, "overridden"] = ", ".join(touched)
    return df


def _calibrate_bonus_prior(df: pd.DataFrame, raw: pd.Series, prior: pd.Series,
                           evidence: pd.Series,
                           prior_minutes: pd.Series) -> pd.Series:
    """Scale the bonus prior so the shrunk rates pay out what the rules do.

    A per-90 rate becomes points by way of expected minutes, and across the
    league those minutes are fixed: eleven players on the pitch per club, and
    2.78 bonus points to divide between them. So the prior is not free -- it is
    whatever makes the shrunk rates add up to that, given the evidence already
    in hand. Solved rather than assumed, because the two parts are linear:

        sum(w.raw.share) + m . sum((1-w).prior.share) = clubs x 2.78

    leaves one unknown. `m` comes out near 0.75, which is the size of the bias
    the established-player average was carrying.

    The positions keep their relative order -- forwards still out-earn
    defenders by whatever the data says -- only the level moves.
    """
    share = pd.to_numeric(df["exp_minutes"], errors="coerce").fillna(0.0) / 90.0
    weight = (evidence / (evidence + prior_minutes)).fillna(0.0)

    target = float(df["team"].nunique()) * BONUS_PER_TEAM_MATCH
    evidenced = float((weight * raw * share).sum())
    assumed = float(((1.0 - weight) * prior * share).sum())
    if assumed <= 0:
        return prior

    # Clipped, not trusted blindly: if the evidence alone already pays the whole
    # pool the honest answer is a prior of nothing, and a runaway multiplier the
    # other way would be a different bug wearing this one's clothes.
    multiplier = float(np.clip((target - evidenced) / assumed, 0.0, 3.0))
    return prior * multiplier


def _bisect_scale(values: np.ndarray, target: float) -> np.ndarray:
    """One bounded multiplier that brings `values` to `target`, each clipped at
    MAX_P_START. Shared by every tier of `renormalise_minutes`: the same shape
    solves "bring this group to eleven" and "bring this position's free players
    back to what they would have had," just with a different `values`/`target`.
    """
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
    """Put each club back to eleven starters after an override moved somebody.

    Minutes are a fixed pool. Eleven players start, and asserting that one of
    them starts more can only mean somebody else starts less -- but overrides are
    applied at the end of the pipeline, long after `minutes_model` balanced the
    squad, and nothing used to rebalance it. Raising one fringe player's p_start
    to 0.9 left Manchester City fielding 11.6 players and scoring 8% more than
    the odds said they would, with the extra goals conjured out of nothing rather
    than taken from a team-mate.

    So the assertion is kept and the rest of the club absorbs it -- and absorbs
    it unevenly. A promoted winger competes for a shirt with the other wingers,
    not with the centre-backs, so whoever shares a position with the player who
    moved feels most of the consequence: their free peers at the same position
    are rescaled first, by one bounded multiplier, to soak up exactly what that
    position gained or lost. Only what they cannot absorb -- because they are
    already at nought, or already as nailed as it gets -- spills into the rest
    of the club, scaled the same way `_normalise_to` does it, so the pecking
    order the data supports survives the adjustment either way. Goalkeepers
    have no such split: a club fields one, so there is no "same position" to
    prefer among the rest.

    `baseline_p_start` is each player's p_start before *any* override touched
    the pool -- `minutes_model`'s own answer -- which is what "what this
    position gained or lost" is measured against. Falls back to the current
    column if not given, which loses the position split for pinned players
    whose own value has already moved, but still balances the club.

    If the pinned players alone already exceed eleven, that stands: the user has
    said so, and quietly scaling their numbers back to fit would be the model
    overruling an assertion. The club simply fields more than eleven and
    `goal_coverage` reports it.
    """
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

    # Everyone who moved needs the rest of the minutes family re-derived from
    # the new p_start; the pinned players already had that done for them.
    _recompute_minutes(df, ~pinned)
    return df


def _shrink(rate: pd.Series, minutes: pd.Series, prior: pd.Series,
            prior_minutes: pd.Series | float = PLAYER_PRIOR_MINUTES) -> pd.Series:
    """Pull a per-90 rate toward a prior, weighted by how much evidence there is."""
    rate = pd.to_numeric(rate, errors="coerce").fillna(0.0)
    weight = minutes / (minutes + prior_minutes)
    return weight * rate + (1 - weight) * prior


def detect_movers(players: pd.DataFrame, team_map: dict[str, str]) -> pd.DataFrame:
    """Flag players whose current club is not one they played for last season.

    Understat records every club a player turned out for, so a player whose FPL
    club appears nowhere in that list moved over the summer. His rates describe
    a different team, which is worth knowing before trusting them.
    """
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
    """Per-90 attacking rates, preferring Understat and falling back to FPL.

    Understat's npxG is non-penalty, which matters: penalties are modelled
    separately and assigned to the designated taker rather than smeared across
    everyone who happens to have a high xG.

    Rates are then adjusted for a change of club and shrunk toward the
    positional average, weighted by the minutes behind them. Without the
    shrinkage the optimiser reliably picks whoever had the smallest, luckiest
    sample in the league.
    """
    df = players.copy()
    minutes = df["minutes"].astype(float)
    minutes_90 = (minutes / 90.0).replace(0, np.nan)

    fpl_xg90 = df["expected_goals_per_90"].fillna(0.0)
    fpl_xa90 = df["expected_assists_per_90"].fillna(0.0)
    us_xg90 = pd.to_numeric(df.get("npxg_per90"), errors="coerce")
    us_xa90 = pd.to_numeric(df.get("xa_per90"), errors="coerce")

    has_understat = us_xg90.notna() & (df["us_minutes"].fillna(0) > 0)
    raw_xg90 = np.where(has_understat, us_xg90, fpl_xg90 * (1 - PENALTY_GOAL_SHARE))
    raw_xa90 = np.where(has_understat, us_xa90, fpl_xa90)

    # The minutes that actually produced each rate, and the full workload they
    # should be read against. An Understat-backed attacking rate is backed by
    # Understat's minutes from Understat's season; everything else is backed by
    # the FPL API's, from whatever point of this season it is serving. Preseason
    # the two are near enough the same number that the distinction looks
    # academic -- but they count different seasons the moment one rolls over,
    # and weighting last season's xG by this season's minutes is exactly how a
    # genuine 0.6 npxG/90 striker gets shrunk to nothing in September.
    us_minutes = pd.to_numeric(df.get("us_minutes"), errors="coerce").fillna(0.0)
    understat_backed = pd.Series(has_understat, index=df.index).fillna(False)
    attack_minutes = us_minutes.where(understat_backed, minutes)
    full_fpl = basis.fpl_minutes if basis else MATCHES_PER_SEASON * 90.0
    full_us = basis.understat_minutes if basis else MATCHES_PER_SEASON * 90.0
    attack_full = pd.Series(np.where(understat_backed, full_us, full_fpl), index=df.index)

    df["rate_source"] = np.where(has_understat, "understat", "fpl")
    df.loc[attack_minutes < THIN_SHARE * attack_full, "rate_source"] = "thin"
    df.loc[attack_minutes <= 0, "rate_source"] = "none"

    df["raw_npxg_per90"] = pd.to_numeric(pd.Series(raw_xg90, index=df.index),
                                         errors="coerce").fillna(0.0)
    df["raw_xa_per90"] = pd.to_numeric(pd.Series(raw_xa90, index=df.index),
                                       errors="coerce").fillna(0.0)
    df["raw_bonus_per90"] = (df["bonus"] / minutes_90).fillna(0.0).clip(0, 3)
    df["raw_yellow_per90"] = (df["yellow_cards"] / minutes_90).fillna(0.0).clip(0, 1)
    df["raw_dc_per90"] = df["defensive_contribution_per_90"].fillna(0.0)
    df["raw_saves_per90"] = df["saves_per_90"].fillna(0.0)

    # Recency: tilt each rate by how the player was trending late last season.
    # Applied before shrinkage on purpose, so a big multiplier off a short hot
    # streak still gets pulled back toward the positional prior rather than
    # sailing straight through into the projection.
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

    # A change of club moves a player's attacking output toward his new team's
    # level. A striker leaving a mid-table side for a title contender gets more
    # and better chances; the reverse costs him. Only the attacking rates are
    # adjusted -- defensive contribution is a function of role and position far
    # more than of team quality, and the clean-sheet and concession terms
    # already use the new club's defence.
    df["team_context"] = 1.0
    if strength is not None and us_attack_rating and "moved_club" in df:
        new_rating = df["team"].map(strength.set_index("team")["attack_rating"])
        old_rating = df["previous_club"].map(us_attack_rating)
        ratio = (new_rating / old_rating).replace([np.inf, -np.inf], np.nan)
        context = ratio.clip(0.4, 2.5) ** TRANSFER_CONTEXT_ALPHA
        df["team_context"] = np.where(df["moved_club"] & context.notna(),
                                      context.fillna(1.0), 1.0)
        df["raw_npxg_per90"] *= df["team_context"]
        df["raw_xa_per90"] *= df["team_context"]

    # One prior per rate, because the evidence for them is not the same. The two
    # attacking rates are measured across seasons (see NPXG_PRIOR_MINUTES); the
    # rest keep the standing default, since nothing in the cache spans two
    # seasons for defensive contribution, saves, bonus or cards.
    def prior_for(base: float) -> pd.Series:
        series = pd.Series(base, index=df.index, dtype=float)
        if "moved_club" in df:
            series = series.where(~df["moved_club"], base * MOVER_PRIOR_MULTIPLIER)
        return series

    rate_prior = {
        "raw_npxg_per90": prior_for(NPXG_PRIOR_MINUTES),
        "raw_xa_per90": prior_for(XA_PRIOR_MINUTES),
    }
    default_prior = prior_for(PLAYER_PRIOR_MINUTES)

    # The prior for each rate is the minutes-weighted average among established
    # players in the same position, so a fringe forward is shrunk toward what
    # forwards do rather than toward what the league as a whole does.
    #
    # Established *by the measure that backs that rate*, and shrunk against the
    # same. Reading every prior off one fixed 900-minute cut is what emptied the
    # group the week a season rolled over, and an empty group is a prior of
    # zero -- which shrinks the entire league toward scoring nothing at the exact
    # moment the model has least of its own to say.
    #
    # Yellow cards are in this list now too. A rate over a small sample is noise
    # whichever sign it carries, and left raw a player with one booking in a
    # single 90-minute cameo projected a card every match.
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

        # Bonus is a fixed pool -- three points per match per side, by rule --
        # and a prior that does not respect that quietly mints points. Taken as
        # the minutes-weighted rate among *established* players it is the rate of
        # a regular, and it was then handed to everyone, including players with
        # no minutes at all. Summed over expected minutes the league came out
        # 33% above what the rules actually pay.
        #
        # Nothing downstream noticed, because conserve_team_output forced each
        # club back to its 2.78 anyway -- by scaling the whole squad, so the
        # invented points were taken back off whoever had really earned them.
        # That is what made the model pay Haaland half his historical bonus:
        # not regression, but a biased prior recovered from the wrong player.
        prior_minutes = rate_prior.get(raw, default_prior)
        if target == "bonus_per90" and "exp_minutes" in df:
            prior = _calibrate_bonus_prior(df, df[raw], prior, evidence, prior_minutes)

        shrunk[target] = _shrink(df[raw], evidence, prior, prior_minutes)
    for target, series in shrunk.items():
        df[target] = series

    # How much of each rate is the player's own record rather than the
    # positional prior standing in for it. Kept because conserve_team_output
    # needs to know whose numbers it is allowed to move: a squad's books have to
    # balance, but the player who should pay for that is the one the model was
    # guessing about, not the one it measured over three thousand minutes.
    # Each against the prior that rate was actually shrunk with -- npxG is
    # believed far sooner than xA is, and conservation has to know which.
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


# --------------------------------------------------------------------------- #
# Layer 3: points
# --------------------------------------------------------------------------- #

def _minutes_scenarios(player: pd.Series) -> list[tuple[float, float, float]]:
    """[(probability, minutes, reaches_60)] for the start and substitute cases.

    Clean sheets, goals conceded, saves and defensive contribution are all
    non-linear in minutes, so they must be evaluated per scenario and then
    averaged -- evaluating them once at the mean would be wrong.

    `reaches_60` rides along rather than being recovered from `minutes` because
    the two are not the same question. A start of 78 minutes is a *mean*: it
    reaches the hour 87% of the time, not always, and a start of 60 reaches it
    about half the time. A substitute's 22 minutes never does. Carrying it here
    is also what keeps the appearance term honest -- p60 is the probability-
    weighted sum of this column, by construction, so the model cannot say a
    player reaches 60 minutes one often for his appearance point and another
    often for his clean sheet.
    """
    p_start = float(player["p_start"])
    p_sub_given_no_start = float(player["p_sub"])
    mins_if_start = _mins_if_start(player)
    return [
        (p_start, mins_if_start, float(_p60_given_start(mins_if_start))),
        ((1 - p_start) * p_sub_given_no_start, ASSUMED_SUB_MINUTES, 0.0),
    ]


def _player_fixture_points(player: pd.Series, lam_for: float, lam_against: float,
                           team_npxg: float, team_xgc: float) -> dict[str, float]:
    """Expected FPL points for one player in one fixture, broken down by source."""
    pos = player["pos"]
    minutes_share = float(player["exp_minutes"]) / 90.0

    # Scale the player's season-average rate to this specific fixture.
    lam_openplay = lam_for * (1 - PENALTY_GOAL_SHARE)
    attack_scale = lam_openplay / team_npxg if team_npxg > 0 else 1.0
    defence_scale = lam_against / team_xgc if team_xgc > 0 else 1.0

    exp_goals = float(player["npxg_per90"]) * minutes_share * attack_scale
    exp_assists = float(player["xa_per90"]) * minutes_share * attack_scale

    # Penalties go to the designated taker only, and only while he is on the
    # pitch. Scaled by minutes share rather than by p_start, like every other
    # rate here: penalties are awarded across the whole match, but a starter is
    # only out there for 78 of the 90. Charging the full match to a man who
    # plays 87% of it overstated every premium penalty taker by about 13%.
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
    # The club's full-match figure, reported for context. What a player is paid
    # for is the on-pitch one computed per scenario below.
    team_cs_prob = ps.clean_sheet_prob(lam_against)

    for probability, minutes, reaches_60 in _minutes_scenarios(player):
        if probability <= 0:
            continue
        share = minutes / 90.0
        lam_on_pitch = lam_against * share

        # A clean sheet needs 60 minutes, and a player who starts does not always
        # get them -- he is subbed, or injured, or sent off. The appearance term
        # already prices that in through p60; without the same factor here the
        # model would say a starter reaches 60 minutes 87% of the time for his
        # appearance point and 100% of the time for his clean sheet. Handed down
        # by _minutes_scenarios, which is where the two are kept equal.

        # The rule pays for conceding nothing *while he is on the pitch*, not
        # for the team keeping a clean sheet: a defender subbed at 78 minutes
        # keeps his four points if the goal arrives at 85. So this is the same
        # on-pitch lambda the concession term below already uses -- the two are
        # the same event counted at zero and at one, and having them read
        # different lambdas was the model disagreeing with itself.
        cs_prob = ps.clean_sheet_prob(lam_on_pitch)

        # Counted for every position, including forwards who score nothing for
        # it: the number answers "will this be kept out while he is playing",
        # which is what you are buying a defender for, and it should not vanish
        # because the player in front of you does not get paid for it.
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


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def project(horizon: int = 5, start_gw: int | None = None,
            overrides: pd.DataFrame | None = None,
            recency_half_life: float | None = None,
            force_refresh: bool = False,
            calibrate_to_odds: bool = True) -> Projection:
    """Run the full pipeline and return projected points over the horizon."""
    fpl_players = fpl_api.players(force_refresh)
    fpl_teams = fpl_api.teams(force_refresh)
    all_fixtures = fpl_api.fixtures(force_refresh)
    us_stats = understat.player_stats(force_refresh=force_refresh)

    team_names = fpl_teams["name"].tolist()
    id_to_name = dict(zip(fpl_teams["team_id"], fpl_teams["name"]))
    # How far into the season the totals in hand actually are. Everything that
    # divides a season total by something reads this rather than assuming 38.
    basis = season_basis(all_fixtures, id_to_name, us_stats)

    # Clubs that were relegated map to nothing and are simply dropped.
    us_clubs = sorted({club for clubs in us_stats["us_team_list"] for club in clubs})
    team_map = {club: match_team(club, team_names) for club in us_clubs}

    players = match_players(fpl_players, us_stats, team_map)
    players = detect_movers(players, team_map)

    # The per-gameweek archive, fetched once and read for two different things.
    # It is third-party and can be unavailable, so both readings degrade to the
    # season-long behaviour rather than taking the projection down.
    gw_history = history.gameweek_history(force_refresh=force_refresh)

    # Optional: tilt rates toward how players were performing late last season.
    if recency_half_life:
        multipliers = history.recency_multipliers(gw_history, recency_half_life)
        if len(multipliers):
            players = players.merge(multipliers, on="code", how="left")

    # Not optional, unlike the tilt above. Who has been starting lately is not a
    # stylistic preference about form, it is the difference between a start rate
    # that answers this week's question and one that answers last season's.
    form = history.start_form(gw_history, START_FORM_HALF_LIFE)
    if len(form):
        players = players.merge(form, on="code", how="left")

    # Same archive, same recency weighting, a different question: not how often
    # he starts but how long the shift is once he does. See MINUTES_FORM_PRIOR_MATCHES.
    mins_form = history.minutes_form(gw_history, START_FORM_HALF_LIFE)
    if len(mins_form):
        players = players.merge(mins_form, on="code", how="left")

    # Team strength has to be known before player rates, because adjusting a
    # transferred player's output needs the ratings of both clubs involved.
    strength = team_strength(players, us_stats, team_map, basis)
    us_teams = understat.team_rates(us_stats)
    league_mean = us_teams["team_npxg_per_match"].mean()
    us_attack_rating = (us_teams.set_index("us_team")["team_npxg_per_match"]
                        / league_mean).to_dict() if league_mean else {}

    # Minutes before rates. The two stages are independent -- neither reads the
    # other's output -- but a rate only becomes points by way of expected
    # minutes, and the bonus prior is calibrated against the pool those minutes
    # imply, so it needs them in hand.
    players = minutes_model(players, basis=basis, horizon=horizon)
    players = attach_rates(players, strength, us_attack_rating, basis)
    # renormalise_minutes prefers the same position as whoever an override
    # moved, which means it needs to know what that position had *before* the
    # override -- captured here, since apply_overrides is about to overwrite
    # the very column it would otherwise have to read that from.
    minutes_baseline = players["p_start"].copy()
    # Applied after every rate has been derived and shrunk, so that an asserted
    # number is the one the model actually uses.
    players = apply_overrides(players, overrides)
    # An override moves one player; these two put the club back together around
    # him. Minutes are a fixed pool, so asserting that somebody starts takes the
    # minutes from a team-mate rather than inventing a twelfth starter -- and
    # the books are balanced afterwards rather than before, so a squad edited
    # into a different shape still adds up to what the fixtures say it scores.
    #
    # Neither step can touch what was asserted: renormalise_minutes pins the
    # overridden players, and conserve_team_output sees an asserted rate as
    # fully evidenced and takes its correction from the modelled ones instead.
    players = renormalise_minutes(players, minutes_baseline)
    players = conserve_team_output(players, strength)
    per_gameweek = gameweek_overrides(overrides, players)

    start_gw = start_gw or fpl_api.next_gameweek(force_refresh)
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
                # Per-match opinions land here, before the availability check:
                # "he is back for gameweek 8" has to be able to bring in a
                # player the season-level numbers say cannot play at all.
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

    # Left join, not inner: a player nobody expects to feature still belongs in
    # the table on zero points. Dropping him would silently hide every summer
    # signing with no Premier League minutes, which is exactly the group the
    # user most needs to see in order to override him.
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

    # Flag players the model cannot see: priced as though they will play, but
    # with too little Premier League history to project from. A share of the
    # season rather than a fixed 450 minutes, so the list still means the same
    # thing in September as it does in May.
    summary["needs_override"] = ((summary["minutes"] < BLINDSPOT_SHARE * basis.fpl_minutes)
                                 & (summary["price"] >= 5.0))

    # How much of each club's expected goals the model actually manages to
    # attribute to somebody. A squad full of players with no Premier League
    # history -- a promoted club, or one that rebuilt over the summer -- has
    # goals the model knows the team will score but cannot assign, so its
    # attackers look collectively cheaper than they are. This does not distort
    # any individual player's projection, but it tells you where the pool is
    # incomplete and an override would earn its keep.
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
    # Named distinctly: `coverage` already means odds coverage in this function.
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
    )


def reproject_player(projection: Projection, fpl_id: int,
                     overrides: dict[str, Any]) -> dict:
    """Recompute one player's points after editing his inputs.

    Re-running the whole pipeline to change one number takes seconds and refetches
    nothing useful; every other player's answer is unchanged. This walks the same
    `_player_fixture_points` the full run uses, so an edited player is scored by
    exactly the same code as an unedited one -- the alternative, a simplified
    "quick" path, is how a UI and its model quietly drift apart.

    `overrides` may carry a `gw` key mapping a gameweek to its own fields, which
    are layered on top of the player-level ones for that fixture only.
    """
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
        # The minutes family is derived, never typed, so it is absent from
        # `inputs` -- and p_play is what the optimiser filters its pool on.
        "derived": {k: (None if pd.isna(player.get(k)) else float(player[k]))
                    for k in ("p_sub", "p_play", "p60", "exp_minutes")
                    if k in player.index},
        "overridden": str(player.get("overridden", "")),
    }

"""Static configuration: scoring rules, squad rules, model constants, paths."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache"
OUT_DIR = ROOT / "out"

load_dotenv(ROOT / ".env")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "uk").strip()
UNDERSTAT_SEASON = os.getenv("UNDERSTAT_SEASON", "2025").strip()

# --- Asking questions about a projection --------------------------------------
# Any OpenAI-compatible /chat/completions endpoint, because every free option
# worth using speaks that shape and pinning the tool to one vendor would age
# badly. The default is a local Ollama, which is the only arrangement that is
# free in the sense that matters -- no key, no account, no quota, and nothing
# about your squad leaving the machine. Set the three variables to point at a
# hosted free tier instead; see .env.example.
#
# The key is read here and used only in the server process. It is never put in
# the snapshot and never reaches the browser.
def _env(name: str, default: str) -> str:
    """Environment value, treating an empty variable as absent.

    `.env.example` ships these keys blank so they are visible and documented,
    and a blank in a dotenv file is a set-but-empty variable rather than an
    unset one -- which would otherwise override every default below with "".
    """
    return (os.getenv(name) or "").strip() or default


AI_BASE_URL = _env("FPL_AI_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
AI_MODEL = _env("FPL_AI_MODEL", "llama3.1:8b")
AI_API_KEY = _env("FPL_AI_KEY", "")
# A projection is a lot of numbers and an explanation is a paragraph. Capping the
# response keeps a local model from rambling and a hosted free tier from burning
# its quota on one question.
AI_MAX_TOKENS = int(_env("FPL_AI_MAX_TOKENS", "700"))
AI_TIMEOUT = float(_env("FPL_AI_TIMEOUT", "120"))

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# --- FPL scoring (2025/26 ruleset, incl. defensive contribution) ---------------
GOAL_POINTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3
APPEARANCE_POINTS = 1  # for any minutes
APPEARANCE_60_POINTS = 1  # additional point at 60+ minutes
GOALS_CONCEDED_PENALTY = -1  # per 2 conceded, GKP/DEF only
SAVE_POINTS = 1  # per 3 saves, GKP only
YELLOW_CARD_POINTS = -1
RED_CARD_POINTS = -3
PENALTY_MISS_POINTS = -2

# Defensive contribution: 2 pts once the per-match count clears the threshold.
DEF_CONTRIB_POINTS = 2
DEF_CONTRIB_THRESHOLD = {"GKP": None, "DEF": 10, "MID": 12, "FWD": 12}

# Defensive contribution is the only threshold in the scoring rules, and a
# threshold pays the tail rather than the mean. Modelled as Poisson off shrunk
# rates it produced 36 points a gameweek against the 61 the league actually paid
# (measured over 8,631 player-gameweeks). Widening the count distribution to
# Var = 2.25 x mean reproduces that total. See poisson.prob_at_least for why
# this is larger than the 1.34 dispersion the raw counts show.
DC_DISPERSION = 2.25

# --- Squad rules --------------------------------------------------------------
SQUAD_SIZE = 15
SQUAD_BY_POS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_SIZE = 11
XI_MIN_BY_POS = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX_BY_POS = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}
MAX_PER_CLUB = 3
DEFAULT_BUDGET = 100.0

# --- Transfer rules -----------------------------------------------------------
# One free transfer a gameweek, banked up to five. The cap is `1 +
# max_extra_free_transfers` in the API's game_settings block, and has been four
# extra since 2024/25. Everything past the free allowance costs four points.
FREE_TRANSFERS_PER_GW = 1
MAX_FREE_TRANSFERS = 5
HIT_COST = 4.0

# Playing a wildcard or a free hit does not earn you that gameweek's free
# transfer, but since 2024/25 it no longer burns the ones you had banked. Both
# halves of that matter to the accounting, so they are named rather than folded
# into a constant: the chip cancels the +1, and nothing else.
CHIP_KEEPS_BANKED_TRANSFERS = True

# 50% of any profit is taken back when you sell, rounded down to £0.1m
# (`transfers_sell_on_fee`). Only bites on a player who has risen since you
# bought him, which is why a plan built before a ball is kicked never sees it.
SELL_ON_FEE = 0.5

# --- What a transfer is worth -------------------------------------------------
# A banked free transfer is worth points you have not scored yet, and a plan
# that does not say so will always spend it: holding is free, so the solver
# takes any gain above zero. These numbers put a price on the option.
#
# The shape is the community solver's, and the shape is the load-bearing part:
# the first transfer you bank is worth most, the fifth barely anything, because
# the cap at five means the fifth can only ever be used by a week in which you
# also use the other four. Values in points.
FT_VALUE = 1.5  # marginal value of a banked transfer, beyond the tabulated ones
FT_VALUE_BY_STATE = {2: 2.0, 3: 1.6, 4: 1.3, 5: 1.1}

# A flat charge on every transfer actually made, on top of the free-transfer
# accounting. Without it the solver books a move whenever the projection edges
# ahead by 0.01 points -- a difference well inside the model's own error, and
# the mechanism behind the schedules that used to swap a player out and back in.
# It buys nothing except a refusal to act on noise, which is the whole point.
TRANSFER_FRICTION = 0.2

# Points per £1m left in the bank. Cash is not scored, but it is the option to
# take a price rise or to fix an injury without a hit, and a solver that values
# it at zero will spend the squad down to the last £0.1m every time.
BANK_VALUE = 0.08

# Vice-captain, weighted by roughly how often the armband falls through.
VICE_CAPTAIN_WEIGHT = 0.1

# --- Chips --------------------------------------------------------------------
# The API's own names, which is what `chips` in bootstrap-static uses and what
# the windows come back keyed by.
CHIPS = ("wildcard", "freehit", "bboost", "3xc")
CHIP_LABELS = {"wildcard": "Wildcard", "freehit": "Free Hit",
               "bboost": "Bench Boost", "3xc": "Triple Captain"}
TRIPLE_CAPTAIN_MULTIPLIER = 3

# One chip per gameweek, and each chip once per half-season. Both come from the
# rules rather than from taste, so they are constraints, not preferences.
MAX_CHIPS_PER_GW = 1

# What a chip is worth if you hold it instead of playing it now.
#
# This is the single most important number in the chip model, because without
# it a chip left unplayed at the end of the planning window is worth exactly
# zero and the plan burns all four in the first four gameweeks. The window is
# six gameweeks; the chip's window is nineteen.
#
# So each chip carries a reservation price: play it now only if this gameweek
# beats what the chip is worth well-timed later. The numbers are the published
# aggregates for a chip played on a double or a blank -- the gameweeks the
# fixture list does not show until cup rounds are drawn and games are
# postponed, which is the whole reason holding is worth anything.
#
#   bboost   a double-gameweek bench boost returns 15-25 against 8-12 in an
#            ordinary week; 14 is the low end of the case for waiting.
#   3xc      the third captain multiple, on a premium attacker with two games.
#   freehit  fielding eleven players in a blank instead of the five or six you
#            would otherwise have.
#   wildcard a full restructure at the season's largest fixture swing, which is
#            worth more than any single gameweek's points.
#
# They are estimates and they are knobs. Set them to zero to ask the narrower
# question "when in this window is each chip best?", which is what the plan
# answered before it could weigh holding.
CHIP_HOLD_VALUE = {"bboost": 14.0, "3xc": 10.0, "freehit": 12.0, "wildcard": 15.0}

# --- Model constants ----------------------------------------------------------
# Share of a team's goals that come from penalties, league-wide.
PENALTY_GOAL_SHARE = 0.09
PENALTY_CONVERSION = 0.79

# Home advantage as a multiplier on expected goals, used only when we have no
# bookmaker odds for a fixture and must fall back to xG-derived team ratings.
HOME_ADVANTAGE = 1.12
LEAGUE_MEAN_GOALS = 1.42  # per team per match

# Minutes model.
ASSUMED_START_MINUTES = 78.0  # average minutes played by a player who starts
ASSUMED_SUB_MINUTES = 22.0  # average minutes played by a player off the bench
P60_GIVEN_START = 0.87  # a starter reaches 60 minutes this often
MATCHES_PER_SEASON = 38

# Bench players are worth something but rarely play; weight them down in the
# optimiser objective so the solver does not overpay for a strong bench.
DEFAULT_BENCH_WEIGHT = 0.12

# Bench slots are not interchangeable. FPL auto-subs work down the bench in
# order, so the first outfield substitute comes on whenever any starter blanks
# -- often enough to be worth paying for -- while the third is close to
# decoration. The reserve keeper only ever plays if the first-choice keeper
# doesn't, which is rare.
#
# These are *relative* weights; the `bench_weight` knob scales the whole
# profile, so at the default 0.12 they work out at roughly 0.03 / 0.24 / 0.10 /
# 0.04 -- about how often each slot actually returns points.
BENCH_SLOT_PROFILE = {"GKP": 0.25, 1: 2.0, 2: 0.85, 3: 0.35}

# The same thing expressed absolutely: what each slot is worth once the default
# `bench_weight` has scaled the profile. This is what the board's per-slot
# sliders start at, because a slider is only meaningful if the number it shows
# is the number the objective uses -- a "relative profile" slider would move a
# weight the user cannot see.
# DEFAULT_BENCH_WEIGHT x BENCH_SLOT_PROFILE, snapped to the board's slider step
# so the four sliders open on values they can actually represent.
DEFAULT_BENCH_SLOT_WEIGHTS = {"GKP": 0.03, 1: 0.24, 2: 0.10, 3: 0.04}

# JSON has no integer keys, so the wire format names the slots.
BENCH_SLOT_KEYS = {"GKP": "GKP", "1": 1, "2": 2, "3": 3}

STATUS_AVAILABILITY = {
    "a": 1.00,  # available
    "d": None,  # doubtful -> use chance_of_playing_next_round
    "i": 0.00,  # injured
    "s": 0.00,  # suspended
    "u": 0.00,  # unavailable / left the league
    "n": 0.00,  # not in squad
}

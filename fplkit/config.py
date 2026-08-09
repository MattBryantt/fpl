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

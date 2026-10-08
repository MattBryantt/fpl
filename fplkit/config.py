
from __future__ import annotations

import math
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache"
OUT_DIR = ROOT / "out"

load_dotenv(ROOT / ".env")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "uk").strip()

UNDERSTAT_SEASON = os.getenv("UNDERSTAT_SEASON", "").strip() or None

def _env(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


AI_BASE_URL = _env("FPL_AI_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
AI_MODEL = _env("FPL_AI_MODEL", "llama3.1:8b")
AI_API_KEY = _env("FPL_AI_KEY", "")
AI_MAX_TOKENS = int(_env("FPL_AI_MAX_TOKENS", "700"))
AI_TIMEOUT = float(_env("FPL_AI_TIMEOUT", "120"))

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

GOAL_POINTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3
APPEARANCE_POINTS = 1
APPEARANCE_60_POINTS = 1
GOALS_CONCEDED_PENALTY = -1
SAVE_POINTS = 1
YELLOW_CARD_POINTS = -1
RED_CARD_POINTS = -3
PENALTY_MISS_POINTS = -2

DEF_CONTRIB_POINTS = 2
DEF_CONTRIB_THRESHOLD = {"GKP": None, "DEF": 10, "MID": 12, "FWD": 12}

DC_DISPERSION = 2.25

SQUAD_SIZE = 15
SQUAD_BY_POS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_SIZE = 11
XI_MIN_BY_POS = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX_BY_POS = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}
MAX_PER_CLUB = 3
DEFAULT_BUDGET = 100.0

FREE_TRANSFERS_PER_GW = 1
MAX_FREE_TRANSFERS = 5
HIT_COST = 4.0

CHIP_KEEPS_BANKED_TRANSFERS = True

SELL_ON_FEE = 0.5

FT_VALUE = 1.5
FT_VALUE_BY_STATE = {2: 2.0, 3: 1.6, 4: 1.3, 5: 1.1}

TRANSFER_FRICTION = 0.2

BANK_VALUE = 0.08

VICE_CAPTAIN_WEIGHT = 0.1

CHIPS = ("freehit", "bboost", "3xc")
CHIP_LABELS = {"freehit": "Free Hit",
               "bboost": "Bench Boost", "3xc": "Triple Captain"}
TRIPLE_CAPTAIN_MULTIPLIER = 3

MAX_CHIPS_PER_GW = 1

CHIP_HOLD_VALUE = {"bboost": 14.0, "3xc": 10.0, "freehit": 12.0}

PENALTY_GOAL_SHARE = 0.09
PENALTY_CONVERSION = 0.79

HOME_ADVANTAGE = 1.12
LEAGUE_MEAN_GOALS = 1.42

ASSUMED_START_MINUTES = 78.0
ASSUMED_SUB_MINUTES = 22.0
P60_GIVEN_START = 0.87
MATCHES_PER_SEASON = 38

P60_MIDPOINT_MINUTES = 60.0
P60_SLOPE_MINUTES = ((ASSUMED_START_MINUTES - P60_MIDPOINT_MINUTES)
                     / math.log(P60_GIVEN_START / (1.0 - P60_GIVEN_START)))

DEFAULT_BENCH_WEIGHT = 0.12

BENCH_SLOT_PROFILE = {"GKP": 0.25, 1: 2.0, 2: 0.85, 3: 0.35}

DEFAULT_BENCH_SLOT_WEIGHTS = {"GKP": 0.03, 1: 0.24, 2: 0.10, 3: 0.04}

BENCH_SLOT_KEYS = {"GKP": "GKP", "1": 1, "2": 2, "3": 3}

STATUS_AVAILABILITY = {
    "a": 1.00,
    "d": None,
    "i": 0.00,
    "s": 0.00,
    "u": 0.00,
    "n": 0.00,
}

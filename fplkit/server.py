"""Local web server for the drafting UI.

The split between server and browser is deliberate. The server does the two
things only Python can do -- run the projection pipeline and solve the squad
MILP -- and hands the browser a complete pool of players with their per-gameweek
points already computed. Everything the draft interaction touches (adding a
player, checking the club limit, recomputing the best XI, redrawing the
timeline) then happens client-side with no round trip, so the UI responds
instantly instead of waiting on a 2-second projection for every click.
"""

from __future__ import annotations

import json
import math
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import cache, config, snapshot
from .config import (
    BENCH_SLOT_KEYS,
    DEFAULT_BENCH_SLOT_WEIGHTS,
    DEFAULT_BENCH_WEIGHT,
    DEFAULT_BUDGET,
    MAX_PER_CLUB,
    ODDS_API_KEY,
    ODDS_REGIONS,
    OUT_DIR,
    ROOT,
    SQUAD_BY_POS,
    XI_MAX_BY_POS,
    UNDERSTAT_SEASON,
    XI_MIN_BY_POS,
)
from .model import OVERRIDABLE, apply_overrides, project, reproject_player
from .planning import (DEFAULT_HALF_LIFE, apply_plan_weighting, decay_weights,
                       injury_hazard, price_forecast, weighted_points)
from .optimise import optimise
from .sources import fpl_api, history, understat

app = FastAPI(title="FPL drafting board")

# Set by serve() when the board is reachable from anywhere but this machine.
# None means no check, which is only the default because the default bind is
# loopback -- see serve().
AUTH_TOKEN: str | None = None

# Paths that must stay open for a browser to bootstrap itself: the page has to
# load before its script can attach the token header, and it reads the token
# from the query string on that first request. Its assets and service worker are
# in the same position, and none of them are data. /snapshot.json *is* the data,
# so it stays behind the check.
# Club shirts are open for a second reason on top of not being data: an <img>
# cannot carry the token header the way fetch() does, so gating them would leave
# an authorised board with an empty pitch.
OPEN_PATHS = {"/", "/data", "/icon.png", "/sw.js", "/manifest.webmanifest"}
OPEN_PREFIXES = ("/assets/", "/shirts/")


@app.middleware("http")
async def require_token(request: Request, call_next):
    """Gate every endpoint behind a shared token when one is configured.

    The board has write endpoints -- it saves drafts, writes an overrides CSV to
    disk and can force a refetch that spends Odds API quota -- so putting it on a
    network without a check in front would let anyone who finds the port use all
    of that. The token is checked on the header the app sends, or on a query
    parameter so a link or QR code can carry it on the first load.
    """
    if AUTH_TOKEN is None:
        return await call_next(request)

    supplied = (request.headers.get("x-fpl-token")
                or request.query_params.get("t", ""))
    if secrets.compare_digest(supplied, AUTH_TOKEN):
        return await call_next(request)

    if (request.url.path in OPEN_PATHS
            or request.url.path.startswith(OPEN_PREFIXES)):
        # Send the page, but with nothing in it -- the script cannot run without
        # a token anyway, and a bare 401 in a browser is a dead end.
        return HTMLResponse(UNAUTHORISED_PAGE, status_code=401)
    return JSONResponse({"detail": "missing or invalid token"}, status_code=401)


UNAUTHORISED_PAGE = """<!doctype html><meta charset=utf-8>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FPL drafting board — token required</title>
<style>body{font:15px/1.6 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1.5rem;
color:#0b0b0b;background:#f9f9f7}code{background:#f0efec;padding:1px 5px;border-radius:4px}
h1{font-size:20px}form{display:flex;gap:8px;margin:18px 0 8px}
input{flex:1;font:16px system-ui;padding:11px 12px;border:1px solid #0002;border-radius:8px;
background:#fff;color:inherit}
button{font:15px system-ui;padding:11px 18px;border:0;border-radius:8px;background:#2a78d6;
color:#fff;font-weight:500}
@media(prefers-color-scheme:dark){body{background:#0d0d0d;color:#fff}code{background:#26261f}
input{background:#1a1a19;border-color:#fff3}}</style>
<h1>Token required</h1>
<p>Paste the token and this will let you in:</p>
<form onsubmit="event.preventDefault();
  var t=document.getElementById('t').value.trim();
  if(t) location.href = location.pathname + '?t=' + encodeURIComponent(t);">
  <input id="t" placeholder="token" autocapitalize="off" autocorrect="off" spellcheck="false">
  <button type="submit">Open</button>
</form>
<p>The full link looks like <code>https://&lt;host&gt;/?t=&lt;token&gt;</code>. If a chat app
mangled it, the token is the part after <code>?t=</code> — paste just that above.</p>
<p>It is printed in the terminal running <code>fpl.py serve</code>.</p>"""

WEB_DIR = ROOT / "fplkit" / "web"
OVERRIDES_PATH = OUT_DIR / "overrides.csv"
DRAFTS_PATH = OUT_DIR / "drafts.json"

# Projections are expensive (a couple of seconds) and pure, so they are cached
# for the life of the process. The UI changes horizon and half-life freely.
_projection_cache: dict[tuple[int | None, int, float], Any] = {}
_pool_cache: dict[tuple[int | None, int, float | None, float], dict] = {}


def _get_projection(start_gw: int | None, horizon: int, recency: float = 0.0):
    key = (start_gw, horizon, recency)
    if key not in _projection_cache:
        _projection_cache[key] = project(horizon=horizon, start_gw=start_gw,
                                         recency_half_life=recency or None)
    return _projection_cache[key]


def _clean(value: Any) -> Any:
    """JSON has no NaN or numpy scalars; strip both."""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else round(number, 4)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str):
        return value
    return value


def _build_pool(start_gw: int | None, horizon: int, half_life: float | None,
                recency: float = 0.0) -> dict:
    key = (start_gw, horizon, half_life, recency)
    if key in _pool_cache:
        return _pool_cache[key]

    projection = _get_projection(start_gw, horizon, recency)
    players = apply_plan_weighting(projection, half_life)
    players = price_forecast(players, len(projection.horizon), fpl_api.total_managers())
    raw, weighted = weighted_points(projection, half_life)
    gameweeks = [int(gw) for gw in raw.columns]

    # Opponent labels per player per gameweek, for tooltips. A double gameweek
    # yields two, joined.
    fixtures = projection.per_fixture.copy()
    fixtures["label"] = np.where(fixtures["was_home"],
                                 fixtures["opponent"] + " (H)",
                                 fixtures["opponent"] + " (A)")
    opponents = (fixtures.groupby(["fpl_id", "gw"])["label"]
                 .agg(lambda s: " + ".join(s)).unstack())
    opponents = opponents.reindex(columns=gameweeks)

    rows = []
    for _, player in players.iterrows():
        fpl_id = int(player["fpl_id"])
        if fpl_id in raw.index:
            per_gw = [round(float(v), 3) for v in raw.loc[fpl_id, gameweeks]]
            labels = [("" if pd.isna(v) else str(v))
                      for v in opponents.loc[fpl_id, gameweeks]] \
                if fpl_id in opponents.index else ["" for _ in gameweeks]
        else:
            per_gw = [0.0 for _ in gameweeks]
            labels = ["" for _ in gameweeks]

        rows.append({
            "id": fpl_id,
            "name": str(player["web_name"]),
            "full_name": str(player["full_name"]),
            "pos": str(player["pos"]),
            "team": str(player["team"]),
            "team_short": str(player["team_short"]),
            "price": _clean(player["price"]),
            "xpts_plan": _clean(player["xpts_plan"]),
            "xpts_raw": _clean(player["xpts_raw"]),
            "p_start": _clean(player["p_start"]),
            "owned": _clean(pd.to_numeric(player["selected_by_percent"], errors="coerce")),
            "price_change": _clean(player.get("exp_price_change")),
            "confidence": str(player.get("confidence", "")),
            "cs": _clean(player.get("exp_clean_sheets")),
            "recency": _clean(player.get("recency")),
            "moved": bool(player.get("moved_club", False)),
            "previous_club": str(player.get("previous_club", "") or ""),
            "status": str(player["status"]),
            "news": str(player["news"] or ""),
            "gw": per_gw,
            "opp": labels,
            # The model inputs a user may disagree with, so the editor can open
            # showing what the model currently believes without a round trip.
            "inputs": {field: _clean(player[field])
                       for field in OVERRIDABLE if field in player.index},
        })

    payload = {
        "gameweeks": gameweeks,
        "players": rows,
        "meta": {
            "start_gw": gameweeks[0],
            "horizon": horizon,
            "half_life": half_life,
            "odds_coverage": round(projection.odds_coverage, 3),
            "odds_note": projection.odds_note,
            "budget": DEFAULT_BUDGET,
            "max_per_club": MAX_PER_CLUB,
            "squad_by_pos": SQUAD_BY_POS,
            "xi_min": XI_MIN_BY_POS,
            "xi_max": XI_MAX_BY_POS,
            "bench_slot_weights": {str(slot): weight for slot, weight
                                   in DEFAULT_BENCH_SLOT_WEIGHTS.items()},
            "priced_gws": [
                int(gw) for gw in gameweeks
                if bool(projection.fixtures.loc[projection.fixtures["gw"] == gw,
                                                "has_odds"].any())
            ],
        },
    }
    _pool_cache[key] = payload
    return payload


def _plan_weight_player(projection, half_life: float | None, fpl_id: int,
                        per_gw: list[float]) -> float:
    """Apply the same decay and survival curve a full plan run would."""
    players = projection.players
    row = players[players["fpl_id"] == fpl_id]
    hazard = float(injury_hazard(row).iloc[0])
    decay = decay_weights(projection.horizon, half_life).to_numpy()
    survival = (1 - hazard) ** np.arange(len(projection.horizon))
    return float(np.sum(np.asarray(per_gw) * decay * survival))


def _apply_edits(projection, half_life: float | None,
                 edits: dict[int, dict[str, float]]) -> pd.DataFrame:
    """Plan-weighted player table with the user's edited players patched in.

    Only edited players are recomputed, so the optimiser sees the same numbers
    the browser is showing without paying for a full re-projection.

    The inputs go through `apply_overrides` rather than being written column by
    column. That matters for more than tidiness: several of the fields are not
    independent. Raising p_start also raises p_play, p60 and exp_minutes, and
    the optimiser filters its pool on p_play -- so patching p_start alone left
    an edited fringe player with a big xpts_plan and a stale p_play, and the
    solver dropped him at `min_minutes_prob` no matter how far the slider moved.
    """
    players = apply_plan_weighting(projection, half_life)
    live = {int(i): f for i, f in edits.items() if f}
    if not live:
        return players

    # Season-level fields go through the table path; the per-match ones are
    # applied per fixture inside reproject_player, so they must not travel in
    # this frame -- a dict in a DataFrame cell is not a number.
    season = [{"fpl_id": i, **{k: v for k, v in f.items() if k != "gw"}}
              for i, f in live.items()]
    season = [row for row in season if len(row) > 1]
    if season:
        players = apply_overrides(players, pd.DataFrame(season))

    players = players.set_index("fpl_id", drop=False)
    for fpl_id, fields in live.items():
        if fpl_id not in players.index:
            continue
        result = reproject_player(projection, fpl_id, fields)
        players.loc[fpl_id, "xpts"] = result["xpts"]
        players.loc[fpl_id, "xpts_plan"] = _plan_weight_player(
            projection, half_life, fpl_id, result["gw"])
    players["xpts_plan_per_m"] = players["xpts_plan"] / players["price"]
    return players.reset_index(drop=True)


def _check_overridable(fields) -> None:
    """Reject field names the model does not know, rather than ignoring them.

    `apply_overrides` skips anything it does not recognise, so a typo in a
    saved CSV or a stale field name would otherwise read as "the override did
    nothing" with no way to tell that from "the override had no effect".

    `gw` is the one reserved key: it holds per-match overrides, whose fields are
    checked the same way one level down.
    """
    known = set(OVERRIDABLE) | {f"{f}_mult" for f in OVERRIDABLE}
    unknown = set(fields) - known - {"gw"}
    if unknown:
        raise HTTPException(422, f"not overridable: {', '.join(sorted(unknown))}")
    for gameweek, per_match in (fields.get("gw") or {}).items():
        bad = set(per_match) - known
        if bad:
            raise HTTPException(
                422, f"not overridable in GW{gameweek}: {', '.join(sorted(bad))}")


class PoolRequest(BaseModel):
    horizon: int = Field(8, ge=1, le=20)
    # null means no decay -- every gameweek in the horizon at full value. JSON
    # has no infinity, and the board's slider has that setting at its top stop.
    # There is no upper bound below that: the slider's last few positions work
    # out to half-lives of dozens of gameweeks, which is the whole point of
    # having them, and a large half-life is only ever a gentler discount.
    half_life: Annotated[float, Field(gt=0)] | None = DEFAULT_HALF_LIFE
    start_gw: int | None = None
    recency: float = Field(0.0, ge=0, le=38)


class OptimiseRequest(PoolRequest):
    budget: float = Field(DEFAULT_BUDGET, gt=0, le=200)
    bench_weight: float = Field(DEFAULT_BENCH_WEIGHT, ge=0, le=1)
    # Absolute weight per bench slot, keyed "GKP"/"1"/"2"/"3". Supersedes
    # bench_weight when given; omit to keep the scaled default profile.
    bench_slot_weights: dict[str, float] | None = None
    ownership_weight: float = Field(0.0, ge=0, le=2)
    min_start: float = Field(0.3, ge=0, le=1)
    include: list[int] = []
    exclude: list[int] = []
    edits: dict[int, dict[str, Any]] = {}
    max_per_club: int = Field(MAX_PER_CLUB, ge=1, le=15)
    formation: dict[str, int] | None = None


class EditRequest(PoolRequest):
    fpl_id: int
    # Values are floats, except the reserved `gw` key, whose value maps a
    # gameweek to its own field dict.
    overrides: dict[str, Any] = {}


class SaveRequest(BaseModel):
    edits: dict[int, dict[str, Any]] = {}
    path: str | None = None


class DraftRequest(BaseModel):
    name: str
    squad: list[int] = []
    notes: str = ""
    context: dict[str, Any] = {}
    saved_at: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/pool")
def pool(horizon: int = 8, half_life: float | None = DEFAULT_HALF_LIFE,
         start_gw: int | None = None, recency: float = 0.0) -> dict:
    request = PoolRequest(horizon=horizon, half_life=half_life,
                          start_gw=start_gw, recency=recency)
    return _build_pool(request.start_gw, request.horizon, request.half_life,
                       request.recency)


@app.post("/api/edit")
def edit_player(request: EditRequest) -> dict:
    """Recompute one player after changing his inputs."""
    projection = _get_projection(request.start_gw, request.horizon, request.recency)
    _check_overridable(request.overrides)
    try:
        result = reproject_player(projection, request.fpl_id, request.overrides)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    result["xpts_plan"] = round(_plan_weight_player(
        projection, request.half_life, request.fpl_id, result["gw"]), 4)
    return result


# --------------------------------------------------------------------------- #
# Asking questions about a player
# --------------------------------------------------------------------------- #

# What each number in the dossier means, sent alongside it. A language model
# handed `xpts_defcon: 2.1` has no way to know that is a threshold term rather
# than a rate, and will confidently say something wrong about it. Explaining the
# schema is most of what keeps the answers tied to the model.
DOSSIER_GUIDE = """
Every number you are given comes from the projection itself. Field meanings:

- points_by_source: expected points over the horizon, split by scoring rule,
  BEFORE the plan discount. These sum to xpts_raw.
- xpts_raw: total expected points over the horizon, undiscounted.
- xpts_plan: what the board ranks on. Later gameweeks are halved every
  `half_life` gameweeks of distance and multiplied by the player's chance of
  still being available, so it is always lower than xpts_raw. A null half_life
  means the user has turned fixture decay off: every gameweek in the horizon
  counts its full value, and only the availability curve separates the two
  totals.
- xppg: xpts_raw divided by his club's fixtures in the horizon. This is the
  number to compare between two players on different fixture runs.
- p_start: probability he starts a given match; the single biggest lever.
- npxg_per90 / xa_per90: NON-penalty expected goals and expected assists per 90
  minutes. Penalties are modelled separately and only credited to the
  designated taker (penalties_order = 1).
- dc_per90: defensive contribution per 90. It pays 2 points only when the
  per-match count clears a threshold (10 for defenders, 12 otherwise), so it is
  a tail event, not a rate.
- position_comparison: where he sits among players in the same position.
  `position_median_by_source` is the median of each component taken separately,
  so those medians do not add up to `position_median_xpts_plan` and must not be
  summed. Compare one component against its own median, never the set.
- per_fixture.lambda_source: "odds" means bookmakers have priced that fixture,
  "xg" means it falls back to team ratings and is a weaker number.
- confidence / rate_source: how much history stands behind the rates. "moved"
  means his numbers were produced at a different club and are adjusted for it.
"""

ASK_SYSTEM = """You explain a Fantasy Premier League projection to the person \
who built it. You are given a JSON dossier for one player, produced by their \
own model.

Rules, in order of importance:
1. Use ONLY the numbers in the dossier. Never invent a statistic, a fixture, a \
price, an injury or a piece of news. You have no knowledge of real-world events \
beyond what is in the JSON.
2. If the dossier cannot answer the question, say exactly that, and say what \
would answer it. Do not guess.
3. Quote the numbers you are reasoning from. "He is on 6.2 expected points a \
fixture, and 4.1 of that is goals" beats "he scores a lot".
4. Be concrete and brief -- a short paragraph, or a few bullets. No preamble, \
no restating the question.
5. This is a model's opinion, not a fact. Where the dossier flags thin evidence \
(low confidence, a club change, few minutes), say so rather than reporting the \
projection as certain.
"""


_AI_PROBE: tuple[float, dict] | None = None
AI_PROBE_TTL = 30.0


def _ai_status() -> dict:
    """Whether a question can actually be answered, and by what.

    Probed rather than assumed. Offering an ask box that turns out to have
    nothing behind it wastes the one interaction the user came for, and the
    common case -- the default local Ollama, not installed -- is exactly the one
    a configuration check alone would call available.

    Cached briefly, because the drawer asks this every time it opens and a dead
    endpoint costs a connection timeout to discover.
    """
    global _AI_PROBE
    now = time.monotonic()
    if _AI_PROBE and now - _AI_PROBE[0] < AI_PROBE_TTL:
        return _AI_PROBE[1]

    hosted = bool(config.AI_API_KEY)
    status = {
        "model": config.AI_MODEL,
        "base_url": config.AI_BASE_URL,
        "hosted": hosted,
        # Named so the UI can be honest about where the numbers go. A local
        # Ollama keeps them on the machine; anything with a key does not.
        "local": not hosted and ("127.0.0.1" in config.AI_BASE_URL
                                 or "localhost" in config.AI_BASE_URL),
    }
    headers = {"Authorization": f"Bearer {config.AI_API_KEY}"} if hosted else {}
    try:
        response = requests.get(f"{config.AI_BASE_URL}/models",
                                headers=headers, timeout=2.5)
        status["available"] = response.ok
        if not response.ok:
            status["reason"] = f"the endpoint answered {response.status_code}"
    except requests.exceptions.RequestException:
        status["available"] = False
        status["reason"] = f"nothing reachable at {config.AI_BASE_URL}"

    _AI_PROBE = (now, status)
    return status


def _round(value: Any, dp: int = 3) -> Any:
    cleaned = _clean(value)
    return round(cleaned, dp) if isinstance(cleaned, (int, float)) else cleaned


def _player_dossier(projection, fpl_id: int, half_life: float | None,
                    edits: dict[str, Any] | None) -> dict:
    """Everything the model knows about one player, as plain JSON.

    Built from `reproject_player`, which is the same code path the projection
    and the browser both score with -- so an answer cannot be grounded in
    numbers that disagree with the ones on screen. That is the whole point of
    routing this through the model rather than handing a chat endpoint a CSV.
    """
    players = apply_plan_weighting(projection, half_life)
    row = players[players["fpl_id"] == fpl_id]
    if row.empty:
        raise HTTPException(404, f"no player with id {fpl_id}")
    player = row.iloc[0]

    result = reproject_player(projection, fpl_id, edits or {})
    plan = _plan_weight_player(projection, half_life, fpl_id, result["gw"])

    per_fixture = projection.per_fixture
    mine = per_fixture[per_fixture["fpl_id"] == fpl_id]
    fixtures = [
        {
            "gw": int(f["gw"]),
            "opponent": str(f["opponent"]),
            "at_home": bool(f["was_home"]),
            "points": _round(f["xpts"], 2),
            "team_expected_goals": _round(f["lam_for"], 2),
            "opponent_expected_goals": _round(f["lam_against"], 2),
            "lambda_source": str(f["lam_source"]),
        }
        for _, f in mine.sort_values("gw").iterrows()
    ]

    sources = {
        "appearance": "xpts_appearance", "goals": "xpts_goals",
        "assists": "xpts_assists", "clean_sheets": "xpts_clean_sheet",
        "defensive_contribution": "xpts_defcon", "bonus": "xpts_bonus",
        "saves": "xpts_saves", "goals_conceded": "xpts_conceded",
        "cards_and_penalty_misses": "xpts_cards",
    }
    by_source = {label: _round(result["breakdown"].get(column, 0.0), 2)
                 for label, column in sources.items()}

    # Where he sits among his own position, which is what "rated highly" means.
    peers = players[players["pos"] == player["pos"]]
    ranked = peers.sort_values("xpts_plan", ascending=False).reset_index(drop=True)
    rank = int(ranked.index[ranked["fpl_id"] == fpl_id][0]) + 1 if fpl_id in set(
        ranked["fpl_id"]) else None
    comparison = {
        "position": str(player["pos"]),
        "rank_by_xpts_plan": rank,
        "of_players": int(len(peers)),
        "position_median_xpts_plan": _round(peers["xpts_plan"].median(), 2),
        "position_median_by_source": {
            label: _round(peers[column].median(), 2)
            for label, column in sources.items() if column in peers.columns
        },
    }

    games = int(player.get("n_fixtures") or len(fixtures) or 0)
    notes = []
    if bool(player.get("moved_club")):
        notes.append(f"His rates were produced at {player.get('previous_club')}, "
                     "adjusted for the new club and shrunk harder than his raw history.")
    if str(player.get("confidence")) in ("none", "very low", "low"):
        notes.append(f"Confidence is '{player.get('confidence')}' -- little Premier "
                     "League history stands behind these rates.")
    priced = sum(1 for f in fixtures if f["lambda_source"] == "odds")
    notes.append(f"{priced} of {len(fixtures)} fixtures are priced by bookmakers; "
                 "the rest use xG-derived team ratings.")
    if edits:
        notes.append("The user has overridden some of this player's inputs, and "
                     "these numbers already include those overrides.")

    return {
        "player": {
            "name": str(player["web_name"]),
            "full_name": str(player["full_name"]),
            "position": str(player["pos"]),
            "club": str(player["team"]),
            "price_millions": _round(player["price"], 1),
            "ownership_percent": _round(
                pd.to_numeric(player["selected_by_percent"], errors="coerce"), 1),
            "status": str(player["status"]),
            "news": str(player["news"] or ""),
            "confidence": str(player.get("confidence", "")),
            "rate_source": str(player.get("rate_source", "")),
            "moved_club": bool(player.get("moved_club", False)),
            "previous_club": str(player.get("previous_club", "") or ""),
            "minutes_last_season": _round(player.get("minutes"), 0),
            "points_per_game_last_season": _round(player.get("points_per_game"), 2),
        },
        "projection": {
            "gameweeks": [int(g) for g in projection.horizon],
            "fixtures_in_horizon": games,
            "half_life": half_life,
            "xpts_raw": _round(result["xpts"], 2),
            "xpts_plan": _round(plan, 2),
            "xppg": _round(result["xpts"] / games, 2) if games else None,
            "expected_goals": _round(result["breakdown"].get("exp_goals"), 2),
            "expected_assists": _round(result["breakdown"].get("exp_assists"), 2),
            "expected_clean_sheets": _round(
                result["breakdown"].get("exp_clean_sheets"), 2),
        },
        "points_by_source": by_source,
        "per_fixture": fixtures,
        "inputs": {k: _round(v, 4) for k, v in result["inputs"].items()},
        "derived_minutes": {k: _round(v, 4) for k, v in result["derived"].items()},
        "position_comparison": comparison,
        "model_notes": notes,
    }


def _ask_model(messages: list[dict]) -> str:
    """Send a grounded conversation to whatever OpenAI-compatible endpoint is set."""
    headers = {"Content-Type": "application/json"}
    if config.AI_API_KEY:
        headers["Authorization"] = f"Bearer {config.AI_API_KEY}"

    try:
        response = requests.post(
            f"{config.AI_BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": config.AI_MODEL,
                "messages": messages,
                "temperature": 0.2,  # explaining arithmetic, not writing prose
                "max_tokens": config.AI_MAX_TOKENS,
                "stream": False,
            },
            timeout=config.AI_TIMEOUT,
        )
    except requests.exceptions.ConnectionError as error:
        local = "127.0.0.1" in config.AI_BASE_URL or "localhost" in config.AI_BASE_URL
        hint = ("Nothing is listening at "
                f"{config.AI_BASE_URL}. Start Ollama (`ollama serve`, then "
                f"`ollama pull {config.AI_MODEL}`), or point FPL_AI_BASE_URL at a "
                "hosted free tier -- see .env.example.") if local else (
                f"Could not reach {config.AI_BASE_URL}.")
        raise HTTPException(503, hint) from error
    except requests.exceptions.Timeout as error:
        raise HTTPException(504, f"{config.AI_MODEL} took longer than "
                                 f"{config.AI_TIMEOUT:.0f}s to answer.") from error

    if response.status_code in (401, 403):
        raise HTTPException(502, "The AI endpoint rejected the key (FPL_AI_KEY).")
    if response.status_code == 404:
        raise HTTPException(502, f"The endpoint has no model called "
                                 f"'{config.AI_MODEL}'. Set FPL_AI_MODEL to one it has.")
    if response.status_code == 429:
        raise HTTPException(502, "The AI endpoint's rate limit or free quota is used up.")
    if not response.ok:
        raise HTTPException(502, f"AI endpoint returned {response.status_code}: "
                                 f"{response.text[:200]}")

    try:
        payload = response.json()
        return payload["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError) as error:
        raise HTTPException(502, "Could not read the AI endpoint's reply.") from error


class AskRequest(PoolRequest):
    fpl_id: int
    question: str = Field(min_length=1, max_length=2000)
    # Prior turns, so a follow-up ("what about his fixtures?") has something to
    # follow. Capped because the dossier is re-sent every turn and the context
    # is the expensive part.
    history: list[dict] = Field(default_factory=list)
    edits: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/ai")
def ai_status() -> dict:
    """Whether the ask box should be offered, and what it would use."""
    return _ai_status()


@app.post("/api/ask")
def ask(request: AskRequest) -> dict:
    """Answer a question about one player, grounded in his own projection.

    The dossier is rebuilt server-side on every question rather than trusted
    from the client. A chat endpoint that will happily explain whatever numbers
    it is handed is a good way to produce a confident explanation of numbers the
    model never produced.
    """
    projection = _get_projection(request.start_gw, request.horizon, request.recency)
    dossier = _player_dossier(projection, request.fpl_id, request.half_life,
                              request.edits)

    messages = [{"role": "system", "content": ASK_SYSTEM + DOSSIER_GUIDE}]
    for turn in request.history[-6:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": str(turn["role"]),
                             "content": str(turn["content"])[:4000]})
    messages.append({
        "role": "user",
        "content": (f"Dossier:\n```json\n{json.dumps(dossier, indent=1)}\n```\n\n"
                    f"Question: {request.question}"),
    })

    return {"answer": _ask_model(messages), "model": config.AI_MODEL,
            "player": dossier["player"]["name"]}


@app.get("/api/overrides")
def load_overrides(path: str | None = None) -> dict:
    """Read the saved overrides file so a session can pick up where it left off."""
    target = Path(path) if path else OVERRIDES_PATH
    if not target.exists():
        return {"path": str(target), "edits": {}, "exists": False}
    frame = pd.read_csv(target)
    labels = ("fpl_id", "web_name", "pos", "team_short", "gw")
    edits: dict[int, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        if pd.isna(row.get("fpl_id")):
            continue
        fields = {c: float(row[c]) for c in frame.columns
                  if c not in labels and not pd.isna(row.get(c))}
        if not fields:
            continue
        entry = edits.setdefault(int(row["fpl_id"]), {})
        # A row with a gameweek is an opinion about one match; without one it is
        # an opinion about the player. Same file, same columns, one extra column
        # deciding which -- so an existing overrides.csv still loads unchanged.
        if "gw" in frame.columns and not pd.isna(row.get("gw")):
            entry.setdefault("gw", {})[str(int(row["gw"]))] = fields
        else:
            entry.update(fields)
    return {"path": str(target), "edits": edits, "exists": True}


@app.post("/api/overrides")
def save_overrides(request: SaveRequest) -> dict:
    """Write the edits to CSV, in the shape the CLI's --overrides expects."""
    target = Path(request.path) if request.path else OVERRIDES_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if not request.edits:
        if target.exists():
            target.unlink()
        return {"path": str(target), "rows": 0}

    projection = _get_projection(None, 8)
    names = projection.players.set_index("fpl_id")

    def label(fpl_id: int) -> dict:
        row = {"fpl_id": int(fpl_id)}
        if fpl_id in names.index:
            row["web_name"] = names.at[fpl_id, "web_name"]
            row["pos"] = names.at[fpl_id, "pos"]
            row["team_short"] = names.at[fpl_id, "team_short"]
        return row

    rows = []
    for fpl_id, fields in request.edits.items():
        season = {k: v for k, v in fields.items() if k != "gw"}
        if season:
            rows.append({**label(fpl_id), **season})
        # One row per overridden gameweek. Long rather than wide: a column per
        # (field, gameweek) would be a hundred mostly-empty columns, and the CLI
        # reads this file with pandas either way.
        for gameweek, per_match in sorted((fields.get("gw") or {}).items(),
                                          key=lambda kv: int(kv[0])):
            if per_match:
                rows.append({**label(fpl_id), "gw": int(gameweek), **per_match})

    if not rows:
        if target.exists():
            target.unlink()
        return {"path": str(target), "rows": 0}
    pd.DataFrame(rows).to_csv(target, index=False)
    return {"path": str(target), "rows": len(rows)}


@app.post("/api/refresh")
def refresh_sources() -> dict:
    """Drop every cache so the next request refetches from source."""
    removed = cache.clear()
    _projection_cache.clear()
    _pool_cache.clear()
    return {"cleared_files": removed}


@app.get("/data")
def data_page() -> FileResponse:
    return FileResponse(WEB_DIR / "data.html")


@app.get("/icon.png")
def icon() -> FileResponse:
    """Home-screen icon. Open, like the pages: iOS fetches it without headers."""
    return FileResponse(WEB_DIR / "icon.png", media_type="image/png")


# --------------------------------------------------------------------------- #
# The offline board: static assets, and the snapshot it runs on
# --------------------------------------------------------------------------- #

# An explicit list rather than a StaticFiles mount. The service worker has to
# know exactly what to cache for the board to work with nothing behind it, and a
# mount would let that list and the served set drift apart silently. Media types
# are set here too: a .mjs or .wasm served as text/plain is rejected outright by
# the browser, and the error it gives you does not say so.
ASSETS: dict[str, str] = {
    "board.mjs": "text/javascript",
    "pitch.mjs": "text/javascript",
    "poisson.mjs": "text/javascript",
    "points.mjs": "text/javascript",
    "solver.js": "text/javascript",
    "solver-worker.js": "text/javascript",
    "vendor/highs.js": "text/javascript",
    "vendor/highs.wasm": "application/wasm",
}


@app.get("/assets/{name:path}")
def asset(name: str) -> FileResponse:
    if name not in ASSETS:
        raise HTTPException(404, f"no such asset: {name}")
    return FileResponse(WEB_DIR / name, media_type=ASSETS[name])


# Club shirts, mirrored rather than hot-linked. Three reasons, in order of how
# much they matter: the board is expected to work with nothing behind it, and a
# service worker can only cache what is same-origin without CORS in play; the
# phone should not be making twenty requests to premierleague.com every time the
# pitch renders; and a club that has since been relegated still has a shirt in
# `.cache/` when an old snapshot is loaded. Fetched once per club, then served
# off disk forever.
SHIRT_SOURCE = ("https://fantasy.premierleague.com/dist/img/shirts/standard/"
                "shirt_{name}-110.png")
SHIRT_DIR = config.CACHE_DIR / "shirts"


@app.get("/shirts/{name}.png")
def shirt(name: str) -> FileResponse:
    """One club's outfield (`43`) or goalkeeper (`43_1`) shirt.

    The name is checked against the shape the FPL CDN uses rather than passed
    through: it lands in a filesystem path and in a URL, and "digits, optionally
    followed by _1" is the whole of the legitimate input.
    """
    code, _, keeper = name.partition("_")
    if not code.isdigit() or keeper not in ("", "1"):
        raise HTTPException(404, f"no such shirt: {name}")

    path = SHIRT_DIR / f"{name}.png"
    if not path.exists():
        try:
            response = requests.get(SHIRT_SOURCE.format(name=name),
                                    headers=fpl_api.HEADERS, timeout=15)
            response.raise_for_status()
        except requests.RequestException as error:
            # The pitch falls back to a plain club tile on a failed image, so a
            # club with no shirt is a cosmetic loss and never a broken board.
            raise HTTPException(502, f"could not fetch shirt {name}: {error}")
        SHIRT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)

    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=31536000"})


@app.get("/sw.js")
def service_worker() -> FileResponse:
    """Must be served from the root: a worker's scope cannot rise above its own
    path, and one parked under /assets/ could not control the page."""
    return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/snapshot.json")
def snapshot_file() -> FileResponse:
    """The frozen projection the board actually runs on.

    Built on demand the first time, so a fresh checkout serves something rather
    than 404ing at the one request the page cannot start without.
    """
    if not snapshot.SNAPSHOT_PATH.exists():
        snapshot.write()
    return FileResponse(snapshot.SNAPSHOT_PATH, media_type="application/json",
                        headers={"Cache-Control": "no-cache"})


class SyncRequest(BaseModel):
    recency: float = Field(0.0, ge=0, le=38)
    start_gw: int | None = None
    refresh: bool = False


@app.post("/api/snapshot")
def rebuild_snapshot(request: SyncRequest) -> dict:
    """Re-project and rewrite the snapshot. This is what 'Sync' does.

    The only endpoint the offline board needs, and the only one that cannot be
    done without the laptop: it is the projection itself.
    """
    if request.refresh:
        cache.clear()
    _projection_cache.clear()
    _pool_cache.clear()
    path, size = snapshot.write(recency=request.recency, start_gw=request.start_gw,
                                force_refresh=request.refresh)
    payload = json.loads(path.read_text())
    return {"path": str(path), "bytes": size,
            "generated_at": payload["generated_at"], "meta": payload["meta"]}


@app.get("/api/provenance")
def provenance(horizon: int = 8) -> dict:
    """Live state of every data source, for the provenance page.

    Deliberately reports what is actually in the cache and what actually
    matched, rather than what the pipeline is supposed to produce -- a
    provenance page that describes intentions is worth very little.
    """
    projection = _get_projection(None, horizon)
    players = projection.players

    matched = int(players["us_name"].notna().sum()) if "us_name" in players else 0
    fixtures = projection.fixtures
    priced = int(fixtures["has_odds"].sum()) if "has_odds" in fixtures else 0

    def cache_state(namespace: str) -> dict:
        entries = cache.inspect(namespace)
        newest = min((e["age_seconds"] for e in entries), default=None)
        return {
            "entries": len(entries),
            "newest_age_seconds": None if newest is None else round(newest),
            "bytes": sum(e["bytes"] for e in entries),
        }

    by_source = players["rate_source"].value_counts().to_dict() if "rate_source" in players else {}
    by_confidence = players["confidence"].value_counts().to_dict() if "confidence" in players else {}

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "horizon": projection.horizon,
        "sources": {
            "fpl": {
                "base": "https://fantasy.premierleague.com/api",
                "endpoints": ["bootstrap-static/", "fixtures/"],
                "ttl_hours": 3,
                "auth": "none",
                "players": int(len(players)),
                "teams": int(players["team"].nunique()),
                "fixtures": int(len(fixtures)),
                "cache": cache_state("fpl"),
            },
            "understat": {
                "base": "https://understat.com/main/getPlayersStats/",
                "endpoints": ["POST league=EPL&season=" + str(UNDERSTAT_SEASON)],
                "ttl_hours": 24,
                "auth": "none",
                "rows": int(len(understat.player_stats())),
                "matched": matched,
                "unmatched": int(len(players) - matched),
                "cache": cache_state("understat"),
            },
            "odds": {
                "base": "https://api.the-odds-api.com/v4",
                "endpoints": ["sports/soccer_epl/odds (h2h, totals)"],
                "ttl_hours": 6,
                "auth": "API key" + ("" if ODDS_API_KEY else " — NOT SET"),
                "key_present": bool(ODDS_API_KEY),
                "regions": ODDS_REGIONS,
                "fixtures_priced": priced,
                "fixtures_total": int(len(fixtures)),
                "coverage": round(projection.odds_coverage, 3),
                "note": projection.odds_note,
                "cache": cache_state("odds"),
            },
        },
        "history": {
            "base": history.BASE,
            "endpoints": ["2025-26/gws/merged_gw.csv", "2025-26/players_raw.csv"],
            "ttl_hours": history.TTL // 3600,
            "auth": "none",
            "third_party": True,
            "rows": int(len(history.gameweek_history())),
            "cache": cache_state("history"),
        },
        "rate_source": {str(k): int(v) for k, v in by_source.items()},
        "confidence": {str(k): int(v) for k, v in by_confidence.items()},
        "movers": int(players["moved_club"].sum()) if "moved_club" in players else 0,
        "promoted_clubs": (
            sorted(projection.strength.loc[projection.strength["is_promoted"], "team"])
            if projection.strength is not None else []
        ),
    }


# --------------------------------------------------------------------------- #
# Saved drafts
# --------------------------------------------------------------------------- #

def _read_drafts() -> list[dict]:
    if not DRAFTS_PATH.exists():
        return []
    try:
        return json.loads(DRAFTS_PATH.read_text()).get("drafts", [])
    except (json.JSONDecodeError, OSError):
        return []


def _write_drafts(drafts: list[dict]) -> None:
    DRAFTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRAFTS_PATH.write_text(json.dumps({"drafts": drafts}, indent=2))


@app.get("/api/drafts")
def list_drafts() -> dict:
    return {"drafts": _read_drafts(), "path": str(DRAFTS_PATH)}


@app.post("/api/drafts")
def save_draft(request: DraftRequest) -> dict:
    """Save (or replace) a named squad.

    Only the player ids and a note of the settings in force at the time are
    stored. Metrics are deliberately not saved: a comparison is only meaningful
    if every draft is scored under the same assumptions, so they are always
    recomputed against the current horizon, half-life and edits rather than
    frozen at whatever was on screen when you hit save.

    `saved_at` comes from the client when it has one -- the browser's merge
    logic picks whichever side has the newer `saved_at`, and re-pushes every
    local draft on each sync (not just the ones that changed) to make sure the
    laptop has everything. Stamping the server's own clock here instead would
    bump an untouched draft's timestamp on every sync and make it look
    freshly-edited, which is exactly what breaks the "newer wins" merge across
    devices. Only a save with no `saved_at` at all (the CLI, or a very old
    client) gets one made up for it.
    """
    name = request.name.strip()
    if not name:
        raise HTTPException(422, "a draft needs a name")

    drafts = [d for d in _read_drafts() if d["name"] != name]
    drafts.append({
        "name": name,
        "squad": request.squad,
        "notes": request.notes,
        "saved_at": request.saved_at or datetime.now().isoformat(timespec="seconds"),
        "context": request.context,
    })
    drafts.sort(key=lambda d: d["name"].lower())
    _write_drafts(drafts)
    return {"drafts": drafts, "saved": name}


@app.delete("/api/drafts/{name}")
def delete_draft(name: str) -> dict:
    drafts = _read_drafts()
    remaining = [d for d in drafts if d["name"] != name]
    if len(remaining) == len(drafts):
        raise HTTPException(404, f"no draft named {name!r}")
    _write_drafts(remaining)
    return {"drafts": remaining, "deleted": name}


@app.post("/api/optimise")
def optimise_squad(request: OptimiseRequest) -> dict:
    """Solve the MILP and return the chosen fifteen."""
    projection = _get_projection(request.start_gw, request.horizon, request.recency)
    for fields in request.edits.values():
        _check_overridable(fields)
    players = _apply_edits(projection, request.half_life, request.edits)

    slot_weights = None
    if request.bench_slot_weights:
        unknown = set(request.bench_slot_weights) - set(BENCH_SLOT_KEYS)
        if unknown:
            raise HTTPException(422, f"unknown bench slot: {', '.join(sorted(unknown))}")
        slot_weights = {BENCH_SLOT_KEYS[k]: v
                        for k, v in request.bench_slot_weights.items()}

    try:
        squad = optimise(
            players, budget=request.budget, bench_weight=request.bench_weight,
            include=request.include or None, exclude=request.exclude or None,
            min_minutes_prob=request.min_start, points_column="xpts_plan",
            ownership_weight=request.ownership_weight,
            max_per_club=request.max_per_club, formation=request.formation,
            bench_slot_weights=slot_weights,
        )
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return {
        "squad": [int(i) for i in squad.players["fpl_id"]],
        "bench": {str(r["fpl_id"]): (None if pd.isna(r["bench_slot"]) else str(r["bench_slot"]))
                  for _, r in squad.players.iterrows() if not r["starting"]},
        "captain": next((int(r["fpl_id"]) for _, r in squad.players.iterrows()
                         if r["is_captain"]), None),
        "objective": round(squad.objective, 3),
        "cost": round(squad.total_cost, 1),
        "xi_points": round(squad.xi_points, 3),
        "bench_points": round(squad.bench_points, 3),
    }


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False,
          token: str | None = None) -> None:
    """Run the board. `token` gates every request; None disables the check.

    Leaving the token off is only safe while the bind address is loopback,
    which is why the CLI refuses to bind anywhere else without one.
    """
    import uvicorn

    global AUTH_TOKEN
    AUTH_TOKEN = token
    uvicorn.run("fplkit.server:app" if reload else app, host=host, port=port,
                reload=reload, log_level="warning")

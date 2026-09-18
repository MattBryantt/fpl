"""Per-gameweek history, for weighting recent form above early-season form.

Neither of the live sources can do this. The FPL API's `element-summary`
history is wiped when a new season starts, and its `history_past` is
season-level totals; Understat's endpoint returns whole-season aggregates and
ignores date parameters. So a season's match-by-match detail simply is not
available from either once that season is over.

This fills the gap from the vaastav/Fantasy-Premier-League archive, a
long-running community mirror that snapshots the FPL API every gameweek and
publishes it as CSV. It is a **third-party** source -- not official, and it can
lag or break -- which is why recency weighting is opt-in and the model falls
back to flat season rates without it.

Element ids are reassigned every season, so joining last season's rows to this
season's players goes through `code`, the stable per-player identifier that
survives across seasons and transfers.
"""

from __future__ import annotations

import datetime
import io

import numpy as np
import pandas as pd
import requests

from ..cache import cached_json

BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
TTL = 12 * 3600
TIMEOUT = 60


def season_folder(start_year: int) -> str:
    """The vaastav archive's folder name for the season starting in `start_year`."""
    return f"{start_year}-{str(start_year + 1)[2:]}"


def current_season(today: datetime.date | None = None) -> str:
    """The vaastav archive's folder name for the season under way right now.

    The FPL season opens in August, so a date from August onward belongs to a
    season starting that year; anything from January to July still belongs to
    the one that opened the August before. Computed rather than hardcoded so
    this file does not need a manual edit every rollover -- which is exactly
    the edit that went stale for the 2026-27 season this fixes.
    """
    today = today or datetime.date.today()
    return season_folder(today.year if today.month >= 8 else today.year - 1)


# A per-gameweek archive that has fallen this far behind the real calendar is
# not "recent form" any more, whatever its own latest row says. The tilt was
# calibrated on a mirror that kept up; two gameweeks is the slack a weekend's
# lag deserves, and past it the archive is dropped rather than read as current.
ARCHIVE_MAX_LAG = 2

# Season totals worth carrying from the completed season, and the column each
# lands on. Pooled with this season's FPL totals by model.attach_rates and
# model.minutes_model, so a rate six matches into a season still knows what
# the same player did over the previous thirty-eight.
PREVIOUS_TOTALS = ["minutes", "starts", "bonus", "yellow_cards", "saves",
                   "defensive_contribution", "expected_goals", "expected_assists",
                   "expected_goals_conceded"]


def season_totals(season: str, force_refresh: bool = False) -> pd.DataFrame:
    """One row per player for a completed season: the FPL API's own season
    totals as the archive last mirrored them, keyed by `code`, plus the club
    he ended it at (`prev_team`, FPL's full club name) and its match count.

    Empty on any failure: the previous season is extra evidence, and losing it
    puts the model back on this season's totals alone rather than taking the
    projection down.
    """
    try:
        players = _cached_csv("history", f"{BASE}/{season}/players_raw.csv", force_refresh)
        teams = _cached_csv("history", f"{BASE}/{season}/teams.csv", force_refresh)
    except Exception:
        return pd.DataFrame(columns=["code", "prev_team", "prev_matches"]
                            + [f"prev_{c}" for c in PREVIOUS_TOTALS])
    out = pd.DataFrame({"code": pd.to_numeric(players["code"], errors="coerce")})
    for column in PREVIOUS_TOTALS:
        out[f"prev_{column}"] = pd.to_numeric(players.get(column), errors="coerce").fillna(0.0)
    names = dict(zip(teams["id"], teams["name"]))
    out["prev_team"] = players["team"].map(names)
    out["prev_matches"] = 38.0
    return out.dropna(subset=["code"]).astype({"code": int}).drop_duplicates("code")

# Columns worth carrying: the counting stats the model derives rates from.
KEEP = ["code", "gw", "minutes", "expected_goals", "expected_assists",
        "expected_goals_conceded", "defensive_contribution", "saves", "bonus",
        "clean_sheets", "goals_conceded", "starts", "total_points"]


def _fetch_csv(url: str) -> str:
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def _cached_csv(namespace: str, url: str, force_refresh: bool = False) -> pd.DataFrame:
    text = cached_json(namespace, url, lambda: _fetch_csv(url), TTL, force_refresh)
    return pd.read_csv(io.StringIO(text))


def gameweek_history(season: str | None = None,
                     force_refresh: bool = False) -> pd.DataFrame:
    """One row per player per gameweek, keyed by the stable player `code`.

    Defaults to the season under way right now (see `current_season`) rather
    than a fixed string, since this is called with no override from
    `model.py` on every run. Returns an empty frame rather than raising if the
    archive is unreachable or the current season's gameweek files do not
    exist yet (true for the first weeks of a new season, before the archive
    catches up) -- recency weighting is an enhancement, and losing it should
    not take the whole projection down with it.
    """
    season = season or current_season()
    try:
        merged = _cached_csv("history", f"{BASE}/{season}/gws/merged_gw.csv", force_refresh)
        players = _cached_csv("history", f"{BASE}/{season}/players_raw.csv", force_refresh)
    except Exception:
        return pd.DataFrame(columns=KEEP)

    if "element" not in merged or "code" not in players:
        return pd.DataFrame(columns=KEEP)

    codes = players[["id", "code"]].rename(columns={"id": "element"})
    df = merged.merge(codes, on="element", how="inner")
    df = df.rename(columns={"round": "gw"})

    for column in KEEP:
        if column not in df.columns:
            df[column] = 0.0
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    return df[KEEP]


def _weighted(history: pd.DataFrame, half_life_matches: float,
              latest: int | None) -> pd.DataFrame | None:
    """The archive with a recency weight on every row, or None when it is too
    stale to use. `latest` is the gameweek just finished according to the FPL
    API; None means trust the archive's own last row (a completed season)."""
    if history.empty or not half_life_matches:
        return None
    archive_latest = int(history["gw"].max())
    if latest is None:
        latest = archive_latest
    elif archive_latest < latest - ARCHIVE_MAX_LAG:
        return None
    df = history.copy()
    df["w"] = 0.5 ** ((latest - df["gw"]) / float(half_life_matches))
    return df


def start_form(history: pd.DataFrame, half_life_matches: float = 4.0,
               latest: int | None = None) -> pd.DataFrame:
    """A recency-weighted start rate, and how many matches stand behind it.

    Separate from `recency_multipliers` on purpose, and not optional the way the
    rate tilt is. That one is a stylistic choice about whether you believe late
    form; this one corrects a plain error. A season-long start rate is the wrong
    answer to "does he start next week" for anyone whose situation changed
    inside the season -- the January signing, the man back from three months
    out, the youngster who took a place in March -- and *every* player's
    situation changes at least once.

    How much better it is was measured rather than assumed. Scored honestly, one
    gameweek ahead, over 724 outfield players and 21,900 predictions where each
    prediction sees only the gameweeks before the one it is guessing:

        flat season rate     Brier 0.11622
        half-life 10         Brier 0.10838
        half-life 6          Brier 0.10490
        half-life 4          Brier 0.10193
        half-life 3          Brier 0.09997
        half-life 2          Brier 0.09789

    The default sits at 4 rather than at the 2 that won, because the sweep above
    is one gameweek ahead and a plan is not. The same measurement run at longer
    leads reverses: at four gameweeks out and beyond a half-life of 10 wins, and
    the flat rate is close behind. Four is where a single number is least wrong
    across the leads a squad is actually built over -- and `model.start_form_weight`
    then decays toward the flat rate as the horizon lengthens, which is the part
    that handles the reversal properly.
    """
    df = _weighted(history, half_life_matches, latest)
    if df is None:
        return pd.DataFrame(columns=["code", "recent_start_rate", "recent_matches"])
    df["started"] = (pd.to_numeric(df["starts"], errors="coerce").fillna(0.0) > 0).astype(float)

    grouped = df.groupby("code")
    weight = grouped["w"].sum()
    rate = grouped.apply(lambda g: (g["started"] * g["w"]).sum(), include_groups=False)
    out = pd.DataFrame({
        "code": weight.index.astype(int),
        "recent_start_rate": (rate / weight.replace(0.0, pd.NA)).astype(float).values,
        # In units of matches, so it can be weighed against a prior expressed the
        # same way. A player present for the last four gameweeks carries about
        # 2.9 of these at a half-life of 4, not 4 -- which is the point: the
        # figure is how much *recent* evidence there is, not how much there is.
        "recent_matches": weight.values,
    })
    return out.dropna(subset=["recent_start_rate"]).reset_index(drop=True)


def minutes_form(history: pd.DataFrame, half_life_matches: float = 4.0,
                 latest: int | None = None) -> pd.DataFrame:
    """A recency-weighted shift length, and how many starts stand behind it.

    Season-to-date `minutes / starts` off the FPL API cannot isolate this: total
    minutes mixes starts with substitute cameos, so the ratio undercounts a
    genuine 90-minute regular the moment he has come off the bench even once
    (see the comment above `mins_if_start` in minutes_model()). Per-gameweek
    rows do not have that problem -- a row where `starts > 0` reports minutes
    from that start alone -- so this is evidence the season aggregate cannot
    give the model at all, not just a recency-weighted version of it.

    Weighted the same way as `start_form`, over started rows only. Also carries
    the weighted standard deviation around that mean -- a nailed 90-minute
    player and one rotated between a token cameo and a full shift can average
    the same figure, and `model._minutes_flags` uses the spread to tell them
    apart.
    """
    columns = ["code", "recent_mins_if_start", "recent_start_matches", "recent_mins_std"]
    df = _weighted(history, half_life_matches, latest)
    if df is None:
        return pd.DataFrame(columns=columns)
    df = df[pd.to_numeric(df["starts"], errors="coerce").fillna(0.0) > 0]
    if df.empty:
        return pd.DataFrame(columns=columns)

    grouped = df.groupby("code")
    weight = grouped["w"].sum()
    mins = grouped.apply(lambda g: (g["minutes"] * g["w"]).sum(), include_groups=False)
    mean = mins / weight.replace(0.0, pd.NA)
    variance = grouped.apply(
        lambda g: (g["w"] * (g["minutes"] - mean.loc[g.name]) ** 2).sum(),
        include_groups=False) / weight.replace(0.0, pd.NA)
    out = pd.DataFrame({
        "code": weight.index.astype(int),
        "recent_mins_if_start": mean.astype(float).values,
        # In units of starts, like `start_form`'s `recent_matches` -- how much
        # *recent* evidence stands behind the figure, not how many he has ever
        # made.
        "recent_start_matches": weight.values,
        "recent_mins_std": np.sqrt(variance.astype(float)).values,
    })
    return out.dropna(subset=["recent_mins_if_start"]).reset_index(drop=True)


def recency_multipliers(history: pd.DataFrame, half_life_matches: float,
                        clip: tuple[float, float] = (0.6, 1.6),
                        min_minutes: float = 270.0,
                        latest: int | None = None) -> pd.DataFrame:
    """How much better or worse a player looked late in the season than overall.

    Deliberately a *multiplier* on the rates the model already has, not a
    replacement for them. The archive carries the FPL API's `expected_goals`,
    which includes penalties, while the model's attacking rate is Understat's
    non-penalty xG -- swapping one for the other would quietly reintroduce the
    penalty contamination that is modelled separately elsewhere. A ratio of
    recent-weighted to season-long rate carries the trend without touching the
    basis.

    Weights halve every `half_life_matches` gameweeks going backwards, so a
    half-life of 10 leaves the final third of the season carrying most of the
    signal. Players with too little weighted playing time get 1.0 -- no opinion.
    """
    columns = ["expected_goals", "expected_assists", "defensive_contribution",
               "saves", "bonus"]
    df = _weighted(history, half_life_matches, latest)
    if df is None:
        return pd.DataFrame(columns=["code"] + [f"{c}_mult" for c in columns])

    out = []
    for code, group in df.groupby("code"):
        season_minutes = group["minutes"].sum()
        weighted_minutes = (group["minutes"] * group["w"]).sum()
        if season_minutes < min_minutes or weighted_minutes <= 0:
            continue
        row = {"code": int(code)}
        for column in columns:
            season_rate = group[column].sum() / season_minutes
            recent_rate = ((group[column] * group["w"]).sum() / weighted_minutes)
            if season_rate <= 0:
                row[f"{column}_mult"] = 1.0
            else:
                row[f"{column}_mult"] = float(
                    min(clip[1], max(clip[0], recent_rate / season_rate)))
        out.append(row)

    if not out:
        return pd.DataFrame(columns=["code"] + [f"{c}_mult" for c in columns])
    return pd.DataFrame(out)

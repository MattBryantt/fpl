"""Bookmaker odds via The Odds API (https://the-odds-api.com).

Free tier is 500 requests/month, so the response is cached hard (6h) and one
request covers every upcoming Premier League fixture the books have priced.

Returns, per fixture: de-vigged 1X2 probabilities and the over/under 2.5 total
line, which `poisson.py` inverts into expected goals for each side.
"""

from __future__ import annotations

import pandas as pd
import requests

from ..cache import cached_json
from ..config import ODDS_API_KEY, ODDS_REGIONS

BASE = "https://api.the-odds-api.com/v4"
SPORT = "soccer_epl"
TTL = 6 * 3600


class OddsUnavailable(RuntimeError):
    """Raised when no usable odds could be fetched."""


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(pd.Series(values).median())


def raw_odds(force_refresh: bool = False) -> list[dict]:
    if not ODDS_API_KEY:
        raise OddsUnavailable("ODDS_API_KEY is not set (see .env.example)")

    def fetch():
        response = requests.get(
            f"{BASE}/sports/{SPORT}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": ODDS_REGIONS,
                "markets": "h2h,totals",
                "oddsFormat": "decimal",
            },
            timeout=30,
        )
        if response.status_code == 401:
            raise OddsUnavailable("The Odds API rejected the key (401)")
        if response.status_code == 429:
            raise OddsUnavailable("The Odds API quota exhausted (429)")
        response.raise_for_status()
        return response.json()

    return cached_json("odds", f"{SPORT}-{ODDS_REGIONS}", fetch, TTL, force_refresh)


def match_odds(force_refresh: bool = False) -> pd.DataFrame:
    """One row per priced fixture with de-vigged probabilities.

    Prices are taken as the median across bookmakers for each outcome, which is
    more robust than trusting a single book.
    """
    events = raw_odds(force_refresh)
    rows = []

    for event in events:
        home, away = event["home_team"], event["away_team"]
        h2h = {"home": [], "draw": [], "away": []}
        totals: dict[float, dict[str, list[float]]] = {}

        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] == "h2h":
                    for outcome in market["outcomes"]:
                        if outcome["name"] == home:
                            h2h["home"].append(outcome["price"])
                        elif outcome["name"] == away:
                            h2h["away"].append(outcome["price"])
                        elif outcome["name"] == "Draw":
                            h2h["draw"].append(outcome["price"])
                elif market["key"] == "totals":
                    for outcome in market["outcomes"]:
                        point = outcome.get("point")
                        if point is None:
                            continue
                        side = outcome["name"].lower()  # "over" / "under"
                        totals.setdefault(float(point), {}).setdefault(side, []).append(
                            outcome["price"]
                        )

        prices = {key: _median(values) for key, values in h2h.items()}
        if not all(prices.values()):
            continue

        # De-vig by multiplicative normalisation of the implied probabilities.
        implied = {key: 1.0 / price for key, price in prices.items()}
        overround = sum(implied.values())
        probs = {key: value / overround for key, value in implied.items()}

        # Prefer the 2.5 line; otherwise take whichever half-line is closest.
        #
        # Half-lines only, deliberately. On an integer line a total landing
        # exactly on it is a push and the stake comes back, so the over and under
        # prices describe the two sides *conditional on no push* -- normalising
        # them to sum to one hands that refunded mass to whichever side is
        # shorter. `poisson.prob_over` computes P(total > line), which excludes
        # the push, so the fit would be matching two different quantities and
        # would bias the resulting lambdas. 2.5 is the standard line, so this
        # drops almost nothing; where a book quotes only integers, the 1X2 prices
        # still carry the fixture on their own.
        over_prob = None
        line = None
        half_lines = [point for point in totals if round(point * 2) % 2 == 1]
        if half_lines:
            line = min(half_lines, key=lambda point: abs(point - 2.5))
            over_price = _median(totals[line].get("over", []))
            under_price = _median(totals[line].get("under", []))
            if over_price and under_price:
                over_implied, under_implied = 1.0 / over_price, 1.0 / under_price
                over_prob = over_implied / (over_implied + under_implied)
            else:
                line = None

        rows.append({
            "home_team_odds": home,
            "away_team_odds": away,
            "commence_time": event["commence_time"],
            "p_home": probs["home"],
            "p_draw": probs["draw"],
            "p_away": probs["away"],
            "totals_line": line,
            "p_over": over_prob,
            "n_books": len(event.get("bookmakers", [])),
        })

    if not rows:
        raise OddsUnavailable("The Odds API returned no priced Premier League fixtures")

    df = pd.DataFrame(rows)
    df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True)
    return df

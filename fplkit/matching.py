"""Fuzzy joins between the three sources, which agree on nothing.

FPL says "Gabriel Fernando de Jesus", Understat says "Gabriel Jesus", the
bookmakers say "Nottingham Forest" where FPL says "Nott'm Forest". Everything
here exists to reconcile those.
"""

from __future__ import annotations

import unicodedata

import pandas as pd
from rapidfuzz import fuzz, process

# Bookmaker / Understat club names that fuzzy matching gets wrong or slow.
TEAM_ALIASES = {
    "nottingham forest": "Nott'm Forest",
    "nottm forest": "Nott'm Forest",
    "wolverhampton wanderers": "Wolves",
    "wolverhampton": "Wolves",
    "tottenham hotspur": "Spurs",
    "tottenham": "Spurs",
    "manchester united": "Man Utd",
    "manchester city": "Man City",
    "newcastle united": "Newcastle",
    "brighton and hove albion": "Brighton",
    "brighton & hove albion": "Brighton",
    "west ham united": "West Ham",
    "leeds united": "Leeds",
    "afc bournemouth": "Bournemouth",
    "sheffield united": "Sheffield Utd",
    "luton town": "Luton",
    "ipswich town": "Ipswich",
    "leicester city": "Leicester",
    "norwich city": "Norwich",
}


def normalise(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    cleaned = "".join(char if char.isalnum() or char.isspace() else " " for char in stripped)
    return " ".join(cleaned.lower().split())


def _same_person(left: str, right: str) -> bool:
    """Whether two normalised full names plausibly belong to the same player.

    The global matching pass cannot lean on the club to disambiguate, so it
    needs a rule that is strict about surnames without being fooled by them.
    Similarity scorers are not usable here: token_set_ratio returns a perfect
    100 for "Will Dennis" against "Dennis Cirkin", because one shared token is
    enough to satisfy it, and that is exactly the mistake that quietly hands a
    goalkeeper someone else's expected goals.

    The rule is that the first name must agree and at least one further name
    must be shared. That accepts "Alejandro Garnacho" for "Alejandro Garnacho
    Ferreyra", where a trailing second surname would defeat a last-token rule,
    while rejecting two different players who happen to share a surname. When
    it is wrong it fails closed: the player keeps his FPL-only rates, which is
    what he had before this pass existed.
    """
    left_parts, right_parts = left.split(), right.split()
    if len(left_parts) < 2 or len(right_parts) < 2:
        return left == right and bool(left)
    if left_parts[0] != right_parts[0]:
        return False
    return bool(set(left_parts[1:]) & set(right_parts[1:]))


def match_team(name: str, fpl_team_names: list[str], cutoff: int = 70) -> str | None:
    """Map an external club name onto an FPL club name."""
    key = normalise(name)
    alias = TEAM_ALIASES.get(key)
    if alias and alias in fpl_team_names:
        return alias

    lookup = {normalise(team): team for team in fpl_team_names}
    if key in lookup:
        return lookup[key]

    best = process.extractOne(key, list(lookup), scorer=fuzz.token_set_ratio,
                              score_cutoff=cutoff)
    return lookup[best[0]] if best else None


def match_players(
    fpl: pd.DataFrame,
    understat: pd.DataFrame,
    team_map: dict[str, str],
    cutoff: int = 78,
    global_cutoff: int = 60,
) -> pd.DataFrame:
    """Left-join Understat rows onto FPL rows.

    Two passes. The first matches within club, which removes most of the
    ambiguity. The second sweeps the whole league for whoever is left, catching
    players who changed club over the summer and are therefore filed under a
    team they have left -- without it, every summer signing silently loses his
    xG history and falls back to whatever the FPL API alone can say.

    A player is tried on his full name first, then on the short web name, which
    is what Understat usually carries for players with long formal names.
    """
    understat = understat.copy()

    # Index a player under every club he played for that season, so a summer or
    # January move does not hide him from the club the FPL API now lists.
    #
    # A name maps to a *list* of rows, because a name does not identify a player:
    # two of them can share one, and keying a single row per name would drop one
    # of the pair before the join ever saw him. Understat rows are deduplicated
    # on player id upstream precisely so both survive to here.
    by_team: dict[str, dict[str, list[int]]] = {}
    for index, row in understat.iterrows():
        clubs = row.get("us_team_list") or [row.get("us_team")]
        for club in clubs:
            fpl_team = team_map.get(club)
            if fpl_team:
                (by_team.setdefault(fpl_team, {})
                        .setdefault(normalise(row["us_name"]), []).append(index))

    global_names: dict[str, list[int]] = {}
    for index, row in understat.iterrows():
        global_names.setdefault(normalise(row["us_name"]), []).append(index)

    matched_index: list[int | None] = [None] * len(fpl)
    scores: list[float] = [0.0] * len(fpl)
    claimed: set[int] = set()

    def available(scope: dict[str, list[int]]) -> dict[str, list[int]]:
        free = {name: [i for i in indices if i not in claimed]
                for name, indices in scope.items()}
        return {name: indices for name, indices in free.items() if indices}

    def try_exact(player, scope):
        for query in (normalise(player["full_name"]), normalise(player["web_name"])):
            if query and query in scope:
                return scope[query][0], 100.0
        return None, 0.0

    def try_structural(player, scope):
        full = normalise(player["full_name"])
        best, best_score = None, 0.0
        for candidate, indices in scope.items():
            if _same_person(full, candidate):
                score = fuzz.token_sort_ratio(full, candidate)
                if score > best_score:
                    best, best_score = indices[0], score
        return best, best_score

    def try_fuzzy(player, scope):
        best, best_score = None, 0.0
        for query in (normalise(player["full_name"]), normalise(player["web_name"])):
            if not query:
                continue
            hit = process.extractOne(query, list(scope), scorer=fuzz.token_set_ratio,
                                     score_cutoff=cutoff)
            if hit and hit[1] > best_score:
                best, best_score = scope[hit[0]][0], hit[1]
        return best, best_score

    # Matching runs in stages, most precise first, and every stage sweeps all
    # remaining players before the next one starts. Order matters because an
    # Understat row can only be claimed once: three Arsenal players are called
    # Gabriel, and the centre-back must take the row named plainly "Gabriel"
    # before Martinelli and Jesus are allowed to compete for it. Running each
    # player to exhaustion instead would let whoever came first in the table
    # take it, and quietly hand a winger a centre-back's expected goals.
    #
    # `token_set_ratio` is the loosest stage for exactly that reason: it scores
    # a perfect 100 whenever one name's tokens are a subset of the other's, so
    # "Gabriel" matches "Gabriel Martinelli Silva" as confidently as it matches
    # the real Gabriel. It stays last, and only sees rows nobody else wanted.
    stages = [
        (try_exact, "club"), (try_structural, "club"),
        (try_exact, "global"), (try_structural, "global"),
        (try_fuzzy, "club"),
    ]

    for attempt, scope_name in stages:
        for position, player_index in enumerate(fpl.index):
            if matched_index[position] is not None:
                continue
            player = fpl.loc[player_index]
            scope = (by_team.get(player["team"], {}) if scope_name == "club"
                     else global_names)
            scope = available(scope)
            if not scope:
                continue
            index, score = attempt(player, scope)
            minimum = global_cutoff if scope_name == "global" else 0.0
            if index is not None and score >= minimum:
                matched_index[position] = index
                scores[position] = score
                claimed.add(index)

    understat_cols = ["us_name", "us_team", "us_team_list", "us_minutes", "npxG",
                      "xA", "xGChain", "shots", "key_passes", "npxg_per90",
                      "xa_per90", "xgchain_per90", "shots_per90", "key_passes_per90"]
    aligned = understat.reindex([i if i is not None else -1 for i in matched_index])
    aligned = aligned[understat_cols].reset_index(drop=True)
    aligned.loc[[i is None for i in matched_index], :] = pd.NA

    out = fpl.reset_index(drop=True).join(aligned)
    out["us_match_score"] = scores
    return out

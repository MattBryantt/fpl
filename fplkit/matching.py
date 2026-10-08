
from __future__ import annotations

import unicodedata

import pandas as pd
from rapidfuzz import fuzz, process

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
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    cleaned = "".join(char if char.isalnum() or char.isspace() else " " for char in stripped)
    return " ".join(cleaned.lower().split())


def _same_person(left: str, right: str) -> bool:
    left_parts, right_parts = left.split(), right.split()
    if len(left_parts) < 2 or len(right_parts) < 2:
        return left == right and bool(left)
    if left_parts[0] != right_parts[0]:
        return False
    return bool(set(left_parts[1:]) & set(right_parts[1:]))


def match_team(name: str, fpl_team_names: list[str], cutoff: int = 70) -> str | None:
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
    understat = understat.copy()

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

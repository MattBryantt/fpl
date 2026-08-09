"""Solve randomised settings with CBC, for the WASM solver to match.

Randomised rather than hand-picked on purpose. A MILP port breaks on the
combinations nobody thinks to write down -- a forced formation that collides
with a required player, a budget tight enough that the club limit starts to
bind, every bench weight at zero. Fixing the seed keeps it reproducible.

    python scripts/make-solver-cases.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from fplkit import server
from fplkit.optimise import optimise
from fplkit.planning import DEFAULT_HALF_LIFE
from fplkit.snapshot import SNAPSHOT_HORIZON

ROOT = Path(__file__).resolve().parent.parent
POOL_OUT = ROOT / "scripts" / "solver-pool.json"
CASES_OUT = ROOT / "scripts" / "solver-cases.json"

FORMATIONS = [None, {"DEF": 3, "MID": 4, "FWD": 3}, {"DEF": 5, "MID": 4, "FWD": 1},
              {"DEF": 3, "MID": 5, "FWD": 2}, {"DEF": 4, "MID": 3, "FWD": 3}]


def main(n_random: int = 40) -> None:
    projection = server._get_projection(None, SNAPSHOT_HORIZON)
    players = server._apply_edits(projection, DEFAULT_HALF_LIFE, {})

    pool = [{
        "id": int(p["fpl_id"]), "pos": p["pos"], "team": p["team"],
        "price": float(p["price"]), "pts": float(p["xpts_plan"] or 0.0),
        "own": float(p.get("selected_by_percent") or 0.0),
        "p_play": float(p["p_play"]),
    } for _, p in players.iterrows()]
    POOL_OUT.write_text(json.dumps(pool, separators=(",", ":")))

    ids = [p["id"] for p in pool]
    rng = random.Random(20260808)

    settings: list[tuple[str, dict]] = [
        ("defaults", {}),
        ("no bench value", {"bench_slot_weights": {"GKP": 0, 1: 0, 2: 0, 3: 0}}),
        ("bench over XI", {"bench_slot_weights": {"GKP": 0.5, 1: 1.5, 2: 1.0, 3: 0.8}}),
        ("heavy tilt", {"ownership_weight": 1.0}),
        ("one club", {"max_per_club": 1}),
        ("five per club", {"max_per_club": 5}),
        ("min start 0.9", {"min_minutes_prob": 0.9}),
        ("min start 0", {"min_minutes_prob": 0.0}),
        ("broke", {"budget": 78.0}),
        ("rich", {"budget": 115.0}),
    ]
    for formation in FORMATIONS[1:]:
        name = "-".join(str(formation[k]) for k in ("DEF", "MID", "FWD"))
        settings.append((f"formation {name}", {"formation": formation}))

    for i in range(n_random):
        opts: dict = {
            "budget": round(rng.uniform(80, 108), 1),
            "min_minutes_prob": round(rng.choice([0.0, 0.2, 0.3, 0.5, 0.75]), 2),
            "max_per_club": rng.choice([2, 3, 3, 4]),
            "ownership_weight": round(rng.choice([0.0, 0.0, 0.15, 0.4]), 2),
            "formation": rng.choice(FORMATIONS),
            "bench_slot_weights": {
                "GKP": round(rng.uniform(0, 0.4), 3), 1: round(rng.uniform(0, 1.2), 3),
                2: round(rng.uniform(0, 0.6), 3), 3: round(rng.uniform(0, 0.3), 3),
            },
        }
        if rng.random() < 0.5:
            opts["include"] = rng.sample(ids, rng.randint(1, 3))
        if rng.random() < 0.4:
            opts["exclude"] = rng.sample(ids, rng.randint(1, 25))
        settings.append((f"random {i}", opts))

    cases = []
    for label, opts in settings:
        try:
            squad = optimise(players, points_column="xpts_plan", **opts)
            infeasible = False
        except (ValueError, RuntimeError):
            squad, infeasible = None, True

        # snake_case on the Python side, camelCase on the JS side.
        js = {
            "budget": opts.get("budget", 100.0),
            "minStart": opts.get("min_minutes_prob", 0.0),
            "maxPerClub": opts.get("max_per_club", 3),
            "ownershipWeight": opts.get("ownership_weight", 0.0),
            "formation": opts.get("formation"),
            "include": opts.get("include", []),
            "exclude": opts.get("exclude", []),
        }
        if "bench_slot_weights" in opts:
            js["benchSlotWeights"] = {str(k): v for k, v in opts["bench_slot_weights"].items()}
        else:
            js["benchWeight"] = 0.12

        cases.append({
            "label": label, "options": js, "infeasible": infeasible,
            "objective": None if infeasible else round(squad.objective, 6),
            "squad": [] if infeasible else sorted(int(i) for i in squad.players["fpl_id"]),
        })

    CASES_OUT.write_text(json.dumps(cases, separators=(",", ":")))
    n_infeasible = sum(c["infeasible"] for c in cases)
    print(f"{CASES_OUT}  {len(cases)} cases ({n_infeasible} infeasible)")
    print(f"{POOL_OUT}  {len(pool)} players")


if __name__ == "__main__":
    main()

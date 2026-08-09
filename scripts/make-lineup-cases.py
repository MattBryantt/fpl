"""Score club rebalances with the Python model, for the JS port to match.

Overriding a player's minutes is not a statement about him alone: a club fields
eleven, so asserting that one man starts takes the minutes off somebody else.
`model.renormalise_minutes` decides who, and `board.mjs` has to reach the same
answer or the board will show a lineup the projection does not believe in.

The cases deliberately include the awkward shapes: a single promotion, a whole
XI pinned at once, a demotion, a keeper (whose pool is one, not ten), and an
over-pinned club where the assertions already exceed eleven and the model is
supposed to keep them rather than scale them back.

    python scripts/make-lineup-cases.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from fplkit.model import project
from fplkit.snapshot import SNAPSHOT_HORIZON

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scripts" / "lineup-cases.json"


def main() -> None:
    base = project(horizon=SNAPSHOT_HORIZON)
    players = base.players

    def ids(team: str, **kw) -> list[int]:
        squad = players[players["team"] == team]
        if kw.get("outfield"):
            squad = squad[squad["pos"] != "GKP"]
        if kw.get("keeper"):
            squad = squad[squad["pos"] == "GKP"]
        return [int(i) for i in squad.nlargest(kw.get("n", 1), kw.get("by", "p_start"))["fpl_id"]]

    fringe = players[(players["team"] == "Man City") & (players["p_start"] < 0.3)]
    cases: list[dict] = [
        {"name": "promote one fringe forward",
         "edits": {str(int(fringe.nlargest(1, "npxg_per90")["fpl_id"].iloc[0])): {"p_start": 0.9}}},
        {"name": "demote a nailed starter",
         "edits": {str(ids("Liverpool", n=1)[0]): {"p_start": 0.1}}},
        {"name": "pin a whole XI",
         "edits": {str(i): {"p_start": 0.95}
                   for i in ids("Arsenal", n=10, outfield=True) + ids("Arsenal", n=1, keeper=True)}},
        {"name": "over-pin: thirteen outfield at 0.95",
         "edits": {str(i): {"p_start": 0.95} for i in ids("Everton", n=13, outfield=True)}},
        {"name": "swap the keeper",
         "edits": {str(ids("Spurs", n=2, keeper=True)[-1]): {"p_start": 0.9}}},
        {"name": "exp_minutes instead of p_start",
         "edits": {str(ids("Chelsea", n=1)[0]): {"exp_minutes": 30.0}}},
        {"name": "two clubs at once",
         "edits": {**{str(i): {"p_start": 0.9} for i in ids("Newcastle", n=2)},
                   **{str(i): {"p_start": 0.2} for i in ids("Brighton", n=2)}}},
    ]

    out = []
    for case in cases:
        edits = {int(k): v for k, v in case["edits"].items()}
        frame = pd.DataFrame([{"fpl_id": i, **fields} for i, fields in edits.items()])
        run = project(horizon=SNAPSHOT_HORIZON, overrides=frame)

        clubs = sorted({str(players.loc[players["fpl_id"] == i, "team"].iloc[0])
                        for i in edits})
        expected = {}
        for club in clubs:
            squad = run.players[run.players["team"] == club]
            expected[club] = {
                "starters": round(float(squad["p_start"].sum()), 6),
                "players": {str(int(r["fpl_id"])): round(float(r["p_start"]), 9)
                            for _, r in squad.iterrows()},
            }
        out.append({"name": case["name"], "edits": case["edits"], "expected": expected})

    OUT.write_text(json.dumps(out, indent=1))
    print(f"{OUT}  {len(out)} cases over "
          f"{sum(len(c['expected']) for c in out)} club rebalances")


if __name__ == "__main__":
    main()

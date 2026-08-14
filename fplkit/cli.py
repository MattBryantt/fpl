"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from rich.console import Console
from rich.table import Table

from . import cache, transfers
from .config import CHIPS, DEFAULT_BENCH_WEIGHT, DEFAULT_BUDGET, OUT_DIR
from .model import OVERRIDABLE, Projection, project
from .optimise import marginal_value, optimise
from .planning import DEFAULT_HALF_LIFE, build_plan, horizon_sensitivity
from .snapshot import SNAPSHOT_HORIZON
from .transfers import DEFAULT_TRANSFER_HORIZON, TRANSFER_HALF_LIFE
from .sources import fpl_api

console = Console()

COMPACT_COLUMNS = [
    "web_name", "pos", "team_short", "price", "n_fixtures", "xpts",
    "xpts_per_game", "xpts_per_m", "p_start", "exp_clean_sheets", "rate_source",
]

FULL_COLUMNS = COMPACT_COLUMNS[:-1] + [
    "exp_goals", "exp_assists", "xpts_goals", "xpts_assists",
    "xpts_clean_sheet", "xpts_defcon", "xpts_bonus", "xpts_appearance",
    "rate_source",
]

HEADERS = {
    "web_name": "Player", "pos": "Pos", "team_short": "Team", "price": "£",
    "n_fixtures": "GMS", "xpts": "xPts", "xpts_per_game": "xPts/g",
    "xpts_per_m": "xPts/£m", "p_start": "pStart", "exp_goals": "xG",
    "exp_assists": "xA", "xpts_goals": "pG", "xpts_assists": "pA",
    "xpts_clean_sheet": "pCS", "xpts_defcon": "pDC", "xpts_bonus": "pBon",
    "exp_clean_sheets": "xCS", "recency": "rec", "goal_coverage": "goals covered",
    "xpts_appearance": "pApp", "rate_source": "Src", "price_delta": "Δ£",
    "xpts_delta": "ΔxPts", "xpts_per_extra_m": "ΔxPts/Δ£m",
    "xpts_plan": "xPts(plan)", "ownership_pct": "own%", "exposure": "exposure", "team_context": "ctx", "previous_club": "from", "npxg_per90": "npxG90", "confidence": "conf", "swing": "swing", "worst_vs": "worst v", "from_gw": "from", "to_gw": "to", "length": "len", "xpts_gw1": "xPts(gw1)", "xpts_raw": "xPts(raw)",
    "exp_price_change": "Δ£ fcast", "xi_xpts": "XI xPts", "decay": "weight",
    "odds_priced": "priced", "changed_vs_prev": "churn",
    "shared_with_plan": "∩ plan", "frontloaded": "front",
    "mins_if_start": "mins/start", "recent_mins_std": "std", "mins_flags": "flags",
    "chip": "chip", "transfers": "TRs", "free": "FT", "hits": "hits",
    "out": "out", "in": "in", "out_price": "£ out", "in_price": "£ in",
    "in_team": "to", "gain": "gain", "worth": "worth", "edge": "edge",
    "verdict": "read", "weight": "weight", "xi_points": "XI xPts",
    "formation": "shape", "captain": "captain", "vice_captain": "vice",
    "bench_order": "bench (autosub order)",
}

FLOAT_FORMATS = {
    "n_fixtures": "{:.0f}",
    "price": "{:.1f}", "xpts": "{:.2f}", "xpts_per_game": "{:.2f}",
    "xpts_per_m": "{:.3f}", "p_start": "{:.2f}", "exp_goals": "{:.2f}",
    "exp_assists": "{:.2f}", "xpts_goals": "{:.2f}", "xpts_assists": "{:.2f}",
    "xpts_clean_sheet": "{:.2f}", "xpts_defcon": "{:.2f}", "xpts_bonus": "{:.2f}",
    "exp_clean_sheets": "{:.2f}", "recency": "{:.2f}", "goal_coverage": "{:.2f}",
    "xpts_plan": "{:.2f}", "ownership_pct": "{:.1f}", "exposure": "{:.2f}", "team_context": "{:.2f}", "npxg_per90": "{:.3f}", "minutes": "{:.0f}", "from_gw": "{:.0f}", "to_gw": "{:.0f}", "length": "{:.0f}", "swing": "{:.2f}", "xpts_gw1": "{:.2f}", "xpts_raw": "{:.2f}",
    "exp_price_change": "{:+.2f}", "xi_xpts": "{:.1f}", "decay": "{:.2f}",
    "gain": "{:.2f}", "cost": "{:+.1f}", "bank": "{:.1f}",
    "horizon": "{:.0f}", "last_gw": "{:.0f}", "changed_vs_prev": "{:.0f}",
    "shared_with_plan": "{:.0f}", "gw": "{:.0f}", "frontloaded": "{:.2f}",
    "transfers": "{:.0f}", "free": "{:.0f}", "hits": "{:.0f}",
    "out_price": "{:.1f}", "in_price": "{:.1f}", "worth": "{:.1f}",
    "edge": "{:+.1f}", "weight": "{:.2f}", "xi_points": "{:.1f}",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _columns(args) -> list[str]:
    """Compact by default; the full points breakdown behind --full."""
    return FULL_COLUMNS if getattr(args, "full", False) else COMPACT_COLUMNS


def _render(df: pd.DataFrame, title: str, columns: list[str] | None = None,
            default_format: str = "{:.2f}") -> None:
    """`default_format` covers columns FLOAT_FORMATS cannot name -- the chip
    option table's one-per-gameweek columns are built from the horizon."""
    columns = [c for c in (columns or df.columns) if c in df.columns]
    table = Table(title=title, header_style="bold cyan", title_style="bold")
    for column in columns:
        justify = "left" if df[column].dtype == object else "right"
        table.add_column(HEADERS.get(column, column), justify=justify, no_wrap=True)
    for _, row in df.iterrows():
        cells = []
        for column in columns:
            value = row[column]
            if pd.isna(value):
                cells.append("-")
            elif column in FLOAT_FORMATS:
                cells.append(FLOAT_FORMATS[column].format(value))
            elif isinstance(value, float):
                cells.append(default_format.format(value))
            else:
                cells.append(str(value))
        table.add_row(*cells)
    console.print(table)


def _save(df: pd.DataFrame, filename: str | None) -> None:
    if not filename:
        return
    path = Path(filename) if "/" in filename else OUT_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    console.print(f"[dim]written {path}[/dim]")


def _load_overrides(path: str | None) -> pd.DataFrame | None:
    if not path:
        return None
    df = pd.read_csv(path)
    fields = [c for c in df.columns
              if c.lower() not in ("fpl_id", "web_name", "pos", "team_short")]
    console.print(f"[dim]loaded {len(df)} overrides from {path} "
                  f"({', '.join(fields) if fields else 'no fields'})[/dim]")
    return df


def _run_projection(args) -> Projection:
    with console.status("[cyan]fetching data and projecting…"):
        projection = project(
            horizon=args.horizon,
            start_gw=args.start_gw,
            overrides=_load_overrides(args.overrides),
            recency_half_life=getattr(args, "recency", 0.0) or None,
            force_refresh=args.refresh,
        )
    gws = projection.horizon
    coverage = projection.odds_coverage
    source = (f"bookmaker odds on {coverage:.0%} of fixtures"
              if coverage > 0 else "xG-derived ratings only")
    console.print(
        f"[dim]GW{gws[0]}–GW{gws[-1]} · {len(projection.fixtures)} fixtures · {source}[/dim]"
    )
    if projection.odds_note:
        console.print(f"[yellow]odds: {projection.odds_note}[/yellow]")
    if getattr(args, "recency", 0):
        tilted = int((projection.players.get("recency", pd.Series(dtype=float)) != 1.0).sum())
        console.print(f"[dim]recency weighting on, {args.recency:g}-gameweek "
                      f"half-life, {tilted} players tilted[/dim]"
                      if tilted else
                      "[yellow]recency requested but no per-gameweek history "
                      "was available[/yellow]")

    blind = projection.players[projection.players["needs_override"]
                               & (projection.players["p_start"] < 0.2)]
    if len(blind):
        console.print(f"[dim]{len(blind)} players excluded for lack of minutes "
                      f"history (`blindspots` to list them)[/dim]")
    return projection


def _filter(df: pd.DataFrame, args) -> pd.DataFrame:
    out = df.copy()
    if getattr(args, "pos", None):
        wanted = [p.strip().upper() for p in args.pos.split(",")]
        out = out[out["pos"].isin(wanted)]
    if getattr(args, "team", None):
        wanted = [t.strip().lower() for t in args.team.split(",")]
        out = out[out["team_short"].str.lower().isin(wanted)
                  | out["team"].str.lower().isin(wanted)]
    if getattr(args, "max_price", None) is not None:
        out = out[out["price"] <= args.max_price]
    if getattr(args, "min_price", None) is not None:
        out = out[out["price"] >= args.min_price]
    if getattr(args, "min_start", None) is not None:
        out = out[out["p_start"] >= args.min_start]
    return out


def _resolve(players: pd.DataFrame, query: str) -> pd.Series:
    """Find one player from a loose name, disambiguating by price if needed."""
    exact = players[players["web_name"].str.lower() == query.strip().lower()]
    if len(exact) == 1:
        return exact.iloc[0]
    if len(exact) > 1:
        console.print(f"[yellow]'{query}' is ambiguous:[/yellow]")
        _render(exact, f"matches for '{query}'",
                ["fpl_id", "web_name", "full_name", "pos", "team_short", "price"])
        raise SystemExit(1)

    choices = {f"{row.web_name} | {row.full_name}": index
               for index, row in players.iterrows()}
    hit = process.extractOne(query, list(choices), scorer=fuzz.WRatio, score_cutoff=60)
    if not hit:
        console.print(f"[red]no player matching '{query}'[/red]")
        raise SystemExit(1)
    return players.loc[choices[hit[0]]]


def _print_squad(squad, label: str, extra_columns: list[str] | None = None) -> None:
    players = squad.players.copy()
    players["role"] = players.apply(
        lambda r: "XI (C)" if r["is_captain"] else ("XI" if r["starting"] else "bench"),
        axis=1,
    )
    _render(players, label,
            ["role", "web_name", "pos", "team_short", "price", "xpts",
             "xpts_per_m", "p_start"] + (extra_columns or []))
    console.print(
        f"cost [bold]£{squad.total_cost:.1f}m[/bold] · "
        f"XI xPts [bold]{squad.xi_points:.1f}[/bold] (captain doubled) · "
        f"bench xPts {squad.bench_points:.1f} · "
        f"objective {squad.objective:.1f} · "
        f"captain [bold]{squad.captain['web_name']}[/bold]"
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_rank(args) -> None:
    projection = _run_projection(args)
    df = _filter(projection.players, args)
    df = df.sort_values(args.sort, ascending=False).head(args.limit)
    _render(df, f"Top {len(df)} by {args.sort}", _columns(args))
    _save(df, args.csv)


def cmd_value(args) -> None:
    """Points per million, restricted to players who will actually play."""
    projection = _run_projection(args)
    df = _filter(projection.players, args)
    df = df[df["p_start"] >= args.min_start]
    df = df.sort_values("xpts_per_m", ascending=False).head(args.limit)
    _render(df, f"Best value (xPts per £m, pStart ≥ {args.min_start})", _columns(args))
    _save(df, args.csv)


def cmd_compare(args) -> None:
    projection = _run_projection(args)
    players = projection.players
    picks = [_resolve(players, name) for name in args.players]
    frame = pd.DataFrame(picks)

    _render(frame, "Head to head", FULL_COLUMNS)

    console.print()
    for i in range(len(picks)):
        for j in range(i + 1, len(picks)):
            a, b = picks[i], picks[j]
            dearer, cheaper = (a, b) if a["price"] >= b["price"] else (b, a)
            price_gap = dearer["price"] - cheaper["price"]
            points_gap = dearer["xpts"] - cheaper["xpts"]

            console.print(
                f"[bold]{dearer['web_name']}[/bold] costs "
                f"[bold]£{price_gap:.1f}m[/bold] more than "
                f"[bold]{cheaper['web_name']}[/bold] and is projected "
                f"[bold]{points_gap:+.2f}[/bold] xPts better over "
                f"GW{projection.horizon[0]}–{projection.horizon[-1]}."
            )
            if price_gap > 0:
                console.print(
                    f"  That is {points_gap / price_gap:+.2f} xPts per extra £m. "
                    f"Median xPts/£m in the pool is "
                    f"{players[players['p_start'] >= 0.5]['xpts_per_m'].median():.2f}."
                )
            else:
                console.print("  Same price, so the points gap is the whole story.")

    if args.squad_test:
        console.print()
        console.print("[dim]rebuilding the optimal squad with and without each "
                      "player (4 solves)…[/dim]")
        results = []
        for pick in picks:
            outcome = marginal_value(
                players, int(pick["fpl_id"]), budget=args.budget,
                bench_weight=args.bench_weight, min_minutes_prob=args.min_start,
            )
            results.append({
                "web_name": pick["web_name"],
                "price": pick["price"],
                "own_xpts": pick["xpts"],
                "squad_with": outcome["squad_xpts_with"],
                "squad_without": outcome["squad_xpts_without"],
                "marginal": outcome["delta"],
            })
        table = pd.DataFrame(results).sort_values("marginal", ascending=False)
        _render(table, f"Marginal squad value (£{args.budget}m budget)")
        console.print(
            "[dim]marginal = what the best possible squad gains from having this "
            "player available at his price, versus the best squad built without "
            "him. It already charges him for the money he ties up, so it is the "
            "fair test of whether the extra spend pays for itself.[/dim]"
        )
        best = table.iloc[0]
        console.print(
            f"On that measure [bold]{best['web_name']}[/bold] is the pick, worth "
            f"[bold]{best['marginal']:+.2f}[/bold] xPts to the squad."
        )

    _save(frame, args.csv)


def cmd_squad(args) -> None:
    projection = _run_projection(args)
    players = projection.players
    include = [int(_resolve(players, name)["fpl_id"]) for name in (args.include or [])]
    exclude = [int(_resolve(players, name)["fpl_id"]) for name in (args.exclude or [])]

    squad = optimise(players, budget=args.budget, bench_weight=args.bench_weight,
                     include=include or None, exclude=exclude or None,
                     min_minutes_prob=args.min_start)
    _print_squad(squad, f"Optimal 15 for GW{projection.horizon[0]}–{projection.horizon[-1]}")
    _save(squad.players, args.csv)


def cmd_upgrade(args) -> None:
    """For one player, what every affordable alternative would cost or gain."""
    projection = _run_projection(args)
    players = projection.players
    target = _resolve(players, args.player)

    pool = players[(players["pos"] == target["pos"])
                   & (players["fpl_id"] != target["fpl_id"])
                   & (players["p_start"] >= args.min_start)]
    if args.budget_extra is not None:
        pool = pool[pool["price"] <= target["price"] + args.budget_extra]

    pool = pool.copy()
    pool["price_delta"] = pool["price"] - target["price"]
    pool["xpts_delta"] = pool["xpts"] - target["xpts"]
    pool["xpts_per_extra_m"] = pool.apply(
        lambda r: r["xpts_delta"] / r["price_delta"] if r["price_delta"] > 0 else float("nan"),
        axis=1,
    )
    pool = pool.sort_values("xpts_delta", ascending=False).head(args.limit)

    _render(pool, f"Alternatives to {target['web_name']} (£{target['price']:.1f}m, "
                  f"{target['xpts']:.2f} xPts)",
            ["web_name", "team_short", "price", "price_delta", "xpts",
             "xpts_delta", "xpts_per_extra_m", "p_start"])
    _save(pool, args.csv)


def cmd_fixtures(args) -> None:
    projection = _run_projection(args)
    df = projection.fixtures.copy()
    df["fixture"] = df["home_team"] + " v " + df["away_team"]
    df["cs_home"] = np.exp(-df["lam_away"])
    df["cs_away"] = np.exp(-df["lam_home"])
    columns = ["gw", "fixture", "lam_home", "lam_away", "cs_home", "cs_away",
               "lam_source"]
    _render(df.sort_values(["gw", "fixture"])[columns], "Fixture expected goals")
    _save(df, args.csv)


def cmd_plan(args) -> None:
    """An initial squad plus the reasoning around it: where it goes, what it risks."""
    projection = _run_projection(args)
    players = projection.players
    include = [int(_resolve(players, name)["fpl_id"]) for name in (args.include or [])]
    exclude = [int(_resolve(players, name)["fpl_id"]) for name in (args.exclude or [])]

    with console.status("[cyan]building the plan…"):
        plan = build_plan(
            projection, budget=args.budget, bench_weight=args.bench_weight,
            min_minutes_prob=args.min_start, half_life=args.half_life,
            total_managers=fpl_api.total_managers(),
            include=include or None, exclude=exclude or None,
            ownership_weight=args.ownership_weight,
        )

    horizon = plan.horizon
    console.print(
        f"[bold]Plan for GW{horizon[0]}–GW{horizon[-1]}[/bold], "
        f"discounting future gameweeks with a {plan.half_life:g}-gameweek half-life "
        f"and an injury/rotation survival curve.\n"
    )

    squad = plan.squad.players.copy()
    squad["role"] = squad.apply(
        lambda r: "XI (C)" if r["is_captain"] else ("XI" if r["starting"] else "bench"),
        axis=1)
    squad["keep"] = squad["fpl_id"].isin(plan.core["fpl_id"]).map({True: "core", False: ""})
    _render(squad, "Squad",
            ["role", "web_name", "pos", "team_short", "price", "xpts_plan",
             "xpts_gw1", "p_start", "exp_price_change", "keep"])
    console.print(
        f"cost [bold]£{plan.squad.total_cost:.1f}m[/bold] · "
        f"in the bank £{plan.bank:.1f}m · "
        f"captain [bold]{plan.squad.captain['web_name']}[/bold] · "
        f"expected value change over the window "
        f"[bold]{squad['exp_price_change'].sum():+.1f}m[/bold]\n"
    )

    _render(plan.per_gw, "Projected starting XI points by gameweek",
            ["gw", "xi_xpts", "decay", "odds_priced"])

    console.print()
    _render(plan.lineups, "Starting XI, bench order and captain — picked fresh each gameweek",
            ["gw", "formation", "captain", "vice_captain", "bench_order", "xi_points"])
    console.print("[dim]same 15-man squad throughout; who starts, who's benched (in autosub "
                  "order) and who captains rotates with that week's fixture. Starting XI = "
                  "squad minus bench_order.[/dim]")

    console.print()
    gw_columns = [f"gw{gw}" for gw in plan.horizon]
    _render(plan.timeline, "Fixture timeline — when each player's fixtures turn",
            ["web_name", "pos", "team_short"] + gw_columns + ["swing", "worst_vs"])
    console.print("[dim]each cell is that gameweek's projected points, marked "
                  "against the player's own average: [bold]+[/bold] good, "
                  "[bold]=[/bold] par, [bold]-[/bold] bad. Sorted by swing, so the "
                  "most fixture-dependent players are at the top. Opponent shown "
                  "in CAPS when at home.[/dim]")

    if len(plan.windows):
        console.print()
        _render(plan.windows.head(10),
                "Bad runs — bank a transfer for these rather than spending now",
                ["web_name", "pos", "from_gw", "to_gw", "length"])

    console.print()
    _render(plan.exposure, "Rank risk — best players you do not own",
            ["web_name", "pos", "team_short", "price", "ownership_pct",
             "xpts_plan", "exposure"])
    cover = plan.coverage
    console.print(
        f"[dim]exposure = ownership x projected points: roughly what the average "
        f"rival banks from him and you do not. Your squad covers "
        f"[/dim][bold]{cover['covered_share']:.0%}[/bold][dim] of the field's "
        f"expected points. Covering everything guarantees an average finish — "
        f"this is a risk list, not a shopping list. Tilt toward the template "
        f"with --ownership-weight.[/dim]"
    )

    if not args.no_transfer_plan:
        console.print()
        with console.status("[cyan]solving the transfer path and chip timing…"):
            path = transfers.plan_transfers(
                projection, plan.players, horizon=args.transfer_horizon,
                budget=args.budget, min_minutes_prob=args.min_start,
                chip_windows=fpl_api.chip_windows(projection.horizon[0]),
                include=include or None, exclude=exclude or None)
        _print_transfer_path(path)

    console.print()
    console.print(f"[bold]{len(plan.core)}[/bold] of 15 are core picks — chosen "
                  f"whether you plan one gameweek ahead or six.")
    for note in plan.notes:
        console.print(f"[dim]· {note}[/dim]")
    console.print(
        "[dim]· a wildcard rebuilds the whole squad from GW2, so this fifteen "
        "only has to be right for the first few gameweeks.[/dim]"
    )
    _save(plan.squad.players, args.csv)


def _print_transfer_path(path, show_lineups: bool = False) -> None:
    """The transfer ledger, the moves, and what the chips are worth."""
    _render(path.ledger, "Transfer path — free transfers, hits and chips by gameweek",
            ["gw", "chip", "transfers", "free", "hits", "bank", "xi_xpts", "weight"])
    console.print(
        "[dim]free = free transfers available that gameweek, after banking. A "
        "free hit costs you that week's new transfer but no longer burns the "
        "ones you saved. Points past the first gameweek are weighted down; hits "
        "and banked transfers are weighted with them.[/dim]")

    if len(path.moves):
        console.print()
        _render(path.moves, "Moves", ["gw", "pos", "out", "out_price", "in",
                                      "in_price", "in_team", "gain"])
        console.print("[dim]gain is the incoming player's projected points from that "
                      "gameweek to the end of the window, minus the outgoing "
                      "player's over the same weeks — before the transfer is "
                      "charged for what it spends.[/dim]")
    else:
        console.print("\n[dim]no transfer anywhere in the window clears what it "
                      "costs to make it.[/dim]")

    if len(path.chips):
        console.print()
        _render(path.chips, "Chips", ["chip", "gw", "worth", "edge", "verdict"])
        console.print(
            "[dim]worth is the chip's payout in the gameweek chosen — the bench "
            "net of what it already earns from its slot weights, or the third "
            "helping of the captain; edge is how much better that gameweek is "
            "than the median one in the window. A chip is only played when it "
            "beats what it is worth held for a well-timed gameweek later (see "
            "CHIP_HOLD_VALUE) — which is why a flat fixture list produces a row "
            "of holds.[/dim]")
        if path.chips["verdict"].str.startswith("forced").any():
            console.print(
                "[dim]rows marked forced were played because --force-chip said "
                "to, with their reservation price set aside; the gameweek is "
                "still the best one in the window for them.[/dim]")

    if len(path.chip_options):
        console.print()
        _render(path.chip_options, "Every gameweek each chip was weighed in",
                list(path.chip_options.columns), default_format="{:.1f}")
        console.print(
            "[dim]the whole option set, not just the winner: what the chip would "
            "pay in each gameweek, against the reserve it has to clear. A dash "
            "means the chip cannot be played that gameweek. The solver compares "
            "these discounted, so on a flat calendar — where the columns barely "
            "differ — the earliest gameweek wins on the discount alone, which is "
            "the sign that nothing in the fixture list is choosing for you. The "
            "free hit is absent because it has no per-gameweek payout of its "
            "own; --chip-value prices it.[/dim]")

    if show_lineups:
        console.print()
        _render(path.lineups, "Squad each gameweek",
                ["gw", "chip", "formation", "captain", "bench_order", "xi_points"])

    for note in path.notes:
        console.print(f"[dim]· {note}[/dim]")


def _load_squad(players: pd.DataFrame, path: str | None) -> tuple[list[int], dict]:
    """Read the fifteen you own from a CSV, with purchase prices if given.

    Accepts whatever `plan --csv` writes, so the usual route is to plan a squad,
    keep the file, and feed it back in once the season is running.
    """
    if not path:
        return [], {}
    df = pd.read_csv(path)
    if "fpl_id" in df.columns:
        ids = [int(i) for i in df["fpl_id"]]
    else:
        ids = [int(_resolve(players, str(name))["fpl_id"]) for name in df["web_name"]]

    sell = {}
    column = next((c for c in ("sell_price", "purchase_price") if c in df.columns), None)
    if column:
        sell = {i: float(v) for i, v in zip(ids, df[column]) if pd.notna(v)}
    return ids, sell


def cmd_transfers(args) -> None:
    """Transfers and chips over the window, under the actual rules."""
    args.horizon = max(args.horizon, args.transfer_horizon)
    projection = _run_projection(args)
    players = projection.players
    owned, sell_prices = _load_squad(players, args.squad)
    include = [int(_resolve(players, name)["fpl_id"]) for name in (args.include or [])]
    exclude = [int(_resolve(players, name)["fpl_id"]) for name in (args.exclude or [])]

    settings = dict(
        horizon=args.transfer_horizon, budget=args.budget, squad=owned or None,
        bank=args.bank, free_transfers=args.free_transfers,
        sell_prices=sell_prices or None, min_minutes_prob=args.min_start,
        chip_windows=fpl_api.chip_windows(projection.horizon[0]),
        chips_used=args.chips_used or None,
        chip_hold=({chip: 0.0 for chip in CHIPS} if args.ignore_chip_hold else None),
        force_chips=args.force_chip or None,
        half_life=args.transfer_half_life, include=include or None,
        exclude=exclude or None, hit_limit=args.hit_limit, seconds=args.seconds,
    )

    with console.status("[cyan]solving transfers and chips together…"):
        path = transfers.plan_transfers(projection, players, **settings)

    console.print(
        f"[bold]GW{path.gameweeks[0]}–GW{path.gameweeks[-1]}[/bold], "
        f"{'preseason — the opening fifteen is a free choice' if not owned else f'from the {len(owned)} you own'}"
        f", discounting at a {args.transfer_half_life:g}-gameweek half-life.\n")
    _print_transfer_path(path, show_lineups=args.lineups)

    # Only the first gameweek is a decision. Price it.
    if owned:
        console.print()
        with console.status("[cyan]pricing this week's move against rolling…"):
            decision = transfers.value_of_acting(projection, players, plan=path,
                                                 **settings)
        gain, moves = decision["gain"], decision["moves"]
        if len(moves):
            names = ", ".join(f"{r['out']} → {r['in']}" for _, r in moves.iterrows())
            console.print(
                f"[bold]This gameweek:[/bold] {names} — worth "
                f"[bold]{gain:+.2f}[/bold] against rolling the transfer instead.")
        else:
            console.print("[bold]This gameweek:[/bold] roll. Nothing available "
                          "beats banking the transfer.")
        console.print("[dim]that number already nets off the four points a hit costs, "
                      "the transfer it spends and the friction charge, because both "
                      "sides are scored on the same objective. Everything after this "
                      "gameweek is the shape of the plan, not an instruction — it "
                      "gets re-solved next week with team news you do not have.[/dim]")

    if args.chip_value:
        console.print()
        with console.status("[cyan]pricing each chip by taking it away…"):
            values = transfers.chip_values(projection, players, **settings)
        _render(values, "What each chip is worth to the plan", ["chip", "gw", "worth"])
        console.print(
            "[dim]measured by re-solving without it: the difference between the best "
            "plan that has the chip and the best plan that does not. That is not the "
            "chip's payout — having a bench boost changes which fifteen you buy in "
            "the weeks before it.[/dim]")
        if not values.empty and values["worth"].abs().max() < 0.01:
            console.print("[dim]all zero because the plan holds every chip: taking "
                          "away something it was not going to play costs nothing. "
                          "Rerun with --ignore-chip-hold to see what they would be "
                          "worth if you had to spend them inside this window.[/dim]")

    _save(path.ledger, args.csv)


def cmd_horizon(args) -> None:
    """How much the recommended squad depends on how far ahead you look."""
    projection = _run_projection(args)
    with console.status("[cyan]solving a squad at every horizon…"):
        table = horizon_sensitivity(
            projection, budget=args.budget, bench_weight=args.bench_weight,
            min_minutes_prob=args.min_start, half_life=args.half_life,
        )

    _render(table, "Squad churn as the horizon is extended",
            ["horizon", "last_gw", "changed_vs_prev", "shared_with_plan"])
    console.print(
        "[dim]changed_vs_prev: players swapped out when you look one gameweek "
        "further ahead. shared_with_plan: overlap with the discounted plan "
        "squad, out of 15.[/dim]\n"
    )

    churn = table["changed_vs_prev"].dropna()
    settled = None
    for horizon, changed in zip(table["horizon"][1:], churn):
        if changed <= 1:
            settled = horizon
            break

    if settled:
        console.print(f"Raw squads settle down by horizon [bold]{settled}[/bold].")
    else:
        console.print(
            f"Raw squads never settle — they still churn "
            f"[bold]{churn.iloc[-1]:.0f}[/bold] players at the longest horizon "
            f"tested. Picking a single horizon means picking one of these "
            f"arbitrarily."
        )
    console.print(
        f"The discounted plan squad shares "
        f"[bold]{table['shared_with_plan'].mean():.1f}/15[/bold] players with the "
        f"raw squads on average, so it sits in the middle of that range rather "
        f"than at either extreme. Use [bold]plan[/bold] rather than a fixed "
        f"horizon, and tune with --half-life."
    )
    _save(table, args.csv)


def cmd_movers(args) -> None:
    """Players whose past numbers were produced at a different club."""
    projection = _run_projection(args)
    df = projection.players
    movers = df[df["moved_club"] & (df["minutes"] >= args.min_minutes)].copy()
    movers = movers.nlargest(args.limit, "price")

    _render(movers, "Changed club since those numbers were recorded",
            ["web_name", "pos", "team_short", "previous_club", "price", "minutes",
             "mins_if_start", "team_context", "npxg_per90", "xpts", "confidence"])
    console.print(
        "[dim]team_context is the multiplier applied to his attacking rates for "
        "the move: above 1 means he joined a better attack than the one his "
        "numbers came from, below 1 the reverse. It is deliberately damped "
        "(square root), since output is part player and part team. These players "
        "also carry a heavier prior, so their rates sit closer to the positional "
        "average than their raw history suggests.[/dim]"
    )
    _save(movers, args.csv)


def cmd_flags(args) -> None:
    """Players whose minutes assumption is worth a second look before trusting it.

    Distinct from `blindspots`: those players have no evidence at all. These
    have evidence, but something about it -- an inconsistent shift length, a
    club move, a fitness doubt -- makes the average a shakier summary than
    usual. See model._minutes_flags.
    """
    projection = _run_projection(args)
    df = projection.players
    flagged = df[df["mins_flags"] != ""].copy()
    flagged = flagged.sort_values("price", ascending=False).head(args.limit)

    _render(flagged, "Minutes worth a manual look",
            ["web_name", "pos", "team_short", "price", "p_start", "mins_if_start",
             "recent_mins_std", "mins_flags"])
    console.print(
        "[dim]mins_flags: volatile minutes = his own recent shift length swings "
        "wide enough that the average may not describe any single week; changed "
        "club = his history may be from a different role; status = the API's "
        "own fitness/squad status, not 'a' (available).[/dim]"
    )
    _save(flagged, args.csv)


def cmd_blindspots(args) -> None:
    """Players the model cannot see, and a template for telling it what you think.

    Every projection is only as good as its minutes assumption, and for a summer
    signing there is no minutes history at all. This lists them and writes an
    overrides CSV you can edit and feed back in with --overrides.
    """
    projection = _run_projection(args)
    blind = projection.players[projection.players["needs_override"]].copy()
    blind = blind.sort_values("price", ascending=False).head(args.limit)

    _render(blind, "Priced players with too little history to project",
            ["web_name", "pos", "team_short", "price", "minutes", "starts",
             "p_start", "xpts", "rate_source"])

    cov = (projection.players.groupby("team_short")["goal_coverage"].first()
           .dropna().sort_values())
    if len(cov):
        table = cov.reset_index()
        table.columns = ["team_short", "goal_coverage"]
        _render(table.head(8), "Clubs whose expected goals the model cannot fully attribute",
                ["team_short", "goal_coverage"])
        console.print(
            "[dim]1.00 means every goal the club is expected to score is assigned to "
            "some player. Below that, the missing share belongs to players with no "
            "Premier League history — it does not distort the players you can see, "
            "but their team-mates are invisible. Override the ones you expect to "
            "play.[/dim]\n")

    template = blind[["fpl_id", "web_name", "pos", "team_short", "price"]].copy()
    template["p_start"] = ""
    template["mins_if_start"] = ""
    template["exp_minutes"] = ""
    _save(template, args.csv or "overrides-template.csv")
    console.print(
        "[dim]fill in p_start (0-1), mins_if_start (0-90, how long his shift is on "
        "the weeks he plays) or exp_minutes (0-90, the two multiplied out) in that "
        "file, delete the rows you do not care about, then rerun with "
        "--overrides out/overrides-template.csv[/dim]"
    )


def cmd_overrides(args) -> None:
    """Show what can be overridden, and write a template for named players."""
    rows = [{"field": field, "min": low, "max": high} for field, (low, high) in OVERRIDABLE.items()]
    _render(pd.DataFrame(rows), "Overridable model inputs", ["field", "min", "max"])
    console.print(
        "[dim]Identify a player by fpl_id or web_name. Set a field to replace the "
        "model's value outright, or add [/dim][bold]_mult[/bold][dim] (e.g. "
        "npxg_per90_mult=1.2) to scale it. An override bypasses shrinkage — if you "
        "assert a number, the model uses that number.[/dim]"
    )

    if not args.players:
        console.print("\n[dim]pass player names to write a filled template, e.g. "
                      "`fpl.py overrides Senesi Rashford`[/dim]")
        return

    projection = _run_projection(args)
    picks = [_resolve(projection.players, name) for name in args.players]
    template = pd.DataFrame([{
        "fpl_id": int(p["fpl_id"]), "web_name": p["web_name"],
        "pos": p["pos"], "team_short": p["team_short"],
        **{f: (None if pd.isna(p.get(f)) else round(float(p[f]), 4))
           for f in OVERRIDABLE if f in p.index},
    } for p in picks])
    _render(template, "Current values — edit these and pass with --overrides")
    _save(template, args.csv or "overrides.csv")


def _local_ip() -> str:
    """Best guess at this machine's address on the local network."""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))  # never sent; just picks the route
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _print_qr(url: str) -> None:
    """Terminal QR so a phone can join without typing a long token."""
    try:
        import qrcode
    except ImportError:
        console.print("[dim]`pip install qrcode` to print a scannable QR code[/dim]")
        return
    code = qrcode.QRCode(border=1)
    code.add_data(url)
    code.make()
    code.print_ascii(invert=True)


def cmd_snapshot(args) -> None:
    """Freeze the projection into the file the offline board runs on."""
    from . import snapshot as snapshot_module

    with console.status("[cyan]projecting and freezing…"):
        path, size = snapshot_module.write(
            path=args.out, horizon=args.horizon, start_gw=args.start_gw,
            recency=args.recency, force_refresh=args.refresh)

    payload = json.loads(Path(path).read_text())
    meta = payload["meta"]
    console.print(f"[bold]{path}[/bold]  [dim]{size / 1024:.0f} KB[/dim]")
    console.print(
        f"[dim]GW{meta['start_gw']}–GW{meta['start_gw'] + meta['horizon'] - 1} · "
        f"{len(payload['players'])} players · "
        f"{meta['odds_coverage']:.0%} of fixtures bookmaker-priced[/dim]")
    console.print("[dim]the board picks this up on its next load; on a phone, pull "
                  "to refresh or press Sync while the laptop is reachable[/dim]")


def cmd_build(args) -> None:
    """Freeze the board into a directory any static host can serve."""
    from . import site
    from . import snapshot as snapshot_module

    out = Path(args.out)
    snapshot_path = Path(args.snapshot) if args.snapshot else None

    if snapshot_path is None or not snapshot_path.exists():
        with console.status("[cyan]projecting and freezing…"):
            written, _ = snapshot_module.write(
                horizon=args.horizon, start_gw=args.start_gw,
                recency=args.recency, force_refresh=args.refresh)
        snapshot_path = Path(written)
        console.print(f"[dim]built {snapshot_path}[/dim]")

    with console.status("[cyan]assembling the site…"):
        counts = site.build(out, snapshot_path)

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    console.print(f"[bold]{out}[/bold]  [dim]{total / 1024:.0f} KB[/dim]")
    console.print(f"[dim]{counts['pages']} pages · {counts['assets']} assets · "
                  f"{counts['shirts']} shirts · {counts['players']} players over "
                  f"{counts['gameweeks']} gameweeks[/dim]")
    console.print(f"[dim]shell: {counts['shell_version']}[/dim]")
    console.print("[dim]serve it with any static host, or check it locally:\n"
                  f"  python -m http.server -d {out} 8001[/dim]")


def cmd_serve(args) -> None:
    """Run the drafting board."""
    import secrets
    import string

    from .server import serve

    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    host = args.host
    if args.lan:
        host, loopback = "0.0.0.0", False

    # The board writes files and spends API quota, so anything beyond loopback
    # needs a token. Refuse rather than quietly exposing it.
    token = args.token or os.environ.get("FPL_TOKEN") or None
    if not loopback and not token:
        if args.insecure:
            console.print("[red]running with NO access control on a non-local "
                          "address — anyone who can reach this port can use "
                          "and modify your data[/red]")
        else:
            # Letters and digits only. token_urlsafe emits "-" and "_", and a
            # token like "aB_cd_" turns into italics the moment the link is
            # pasted into anything that renders markdown -- the underscores are
            # eaten and the link arrives with a token that no longer matches.
            alphabet = string.ascii_letters + string.digits
            token = "".join(secrets.choice(alphabet) for _ in range(16))
            console.print("[dim]no --token given, generated one for this run[/dim]")

    reachable = _local_ip() if host == "0.0.0.0" else host
    url = f"http://{reachable}:{args.port}"
    if token:
        url += f"/?t={token}"

    console.print(f"[bold]FPL drafting board[/bold] → [cyan]{url}[/cyan]")
    if token:
        console.print(f"[dim]token: {token} · keep the whole link, it is the "
                      f"only credential[/dim]")
    if not loopback:
        console.print("[yellow]listening beyond this machine. Anyone who can "
                      "reach this address and has the link has full control of "
                      "the board — do not port-forward it to the open internet; "
                      "use Tailscale or a tunnel with its own auth.[/yellow]")
        _print_qr(url)
    console.print("[dim]first load runs the projection and takes a few seconds; "
                  "ctrl-c to stop[/dim]")

    if not args.no_open and loopback:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    serve(host=host, port=args.port, reload=args.reload, token=token)


def cmd_cache(args) -> None:
    removed = cache.clear(args.namespace)
    console.print(f"removed {removed} cached files")


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fpl",
        description="Project FPL points from bookmaker odds and xG, and build a squad.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--horizon", type=int, default=5,
                         help="number of gameweeks to project (default 5)")
        sub.add_argument("--start-gw", type=int, default=None,
                         help="first gameweek (default: the next one)")
        sub.add_argument("--refresh", action="store_true",
                         help="bypass the cache and refetch every source")
        sub.add_argument("--recency", type=float, default=0.0, metavar="N",
                         help="weight recent matches above early-season ones. "
                              "N is the half-life in gameweeks (10 is a good "
                              "start, 0 = off, lower = more reactive)")
        sub.add_argument("--overrides", default=None,
                         help="CSV of your own numbers. Identify a player by "
                              "fpl_id or web_name, then set any of: "
                              + ", ".join(OVERRIDABLE)
                              + ". Add _mult to any of those to scale the "
                                "model's value instead of replacing it. "
                                "`fpl.py overrides` writes a template.")
        sub.add_argument("--csv", default=None, help="write the result to out/<name>")
        sub.add_argument("--full", action="store_true",
                         help="show the full points breakdown, not just totals")

    def add_filters(sub):
        sub.add_argument("--pos", default=None, help="GKP,DEF,MID,FWD")
        sub.add_argument("--team", default=None, help="team short names, comma separated")
        sub.add_argument("--max-price", type=float, default=None)
        sub.add_argument("--min-price", type=float, default=None)
        sub.add_argument("--limit", type=int, default=25)

    rank = subparsers.add_parser("rank", help="rank players by projected points")
    add_common(rank); add_filters(rank)
    rank.add_argument("--min-start", type=float, default=0.0,
                      help="minimum probability of starting")
    rank.add_argument("--sort", default="xpts",
                      choices=["xpts", "xpts_per_m", "xpts_per_game", "price"])
    rank.set_defaults(func=cmd_rank)

    value = subparsers.add_parser("value", help="best points per million")
    add_common(value); add_filters(value)
    value.add_argument("--min-start", type=float, default=0.5)
    value.set_defaults(func=cmd_value)

    compare = subparsers.add_parser("compare", help="compare two or more players")
    add_common(compare)
    compare.add_argument("players", nargs="+", help="player names")
    compare.add_argument("--squad-test", action="store_true",
                         help="rebuild the optimal squad around each player "
                              "(the fair test of whether the extra money is worth it)")
    compare.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    compare.add_argument("--bench-weight", type=float, default=DEFAULT_BENCH_WEIGHT)
    compare.add_argument("--min-start", type=float, default=0.5)
    compare.set_defaults(func=cmd_compare)

    squad = subparsers.add_parser("squad", help="build the optimal 15")
    add_common(squad)
    squad.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    squad.add_argument("--bench-weight", type=float, default=DEFAULT_BENCH_WEIGHT)
    squad.add_argument("--include", nargs="*", default=None, help="players to force in")
    squad.add_argument("--exclude", nargs="*", default=None, help="players to bar")
    squad.add_argument("--min-start", type=float, default=0.3)
    squad.set_defaults(func=cmd_squad)

    upgrade = subparsers.add_parser(
        "upgrade", help="what else could I buy instead of this player?")
    add_common(upgrade)
    upgrade.add_argument("player")
    upgrade.add_argument("--budget-extra", type=float, default=None,
                         help="how much more than the current player you can spend")
    upgrade.add_argument("--min-start", type=float, default=0.5)
    upgrade.add_argument("--limit", type=int, default=20)
    upgrade.set_defaults(func=cmd_upgrade)

    fixtures = subparsers.add_parser("fixtures", help="expected goals per fixture")
    add_common(fixtures)
    fixtures.set_defaults(func=cmd_fixtures)

    plan = subparsers.add_parser(
        "plan", help="initial squad, fixture timeline, rank risk and chip timing "
                     "(recommended over a fixed horizon)")
    add_common(plan)
    plan.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    plan.add_argument("--bench-weight", type=float, default=DEFAULT_BENCH_WEIGHT)
    plan.add_argument("--half-life", type=float, default=DEFAULT_HALF_LIFE,
                      help="gameweeks until a future point is worth half of one "
                           "now (default 3; lower chases fixtures, higher builds "
                           "for the long run)")
    plan.add_argument("--include", nargs="*", default=None)
    plan.add_argument("--exclude", nargs="*", default=None)
    plan.add_argument("--min-start", type=float, default=0.3)
    plan.add_argument("--ownership-weight", type=float, default=0.0,
                      help="tilt toward widely owned players as insurance "
                           "against the field (0 = pure points, 0.15 = mild)")
    plan.add_argument("--transfer-horizon", type=int, default=DEFAULT_TRANSFER_HORIZON,
                      help=f"gameweeks to plan transfers and chips over "
                           f"(default {DEFAULT_TRANSFER_HORIZON})")
    plan.add_argument("--no-transfer-plan", action="store_true",
                      help="skip the transfer path and chip timing, which is the "
                           "slow part")
    plan.set_defaults(func=cmd_plan, horizon=8)

    tr = subparsers.add_parser(
        "transfers", help="transfers and chips over the window, solved together "
                          "under the free-transfer and hit rules")
    add_common(tr)
    tr.add_argument("--squad", default=None,
                    help="CSV of the fifteen you own (fpl_id or web_name, and "
                         "optionally sell_price). Omit before the season starts, "
                         "when the opening squad is a free choice")
    tr.add_argument("--bank", type=float, default=0.0,
                    help="money not in the squad, in millions")
    tr.add_argument("--free-transfers", type=int, default=1,
                    help="free transfers available this gameweek (1-5)")
    tr.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    tr.add_argument("--transfer-horizon", type=int, default=DEFAULT_TRANSFER_HORIZON,
                    help=f"gameweeks to plan over (default {DEFAULT_TRANSFER_HORIZON})")
    tr.add_argument("--transfer-half-life", type=float, default=TRANSFER_HALF_LIFE,
                    help="gameweeks until a future point is worth half of one now "
                         f"(default {TRANSFER_HALF_LIFE:g}; gentler than the plan's "
                         "because optionality is modelled here rather than assumed)")
    tr.add_argument("--chips-used", nargs="*", default=None, choices=list(CHIPS),
                    help="chips already played this half of the season")
    tr.add_argument("--force-chip", nargs="*", default=None, choices=list(CHIPS),
                    metavar="CHIP",
                    help="chips the plan must play somewhere in the window, "
                         "which asks 'if I play this, what is the best squad "
                         "and the best gameweek for it?' rather than 'should "
                         f"I play it?' (one of: {', '.join(CHIPS)})")
    tr.add_argument("--ignore-chip-hold", action="store_true",
                    help="set every chip's reservation price to zero, which asks "
                         "the narrower question 'when in this window is each chip "
                         "best?' and will spend every one of them")
    tr.add_argument("--chip-value", action="store_true",
                    help="price each chip by re-solving without it (one extra "
                         "solve per chip)")
    tr.add_argument("--hit-limit", type=int, default=None,
                    help="cap the total points hits taken across the window")
    tr.add_argument("--lineups", action="store_true",
                    help="also print the XI, bench order and captain each gameweek")
    tr.add_argument("--include", nargs="*", default=None,
                    help="players to hold for the whole window")
    tr.add_argument("--exclude", nargs="*", default=None)
    tr.add_argument("--min-start", type=float, default=0.3)
    tr.add_argument("--seconds", type=int, default=transfers.SOLVER_SECONDS,
                    help="solver time limit")
    tr.set_defaults(func=cmd_transfers, horizon=DEFAULT_TRANSFER_HORIZON)

    movers = subparsers.add_parser(
        "movers", help="players who changed club, whose past rates describe a "
                       "different team")
    add_common(movers)
    movers.add_argument("--limit", type=int, default=25)
    movers.add_argument("--min-minutes", type=int, default=500)
    movers.set_defaults(func=cmd_movers)

    horizon = subparsers.add_parser(
        "horizon", help="how much the squad depends on the horizon you pick")
    add_common(horizon)
    horizon.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    horizon.add_argument("--bench-weight", type=float, default=DEFAULT_BENCH_WEIGHT)
    horizon.add_argument("--half-life", type=float, default=DEFAULT_HALF_LIFE)
    horizon.add_argument("--min-start", type=float, default=0.3)
    horizon.set_defaults(func=cmd_horizon, horizon=10)

    blind = subparsers.add_parser(
        "blindspots", help="players with too little history to project, plus an "
                           "overrides template")
    add_common(blind)
    blind.add_argument("--limit", type=int, default=40)
    blind.set_defaults(func=cmd_blindspots)

    flags = subparsers.add_parser(
        "flags", help="players whose minutes assumption is worth a manual look "
                      "(volatile shift length, club move, fitness status)")
    add_common(flags)
    flags.add_argument("--limit", type=int, default=40)
    flags.set_defaults(func=cmd_flags)

    ov = subparsers.add_parser(
        "overrides", help="list what you can override, and write a template")
    add_common(ov)
    ov.add_argument("players", nargs="*", help="players to pre-fill with current values")
    ov.set_defaults(func=cmd_overrides)

    snap = subparsers.add_parser(
        "snapshot", help="freeze the projection so the board runs with no server")
    snap.add_argument("--horizon", type=int, default=SNAPSHOT_HORIZON,
                      help=f"gameweeks to freeze (default {SNAPSHOT_HORIZON}); the "
                           "board can show fewer but never more")
    snap.add_argument("--start-gw", type=int, default=None)
    snap.add_argument("--recency", type=float, default=0.0, metavar="N",
                      help="recent-form half-life. Unlike horizon and half-life "
                           "this cannot be changed after the fact, because it "
                           "changes the underlying rates rather than how they "
                           "are weighted")
    snap.add_argument("--refresh", action="store_true",
                      help="bypass the cache and refetch every source first")
    snap.add_argument("--out", default=None, help="where to write it")
    snap.set_defaults(func=cmd_snapshot)

    build_cmd = subparsers.add_parser(
        "build", help="write the whole board to a directory a static host can serve")
    build_cmd.add_argument("--out", default="dist", help="directory to write (default dist/)")
    build_cmd.add_argument("--snapshot", default=None,
                           help="use an existing snapshot instead of projecting")
    build_cmd.add_argument("--horizon", type=int, default=SNAPSHOT_HORIZON)
    build_cmd.add_argument("--start-gw", type=int, default=None)
    build_cmd.add_argument("--recency", type=float, default=0.0, metavar="N")
    build_cmd.add_argument("--refresh", action="store_true",
                           help="bypass the cache and refetch every source first")
    build_cmd.set_defaults(func=cmd_build)

    serve_cmd = subparsers.add_parser(
        "serve", help="run the interactive drafting board in a browser")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)
    serve_cmd.add_argument("--no-open", action="store_true",
                           help="do not open a browser window")
    serve_cmd.add_argument("--reload", action="store_true", help="auto-reload on edit")
    serve_cmd.add_argument("--lan", action="store_true",
                           help="listen on every interface so a phone or another "
                                "machine can reach it (implies a token)")
    serve_cmd.add_argument("--token", default=None,
                           help="shared secret required on every request; also "
                                "read from FPL_TOKEN. Generated automatically "
                                "when listening beyond this machine")
    serve_cmd.add_argument("--insecure", action="store_true",
                           help="allow non-local listening with NO token. Only "
                                "sensible behind a tunnel that does its own auth")
    serve_cmd.set_defaults(func=cmd_serve)

    cache_cmd = subparsers.add_parser("cache-clear", help="empty the HTTP cache")
    cache_cmd.add_argument("--namespace", default=None,
                           choices=["fpl", "understat", "odds"])
    cache_cmd.set_defaults(func=cmd_cache)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        console.print(f"[red]error:[/red] {error}")
        if "--debug" in sys.argv:
            raise
        return 1
    return 0

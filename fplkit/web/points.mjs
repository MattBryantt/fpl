/* Port of the scoring layer: `_player_fixture_points`, `_minutes_scenarios`,
 * `apply_overrides` and the plan weighting, so an override can be recomputed on
 * the phone with no server.
 *
 * Every constant comes from the snapshot's `rules` block, which the exporter
 * reads straight out of fplkit/config.py. Nothing is copied. A scoring change
 * on the Python side reaches this file through the next sync rather than
 * through somebody remembering to edit two places.
 *
 * Verified against the Python for every player in the pool, to 1e-9, by
 * scripts/verify-js-port.mjs.
 */

import { cleanSheetProb, expectedConcessionPenalty, expectedSavePoints,
         probAtLeast } from "./poisson.mjs";

/* ------------------------------------------------------------------ minutes
   p_start, mins_if_start, p_sub, p_play, p60 and exp_minutes are six views of
   one assumption, and the points model reads five of them. Setting p_start
   without re-deriving the rest produces a player who starts every week and
   plays no minutes -- the same trap the server hit through a different door. */

/* One player's shift length, falling back to the league average. The field is
 * younger than the rest of the family, so a snapshot written before it exists
 * reads as the 78 minutes everybody used to be assumed to play — which is
 * exactly what that snapshot was scored with. Port of model._mins_if_start. */
const minsIfStart = (p, rules) =>
  Number.isFinite(p.mins_if_start) ? p.mins_if_start : rules.ASSUMED_START_MINUTES;

/* P(reaches 60 minutes | starts), given how long his shift is. A logistic
 * pinned at two points rather than fitted — an hour-long shift reaches the hour
 * half the time by definition, and the league-average shift reaches it
 * P60_GIVEN_START of the time. Port of model._p60_given_start; the constants
 * ride in on the snapshot, and the older ones are the fallback for a snapshot
 * that predates them (where every shift is 78 minutes anyway). */
function p60GivenStart(minutes, rules) {
  const mid = rules.P60_MIDPOINT_MINUTES;
  const slope = rules.P60_SLOPE_MINUTES;
  if (!Number.isFinite(mid) || !Number.isFinite(slope)) return rules.P60_GIVEN_START;
  return 1 / (1 + Math.exp(-(minutes - mid) / slope));
}

function recomputeMinutes(p, rules) {
  const mins = minsIfStart(p, rules);
  p.p_play = p.p_start + (1 - p.p_start) * p.p_sub;
  p.p60 = p.p_start * p60GivenStart(mins, rules);
  p.exp_minutes = p.p_start * mins
                + (1 - p.p_start) * p.p_sub * rules.ASSUMED_SUB_MINUTES;
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* Inverts the forward minutes formula (see recomputeMinutes) from an explicit
 * exp_minutes. Returns [p_start, mins_if_start]; port of
 * model._solve_exp_minutes, and see there for why the shift is preferred over
 * the start probability — lengthening a man's shift says nothing about anyone
 * else, while raising his p_start takes a shirt off a team-mate. Only when the
 * ninety runs out does p_start absorb the rest, clipped to rules.MAX_P_START
 * to match the ceiling every other minutes path uses. */
function solveExpMinutes(expMinutes, pStart, pSub, rules) {
  const maxMins = rules.MAX_MINS_IF_START ?? 90;
  if (pStart > 0) {
    const shift = (expMinutes - (1 - pStart) * pSub * rules.ASSUMED_SUB_MINUTES) / pStart;
    if (shift <= maxMins) return [pStart, clamp(shift, 0, maxMins)];
  }
  const denom = maxMins - pSub * rules.ASSUMED_SUB_MINUTES;
  const solved = (expMinutes - pSub * rules.ASSUMED_SUB_MINUTES) / denom;
  return [clamp(solved, 0, rules.MAX_P_START), maxMins];
}

/** A copy of `player` with the user's overrides applied, mirroring
 *  model.apply_overrides — including `<field>_mult` and the minutes coupling. */
export function applyOverrides(player, overrides, rules) {
  const p = { ...player };
  if (!overrides) return p;

  let minutesTouched = false, explicitMinutes = false;
  const touched = [];
  for (const [field, [lo, hi]] of Object.entries(rules.OVERRIDABLE)) {
    if (!(field in p)) continue;
    const value = overrides[field];
    const mult = overrides[`${field}_mult`];

    if (value !== undefined && value !== null && !Number.isNaN(value)) {
      p[field] = clamp(Number(value), lo, hi);
      touched.push(field);
    } else if (mult !== undefined && mult !== null && !Number.isNaN(mult)) {
      p[field] = clamp(Number(p[field]) * Number(mult), lo, hi);
      touched.push(`${field}×${mult}`);
    } else {
      continue;
    }
    // exp_minutes is solved last and wins: it is the more specific claim, and
    // solveExpMinutes reads whatever the other three just set. Relies on
    // rules.OVERRIDABLE keeping the Python dict's order, which JSON does.
    if (field === "p_start" || field === "mins_if_start" || field === "p_sub") minutesTouched = true;
    else if (field === "exp_minutes") {
      [p.p_start, p.mins_if_start] =
        solveExpMinutes(p.exp_minutes, p.p_start, p.p_sub, rules);
      minutesTouched = explicitMinutes = true;
    }
  }

  if (minutesTouched) {
    const pinned = explicitMinutes ? p.exp_minutes : null;
    recomputeMinutes(p, rules);
    if (pinned !== null) p.exp_minutes = pinned;
  }
  p.overridden = touched.join(", ");
  return p;
}

/* [probability, minutes, reaches60] for the start case and the substitute case.
   Clean sheets, concessions, saves and defensive contribution are all
   non-linear in minutes, so they are evaluated per scenario and averaged.
   Evaluating once at the mean would be a different — wrong — number.

   reaches60 rides along rather than being recovered from minutes: a start of 78
   minutes is a *mean*, reaching the hour 87% of the time, and a start of 60
   reaches it about half the time. A substitute's 22 minutes never does — that
   curve is calibrated on starts, and a man who came on with twenty left is
   bounded by when he came on. Carrying it here is what keeps p60 equal to the
   probability-weighted sum of this column. */
const minutesScenarios = (p, rules) => {
  const mins = minsIfStart(p, rules);
  return [
    [p.p_start, mins, p60GivenStart(mins, rules)],
    [(1 - p.p_start) * p.p_sub, rules.ASSUMED_SUB_MINUTES, 0],
  ];
};

/** Expected points for one player in one fixture, broken down by source. */
export function playerFixturePoints(player, lamFor, lamAgainst, teamNpxg, teamXgc, rules) {
  const pos = player.pos;
  const minutesShare = player.exp_minutes / 90;

  const lamOpenplay = lamFor * (1 - rules.PENALTY_GOAL_SHARE);
  const attackScale = teamNpxg > 0 ? lamOpenplay / teamNpxg : 1;
  const defenceScale = teamXgc > 0 ? lamAgainst / teamXgc : 1;

  const expGoals = player.npxg_per90 * minutesShare * attackScale;
  const expAssists = player.xa_per90 * minutesShare * attackScale;

  // Penalties go to the designated taker, and only while he is on the pitch --
  // scaled by minutes share like every other rate here, because a penalty can
  // be awarded in the 12 minutes a starter is typically not out there.
  let penGoals = 0, penMiss = 0;
  if (player.penalties_order === 1) {
    const awarded = (lamFor * rules.PENALTY_GOAL_SHARE) / rules.PENALTY_CONVERSION;
    penGoals = awarded * rules.PENALTY_CONVERSION * minutesShare;
    penMiss = awarded * (1 - rules.PENALTY_CONVERSION) * minutesShare;
  }

  const appearance = rules.APPEARANCE_POINTS * player.p_play
                   + rules.APPEARANCE_60_POINTS * player.p60;
  const goalsPts = rules.GOAL_POINTS[pos] * (expGoals + penGoals);
  const assistsPts = rules.ASSIST_POINTS * expAssists;
  const bonusPts = player.bonus_per90 * minutesShare;
  const cardsPts = rules.YELLOW_CARD_POINTS * player.yellow_per90 * minutesShare;
  const penMissPts = rules.PENALTY_MISS_POINTS * penMiss;

  let cleanSheetPts = 0, concedePts = 0, savesPts = 0, dcPts = 0, expCleanSheets = 0;
  const threshold = rules.DEF_CONTRIB_THRESHOLD[pos];
  // The club's full-match figure, reported for context. What a player is paid
  // for is the on-pitch one computed per scenario below.
  const teamCsProb = cleanSheetProb(lamAgainst);

  for (const [probability, minutes, reaches60] of minutesScenarios(player, rules)) {
    if (probability <= 0) continue;
    const share = minutes / 90;
    const lamOnPitch = lamAgainst * share;
    // A clean sheet needs 60 minutes and a starter does not always get them.
    // Without this the model would say he reaches 60 minutes 87% of the time
    // for his appearance point and 100% of the time for his clean sheet.
    // Handed down by minutesScenarios, which is where the two are kept equal.

    // Paid for conceding nothing *while on the pitch*, not for the club keeping
    // a clean sheet -- the same on-pitch lambda the concession term below uses.
    const csProb = cleanSheetProb(lamOnPitch);

    expCleanSheets += probability * reaches60 * csProb;
    if (rules.CLEAN_SHEET_POINTS[pos]) {
      cleanSheetPts += probability * reaches60 * rules.CLEAN_SHEET_POINTS[pos] * csProb;
    }
    if (pos === "GKP" || pos === "DEF") {
      concedePts -= probability * expectedConcessionPenalty(lamOnPitch);
    }
    if (pos === "GKP") {
      const expSaves = player.saves_per90 * share * defenceScale;
      savesPts += probability * rules.SAVE_POINTS * expectedSavePoints(expSaves);
    }
    if (threshold) {
      const expDc = player.dc_per90 * share;
      dcPts += probability * rules.DEF_CONTRIB_POINTS
             * probAtLeast(threshold, expDc, rules.DC_DISPERSION ?? 1);
    }
  }

  const total = appearance + goalsPts + assistsPts + cleanSheetPts + concedePts
              + savesPts + dcPts + bonusPts + cardsPts + penMissPts;

  return {
    xpts: total,
    xpts_appearance: appearance,
    xpts_goals: goalsPts,
    xpts_assists: assistsPts,
    xpts_clean_sheet: cleanSheetPts,
    xpts_conceded: concedePts,
    xpts_saves: savesPts,
    xpts_defcon: dcPts,
    xpts_bonus: bonusPts,
    xpts_cards: cardsPts + penMissPts,
    exp_goals: expGoals + penGoals,
    exp_assists: expAssists,
    exp_clean_sheets: expCleanSheets,
    team_cs_prob: teamCsProb,
  };
}

/** Recompute one edited player across the horizon — the port of
 *  model.reproject_player. `snap` is the loaded snapshot.
 *
 *  `overrides.gw` maps a gameweek to fields that apply to that fixture only,
 *  layered on top of the player-level ones. Rotation, a rest before a European
 *  tie, a return date from injury — none of those are opinions about a player,
 *  they are opinions about a match, and flattening them across the horizon is
 *  the wrong answer rather than an approximate one. */
export function reprojectPlayer(snap, player, overrides) {
  const rules = snap.rules;
  const perMatch = overrides?.gw || null;
  const season = {};
  if (overrides) {
    for (const [k, v] of Object.entries(overrides)) if (k !== "gw") season[k] = v;
  }
  const edited = applyOverrides(player, season, rules);
  const strength = snap.strength[player.team];
  const teamNpxg = strength.npxg_per_match, teamXgc = strength.xgc_per_match;

  const perGw = new Map(snap.gameweeks.map((gw) => [gw, 0]));
  const breakdown = {};
  for (const f of snap.fixtures) {
    const atHome = f.home_team === player.team;
    if (!atHome && f.away_team !== player.team) continue;
    const lamFor = atHome ? f.lam_home : f.lam_away;
    const lamAgainst = atHome ? f.lam_away : f.lam_home;
    const fields = perMatch ? perMatch[f.gw] || perMatch[String(f.gw)] : null;
    const scored = fields ? applyOverrides(edited, fields, rules) : edited;
    const pts = playerFixturePoints(scored, lamFor, lamAgainst, teamNpxg, teamXgc, rules);
    perGw.set(f.gw, (perGw.get(f.gw) || 0) + pts.xpts);
    for (const [k, v] of Object.entries(pts)) breakdown[k] = (breakdown[k] || 0) + v;
  }

  const gw = snap.gameweeks.map((g) => perGw.get(g) || 0);
  const inputs = {};
  for (const field of Object.keys(rules.OVERRIDABLE)) {
    if (field in edited) inputs[field] = edited[field];
  }
  // The minutes family is *derived*, not overridable, so it is absent from
  // `inputs` — and p_play is what the optimiser filters its pool on. Reporting
  // it separately is what stops a caller reading a stale p_play off the
  // original row and concluding an edited player still cannot play.
  const derived = {};
  for (const field of DERIVED_FIELDS) if (field in edited) derived[field] = edited[field];

  return { fpl_id: player.id, gw, xpts: gw.reduce((a, b) => a + b, 0),
           breakdown, inputs, derived, overridden: edited.overridden || "" };
}

// Read by the scoring model, written only by recomputeMinutes.
const DERIVED_FIELDS = ["p_sub", "p_play", "p60", "exp_minutes"];

/** Plan-weighted total: geometric decay on distance, times the survival curve.
 *  Port of planning.decay_weights x survival_curve. `horizon` truncates to the
 *  first N gameweeks, which is exactly what shortening the horizon means once
 *  the per-gameweek points are already known. */
export function planWeight(perGw, hazard, halfLife, horizon = perGw.length) {
  let total = 0;
  for (let i = 0; i < Math.min(horizon, perGw.length); i++) {
    total += perGw[i] * Math.pow(0.5, i / halfLife) * Math.pow(1 - hazard, i);
  }
  return total;
}


import { cleanSheetProb, expectedConcessionPenalty, expectedSavePoints,
         probAtLeast } from "./poisson.mjs";

const minsIfStart = (p, rules) =>
  Number.isFinite(p.mins_if_start) ? p.mins_if_start : rules.ASSUMED_START_MINUTES;

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

const minutesScenarios = (p, rules) => {
  const mins = minsIfStart(p, rules);
  return [
    [p.p_start, mins, p60GivenStart(mins, rules)],
    [(1 - p.p_start) * p.p_sub, rules.ASSUMED_SUB_MINUTES, 0],
  ];
};

export function playerFixturePoints(player, lamFor, lamAgainst, teamNpxg, teamXgc, rules) {
  const pos = player.pos;
  const minutesShare = player.exp_minutes / 90;

  const lamOpenplay = lamFor * (1 - rules.PENALTY_GOAL_SHARE);
  const attackScale = teamNpxg > 0 ? lamOpenplay / teamNpxg : 1;
  const defenceScale = teamXgc > 0 ? lamAgainst / teamXgc : 1;

  const expGoals = player.npxg_per90 * minutesShare * attackScale;
  const expAssists = player.xa_per90 * minutesShare * attackScale;

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
  const teamCsProb = cleanSheetProb(lamAgainst);

  for (const [probability, minutes, reaches60] of minutesScenarios(player, rules)) {
    if (probability <= 0) continue;
    const share = minutes / 90;
    const lamOnPitch = lamAgainst * share;

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
  const derived = {};
  for (const field of DERIVED_FIELDS) if (field in edited) derived[field] = edited[field];

  return { fpl_id: player.id, gw, xpts: gw.reduce((a, b) => a + b, 0),
           breakdown, inputs, derived, overridden: edited.overridden || "" };
}

const DERIVED_FIELDS = ["p_sub", "p_play", "p60", "exp_minutes"];

export function planWeight(perGw, hazard, halfLife, horizon = perGw.length) {
  let total = 0;
  for (let i = 0; i < Math.min(horizon, perGw.length); i++) {
    total += perGw[i] * Math.pow(0.5, i / halfLife) * Math.pow(1 - hazard, i);
  }
  return total;
}


import { reprojectPlayer, planWeight, applyOverrides } from "./points.mjs";

export function truncate(snap, horizon) {
  const count = Math.max(1, Math.min(horizon, snap.gameweeks.length));
  if (count === snap.gameweeks.length) return snap;
  const gameweeks = snap.gameweeks.slice(0, count);
  const keep = new Set(gameweeks);
  return { ...snap, gameweeks, fixtures: snap.fixtures.filter((f) => keep.has(f.gw)) };
}

export const fixtureKey = (f) => `${f.gw}|${f.home_team}|${f.away_team}`;

export function withOddsCalibration(fixtures, calibrateOdds) {
  if (calibrateOdds !== false) return fixtures;
  let changed = false;
  const next = fixtures.map((f) => {
    if (f.lam_home === f.lam_home_uncalibrated && f.lam_away === f.lam_away_uncalibrated) return f;
    changed = true;
    return { ...f, lam_home: f.lam_home_uncalibrated, lam_away: f.lam_away_uncalibrated };
  });
  return changed ? next : fixtures;
}

function applyFixtureEdits(fixtures, fixtureEdits) {
  if (!fixtureEdits || !Object.keys(fixtureEdits).length) return fixtures;
  let changed = false;
  const next = fixtures.map((f) => {
    const edit = fixtureEdits[fixtureKey(f)];
    if (!edit) return f;
    changed = true;
    return {
      ...f,
      lam_home: edit.lam_home ?? f.lam_home,
      lam_away: edit.lam_away ?? f.lam_away,
    };
  });
  return changed ? next : fixtures;
}

const sum = (xs, upTo) => {
  let total = 0;
  for (let i = 0; i < Math.min(upTo, xs.length); i++) total += xs[i];
  return total;
};

const touchesMinutes = (edit) =>
  !!edit && ["p_start", "exp_minutes", "p_start_mult", "exp_minutes_mult"]
    .some((f) => edit[f] !== undefined && edit[f] !== null);

function bisectScale(values, target, maxStart, maxScale) {
  if (!values.length) return values.slice();
  const total = values.reduce((a, b) => a + b, 0);
  if (total <= 0) return values.slice();
  const fielded = (lam) => values.reduce((a, v) => a + Math.min(maxStart, v * lam), 0);
  const cap = fielded(maxScale);
  let lam;
  if (cap <= target) {
    lam = maxScale;
  } else {
    let lo = 0, hi = maxScale;
    for (let i = 0; i < 60; i++) {
      const mid = (lo + hi) / 2;
      if (fielded(mid) < target) lo = mid; else hi = mid;
    }
    lam = (lo + hi) / 2;
  }
  return values.map((v) => Math.min(maxStart, v * lam));
}

export function renormaliseMinutes(snap, edits) {
  const rules = snap.rules;
  const maxStart = rules.MAX_P_START ?? 0.95;
  const maxScale = rules.MAX_MINUTES_SCALE ?? 2.5;
  const outfield = rules.XI_OUTFIELD ?? 10;
  const bisect = (values, target) => bisectScale(values, target, maxStart, maxScale);

  const touched = new Set();
  for (const [id, edit] of Object.entries(edits || {})) {
    if (touchesMinutes(edit)) touched.add(+id);
  }
  if (!touched.size) return new Map();

  const clubs = new Set();
  for (const p of snap.players) if (touched.has(p.id)) clubs.add(p.team);

  const moved = new Map();
  const record = (free, next) => free.forEach((p, i) => {
    if (Math.abs(next[i] - p.p_start) > 1e-9) moved.set(p.id, next[i]);
  });

  for (const club of clubs) {
    const keepers = snap.players.filter((p) => p.team === club && p.pos === "GKP");
    const keeperFree = keepers.filter((p) => !touched.has(p.id));
    if (keeperFree.length) {
      let spokenFor = 0;
      for (const p of keepers) {
        if (touched.has(p.id)) spokenFor += applyOverrides(p, edits[p.id], rules).p_start;
      }
      const remaining = Math.max(1 - spokenFor, 0);
      record(keeperFree, bisect(keeperFree.map((p) => p.p_start), remaining));
    }

    const group = snap.players.filter((p) => p.team === club && p.pos !== "GKP");
    const free = group.filter((p) => !touched.has(p.id));
    if (!free.length) continue;

    let spokenFor = 0;
    const pinnedByPos = new Map();
    for (const p of group) {
      if (!touched.has(p.id)) continue;
      const now = applyOverrides(p, edits[p.id], rules).p_start;
      spokenFor += now;
      const rec = pinnedByPos.get(p.pos) || { orig: 0, now: 0 };
      rec.orig += p.p_start; rec.now += now;
      pinnedByPos.set(p.pos, rec);
    }
    const remaining = Math.max(outfield - spokenFor, 0);

    let claimed = 0;
    const settled = new Set();
    for (const [pos, rec] of pinnedByPos) {
      const posFree = free.filter((p) => p.pos === pos);
      if (!posFree.length) continue;
      const delta = rec.now - rec.orig;
      const values = posFree.map((p) => p.p_start);
      const total = values.reduce((a, b) => a + b, 0);
      const cap = posFree.length * maxStart;
      const want = Math.min(cap, Math.max(0, total - delta));
      record(posFree, bisect(values, want));
      claimed += want;
      posFree.forEach((p) => settled.add(p.id));
    }

    const otherFree = free.filter((p) => !settled.has(p.id));
    if (otherFree.length) {
      const remainingOther = Math.max(remaining - claimed, 0);
      record(otherFree, bisect(otherFree.map((p) => p.p_start), remainingOther));
    }
  }
  return moved;
}

export function effectiveEdits(snap, edits) {
  const moved = renormaliseMinutes(snap, edits);
  if (!moved.size) return edits || {};
  const out = { ...(edits || {}) };
  for (const [id, pStart] of moved) {
    if (!touchesMinutes(out[id])) out[id] = { ...(out[id] || {}), p_start: pStart };
  }
  return out;
}

const hazardOf = (raw, dropout) => (dropout === false ? 0 : raw.hazard || 0);

export function derivePool(snap, edits, { horizon, halfLife, dropout = true, calibrateOdds = true },
                           fixtureEdits) {
  const view = truncate(snap, horizon);
  const count = view.gameweeks.length;
  const overridable = Object.keys(snap.rules.OVERRIDABLE);

  const games = {};
  for (const f of view.fixtures) {
    games[f.home_team] = (games[f.home_team] || 0) + 1;
    games[f.away_team] = (games[f.away_team] || 0) + 1;
  }

  const calibratedFixtures = withOddsCalibration(view.fixtures, calibrateOdds);
  const patchedFixtures = applyFixtureEdits(calibratedFixtures, fixtureEdits);
  const fixturesView = patchedFixtures === view.fixtures ? view : { ...view, fixtures: patchedFixtures };
  const fixtureAffectedTeams = new Set();
  if (patchedFixtures !== view.fixtures) {
    for (let i = 0; i < patchedFixtures.length; i++) {
      const f = patchedFixtures[i], original = view.fixtures[i];
      if (f.lam_home !== original.lam_home || f.lam_away !== original.lam_away) {
        fixtureAffectedTeams.add(f.home_team);
        fixtureAffectedTeams.add(f.away_team);
      }
    }
  }

  const rows = [];

  const userEdits = edits || {};
  const applied = effectiveEdits(snap, userEdits);

  for (const raw of snap.players) {
    const edit = applied[raw.id];
    const personalEdit = !!(edit && Object.keys(edit).length);
    const fixtureAffected = fixtureAffectedTeams.has(raw.team);
    const edited = personalEdit || fixtureAffected;
    const byUser = !!(userEdits[raw.id] && Object.keys(userEdits[raw.id]).length);
    let gw, cs, price, pStart, pPlay, expMinutes;

    if (edited) {
      const out = reprojectPlayer(fixturesView, raw, edit);
      gw = out.gw;
      cs = out.breakdown.exp_clean_sheets || 0;
      price = out.inputs.price;
      pStart = out.inputs.p_start;
      expMinutes = out.derived.exp_minutes ?? raw.exp_minutes;
      pPlay = out.derived.p_play ?? raw.p_play;
    } else {
      gw = raw.gw.slice(0, count);
      cs = sum(raw.cs, count);
      price = raw.price;
      pStart = raw.p_start;
      pPlay = raw.p_play;
      expMinutes = raw.exp_minutes;
    }

    const inputs = {};
    for (const field of overridable) if (field in raw) inputs[field] = raw[field];
    const rawInputs = {};
    for (const field of overridable) {
      const key = `raw_${field}`;
      if (key in raw && raw[key] !== null && raw[key] !== undefined) rawInputs[field] = raw[key];
    }

    const played = games[raw.team] || 0;
    const xptsRaw = sum(gw, count);
    const hazard = hazardOf(raw, dropout);

    rows.push({
      id: raw.id, code: raw.code, name: raw.name, full_name: raw.full_name,
      pos: raw.pos, team: raw.team, team_short: raw.team_short,
      price, p_start: pStart,
      xpts_plan: planWeight(gw, hazard, halfLife),
      xpts_raw: xptsRaw,
      model_xpts_plan: planWeight(raw.gw.slice(0, count), hazard, halfLife),
      hazard,
      games: played,
      xppg: played ? xptsRaw / played : 0,
      ppg: raw.ppg,
      minutes_last: raw.minutes_last,
      ppg_fixture: (raw.pts_last || 0) / 38,
      apps_last: raw.ppg ? Math.round((raw.pts_last || 0) / raw.ppg) : 0,
      cs,
      owned: raw.owned, price_change: raw.price_change,
      confidence: raw.confidence, recency: raw.recency,
      start_long_run: raw.start_long_run, start_recent: raw.start_recent,
      moved: raw.moved, previous_club: raw.previous_club,
      status: raw.status, news: raw.news,
      seasons: raw.seasons || null,
      gw, opp: raw.opp.slice(0, count),
      p_play: pPlay,
      exp_minutes: expMinutes,
      inputs,
      raw_inputs: rawInputs,
      edited: byUser,
      adjusted: personalEdit && !byUser,
      fixture_edited: fixtureAffected,
    });
  }

  return {
    players: rows,
    gameweeks: view.gameweeks,
    meta: {
      ...snap.meta,
      horizon: count,
      half_life: halfLife,
      generated_at: snap.generated_at,
      budget: snap.rules.DEFAULT_BUDGET,
      max_per_club: snap.rules.MAX_PER_CLUB,
      squad_by_pos: snap.rules.SQUAD_BY_POS,
      xi_min: snap.rules.XI_MIN_BY_POS,
      xi_max: snap.rules.XI_MAX_BY_POS,
      bench_slot_weights: snap.rules.DEFAULT_BENCH_SLOT_WEIGHTS,
      priced_gws: (snap.meta.priced_gws || []).filter((gw) => view.gameweeks.includes(gw)),
      snapshot_horizon: snap.gameweeks.length,
    },
  };
}

export const POINT_SOURCES = [
  { key: "xpts_appearance", label: "Appearance", note: "1 for playing, 1 more for 60 minutes" },
  { key: "xpts_goals", label: "Goals", note: "open play plus penalties, if he takes them" },
  { key: "xpts_assists", label: "Assists" },
  { key: "xpts_clean_sheet", label: "Clean sheets", note: "only counts if he is on the pitch" },
  { key: "xpts_defcon", label: "Defensive contribution", note: "2 points when he clears the threshold" },
  { key: "xpts_bonus", label: "Bonus" },
  { key: "xpts_saves", label: "Saves", note: "1 per 3, keepers only" },
  { key: "xpts_conceded", label: "Goals conceded", note: "−1 per 2 conceded while on the pitch", sign: -1 },
  { key: "xpts_cards", label: "Cards and penalty misses", sign: -1 },
];

export function explainPlayer(snap, raw, edit, { horizon, halfLife, dropout = true, calibrateOdds = true },
                              fixtureEdits) {
  const view = truncate(snap, horizon);
  const calibratedFixtures = withOddsCalibration(view.fixtures, calibrateOdds);
  const patchedFixtures = applyFixtureEdits(calibratedFixtures, fixtureEdits);
  const fixturesView = patchedFixtures === view.fixtures ? view : { ...view, fixtures: patchedFixtures };
  const applied = edit && Object.keys(edit).length ? edit : null;
  const out = reprojectPlayer(fixturesView, raw, applied);
  const hazard = hazardOf(raw, dropout);

  const rows = view.gameweeks.map((gw, i) => {
    const decay = Math.pow(0.5, i / halfLife);
    const survival = Math.pow(1 - hazard, i);
    const points = out.gw[i] || 0;
    return {
      gw, points, decay, survival,
      weight: decay * survival,
      weighted: points * decay * survival,
      opp: (raw.opp || [])[i] || "",
    };
  });

  const games = view.fixtures.filter(
    (f) => f.home_team === raw.team || f.away_team === raw.team).length;
  const total = (pick) => rows.reduce((a, r) => a + pick(r), 0);

  return {
    breakdown: out.breakdown,
    inputs: out.inputs,
    derived: out.derived,
    gw: rows,
    games,
    hazard,
    half_life: halfLife,
    raw_total: total((r) => r.points),
    plan_total: total((r) => r.weighted),
    edited: !!applied,
  };
}

export function clubLineup(players, team) {
  const squad = players.filter((p) => p.team === team)
    .sort((a, b) => b.exp_minutes - a.exp_minutes || b.xpts_raw - a.xpts_raw);

  const starters = squad.reduce((a, p) => a + p.p_start, 0);
  const keeper = squad.filter((p) => p.pos === "GKP")
    .reduce((a, p) => a + p.p_start, 0);

  return {
    team,
    players: squad,
    starters,
    keepers: keeper,
    outfield: starters - keeper,
    balanced: Math.abs(starters - 11) < 0.01,
    edited: squad.some((p) => p.edited),
    minutes: squad.reduce((a, p) => a + p.exp_minutes, 0),
  };
}

export function editPlayer(snap, playerId, overrides, { horizon, halfLife, dropout = true, calibrateOdds = true }) {
  const view = truncate(snap, horizon);
  const raw = snap.players.find((p) => p.id === playerId);
  if (!raw) throw new Error(`no player with id ${playerId}`);
  const calibratedFixtures = withOddsCalibration(view.fixtures, calibrateOdds);
  const fixturesView = calibratedFixtures === view.fixtures ? view : { ...view, fixtures: calibratedFixtures };
  const out = reprojectPlayer(fixturesView, raw, overrides);
  out.xpts_plan = planWeight(out.gw, hazardOf(raw, dropout), halfLife);
  return out;
}

export function checkOverridable(fields, snap) {
  const known = new Set(Object.keys(snap.rules.OVERRIDABLE));
  const ok = (f) => known.has(f) || (f.endsWith("_mult") && known.has(f.slice(0, -5)));
  const unknown = Object.keys(fields || {}).filter((f) => f !== "gw" && !ok(f));
  if (unknown.length) throw new Error(`not overridable: ${unknown.sort().join(", ")}`);
  for (const [gameweek, perMatch] of Object.entries(fields?.gw || {})) {
    const bad = Object.keys(perMatch).filter((f) => !ok(f));
    if (bad.length) throw new Error(`not overridable in GW${gameweek}: ${bad.sort().join(", ")}`);
  }
}

const STALE_WARN_HOURS = 24;
const STALE_BAD_HOURS = 72;

export function staleness(generatedAt) {
  const then = new Date(generatedAt);
  if (isNaN(then)) return { label: "unknown age", level: "bad" };
  const minutes = Math.round((Date.now() - then) / 60000);
  const hours = minutes / 60;
  const level = hours >= STALE_BAD_HOURS ? "bad" : hours >= STALE_WARN_HOURS ? "warn" : "fresh";
  const label = minutes < 2 ? "just now"
    : minutes < 90 ? `${minutes} min ago`
    : hours < 36 ? `${Math.round(hours)}h ago`
    : `${Math.round(hours / 24)}d ago`;
  return { label, level };
}

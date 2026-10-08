"use strict";
import { $, S, planOpts, noDecay, transferHalfLife } from "/assets/state.mjs";
import {
  benchWeights, squadCost, sellPrice, squadSellValue, planXI, captaincy, parseFormation, optDiff, renderAll,
} from "/assets/squad-view.mjs";
import { renderCompare, renderSquad } from "/assets/squad-view.mjs";
import { renderGwChart } from "/assets/analysis-view.mjs";
import {
  candidatePool, captainPool, freeTransferValue, chipSlots, fixtureVariation,
  chipPayouts, survivalAdjusted,
} from "/assets/chips.mjs";
import { renderChips, renderWeekIdeal, renderWeekNearMisses } from "/assets/compare-view.mjs";

let solveTimer = null, solveSeq = 0;

export function scheduleSolve(delay = 400) {
  clearTimeout(solveTimer);
  S.optimalState = "pending";
  renderOptStatusSafe();
  solveTimer = setTimeout(() => {
    solveOptimal();
    if (S.tab === "chips") renderChips();
  }, delay);
}
import { renderOptStatus } from "/assets/squad-view.mjs";
function renderOptStatusSafe() { renderOptStatus(); }

export function solverOptions() {
  return {
    budget: +$("#budget").value,
    ownershipWeight: +$("#ownw").value,
    minStart: +$("#minstart").value,
    include: S.include, exclude: S.exclude,
    maxPerClub: +$("#maxclub").value,
    formation: parseFormation($("#formation").value),
    benchSlotWeights: benchWeights(),
    benchSlotProfile: S.snapshot.rules.BENCH_SLOT_PROFILE,
    squadByPos: S.meta.squad_by_pos, xiMin: S.meta.xi_min, xiMax: S.meta.xi_max,
    squadSize: S.snapshot.rules.SQUAD_SIZE, xiSize: S.snapshot.rules.XI_SIZE,
  };
}

export async function solveOptimal() {
  clearTimeout(solveTimer);
  if (!S.meta) return;
  const seq = ++solveSeq;
  S.optimalState = "solving"; S.optimalError = "";
  renderOptStatus();

  let data;
  try {
    data = await solveInWorker(seq, solverOptions());
  } catch (error) {
    if (seq !== solveSeq) return;
    S.optimalState = "error";
    S.optimalError = String(error.message || error);
    S.optimal = []; S.optimalPts = null; S.optimalCost = null; S.optimalBench = {};
    renderCompare(); renderSquad(); renderGwChart();
    return;
  }
  if (seq !== solveSeq) return;

  S.optimal = data.squad;
  S.optimalBench = data.bench || {};
  S.optimalPts = squadTotalRef(S.optimal);
  S.optimalCost = data.cost;
  S.optimalSolveMs = data.ms;
  S.optimalState = "ready";
  renderCompare();
  renderSquad();
  renderGwChart();
}
import { squadTotal as squadTotalRef } from "/assets/squad-view.mjs";

let solverWorker = null;
const solveWaiters = new Map();

function ensureWorker() {
  if (solverWorker) return solverWorker;
  solverWorker = new Worker("/assets/solver-worker.js");
  solverWorker.onmessage = (event) => {
    const { seq, kind, ok, result, error, ms } = event.data;
    const waiter = solveWaiters.get(seq);
    if (!waiter) return;
    if (kind === "progress") { waiter.progress?.(event.data); return; }
    solveWaiters.delete(seq);
    if (ok) waiter.resolve({ ...result, ms }); else waiter.reject(new Error(error));
  };
  solverWorker.onerror = (event) => {
    for (const [, waiter] of solveWaiters) {
      waiter.reject(new Error(event.message || "the solver failed to start"));
    }
    solveWaiters.clear();
    solverWorker.terminate();
    solverWorker = null;
  };
  return solverWorker;
}

const solverPool = () => S.players.map((p) => ({
  id: p.id, pos: p.pos, team: p.team, price: p.price,
  pts: p.xpts_plan || 0, own: p.owned || 0, p_play: p.p_play,
}));

function solveInWorker(seq, options, job = null) {
  return new Promise((resolve, reject) => {
    for (const [, waiter] of solveWaiters) waiter.reject(new Error("superseded"));
    solveWaiters.clear();
    solveWaiters.set(seq, { resolve, reject, progress: job?.onProgress });
    ensureWorker().postMessage({
      seq, pool: job?.pool || solverPool(), options,
      kind: job?.kind, settings: job?.settings,
    });
  });
}

const sortedNearMisses = (rows) =>
  rows.slice().sort((a, b) => (a.gap === null) - (b.gap === null) || a.gap - b.gap);

function nearMissCancelled(state, seq) {
  if (state.seq !== seq || state.state !== "solving") return state;
  return { ...state, state: state.rows.length ? "ready" : "idle",
           tested: state.rows.length, error: "" };
}

export function nearMissKey() {
  return JSON.stringify([
    S.meta ? solverOptions() : null, S.nearMiss.perClub, $("#horizon").value,
    (S.meta ? +$("#gwdecay").value : null), S.snapshot?.generated_at ?? null, S.edits,
  ]);
}

export function nearMissCount() {
  if (!S.meta || !S.optimal.length || !globalThis.FplSolver) return 0;
  return FplSolver.nearMissCandidates(
    solverPool(), solverOptions(), S.optimal, S.nearMiss.perClub).length;
}

export async function runNearMisses() {
  if (!S.meta || !S.optimal.length) return;
  const key = nearMissKey();
  const seq = ++solveSeq;
  S.nearMiss = { ...S.nearMiss, state: "solving", error: "", key, rows: [], seq,
                 progress: { done: 0, total: nearMissCount() } };
  renderNearMissesRef();

  let data;
  try {
    data = await solveInWorker(seq, solverOptions(), {
      kind: "nearmiss",
      settings: { perClub: S.nearMiss.perClub },
      onProgress: ({ done, total, row }) => {
        if (seq !== solveSeq) return;
        S.nearMiss.progress = { done, total };
        if (row) S.nearMiss.rows = sortedNearMisses([...S.nearMiss.rows, row]);
        renderNearMissesRef();
      },
    });
  } catch (error) {
    if (seq !== solveSeq) {
      S.nearMiss = nearMissCancelled(S.nearMiss, seq);
      renderNearMissesRef();
      return;
    }
    S.nearMiss = { ...S.nearMiss, state: "error", rows: [],
                   error: String(error.message || error) };
    renderNearMissesRef();
    return;
  }
  if (seq !== solveSeq) {
    S.nearMiss = nearMissCancelled(S.nearMiss, seq);
    renderNearMissesRef();
    return;
  }

  S.nearMiss = { ...S.nearMiss, state: "ready", rows: data.rows, error: "",
                 tested: data.tested, ms: data.ms, key };
  renderNearMissesRef();
}
import { renderNearMisses as renderNearMissesRef } from "/assets/analysis-view.mjs";

export function weekSquadProblem(plan, idx) {
  const week = plan.weeks[idx];
  const at = plan.gameweeks.indexOf(week.gw);
  if (at < 0) return null;

  const pool = plan.pool.map((p) => ({
    id: p.id, pos: p.pos, team: p.team, price: p.price,
    pts: (p.pts || [])[at] || 0,
    own: S.byId.get(p.id)?.owned || 0,
    p_play: 1,
  }));

  const budget = Math.round(
    (week.squad.reduce((a, id) => a + (S.byId.get(id)?.price || 0), 0) + week.bank) * 10) / 10;

  const boosted = week.chip === "bboost";
  return {
    week, pool,
    options: {
      ...solverOptions(),
      budget,
      ownershipWeight: 0,
      benchSlotWeights: boosted ? { GKP: 1, 1: 1, 2: 1, 3: 1 } : benchWeights(),
      captainMultiplier: week.chip === "3xc" ? 3 : 2,
    },
  };
}

export function weekIdealNote(week, budget) {
  const rebuild = week.chip === "freehit" || week.chip === "wildcard";
  return {
    bboost: "all fifteen score, so the bench is bought to play rather than to be affordable",
    "3xc": "the armband is worth triple, which is worth paying for",
    freehit: "one week only, out of everything selling the squad would raise",
    wildcard: "a permanent rebuild, judged on this week alone",
  }[week.chip] || (rebuild ? "" : "no chip — this is what a rebuild on £"
      + fmtRef(budget, 1) + "m would field this week");
}
import { fmt as fmtRef } from "/assets/state.mjs";

export function weekSquadScore(problem, { starting = [], bench = {}, captain = null }) {
  const pts = new Map(problem.pool.map((p) => [p.id, p.pts]));
  const weights = problem.options.benchSlotWeights || {};
  let total = starting.reduce((a, id) => a + (pts.get(id) || 0), 0);
  for (const [slot, id] of Object.entries(bench)) {
    total += (weights[slot] || 0) * (pts.get(id) || 0);
  }
  if (captain != null) {
    total += ((problem.options.captainMultiplier ?? 2) - 1) * (pts.get(captain) || 0);
  }
  return total;
}

function resumeBoardSolve() {
  if (S.optimalState === "solving" || S.optimalState === "pending") scheduleSolve(0);
}

export async function solveWeekIdeal(idx, { force = false } = {}) {
  const plan = S.transferPlan.result;
  if (!plan || !plan.weeks[idx]) return;
  const key = S.transferPlan.key;
  if (S.weekIdeal.key !== key) S.weekIdeal = { key, byWeek: {} };
  const had = S.weekIdeal.byWeek[idx];
  if (had && !force && had.state !== "error") return;

  const problem = weekSquadProblem(plan, idx);
  if (!problem) return;
  const week = problem.week;
  const seq = ++solveSeq;
  S.weekIdeal.byWeek[idx] = { state: "solving", seq, gw: week.gw,
                              chip: week.chip || null, budget: problem.options.budget };
  renderWeekIdeal();

  const cancelled = () => {
    if (S.weekIdeal.key === key && S.weekIdeal.byWeek[idx]?.seq === seq) {
      delete S.weekIdeal.byWeek[idx];
    }
    renderWeekIdeal();
  };
  const settle = (patch) => {
    if (S.weekIdeal.key !== key || S.weekIdeal.byWeek[idx]?.seq !== seq) return;
    S.weekIdeal.byWeek[idx] = { ...S.weekIdeal.byWeek[idx], ...patch };
    renderWeekIdeal();
  };

  let data;
  try {
    data = await solveInWorker(seq, problem.options, { pool: problem.pool });
  } catch (error) {
    if (seq !== solveSeq) { cancelled(); return; }
    settle({ state: "error", error: String(error.message || error) });
    resumeBoardSolve();
    return;
  }
  if (seq !== solveSeq) { cancelled(); return; }

  const benchBySlot = {};
  for (const [id, slot] of Object.entries(data.bench || {})) benchBySlot[slot] = +id;
  const planBench = {};
  for (const b of week.bench) planBench[b.slot] = b.id;

  settle({
    state: "ready", ms: data.ms, squad: data.squad, starting: data.starting,
    captain: data.captain, bench: data.bench || {}, cost: data.cost,
    points: weekSquadScore(problem,
      { starting: data.starting, bench: benchBySlot, captain: data.captain }),
    held: weekSquadScore(problem,
      { starting: week.starters, bench: planBench, captain: week.captain }),
  });
  resumeBoardSolve();
}

export async function runWeekNearMisses(idx) {
  const plan = S.transferPlan.result;
  if (!plan) return;
  const problem = weekSquadProblem(plan, idx);
  if (!problem) return;

  const seq = ++solveSeq;
  S.weekNearMiss = {
    state: "solving", rows: [], error: "", gw: problem.week.gw, idx,
    chip: problem.week.chip || null, budget: problem.options.budget,
    key: S.transferPlan.key, seq, progress: { done: 0, total: 0 },
  };
  renderWeekNearMisses();

  let data;
  try {
    data = await solveInWorker(seq, problem.options, {
      kind: "nearmiss", pool: problem.pool, settings: {},
      onProgress: ({ done, total, row }) => {
        if (seq !== solveSeq) return;
        S.weekNearMiss.progress = { done, total };
        if (row) S.weekNearMiss.rows = sortedNearMisses([...S.weekNearMiss.rows, row]);
        renderWeekNearMisses();
      },
    });
  } catch (error) {
    if (seq !== solveSeq) {
      S.weekNearMiss = nearMissCancelled(S.weekNearMiss, seq);
      renderWeekNearMisses();
      return;
    }
    S.weekNearMiss = { ...S.weekNearMiss, state: "error",
                       error: String(error.message || error) };
    renderWeekNearMisses();
    return;
  }
  if (seq !== solveSeq) {
    S.weekNearMiss = nearMissCancelled(S.weekNearMiss, seq);
    renderWeekNearMisses();
    return;
  }

  S.weekNearMiss = { ...S.weekNearMiss, state: "ready", rows: data.rows,
                     tested: data.tested, ms: data.ms, ideal: data.squad };
  renderWeekNearMisses();
}

export const transferGwCount = () =>
  Math.min(S.gameweeks.length, +$("#horizon").value || 6);

let transferSeq = 0;

const MAX_PARALLEL_SOLVES = 4;

function runJobs(jobs, isStale, onProgress) {
  const started = performance.now();
  const count = Math.max(1, Math.min(MAX_PARALLEL_SOLVES, (navigator.hardwareConcurrency || 2) - 1, jobs.length));
  const workers = [];
  const results = new Array(jobs.length);
  let next = 0, done = 0;
  const stop = () => workers.forEach((w) => w.terminate());

  return new Promise((resolve, reject) => {
    const feed = (worker) => {
      if (isStale()) { stop(); return; }
      if (next >= jobs.length) { worker.terminate(); return; }
      const i = next++;
      const { pool, opt, tag } = jobs[i];
      worker.onmessage = (event) => {
        results[i] = { tag, ...event.data };
        onProgress({ done: ++done, total: jobs.length });
        if (done === jobs.length) resolve({ results, ms: Math.round(performance.now() - started) });
        else feed(worker);
      };
      worker.postMessage({ pool, opt });
    };
    for (let k = 0; k < count; k++) {
      const worker = new Worker("/assets/transfer-worker.js");
      worker.onerror = (event) => { stop(); reject(new Error(event.message || "the planner failed to start")); };
      workers.push(worker);
      feed(worker);
    }
  });
}

export function transferInputKey() {
  return JSON.stringify([
    S.include, S.exclude, S.poolOut, S.poolIn, S.chipsUsed, S.chipPlan.opt,
    $("#budget").value, $("#maxclub").value, S.meta?.preseason ? 0 : $("#freetransfers").value,
    $("#minstart").value, $("#formation").value, benchWeights(),
    transferGwCount(), noDecay(), S.gameweeks[0] ?? null,
    S.snapshot?.generated_at ?? null, S.edits, chipHoldValuesRef(), ftValueSettingRef(),
  ]);
}
import { chipHoldValues as chipHoldValuesRef, ftValueSetting as ftValueSettingRef } from "/assets/squad-view.mjs";

export function plannerCandidates(squad = []) {
  const rules = S.snapshot.rules;
  const gwCount = transferGwCount();
  const players = S.players.map((p) => ({
    id: p.id, pos: p.pos, team: p.team, price: p.price, p_play: p.p_play,
    hazard: p.hazard, gw: p.gw,
  }));
  const pointsByPlayer = new Map(players.map((p) => [p.id, survivalAdjusted(p, gwCount)]));
  const keep = [...new Set([...squad, ...S.include])];
  const full = candidatePool(players, pointsByPlayer,
    { keep: [...keep, ...S.poolIn], minMinutesProb: +$("#minstart").value, exclude: S.exclude, caps: rules.POOL_BY_POS,
      pricePointCandidates: rules.PRICE_POINT_CANDIDATES });
  const cut = new Set(S.poolOut);
  const pool = full.filter((p) => !cut.has(p.id) || keep.includes(p.id));
  return { players, pointsByPlayer, keep, pool, full };
}

export function buildTransferPayload(squad = []) {
  if (!S.meta || !S.snapshot) return null;
  if (squad.length && squad.length !== 15) return null;
  const side = squad.length ? "own" : "opt";
  const plan = S.chipPlan[side];
  const rules = S.snapshot.rules;
  const gwCount = transferGwCount();
  const gameweeks = S.gameweeks.slice(0, gwCount);

  const { players, pointsByPlayer, pool } = plannerCandidates(squad);

  const cPool = captainPool(pool, pointsByPlayer, rules.CAPTAIN_CANDIDATES);

  const shape = parseFormation($("#formation").value);
  const xiMinByPos = shape ? { GKP: 1, ...shape } : rules.XI_MIN_BY_POS;
  const xiMaxByPos = shape ? { GKP: 1, ...shape } : rules.XI_MAX_BY_POS;
  const slotWeight = benchWeights();

  const variation = fixtureVariation(S.snapshot.fixtures, gameweeks);
  const chips = chipSlots(S.meta.chip_windows || {}, gameweeks, S.chipsUsed, rules.CHIPS);

  const pinned = {};
  for (const [chip, chosen] of Object.entries(plan.chipWeek || {})) {
    if (!Array.isArray(chosen) || !chosen.length || !chips[chip]) continue;
    const narrowed = chips[chip].filter((gw) => chosen.includes(gw));
    if (!narrowed.length) continue;
    chips[chip] = narrowed;
    if (narrowed.length === 1) pinned[chip] = narrowed[0];
  }
  const forceChips = [...new Set([...plan.forceChips, ...Object.keys(pinned)])]
    .filter((chip) => chip in chips);

  const sweepSlots = Object.fromEntries(Object.entries(chips).map(([c, gws]) => [c, [...gws]]));

  const skipped = {};
  for (const chip of plan.chipSkip) {
    if (!(chip in chips)) continue;
    delete chips[chip];
    delete sweepSlots[chip];
    skipped[chip] = "you left it out — not solved";
  }
  if (chips.freehit && !Object.keys(variation).length && !forceChips.includes("freehit")) {
    delete chips.freehit;
    delete sweepSlots.freehit;
    skipped.freehit = "no blank or double to hit";
  }

  const ftWorth = freeTransferValue(ftValueSettingRef(), rules.FT_VALUE_BY_STATE, rules.MAX_FREE_TRANSFERS);
  const budget = +$("#budget").value;
  const sellPrices = Object.fromEntries(squad.map((id) => [id, sellPrice(id)]));
  const bank = squad.length ? Math.max(0, budget - squadSellValue(squad)) : 0;
  const freeTransfers = S.meta.preseason && !squad.length ? 0 : +$("#freetransfers").value;

  const mode = plan.transferMode;
  const wildcardFirst = side === "own" && !!plan.wildcardNow;
  const noTransferGws = mode === "none" ? gameweeks.filter((gw, i) => !(wildcardFirst && i === 0)) : [];
  const hitLimit = mode === "free" ? 0 : null;

  const poolPayload = pool.map((p) => ({ id: p.id, pos: p.pos, team: p.team, price: p.price,
                                         pts: pointsByPlayer.get(p.id) }));
  const opt = {
    gameweeks, budget, squad: [...squad], bank, freeTransfers, sellPrices,
    chips, forceChips, captainPool: cPool,
    slotWeight,
    squadByPos: rules.SQUAD_BY_POS, xiMinByPos, xiMaxByPos,
    squadSize: rules.SQUAD_SIZE, xiSize: rules.XI_SIZE, maxPerClub: +$("#maxclub").value,
    include: [...S.include], exclude: [...S.exclude],
    halfLife: transferHalfLife(), holdValue: chipHoldValuesRef(),
    friction: rules.TRANSFER_FRICTION, ftWorth, maxFreeTransfers: rules.MAX_FREE_TRANSFERS,
    hitCost: rules.HIT_COST, bankValue: rules.BANK_VALUE, freeTransfersPerGw: rules.FREE_TRANSFERS_PER_GW,
    idleMovePenalty: rules.IDLE_MOVE_PENALTY,
    noTransferGws, hitLimit, wildcardFirst,
  };
  return { pool: poolPayload, opt, variation, skipped, forceChips, pinned, sweepSlots,
           pointsByPlayer, gameweeks, chipLabels: rules.CHIP_LABELS, shape, mode,
           positions: new Map(players.map((p) => [p.id, p.pos])) };
}

export function buildTransferJobs(payload) {
  const { pool, opt } = payload;
  const jobs = [
    { tag: { kind: "plan" }, pool, opt },
    { tag: { kind: "baseline" }, pool, opt: { ...opt, chips: {}, forceChips: [] } },
  ];

  for (const [chip, weeks] of Object.entries(opt.chips)) {
    jobs.push({
      tag: { kind: "chip", chip }, pool,
      opt: { ...opt, chips: { [chip]: weeks }, forceChips: [chip] },
    });
  }

  for (const chip of payload.forceChips) {
    for (const gw of payload.sweepSlots[chip] || []) {
      jobs.push({
        tag: { kind: "pin", chip, gw }, pool,
        opt: { ...opt, chips: { [chip]: [gw] }, forceChips: [chip] },
      });
    }
  }
  return jobs;
}

function benchSpend(ids) {
  return ids.map((id) => S.byId.get(id)?.price || 0)
    .sort((a, b) => a - b).slice(0, 4).reduce((a, b) => a + b, 0);
}

export function resolveChipStudy(results, payload) {
  const resolved = {};
  const built = {};
  let baseline = null;

  const chipWeekOf = (result, chip) =>
    [...result.chipByGw.entries()].find(([, c]) => c === chip)?.[0] ?? null;

  for (const { tag, ok, result } of results) {
    if (!ok) continue;
    if (tag.kind === "baseline") {
      baseline = { objective: result.objective, horizonPoints: result.horizonPoints,
                   squad: result.weeks[0]?.squad || [], weeks: result.weeks,
                   bench: benchSpend(result.weeks[0]?.squad || []) };
      continue;
    }
    if (tag.kind === "chip") {
      const squad = result.weeks[0]?.squad || [];
      built[tag.chip] = { objective: result.objective, horizonPoints: result.horizonPoints,
                          gw: chipWeekOf(result, tag.chip), squad, weeks: result.weeks,
                          bench: benchSpend(squad) };
      continue;
    }
    if (tag.kind !== "pin") continue;
    const payouts = chipPayouts({
      squads: new Map(result.weeks.map((w) => [w.gw, w.squad])),
      pointsByPlayer: payload.pointsByPlayer, gameweeks: payload.gameweeks,
      positions: payload.positions, slotWeight: payload.opt.slotWeight,
    });
    (resolved[tag.chip] ||= {})[tag.gw] = {
      payout: payouts[tag.chip]?.[tag.gw] ?? null,
      objective: result.objective,
      horizonPoints: result.horizonPoints,
    };
  }

  for (const b of Object.values(built)) {
    b.worth = baseline ? b.objective - baseline.objective : null;
    b.benchShift = baseline ? b.bench - baseline.bench : null;
    b.changes = baseline
      ? b.squad.filter((id) => !new Set(baseline.squad).has(id)).length : null;
  }
  return { resolved, built, baseline };
}

export async function planTransfersAndChips() {
  const payload = buildTransferPayload();
  if (!payload) return;
  const jobs = buildTransferJobs(payload);
  const key = transferInputKey();
  const seq = ++transferSeq;
  S.planWeek = 0;
  S.weekIdeal = { key: null, byWeek: {} };
  S.transferPlan = { state: "solving", result: null, error: "", key,
                     progress: { done: 0, total: jobs.length } };
  renderChips();

  let data;
  try {
    data = await runJobs(jobs, () => seq !== transferSeq, ({ done, total }) => {
      if (seq !== transferSeq) return;
      S.transferPlan.progress = { done, total };
      renderChips();
    });
  } catch (error) {
    if (seq !== transferSeq) return;
    S.transferPlan = { state: "error", result: null, error: String(error.message || error), key };
    renderChips();
    return;
  }
  if (seq !== transferSeq) return;

  const plan = data.results.find((r) => r.tag.kind === "plan");
  if (!plan || !plan.ok) {
    S.transferPlan = { state: "error", result: null, key,
                       error: plan?.error || "the planner returned nothing" };
    renderChips();
    return;
  }

  const study = resolveChipStudy(data.results, payload);
  const chipWeek = plan.result.weeks.findIndex((w) => w.chip);
  S.planWeek = chipWeek === -1 ? 0 : chipWeek;
  S.transferPlan = { state: "ready", key, error: "",
                     result: { ...plan.result, ...payload, ...study, ms: data.ms } };
  renderChips();
  solveWeekIdeal(S.planWeek);
}

export function ownedInputKey() {
  return JSON.stringify([
    S.squad, S.include, S.exclude, S.poolOut, S.poolIn, S.chipsUsed, S.chipPlan.own,
    $("#budget").value, $("#maxclub").value, $("#freetransfers").value,
    $("#minstart").value, $("#formation").value, benchWeights(),
    transferGwCount(), noDecay(), S.gameweeks[0] ?? null,
    S.snapshot?.generated_at ?? null, S.edits, chipHoldValuesRef(), ftValueSettingRef(),
  ]);
}

export function buildPlanOnlyJobs(payload) {
  const { pool, opt } = payload;
  const jobs = [{ tag: { kind: "plan" }, pool, opt }];
  for (const chip of payload.forceChips) {
    for (const gw of payload.sweepSlots[chip] || []) {
      jobs.push({
        tag: { kind: "pin", chip, gw }, pool,
        opt: { ...opt, chips: { [chip]: [gw] }, forceChips: [chip] },
      });
    }
  }
  return jobs;
}

let ownedSeq = 0;

export async function planOwnedSquad() {
  const payload = buildTransferPayload(S.squad);
  if (!payload) return;
  const jobs = buildPlanOnlyJobs(payload);
  const key = ownedInputKey();
  const seq = ++ownedSeq;
  S.ownedPlanWeek = 0;
  S.ownedPlan = { state: "solving", result: null, error: "", key,
                 progress: { done: 0, total: jobs.length } };
  renderChips();

  let data;
  try {
    data = await runJobs(jobs, () => seq !== ownedSeq, ({ done, total }) => {
      if (seq !== ownedSeq) return;
      S.ownedPlan.progress = { done, total };
      renderChips();
    });
  } catch (error) {
    if (seq !== ownedSeq) return;
    S.ownedPlan = { state: "error", result: null, error: String(error.message || error), key };
    renderChips();
    return;
  }
  if (seq !== ownedSeq) return;

  const plan = data.results.find((r) => r.tag.kind === "plan");
  if (!plan || !plan.ok) {
    S.ownedPlan = { state: "error", result: null, key,
                   error: plan?.error || "the planner returned nothing" };
    renderChips();
    return;
  }

  const study = resolveChipStudy(data.results, payload);
  const chipWeek = plan.result.weeks.findIndex((w) => w.chip);
  S.ownedPlanWeek = chipWeek === -1 ? 0 : chipWeek;
  S.ownedPlan = { state: "ready", key, error: "",
                 result: { ...plan.result, ...payload, ...study, ms: data.ms } };
  renderChips();
}

export function renderChipTableHTML(plan) {
  const built = plan.built || {};
  const baseline = plan.baseline || null;
  const labels = plan.chipLabels;

  const chips = Object.keys(plan.opt.chips);
  const skipped = Object.entries(plan.skipped || {});
  if (!chips.length && !skipped.length) {
    return `<div class="sub" style="margin-top:10px">No chips are legal in this window.</div>`;
  }

  const money = (v) => `£${fmtRef(v, 1)}m`;
  const signed = (v, d = 1) => (v >= 0 ? "+" : "−") + fmtRef(Math.abs(v), d);

  const row = (chip) => {
    const b = built[chip];
    const played = [...plan.chipByGw.entries()].find(([, c]) => c === chip)?.[0] ?? null;
    const pin = plan.pinned?.[chip] ?? null;
    const hold = plan.opt.holdValue[chip] ?? 0;
    if (!b) {
      return `<tr><td>${labels[chip]}</td><td class="na">—</td><td class="na">—</td>
        <td>${fmtRef(hold, 0)}</td><td class="na">—</td><td class="na">—</td>
        <td class="read">could not be solved on its own</td></tr>`;
    }
    const clears = b.worth != null && b.worth >= hold;
    const verdict = [];
    if (pin !== null) verdict.push(`your week`);
    else if (plan.forceChips.includes(chip)) verdict.push(`you committed to it`);
    verdict.push(b.worth == null ? "no chip-free plan to price it against"
      : clears ? `worth playing here — clears the ${fmtRef(hold, 0)} it is worth held`
      : `save it — ${fmtRef(hold - b.worth, 1)} short of what holding it is worth`);
    if (b.benchShift != null && Math.abs(b.benchShift) >= 0.5) {
      verdict.push(`buys a ${b.benchShift > 0 ? "dearer" : "cheaper"} bench`);
    }
    return `<tr class="${played !== null ? "haschip" : ""}">
      <td>${labels[chip]}</td>
      <td>${b.gw == null ? "—" : "GW" + b.gw}</td>
      <td class="${b.worth != null && clears ? "peak" : ""}"><b>${
        b.worth == null ? "—" : signed(b.worth)}</b></td>
      <td>${fmtRef(hold, 0)}</td>
      <td title="${money(b.bench)} on the four it would bench, against ${
        baseline ? money(baseline.bench) : "—"} with no chip">${money(b.bench)}${
        b.benchShift != null && Math.abs(b.benchShift) >= 0.05
          ? ` <span class="shift">${signed(b.benchShift)}</span>` : ""}</td>
      <td>${b.changes == null ? "—" : b.changes}</td>
      <td class="read">${verdict.join(" · ")}</td>
    </tr>`;
  };

  const skipRow = ([chip, why]) => `<tr>
    <td>${labels[chip] || chip}</td><td class="na">—</td><td class="na">—</td>
    <td>${fmtRef(plan.opt.holdValue[chip] ?? 0, 0)}</td>
    <td class="na">—</td><td class="na">—</td><td class="read">${why}</td></tr>`;

  return `
  <h4 style="margin:18px 0 4px">What each chip is worth</h4>
  <div class="sub">Each row is a fifteen <b>built from scratch over the whole horizon knowing
    that chip is coming</b>, against the same build with no chip at all.</div>
  <table class="chiptable worth"><thead><tr>
      <th>Chip</th><th>Week</th>
      <th title="This chip's whole-plan score minus a chip-free plan's, over the horizon">Worth</th>
      <th title="What the chip is worth held for a double or a blank beyond this window">Hold</th>
      <th title="What the squad built for this chip spends on the four it would bench">Bench</th>
      <th title="How many of the fifteen differ from the chip-free build">Changes</th>
      <th class="read">Read</th>
    </tr></thead><tbody>${chips.map(row).join("")}${skipped.map(skipRow).join("")}</tbody></table>
  ${renderChipNoteHTML(plan)}
  ${plan.forceChips.map((chip) => renderChipWeeksHTML(plan, chip)).join("")}`;
}

export function renderChipWeeksHTML(plan, chip) {
  const sweep = plan.resolved?.[chip];
  if (!sweep || Object.keys(sweep).length < 2) return "";
  const entries = plan.gameweeks.filter((gw) => sweep[gw]).map((gw) => ({ gw, ...sweep[gw] }));
  if (!entries.length) return "";
  const best = (key) => entries.reduce((a, b) => (b[key] > a[key] ? b : a), entries[0]);
  const bestObj = best("objective");
  const pin = plan.pinned?.[chip] ?? null;
  const played = [...plan.chipByGw.entries()].find(([, c]) => c === chip)?.[0] ?? null;
  const infeasible = (plan.sweepSlots?.[chip] || []).filter((gw) => !sweep[gw]);
  const open = S.chipFolds[`weeks:${chip}`];

  return `<details class="chipfold"${open ? " open" : ""}>
    <summary data-fold="weeks:${chip}">${plan.chipLabels[chip]} — every gameweek re-solved
      <span class="scope">— ${entries.length} full solves, one per week</span></summary>
    <div class="foldbody">
      <table class="chiptable worth"><thead><tr>
        <th>GW</th><th>Plan total</th><th>vs best</th><th class="read"></th>
      </tr></thead><tbody>${entries.map((e) => {
        const tags = [];
        if (e.gw === played) tags.push(pin === null ? "<b>the plan's week</b>" : "<b>your week</b>");
        if (e.gw === bestObj.gw && e.gw !== played) tags.push("best week");
        const d = e.objective - bestObj.objective;
        return `<tr class="${e.gw === played ? "picked" : ""}">
          <td>GW${e.gw}</td><td>${fmtRef(e.objective, 1)}</td>
          <td>${e.gw === bestObj.gw ? "—" : fmtRef(d, 1)}</td>
          <td class="read">${tags.join(" · ")}</td></tr>`;
      }).join("")}</tbody></table>
      <div class="sub" style="margin-top:6px">Each row is a complete from-scratch solve of the
        whole horizon with the chip pinned to that gameweek, so every week got to build its own
        squad around it. <b>Plan total</b> is the discounted objective the solver maximises, and
        is what picks the week.${
          pin !== null && bestObj.gw !== pin
            ? ` You picked GW${pin}; GW${bestObj.gw} scores ${
                fmtRef(bestObj.objective - (sweep[pin]?.objective ?? 0), 1)} more over the window —
                set the chip back to <b>best week</b> to take it.`
            : pin !== null ? ` You picked GW${pin}, and it is also the best week.` : ""
        }${infeasible.length ? ` GW${infeasible.join(", GW")} had no legal plan with the chip
          pinned there.` : ""}</div>
    </div></details>`;
}

export function renderChipNoteHTML(plan) {
  const gws = plan.gameweeks;
  const rules = S.snapshot.rules;
  const windows = S.meta?.chip_windows || {};
  const ends = Object.entries(windows)
    .filter(([chip]) => chip in plan.opt.chips)
    .map(([, w]) => w[1]);
  const legalTo = ends.length ? Math.max(...ends) : null;
  const reserves = Object.entries(plan.opt.holdValue)
    .map(([c, v]) => `${rules.CHIP_LABELS[c]} ${fmtRef(v, 0)}`).join(", ");
  const base = plan.baseline;

  const lines = [];
  lines.push([`Worth`, `the chip's whole-plan score over GW${gws[0]}–GW${gws[gws.length - 1]}
    minus a chip-free plan's${base ? ` (${fmtRef(base.objective, 1)})` : ""}. Not a payout: it
    already contains the bench the chip made worth buying and whatever the rest of the squad
    gave up to afford it. Both plans build their own fifteen from scratch on the same budget,
    so the difference is the chip and nothing else.`]);
  lines.push([`Week`, `where the from-scratch build chose to play it, with every legal week
    available. Commit to a chip to see what each of the other weeks would have scored.`]);
  lines.push([`Hold`, `what the chip is worth kept for a double or a blank beyond this window —
    so a chip only earns its place here by beating this. <b>Adjustable above</b> (currently
    ${reserves}); the sliders default to the hand-set constants in <code>fplkit/config.py</code>
    as <code>CHIP_HOLD_VALUE</code>, the published aggregates for a chip played on a double or a
    blank, where a double-gameweek bench boost returns 15–25 against 8–12 in an ordinary week.
    Zero them out and a chip left unplayed at the end of the window is worth nothing, so the plan
    burns all three immediately.`]);
  lines.push([`Not your squad`, `this table answers "what is this chip worth to a side built for
    it", not "what should I do with what I own" — nothing here reads the fifteen you own. <b>Your
    squad</b> below is the anchored answer to that second question.`]);
  lines.push([`Window`, `GW${gws[0]}–GW${gws[gws.length - 1]}${
    S.gameweeks.length > gws.length ? ` of the ${S.gameweeks.length} in this snapshot` : ""
  }${legalTo && legalTo > gws[gws.length - 1] ? `; these chips stay legal to GW${legalTo}` : ""}.
    ${noDecay() ? `Decay is off, so every gameweek counts equally and no dropout risk is
         priced in.`
      : `Later gameweeks are discounted (half-life ${fmtRef(rules.TRANSFER_HALF_LIFE, 1)}) and each
         player's points are cut by his chance of dropping out — on a calendar with no doubles
         or blanks that is enough to pick a chip's week on its own. <b>No decay</b> in
         Settings turns both off.`}`]);

  return `<div class="sub chipnote"><dl>${
    lines.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></div>`;
}

export function copyOptimal() {
  if (S.optimal.length !== 15) return;
  if (S.squad.length) {
    const { outgoing } = optDiff();
    const free = +($("#freetransfers")?.value ?? 1);
    const hits = Math.max(0, outgoing.length - free);
    if (hits > 0) {
      const cost = hits * (S.snapshot?.rules?.HIT_COST ?? 4);
      const ok = confirm(
        `This replaces ${outgoing.length} of your 15 players. You have ${free} free `
        + `transfer${free === 1 ? "" : "s"}, so outside a wildcard or free hit this `
        + `would cost ${hits} hit${hits === 1 ? "" : "s"} (−${cost} points).\n\n`
        + `Copy anyway?`);
      if (!ok) return;
    }
  }
  S.squad = [...S.optimal];
  saveSquadRef(); renderAll();
}
import { saveSquad as saveSquadRef } from "/assets/state.mjs";

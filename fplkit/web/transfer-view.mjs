/* The solve engine behind both pitches: the squad optimiser (Fill optimal),
 * the near-miss sweep, the per-gameweek chip rebuild, and the multi-gameweek
 * transfer-and-chip planner (both from-scratch and anchored to your squad).
 * The Chips tab's own chrome lives in compare-view.mjs, which calls back into
 * this module to run and read every one of these solves -- see REFACTOR.md. */
"use strict";
import { $, S, planOpts, noDecay, transferHalfLife } from "/assets/state.mjs";
import {
  benchWeights, squadCost, planXI, captaincy, parseFormation, optDiff, renderAll,
} from "/assets/squad-view.mjs";
import { renderCompare, renderSquad } from "/assets/squad-view.mjs";
import { renderGwChart } from "/assets/analysis-view.mjs";
import {
  candidatePool, captainPool, freeTransferValue, chipSlots, fixtureVariation,
  chipPayouts, survivalAdjusted,
} from "/assets/chips.mjs";
import { renderChips, renderWeekIdeal, renderWeekNearMisses } from "/assets/compare-view.mjs";

/* --------------------------------------------------------- optimal squad
   The solver's answer is not a one-off any more: it is a second column that
   tracks whatever the settings currently say, so changing a bench weight or a
   budget shows you what it costs you rather than leaving you to remember to
   press a button. Copying it across stays deliberate -- the point of the pair
   is that you can disagree with the solver and see exactly where. */
let solveTimer = null, solveSeq = 0;

export function scheduleSolve(delay = 400) {
  clearTimeout(solveTimer);
  S.optimalState = "pending";
  renderOptStatusSafe();
  solveTimer = setTimeout(() => {
    solveOptimal();
    // The chip plan is minutes of work, so a settings change does not re-solve
    // it -- but the tab has to stop presenting the old answer as current. This
    // rides the same debounce as the solve rather than the raw input event:
    // renderChips rebuilds the whole tab, and a slider drag fires on every
    // pixel. Everything that scopes a plan funnels through here.
    if (S.tab === "chips") renderChips();
  }, delay);
}
// renderOptStatus lives in squad-view.mjs alongside the rest of "the other
// squad"; called through a thin wrapper here purely so scheduleSolve (called
// from nearly every settings control) does not need it in its own static
// import on top of the ones above.
import { renderOptStatus } from "/assets/squad-view.mjs";
function renderOptStatusSafe() { renderOptStatus(); }

/** Every constraint the squad MILP answers to, from the controls on screen.
 *  One helper rather than a literal per call site, for the same reason planOpts
 *  is one: the near-miss sweep is a difference between two objectives, and two
 *  objectives are only comparable when both were solved under exactly this. */
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
    // A slower earlier request must never overwrite a faster later one;
    // dragging a slider fires several solves and only the last one describes
    // the settings now on screen.
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
// squadTotal lives in squad-view.mjs; imported separately below to keep the
// import block above focused on what solverOptions/scheduleSolve need.
import { squadTotal as squadTotalRef } from "/assets/squad-view.mjs";

/* The MILP runs in a worker: HiGHS takes up to a couple of seconds on the full
   pool, and on the main thread that is a page that cannot even animate the
   spinner telling you to wait. */
let solverWorker = null;
const solveWaiters = new Map();

function ensureWorker() {
  if (solverWorker) return solverWorker;
  solverWorker = new Worker("/assets/solver-worker.js");
  solverWorker.onmessage = (event) => {
    const { seq, kind, ok, result, error, ms } = event.data;
    const waiter = solveWaiters.get(seq);
    if (!waiter) return;
    // A near-miss sweep reports each candidate as it lands. It is seconds of
    // work, and a spinner for seconds is indistinguishable from a hang.
    if (kind === "progress") { waiter.progress?.(event.data); return; }
    solveWaiters.delete(seq);
    if (ok) waiter.resolve({ ...result, ms }); else waiter.reject(new Error(error));
  };
  solverWorker.onerror = (event) => {
    for (const [, waiter] of solveWaiters) {
      waiter.reject(new Error(event.message || "the solver failed to start"));
    }
    solveWaiters.clear();
    // A worker that has errored stays broken; drop it so the next solve rebuilds.
    solverWorker.terminate();
    solverWorker = null;
  };
  return solverWorker;
}

/** The solver only needs seven fields per player, not the whole row with its
 *  per-gameweek arrays and opponent labels — this crosses a structured-clone
 *  boundary on every keystroke. */
const solverPool = () => S.players.map((p) => ({
  id: p.id, pos: p.pos, team: p.team, price: p.price,
  pts: p.xpts_plan || 0, own: p.owned || 0, p_play: p.p_play,
}));

function solveInWorker(seq, options, job = null) {
  return new Promise((resolve, reject) => {
    // The worker answers only its newest request and drops the rest without a
    // word, so a superseded promise would otherwise never settle — which is
    // survivable when every request is the same kind and the next reply
    // overwrites the state anyway, and is not once a slider can cancel a
    // near-miss sweep and a sweep can cancel a solve. Both callers already
    // ignore a result whose seq has moved on, so rejecting here is silent.
    for (const [, waiter] of solveWaiters) waiter.reject(new Error("superseded"));
    solveWaiters.clear();
    solveWaiters.set(seq, { resolve, reject, progress: job?.onProgress });
    ensureWorker().postMessage({
      // A caller may bring its own pool: the Chips tab asks the same question
      // about one gameweek, where a player is worth that week's points rather
      // than the horizon's.
      seq, pool: job?.pool || solverPool(), options,
      kind: job?.kind, settings: job?.settings,
    });
  });
}

/* --------------------------------------------------------- the near-miss sweep
   Shares the squad optimiser's worker and its sequence counter deliberately.
   It is the same MILP under the same settings -- the gap is a difference
   between two objectives, and two objectives only subtract when both were
   solved under identical constraints -- so a settings change has to cancel a
   sweep in flight rather than let it finish and land a ranking measured against
   a squad that is no longer the answer. */

/** Closest first, with the players no legal fifteen can hold at the bottom —
 *  the same order the worker returns, applied to a part-finished sweep. */
const sortedNearMisses = (rows) =>
  rows.slice().sort((a, b) => (a.gap === null) - (b.gap === null) || a.gap - b.gap);

/** A sweep cut short because something else claimed the worker — a slider moved
 *  while it was running. The rows that did land are still true answers to the
 *  settings they were solved under, so they stay, and the key check above them
 *  says those settings have since moved. What must not survive is the spinner:
 *  the worker drops a superseded request without a word, so nothing else is
 *  coming and "solving…" would sit there for good. */
function nearMissCancelled(state, seq) {
  if (state.seq !== seq || state.state !== "solving") return state;
  return { ...state, state: state.rows.length ? "ready" : "idle",
           tested: state.rows.length, error: "" };
}

/** What the sweep is an answer *to*. Same idea as transferInputKey: nothing
 *  pushes an invalidation at the card, so it re-derives this on render and
 *  compares. The projection is in here through the snapshot stamp and the
 *  edits, because a changed projection changes every gap. */
export function nearMissKey() {
  return JSON.stringify([
    S.meta ? solverOptions() : null, S.nearMiss.perClub, $("#horizon").value,
    (S.meta ? +$("#gwdecay").value : null), S.snapshot?.generated_at ?? null, S.edits,
  ]);
}

/** How many players the sweep would have to solve for, without solving any of
 *  them. It belongs on the button rather than in the result: this is the one
 *  control on the board whose cost is worth knowing before you press it, and
 *  "every club" multiplies it by about seven. */
export function nearMissCount() {
  // The page's copy of the solver is a plain script tag, so a board that came
  // up without it must still render. The sweep itself does not depend on this:
  // the worker loads its own copy, and the count only decides a button label.
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
        // Each row is a finished answer the moment it lands, and a full pool is
        // a second or two per candidate. Showing the ranking as it builds is
        // the difference between half a minute of progress bar and a table you
        // can read before it is finished.
        if (row) S.nearMiss.rows = sortedNearMisses([...S.nearMiss.rows, row]);
        renderNearMissesRef();
      },
    });
  } catch (error) {
    // A settings change cancels the sweep mid-flight and re-solves the squad.
    // That is not an error and must not be reported as one -- but the rows that
    // landed before it are kept, and the spinner is not.
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
// renderNearMisses lives in analysis-view.mjs; imported below (not above)
// purely to keep this file's import ordering matching the section ordering
// it was extracted from.
import { renderNearMisses as renderNearMissesRef } from "/assets/analysis-view.mjs";

/* ------------------------------------------- the same question, one gameweek
   A chip changes which fifteen is best, so it changes who nearly made it. A
   bench boost pays the bench in full and stops the cheap fourth-choice keeper
   being free; a triple captain is worth more to the man who wears it than to
   the squad around him; a free hit or a wildcard buys a side for one week out
   of money the plan has already worked out you can raise. None of that is
   visible in the board's own answer, which is a horizon, an ordinary armband
   and a bench worth a fraction.

   So the Chips tab asks the same question its own way: the plan's chosen week,
   that week's points rather than the discounted horizon, that week's money, and
   the chip's own scoring. Everything it needs is expressible in the squad model
   already -- a bench boost is four bench slots weighted 1.0, a triple captain
   is captainMultiplier 3 -- which is what keeps this one solver and not two.

   One problem statement, two callers. solveWeekIdeal solves it once and shows
   the fifteen that comes back, which is the answer to "what should my squad be
   if I play this chip?"; runWeekNearMisses solves it once per candidate to rank
   the players it left out. Both have to be the same problem or the ranking is
   against a squad nobody is being shown. */

/** The single-gameweek squad problem the selected week of a plan describes:
 *  its pool (priced in that week's points), its budget, and its scoring. */
export function weekSquadProblem(plan, idx) {
  const week = plan.weeks[idx];
  const at = plan.gameweeks.indexOf(week.gw);
  if (at < 0) return null;

  // The plan's own pool, not the board's. Two reasons, and both are about the
  // comparison meaning something: these are the players the transfer LP was
  // allowed to pick from, so an "ideal fifteen" drawn from them is one the plan
  // could actually have reached; and their points are the plan's own numbers
  // for that week -- survival-adjusted, undecayed -- so a gap here reads as
  // points in that gameweek rather than as a slice of a discounted horizon.
  const pool = plan.pool.map((p) => ({
    id: p.id, pos: p.pos, team: p.team, price: p.price,
    pts: (p.pts || [])[at] || 0,
    own: S.byId.get(p.id)?.owned || 0,
    // candidatePool has already applied the availability filter on the way in,
    // so re-applying it here would only be able to drop players the plan has
    // already picked -- which would make the week's own fifteen unrepresentable.
    p_play: 1,
  }));

  // What a rebuild that week could actually spend: what the fifteen the plan
  // fields that week is worth, plus whatever it left in the bank. Derived from
  // the plan rather than taken from the budget slider because a plan that banks
  // money mid-window has less to spend that week than it opened with.
  const budget = Math.round(
    (week.squad.reduce((a, id) => a + (S.byId.get(id)?.price || 0), 0) + week.bank) * 10) / 10;

  const boosted = week.chip === "bboost";
  return {
    week, pool,
    options: {
      ...solverOptions(),
      budget,
      // Template tilt is the one board setting this tab does not honour -- the
      // transfer LP has no ownership term at all, and chipScopeText says so out
      // loud. Leaving it on here would make the rebuilt fifteen disagree with
      // the plan it is being read against for a reason the tab has just denied.
      ownershipWeight: 0,
      // Under a bench boost every one of the fifteen scores, so the four
      // substitutes are worth exactly what a starter is and the model should
      // stop shopping for cheap bodies to fill them.
      benchSlotWeights: boosted ? { GKP: 1, 1: 1, 2: 1, 3: 1 } : benchWeights(),
      captainMultiplier: week.chip === "3xc" ? 3 : 2,
    },
  };
}

/** Why that week's ideal fifteen is the one it is, in a sentence. */
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

/** What a fifteen scores in one gameweek under that week's scoring — the same
 *  objective the rebuild was maximised on, applied to both squads so that the
 *  gap between them is a number and not a vibe. Bench slots pay their weight
 *  (all four in full under a bench boost, which is the whole point of the
 *  chip), and the armband pays its multiplier.
 *
 *  `bench` is `{slot: id}`, the shape neither caller has to hand: the solver
 *  returns `{id: slot}` and the plan returns a list of `{slot, id}`. Inverting
 *  at the call site keeps this function from having to guess which it was
 *  handed. */
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

/** The board's own optimal-squad solve shares this worker and its sequence
 *  counter, so starting anything here cancels one in flight — and solveOptimal
 *  drops a superseded reply silently, which leaves the pitch's status stuck on
 *  "solving…" with nothing coming. Ask for it again once we are out of the way.
 *  Re-entrant by construction: the next rebuild cancels this one too and
 *  re-schedules it in turn, and the debounce collapses the repeats. */
function resumeBoardSolve() {
  if (S.optimalState === "solving" || S.optimalState === "pending") scheduleSolve(0);
}

/** Rebuild one gameweek of the plan from scratch: the best fifteen that week's
 *  money could buy, scored the way that week actually scores.
 *
 *  The plan answers "what should I do, owning what I own?" — hits, banked
 *  transfers, friction and all. That is the right question most of the time and
 *  the wrong one the moment you commit to a chip, because the plan can only
 *  reach a better squad through moves it has to pay for: force a bench boost
 *  with one free transfer and it plays the chip over the bench you already
 *  have, which is not the squad the chip is worth having. This throws the
 *  ownership away and asks what the chip wants, so the two can be read side by
 *  side and the difference between them priced.
 *
 *  One solve, cached per week against the plan's key, and only ever run for a
 *  week you are looking at — seconds against the plan's minutes, but not free.
 *  `force` re-runs a week already answered. */
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

  // A rebuild the settings have already moved past goes back to "not solved"
  // rather than to an error: nothing went wrong, and selecting the week again
  // should simply ask again.
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
    // The plan's own fifteen for that week, priced the same way. Under a bench
    // boost the LP starts all fifteen and benches nobody, so `planBench` is
    // empty and the sum is simply the whole squad -- which is what a bench
    // boost week scores, and is the number the rebuild has to beat.
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
    // Tied to the plan it was run against, not to the settings as they stand:
    // re-planning is what makes this answer wrong, and the tab already says
    // when the plan itself has been left behind.
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

/* ------------------------------------------------------ transfers & chips
   A second, separate worker rather than a shared one: this MILP is a season's
   worth of players times a multi-gameweek horizon, not one gameweek, and
   mixing its "newest wins" sequence counter with the squad optimiser's would
   let an ordinary settings tweak silently cancel a transfer-plan solve
   in flight, or the reverse. Capped to DEFAULT_TRANSFER_HORIZON gameweeks
   independent of the board's own horizon slider (which runs to 12) because
   the pool here is the union of top-points and top-value players per
   position (up to ~190 before overlap), not the ~50 the squad optimiser
   sees -- see transfers.py's own DEFAULT_TRANSFER_HORIZON for why 6 is where
   the CLI stops too: long enough to bank a transfer for a fixture swing,
   short enough that the last gameweek is not pure fiction.

   The board's horizon slider may shorten it but never lengthen it, so picking
   4 gameweeks up there does scope the plan, and picking 12 does not quietly
   turn a forty-second solve into a ten-minute one. */
const TRANSFER_HORIZON_CAP = 6;
export const transferGwCount = () =>
  Math.min(S.gameweeks.length, TRANSFER_HORIZON_CAP, +$("#horizon").value || TRANSFER_HORIZON_CAP);

let transferWorker = null;
const transferWaiters = new Map();
let transferSeq = 0;

function ensureTransferWorker() {
  if (transferWorker) return transferWorker;
  transferWorker = new Worker("/assets/transfer-worker.js");
  transferWorker.onmessage = (event) => {
    const { seq, kind } = event.data;
    const waiter = transferWaiters.get(seq);
    if (!waiter) return;
    if (kind === "progress") { waiter.progress(event.data); return; }
    transferWaiters.delete(seq);
    waiter.resolve(event.data);
  };
  transferWorker.onerror = (event) => {
    for (const [, waiter] of transferWaiters) waiter.reject(new Error(event.message || "the planner failed to start"));
    transferWaiters.clear();
    transferWorker.terminate();
    transferWorker = null;
  };
  return transferWorker;
}

/** Everything the plan is an answer *to*, as one comparable string. The plan
 *  is not persisted and nothing pushes an invalidation at it, so rather than
 *  chasing every control that could move (squad, budget, free transfers, chips
 *  used, constraints, decay, horizon, and every player edit that changes a
 *  projection), the Chips tab re-derives this on render and compares. A plan
 *  whose key no longer matches is shown with a banner rather than blanked:
 *  the old answer is still the best thing on screen until a new one exists. */
export function transferInputKey() {
  // Your squad and your banked transfers are both absent, and their absence is
  // the point: the plan is built from scratch, so neither can change the answer.
  // Leaving them in would have flashed "settings have changed" at every edit to
  // a squad this model never reads.
  return JSON.stringify([
    S.include, S.exclude, S.chipsUsed, S.chipPlan.opt,
    $("#budget").value, $("#maxclub").value,
    $("#minstart").value, $("#formation").value, benchWeights(),
    transferGwCount(), noDecay(), S.gameweeks[0] ?? null,
    S.snapshot?.generated_at ?? null, S.edits, chipHoldValuesRef(), ftValueSettingRef(),
  ]);
}
import { chipHoldValues as chipHoldValuesRef, ftValueSetting as ftValueSettingRef } from "/assets/squad-view.mjs";

/** Reduce the pool, build the ownership facts and the rule constants into
 *  exactly what transfers.js needs — the same "prep vs solve" split as
 *  chips.mjs's own docstring describes. Returns `null` (nothing to plan)
 *  if an owned squad was asked for but isn't complete.
 *
 *  `squad` is empty by default -- the LP's own preseason mode, where the
 *  opening fifteen is a free choice and the tab answers "what is each chip
 *  worth to a side built for it?". Pass your own fifteen (must be complete)
 *  to anchor the solve instead: the plan then pays a transfer and a hit for
 *  every move away from what you hold, and answers the different, more
 *  practical question "given what I own, when should I play my chips and
 *  what do I transfer?" -- see planOwnedSquad(). */
export function buildTransferPayload(squad = []) {
  // No squad requirement in the default (preseason) mode: the plan builds its
  // own fifteen, so it has an answer on an empty board. Anchored mode needs a
  // complete squad or there is nothing to hold the rest against.
  if (!S.meta || !S.snapshot) return null;
  if (squad.length && squad.length !== 15) return null;
  // Which of the two independent chip strategies this solve reads -- inferred
  // from squad the same way the rest of this function already branches on it,
  // rather than a second parameter every caller would have to keep in sync.
  const side = squad.length ? "own" : "opt";
  const plan = S.chipPlan[side];
  const rules = S.snapshot.rules;
  const gwCount = transferGwCount();
  const gameweeks = S.gameweeks.slice(0, gwCount);

  const players = S.players.map((p) => ({
    id: p.id, pos: p.pos, team: p.team, price: p.price, p_play: p.p_play,
    hazard: p.hazard, gw: p.gw,
  }));
  const pointsByPlayer = new Map(players.map((p) => [p.id, survivalAdjusted(p, gwCount)]));

  // Every one of these used to be a constant here while the same control sat
  // in Settings driving the squad optimiser -- so the two halves of the board
  // answered different questions and neither said so. The plan now reads the
  // same knobs the pitch does.
  // Only the players you have *required*, not everyone you happen to own. A
  // from-scratch build has nothing to sell, and forcing them in would quietly
  // widen the candidate set by fifteen on the strength of a squad this model
  // does not read. Anchored mode is the exception: it has to keep your own
  // fifteen in the pool or the plan could not represent selling them.
  const keep = [...new Set([...squad, ...S.include])];
  const pool = candidatePool(players, pointsByPlayer,
    { keep, minMinutesProb: +$("#minstart").value, exclude: S.exclude, caps: rules.POOL_BY_POS,
      pricePointCandidates: rules.PRICE_POINT_CANDIDATES });
  const cPool = captainPool(pool, pointsByPlayer, rules.CAPTAIN_CANDIDATES);

  // A pinned formation is a floor and a ceiling at once. The keeper is always
  // exactly one; the bench-boost week relaxes each cap to the squad's own count
  // for that position, which is handled in the LP rather than here.
  const shape = parseFormation($("#formation").value);
  const xiMinByPos = shape ? { GKP: 1, ...shape } : rules.XI_MIN_BY_POS;
  const xiMaxByPos = shape ? { GKP: 1, ...shape } : rules.XI_MAX_BY_POS;
  const slotWeight = benchWeights();

  const variation = fixtureVariation(S.snapshot.fixtures, gameweeks);
  const chips = chipSlots(S.meta.chip_windows || {}, gameweeks, S.chipsUsed, rules.CHIPS);

  // Narrow each chip's legal window to whatever candidate set you have chosen
  // below, before anything else reads it -- the sweep, the solve count and the
  // LP itself all have to agree on what "legal" means here. Narrowing to
  // exactly one week is a stronger statement than "must play" -- you cannot
  // pin a week for a chip you are not playing -- so it implies the force
  // rather than requiring both controls.
  const pinned = {};
  for (const [chip, chosen] of Object.entries(plan.chipWeek || {})) {
    if (!Array.isArray(chosen) || !chosen.length || !chips[chip]) continue;
    const narrowed = chips[chip].filter((gw) => chosen.includes(gw));
    if (!narrowed.length) continue; // nothing legal left in the chosen set -- ignore it
    chips[chip] = narrowed;
    if (narrowed.length === 1) pinned[chip] = narrowed[0];
  }
  const forceChips = [...new Set([...plan.forceChips, ...Object.keys(pinned)])]
    .filter((chip) => chip in chips);

  // The (possibly narrowed) legal set, kept aside: the sweep compares a chip's
  // candidate weeks, so it has to see every one of those, not just whichever
  // it settles on.
  const sweepSlots = Object.fromEntries(Object.entries(chips).map(([c, gws]) => [c, [...gws]]));

  const skipped = {};
  // Chips you have excluded, dropped before anything is priced. Each one left in
  // costs a solve, so this is the one control on the tab that makes the wait
  // shorter rather than longer — and the row stays in the table saying why it is
  // empty, because a chip that silently vanished would read as a chip the model
  // decided against.
  for (const chip of plan.chipSkip) {
    if (!(chip in chips)) continue;
    delete chips[chip];
    delete sweepSlots[chip];
    skipped[chip] = "you left it out — not solved";
  }
  // A free hit with no blank or double to hit is a week of unlimited transfers
  // you hand straight back — worth about nothing, and a second squad's worth of
  // binaries to discover. Forcing it says to spend them anyway.
  if (chips.freehit && !Object.keys(variation).length && !forceChips.includes("freehit")) {
    delete chips.freehit;
    delete sweepSlots.freehit;
    skipped.freehit = "no blank or double to hit";
  }

  const ftWorth = freeTransferValue(ftValueSettingRef(), rules.FT_VALUE_BY_STATE, rules.MAX_FREE_TRANSFERS);
  const budget = +$("#budget").value;
  // `squad: []` is the LP's own preseason mode, where the opening fifteen is a
  // free choice out of the whole budget and no transfer is charged for
  // reaching it -- the question the Worth table asks. Anchored to your squad,
  // a chip is priced at whatever it pays over the side you actually hold, and
  // every route to a better one is charged a transfer and a hit, same as FPL
  // itself charges you -- the question "what do I do next" asks instead.
  const bank = squad.length ? Math.max(0, budget - squadCost(squad)) : 0;
  const freeTransfers = squad.length ? +$("#freetransfers").value : 0;

  // "No transfers" bans every purchase, which the squad-size constraint turns
  // into no sales either. A free hit is untouched by it on purpose: that chip
  // does not spend transfers, so a no-transfer plan may still field a free-hit
  // side, which is exactly the question "what can I do without transfers?".
  const mode = plan.transferMode;
  const noTransferGws = mode === "none" ? [...gameweeks] : [];
  const hitLimit = mode === "free" ? 0 : null;

  const poolPayload = pool.map((p) => ({ id: p.id, pos: p.pos, team: p.team, price: p.price,
                                         pts: pointsByPlayer.get(p.id) }));
  const opt = {
    gameweeks, budget, squad: [...squad], bank, freeTransfers,
    chips, forceChips, captainPool: cPool,
    slotWeight,
    squadByPos: rules.SQUAD_BY_POS, xiMinByPos, xiMaxByPos,
    squadSize: rules.SQUAD_SIZE, xiSize: rules.XI_SIZE, maxPerClub: +$("#maxclub").value,
    include: [...S.include], exclude: [...S.exclude],
    halfLife: transferHalfLife(), holdValue: chipHoldValuesRef(),
    friction: rules.TRANSFER_FRICTION, ftWorth, maxFreeTransfers: rules.MAX_FREE_TRANSFERS,
    hitCost: rules.HIT_COST, bankValue: rules.BANK_VALUE, freeTransfersPerGw: rules.FREE_TRANSFERS_PER_GW,
    idleMovePenalty: rules.IDLE_MOVE_PENALTY,
    noTransferGws, hitLimit,
  };
  return { pool: poolPayload, opt, variation, skipped, forceChips, pinned, sweepSlots,
           pointsByPlayer, gameweeks, chipLabels: rules.CHIP_LABELS, shape, mode,
           positions: new Map(players.map((p) => [p.id, p.pos])) };
}

/** The jobs one "Plan" press turns into.
 *
 *  Every one of them builds its fifteen from scratch over the whole horizon (see
 *  `buildTransferPayload` for why), and they differ only in which chips the LP
 *  is allowed to reach for:
 *
 *  - `plan` — every legal chip available, the LP free to play or hold each on
 *    its own merits. This is the answer the tab shows.
 *  - `baseline` — no chip at all. The reference every chip is priced against,
 *    and the reason a chip's worth here is a real number rather than a payout:
 *    a chip is worth what the *whole plan* gains from having it, squad and all.
 *  - `chip` — one chip, forced, week free. The squad is built knowing that chip
 *    is coming, which is the thing the ownership-anchored version structurally
 *    could not do: it is what makes a bench boost buy a bench worth fielding
 *    rather than pay out over the cheap one you happened to own.
 *
 *  Isolated on purpose — one chip per solve, the others withheld — because two
 *  chips in one plan share a squad and a budget, and their gains are not
 *  separable. `worth` has to mean "this chip against no chip" or the column
 *  cannot be read down.
 *
 *  So an ordinary press is 2 + one-per-legal-chip solves. Committing to a chip
 *  adds the per-week sweep on top: one further solve per gameweek it could be
 *  played in, which is the only honest way to rank its weeks and is why it is
 *  charged for only when you ask. */
export function buildTransferJobs(payload) {
  const { pool, opt } = payload;
  const jobs = [
    { tag: { kind: "plan" }, pool, opt },
    { tag: { kind: "baseline" }, pool, opt: { ...opt, chips: {}, forceChips: [] } },
  ];

  // `opt.chips` rather than `sweepSlots`, so a week you pinned is honoured here:
  // the question a pin asks is "what is my week worth", not "what is the best
  // week worth". The sweep below is what still shows you the weeks you did not
  // pick.
  for (const [chip, weeks] of Object.entries(opt.chips)) {
    jobs.push({
      tag: { kind: "chip", chip }, pool,
      opt: { ...opt, chips: { [chip]: weeks }, forceChips: [chip] },
    });
  }

  for (const chip of payload.forceChips) {
    // Over `sweepSlots`, not `opt.chips`: if you have pinned the chip to a week
    // then opt.chips holds that one week, and sweeping it would compare your
    // choice against nothing. The whole value of the sweep when a week is
    // pinned is showing what the other weeks would have paid.
    for (const gw of payload.sweepSlots[chip] || []) {
      jobs.push({
        tag: { kind: "pin", chip, gw }, pool,
        opt: { ...opt, chips: { [chip]: [gw] }, forceChips: [chip] },
      });
    }
  }
  return jobs;
}

/** What a fifteen spends on the four it would bench. The number a bench boost
 *  shows up in: a £4.0m fourth keeper and three bodies to fill the sheet become
 *  four players bought to play, and the difference is money taken off the XI. */
function benchSpend(ids) {
  return ids.map((id) => S.byId.get(id)?.price || 0)
    .sort((a, b) => a - b).slice(0, 4).reduce((a, b) => a + b, 0);
}

/** Fold every job's result into the three things the tab reads.
 *
 *  `baseline` — the best from-scratch plan with no chip at all.
 *  `built[chip]` — the best from-scratch plan built knowing that chip is coming:
 *    its objective, the week it lands, the fifteen, and what that fifteen spends
 *    on its bench. `worth` is the gain over `baseline`, which is what the chip is
 *    worth to a side built for it rather than what it pays over a side that
 *    wasn't.
 *  `resolved[chip][gw]` — the per-week sweep, unchanged, for chips you committed
 *    to. A week that came back infeasible is simply absent: the chip cannot be
 *    played there, which the table shows as a dash rather than a zero. */
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

  // Worth needs the baseline, so it is filled in here rather than above: a chip
  // solve that landed while the baseline failed has an objective but no scale to
  // read it on, and `null` says that far better than a number measured off zero.
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
  // A superseded request is dropped by the worker mid-sweep and never answered,
  // so its waiter would sit in the map forever holding a promise nothing can
  // settle. Nothing awaits those promises any more -- the seq check below is
  // what actually guards the result -- but the entries are worth not keeping.
  transferWaiters.clear();
  // The week on screen is an index into a plan that no longer exists, so it
  // goes back to the first gameweek rather than pointing at nothing.
  S.planWeek = 0;
  // Every rebuild answered the old plan's weeks, and the plan about to land may
  // not even have the same gameweeks in it.
  S.weekIdeal = { key: null, byWeek: {} };
  S.transferPlan = { state: "solving", result: null, error: "", key,
                     progress: { done: 0, total: jobs.length } };
  renderChips();

  let data;
  try {
    data = await new Promise((resolve, reject) => {
      transferWaiters.set(seq, {
        resolve, reject,
        progress: ({ done, total }) => {
          if (seq !== transferSeq) return;
          S.transferPlan.progress = { done, total };
          renderChips();
        },
      });
      ensureTransferWorker().postMessage({ seq, jobs });
    });
  } catch (error) {
    if (seq !== transferSeq) return;
    S.transferPlan = { state: "error", result: null, error: String(error.message || error), key };
    renderChips();
    return;
  }
  if (seq !== transferSeq) return;

  // The plan itself is job zero. If *it* failed there is nothing to show, and
  // the error is the solver's own — a pinned week failing is a fact about that
  // week and is handled by resolveChipStudy leaving it out.
  const plan = data.results.find((r) => r.tag.kind === "plan");
  if (!plan || !plan.ok) {
    S.transferPlan = { state: "error", result: null, key,
                       error: plan?.error || "the planner returned nothing" };
    renderChips();
    return;
  }

  const study = resolveChipStudy(data.results, payload);
  // Open on the chip week when there is one. It is the week you pressed the
  // button to see, and defaulting to GW1 is what made a forced chip read as
  // "nothing changed" when the changed squad was two weeks along.
  const chipWeek = plan.result.weeks.findIndex((w) => w.chip);
  S.planWeek = chipWeek === -1 ? 0 : chipWeek;
  S.transferPlan = { state: "ready", key, error: "",
                     result: { ...plan.result, ...payload, ...study, ms: data.ms } };
  renderChips();
  // The chip week opens selected, so rebuild it without being asked: "what
  // should the squad be if I play this?" is the question the press was, and
  // making it a second click leaves the plan's own compromise looking like the
  // answer. One solve, on the week already on screen.
  solveWeekIdeal(S.planWeek);
}

/** Everything the owned-squad plan is an answer to. Same idea as
 *  transferInputKey, plus the one thing that key deliberately leaves out: the
 *  fifteen you own, which this plan is anchored to. */
export function ownedInputKey() {
  return JSON.stringify([
    S.squad, S.include, S.exclude, S.chipsUsed, S.chipPlan.own,
    $("#budget").value, $("#maxclub").value, $("#freetransfers").value,
    $("#minstart").value, $("#formation").value, benchWeights(),
    transferGwCount(), noDecay(), S.gameweeks[0] ?? null,
    S.snapshot?.generated_at ?? null, S.edits, chipHoldValuesRef(), ftValueSettingRef(),
  ]);
}

/** The owned-squad plan's own jobs: the plan itself, anchored to your fifteen,
 *  plus a per-week sweep for any chip you have committed to. No baseline and no
 *  per-chip solve -- there is no "worth against a from-scratch build" question
 *  here, only "what should this squad do", so it costs one solve instead of
 *  the Worth table's 2-plus-one-per-chip. */
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

/** A second, independent instance of the same worker transfer-worker.js runs
 *  on. The worker only ever answers its newest request and abandons anything
 *  still in flight the moment a fresher one arrives (see its own comment) --
 *  fine when there is one plan on the tab, wrong the moment there are two:
 *  posting the owned-squad plan would silently cut off the from-scratch one
 *  mid-solve and vice versa. A dedicated worker per plan is what keeps them
 *  from racing each other. */
let ownedWorker = null;
const ownedWaiters = new Map();
let ownedSeq = 0;

function ensureOwnedWorker() {
  if (ownedWorker) return ownedWorker;
  ownedWorker = new Worker("/assets/transfer-worker.js");
  ownedWorker.onmessage = (event) => {
    const { seq, kind } = event.data;
    const waiter = ownedWaiters.get(seq);
    if (!waiter) return;
    if (kind === "progress") { waiter.progress(event.data); return; }
    ownedWaiters.delete(seq);
    waiter.resolve(event.data);
  };
  ownedWorker.onerror = (event) => {
    for (const [, waiter] of ownedWaiters) waiter.reject(new Error(event.message || "the planner failed to start"));
    ownedWaiters.clear();
    ownedWorker.terminate();
    ownedWorker = null;
  };
  return ownedWorker;
}

/** Solve the transfer-and-chip plan anchored to the fifteen you actually own
 *  (S.squad), so the tab can answer "given what I hold, what do I do" beside
 *  the from-scratch "what would an ideal side do". Manual, not automatic: like
 *  the from-scratch plan, this is minutes of work, so editing your squad marks
 *  the plan stale (see ownedInputKey) rather than kicking off a re-solve on
 *  every shirt clicked. */
export async function planOwnedSquad() {
  const payload = buildTransferPayload(S.squad);
  if (!payload) return;
  const jobs = buildPlanOnlyJobs(payload);
  const key = ownedInputKey();
  const seq = ++ownedSeq;
  ownedWaiters.clear();
  S.ownedPlanWeek = 0;
  S.ownedPlan = { state: "solving", result: null, error: "", key,
                 progress: { done: 0, total: jobs.length } };
  renderChips();

  let data;
  try {
    data = await new Promise((resolve, reject) => {
      ownedWaiters.set(seq, {
        resolve, reject,
        progress: ({ done, total }) => {
          if (seq !== ownedSeq) return;
          S.ownedPlan.progress = { done, total };
          renderChips();
        },
      });
      ensureOwnedWorker().postMessage({ seq, jobs });
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

/* --------------------------------------------------------------------- chip
   worth table -- a chip per row, priced against a plan that plays no chip
   at all.

   What changed and why it matters: every earlier version of this table was
   anchored to the fifteen you own. It asked "what does this chip pay over the
   squad I happen to have, given every route to a better one costs a transfer
   and a hit?" — which is not what a chip is worth. A bench boost got scored
   against the cheap bench you own rather than against the bench you would buy
   knowing the chip was coming, and that gap *is* the chip.

   So each chip now gets its own from-scratch horizon solve with the chip forced
   and the week free, and `worth` is that plan's objective minus a chip-free
   plan's. It is a whole-plan difference, not a payout: it already contains the
   bench the chip made worth buying, the captain it made worth paying for, and
   whatever the rest of the squad gave up to afford them.

   `Bench` is quoted beside it because that is where a bench boost shows up in
   money, and `Changes` because a chip that reshapes nothing (a triple captain
   usually) is a different kind of decision from one that reshapes four players. */
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
    // Against the chip-free plan, which is the only comparison that makes the
    // number mean "what is this chip worth". Reading it against the reserve is
    // the second question and gets its own clause.
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

/** The per-week grid, for a chip you have committed to — the only place it is
 *  still drawn, because it costs a solve a gameweek.
 *
 *  Folded away rather than stacked under the table: committing to one chip
 *  should not push the plan itself off the screen, and this answers a narrower
 *  question than the table above ("when?", not "whether?"). */
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

/** The one footnote under the one table: what the numbers are, where they came
 *  from, and what the plan could not see. */
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

/** Copies the from-scratch optimal squad over whatever you currently hold.
 *  Cheap and correct when you have genuinely wildcarded or free-hit -- the
 *  rebuild really is free. Anywhere else, replacing more players than you
 *  have free transfers for means real hits, so this warns rather than
 *  silently assuming the wildcard framing applies. Reuses optDiff(), the
 *  same incoming/outgoing sets the compare pitch already highlights. */
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

/* Board state: the S object, localStorage persistence, and the settings
 * helpers nearly every other module needs. Extracted first because
 * everything else depends on it — see REFACTOR.md for the split this
 * belongs to. */
"use strict";
import { pushOverrides, markSynced } from "/assets/sync.mjs";

export const $ = (s) => document.querySelector(s);

export const POS_ORDER = ["GKP", "DEF", "MID", "FWD"];
export const SVGNS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------- persistence
   Drafts and overrides live in localStorage, because on the phone there is no
   server to write them to and losing a squad you spent twenty minutes on
   because you closed a tab is not acceptable. The laptop's out/drafts.json and
   out/overrides.csv are still written on sync, so the CLI keeps working from
   the same squads -- but this is the copy that is always there. */
export const STORE = { drafts: "fpl.drafts", edits: "fpl.edits", editsAt: "fpl.editsAt",
                editsHistory: "fpl.edits.history", squad: "fpl.squad", purchase: "fpl.purchase",
                settings: "fpl.settings", posTags: "fpl.postags",
                lineupOrder: "fpl.lineuporder", fixtureEdits: "fpl.fixtureedits" };

export function loadLocal(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch (_) { return fallback; }
}
export function saveLocal(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode */ }
}

/* A rolling local backup of S.edits, independent of sync entirely -- sync only
   protects against one device clobbering another; it does nothing for "the
   merge logic itself has a bug" or "I want yesterday's numbers back". Capped
   so it cannot grow without bound, and pruned by age for the same reason. */
export const EDITS_HISTORY_MAX = 20, EDITS_HISTORY_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
export function snapshotEditsHistory() {
  const cutoff = Date.now() - EDITS_HISTORY_MAX_AGE_MS;
  const history = loadLocal(STORE.editsHistory, []).filter((s) => s.at > cutoff);
  const last = history[history.length - 1];
  // An empty set used to return early, on the grounds that there was nothing
  // worth protecting. That skipped the one event most worth recording: going
  // from a full set of overrides to none is what a bad merge looks like, and
  // refusing to log it left the newest entry dated before the loss, with
  // nothing to say the loss had happened at all. A device that has never had
  // an override still records nothing, because there is no `last` to leave.
  if (!Object.keys(S.edits).length && !last) return;
  const snapshot = JSON.stringify(S.edits);
  if (last && last.snapshot === snapshot) return; // unchanged since last save
  history.push({ at: Date.now(), snapshot });
  saveLocal(STORE.editsHistory, history.slice(-EDITS_HISTORY_MAX));
}

/* Edits are saved twice, to two stores with different jobs. localStorage is the
   copy that is always there, including on a phone on a train. out/overrides.csv
   is the copy the CLI reads, and it only exists when the laptop is reachable --
   so it is written on a debounce, silently, and its failure is not an error. */
export const saveEdits = () => {
  saveLocal(STORE.edits, S.edits); saveLocal(STORE.editsAt, S.editsAt);
  snapshotEditsHistory(); pushOverrides(); markSynced();
};
/** `S.squad` stays fpl_id-keyed in memory -- every consumer (pitch, transfer
 *  payload, solver) already expects that. Only what gets *persisted* is
 *  code-keyed, since fpl_id is reassigned every season and code is not; this
 *  is the boundary that translates between the two. */
export const squadCodes = () => S.squad.map((id) => S.byId.get(id)?.code).filter((c) => c != null);
/** What each owned player was bought for, keyed the same way as the squad is
 *  persisted. Pruned to the squad on every save: a player you drop takes his
 *  purchase price with him, so if he comes back he is bought at today's price. */
export const purchaseCodes = () => Object.fromEntries(S.squad
  .filter((id) => S.purchase[id] != null && S.byId.get(id)?.code != null)
  .map((id) => [S.byId.get(id).code, S.purchase[id]]));
export const saveSquad = () => {
  for (const id of Object.keys(S.purchase)) if (!S.squad.includes(+id)) delete S.purchase[id];
  saveLocal(STORE.squad, squadCodes()); saveLocal(STORE.purchase, purchaseCodes()); markSynced();
};
/** Resolve a saved/synced array of player `code`s back to this season's
 *  fpl_ids, dropping (rather than crashing on) anything no longer in the
 *  pool -- a code with no match this season, not merely an id that moved. */
export const resolveSquadCodes = (codes) =>
  (codes || []).map((code) => S.byCode.get(code)).filter((id) => id != null);
export const resolvePurchaseCodes = (byCode) => Object.fromEntries(
  Object.entries(byCode || {}).map(([code, price]) => [S.byCode.get(+code), +price])
    .filter(([id, price]) => id != null && Number.isFinite(price)));

/** One plan's worth of chip strategy -- see S.chipPlan for what each field
 *  means. A function rather than a shared literal, so the two sides start
 *  from independent objects instead of the same array by reference. */
export function newChipPlanState() {
  return { forceChips: [], chipWeek: {}, chipSkip: [], transferMode: "plan" };
}

/** The two chipPlan fields worth remembering across a reload -- transferMode
 *  and chipSkip are standing preferences; forceChips and chipWeek are a
 *  question asked of one solve and die with it (see S.chipPlan's comment). */
export const persistableChipPlan = () => ({
  opt: { transferMode: S.chipPlan.opt.transferMode, chipSkip: [...S.chipPlan.opt.chipSkip] },
  own: { transferMode: S.chipPlan.own.transferMode, chipSkip: [...S.chipPlan.own.chipSkip] },
});
export function applyChipPlanSettings(saved) {
  for (const side of ["opt", "own"]) {
    const s = saved?.[side];
    if (!s) continue;
    if (TRANSFER_MODES_VALUES.includes(s.transferMode)) S.chipPlan[side].transferMode = s.transferMode;
    if (Array.isArray(s.chipSkip)) S.chipPlan[side].chipSkip = s.chipSkip.slice();
  }
}
// TRANSFER_MODES itself lives in transfer-view.mjs (it is a Chips-tab-only
// concept), but applyChipPlanSettings needs to validate against it and runs
// long before that module's controls exist on screen. The three legal values
// are fixed by the LP itself (transfers.py), not by the UI describing them,
// so a plain literal here does not risk drifting from transfer-view.mjs's copy.
const TRANSFER_MODES_VALUES = ["plan", "free", "none"];

export const S = {
  snapshot: null,
  players: [], byId: new Map(), byCode: new Map(), gameweeks: [], meta: null,
  squad: [], optimal: [], optimalPts: null, optimalCost: null, optimalBench: {},
  // fpl_id -> price paid, for the players in `squad` it is known for. Absent
  // means bought at today's price, so sells at it -- see squad-view's sellPrice.
  purchase: {},
  optimalState: "idle", optimalError: "",
  pos: "ALL", search: "", sort: "xpts_plan", dir: -1,
  edits: loadLocal(STORE.edits, {}), editsAt: loadLocal(STORE.editsAt, {}),
  drafts: loadLocal(STORE.drafts, []),
  // Opinions about a match rather than about a player -- an overridden xG for
  // one or both sides of a fixture. Device-local like posTags/lineupOrder
  // below, not part of the edits sync: it is a "what if" you are trying, not a
  // fact about a player worth reconciling across devices.
  fixtureEdits: loadLocal(STORE.fixtureEdits, {}),
  // Detailed positions (LW, CB, and the rest) FPL itself does not carry --
  // see position-tags.mjs. Keyed by id here, unlike the seed it starts from,
  // because this is a browser preference rather than something meant to
  // travel with the player across a season rollover.
  posTags: loadLocal(STORE.posTags, {}),
  // Per-club, per-row left-to-right order on the team-lineup pitch: {team: {pos:
  // [ids]}}. Only ever set by the ‹ › nudge buttons; everything else falls back
  // to the tag-based lane order (see TAG_LANE, effectiveRowOrder).
  lineupOrder: loadLocal(STORE.lineupOrder, {}),
  compare: [], draftsPath: "", include: [], exclude: [],
  // Chips already played this half of the season, for the transfer-and-chip
  // plan -- a fact about the plan like include/exclude, so it travels the
  // same way (readSettings/syncableSettings), not through STORE directly.
  // Shared by both plans below: it is a fact about the season, not a strategy
  // choice either one is free to answer differently.
  chipsUsed: [],
  // Everything about *how* to play the chips, kept once per plan -- "opt" for
  // the from-scratch build, "own" for the one anchored to your squad -- so a
  // forced bench boost on one side does not silently show up forced on the
  // other. Each bucket is CHIP_PLAN_DEFAULTS's own shape:
  //   forceChips  -- chips the next solve must play (lives and dies with the
  //                  plan, not saved: a question you are asking, not a fact).
  //   chipWeek    -- {chip: [gw, ...]}, the candidate weeks you have narrowed
  //                  a chip to. Down to one is a pin, which implies the force.
  //   chipSkip    -- chips to leave out of the solve entirely, to shorten the
  //                  wait. Saved, since it is a standing preference.
  //   transferMode -- "plan" (as it likes), "free" (no hits), "none" (never
  //                  change the opening side). Saved alongside chipSkip.
  chipPlan: { opt: newChipPlanState(), own: newChipPlanState() },
  views: { gw: "chart", exp: "chart", tl: "chart", plan: "chart" },
  // How you have chosen to look at the board: which section, which numbers ride
  // on a shirt, which squad the right-hand pitch holds, and any two players
  // being read against each other. All saved -- re-choosing them on every load
  // is the thing that makes a phone app feel like a web page.
  tab: "squad", metrics: ["xpts", "xppg", "price"],
  compareWith: "optimal", versus: [],
  // The transfer-and-chip plan: not persisted or synced, same treatment as
  // S.optimal* -- it is a re-solvable answer to the current squad and
  // settings, not a fact worth remembering across a reload. `key` is the
  // fingerprint of the inputs it answers (see transferInputKey), so the tab
  // can tell a current plan from one the settings have moved out from under.
  transferPlan: { state: "idle", result: null, error: "", key: null },
  // Which gameweek of that plan the pitch is showing, as an index into
  // result.weeks. The plan is six weeks long and the chip is rarely in the
  // first of them; showing only week one is what made a forced chip look like
  // it had changed nothing.
  planWeek: 0,
  // Who nearly made the optimal fifteen. Not persisted and not solved on load,
  // for the same reason the transfer plan is not: it is seconds of solving that
  // most visits do not ask for. `key` fingerprints the settings it answers, so
  // the card can say when it has been left behind rather than quietly present a
  // stale ranking as current.
  nearMiss: { state: "idle", rows: [], error: "", key: null, tested: 0,
              progress: { done: 0, total: 0 }, perClub: false },
  // The same question asked of one gameweek of the transfer plan, where the
  // chip in force changes both the fifteen and who nearly made it. Keyed by
  // week index rather than shared with the above, because the two answer
  // different questions and showing one where the other belongs would be a lie
  // about which scoring produced it.
  weekNearMiss: { state: "idle", rows: [], error: "", idx: null, gw: null,
                  key: null, progress: { done: 0, total: 0 } },
  // The fifteen a gameweek would field if it were built for that gameweek from
  // scratch, chip and all -- one squad solve per week of the plan, cached
  // against the plan's own key in `byWeek` (index into result.weeks).
  //
  // This is the answer the plan structurally cannot give. The plan starts from
  // the fifteen you own and every step towards a better one costs a transfer, a
  // hit, or both, so a forced bench boost lands on the bench you already have
  // rather than on the bench the chip wants. Asking "what should the squad be
  // if I play this?" means throwing the ownership away and re-solving, which is
  // exactly what this is.
  weekIdeal: { key: null, byWeek: {} },
  // The same transfer-and-chip plan, anchored to the fifteen you actually own
  // instead of built from scratch -- "given what I hold, when should I play
  // my chips and what do I transfer?" rather than "what would an ideal side
  // do?". Lives on its own worker (see ensureOwnedWorker) so re-solving it
  // never races the from-scratch plan above; not persisted, for the same
  // reason transferPlan is not.
  ownedPlan: { state: "idle", result: null, error: "", key: null, progress: null },
  ownedPlanWeek: 0,
  // Which of the Chips tab's collapsible blocks you have opened. Both boxes are
  // re-rendered wholesale whenever their solve advances, which throws away the
  // <details> element's own open state, so it is held here instead — a block
  // snapping shut mid-solve because a progress tick redrew it is worse than the
  // wall of stacked sections the folds exist to fix.
  chipFolds: { ideal: false, near: false },
};
// Held as raw codes until the snapshot loads and S.byCode exists to resolve
// them against -- see rebuildPool(), which consumes and clears this once.
S.squadCodesPending = loadLocal(STORE.squad, []);
S.purchaseCodesPending = loadLocal(STORE.purchase, {});

/* Overrides saved before per-id timestamps existed load with nothing in
   editsAt, and mergeEdits reads a missing entry as 0 -- which loses every
   comparison against a device that has touched anything since. Losing is not
   the damaging part; the damaging part is what losing means here. The winning
   side has no edit for that id, so the id is dropped rather than kept, and the
   smaller set is then pushed as authoritative. One newer edit on one device is
   enough to erase an older set everywhere, which is exactly the failure the
   timestamps were added to prevent.
   Stamping the untimestamped ones on first load is the migration that change
   needed. It does not claim they are newer than a real edit made later -- only
   that they are not older than the epoch. */
(function backfillEditsAt() {
  const now = Date.now();
  let changed = false;
  for (const id of Object.keys(S.edits)) {
    if (!S.editsAt[id]) { S.editsAt[id] = now; changed = true; }
  }
  if (changed) saveLocal(STORE.editsAt, S.editsAt);
})();

/* Module scope means nothing here is reachable from the console any more, which
   is right for the page and wrong for debugging it — and for the browser tests,
   which otherwise have to infer state by reading rendered text. One named
   handle, read-only by convention. */
globalThis.board = S;

/* ---------------------------------------------------------------- settings
   Every knob is written back as you touch it, so the board opens where you left
   it rather than on the defaults -- on the phone especially, where the tab gets
   evicted the moment you switch apps and re-dialling five sliders is the whole
   difference between using the thing and not.

   Two knobs are deliberately not saved. The first gameweek belongs to the
   snapshot, not to you: restoring GW10 over a snapshot frozen at GW5 would put a
   number on screen that nothing behind it agrees with. Bench weights come from
   the snapshot's own rules block until you move a slider -- so what is saved is
   `benchTouched`, and only then the four values.

   Squads, drafts and edits are stored separately above and are never touched
   from here: "Restore defaults" resets knobs, not work. */
export const SETTING_IDS = ["horizon", "gwdecay", "budget", "ownw", "minstart",
                     "maxclub", "formation", "recency", "freetransfers"];
export const DEFAULT_SETTINGS = { horizon: "8", gwdecay: "0.79", budget: "100", ownw: "0",
                           minstart: "0.3", maxclub: "3", formation: "", freetransfers: "1" };
export const DEFAULT_BENCH = { GKP: 0.03, "1": 0.24, "2": 0.10, "3": 0.04 };
export const DEFAULT_CHIP_HOLD = { bboost: 14, "3xc": 10, freehit: 12 };
export const DEFAULT_FT_VALUE = 1.5;
export const VIEW_PANES = { gw: ["#gwChart", "#gwTable"], exp: ["#expChart", "#expTable"],
                     tl: ["#tlChart", "#tlTable"],
                     // The Chips tab's own line chart. Its toggle lives inside
                     // #chipsBody, which is rebuilt on every render, so it is
                     // wired in renderChips rather than once at load.
                     plan: ["#planChart", "#planChartTable"] };

/* ------------------------------------------------------------ fixture decay

   The slider is the per-gameweek multiplier, not the half-life. Half-life is
   the honest parameter and a bad scale to drag: it runs to infinity, so the
   interesting end -- "barely discounted at all" -- is squeezed into the last
   inch of travel, and no finite position on it means "count every fixture the
   same". The multiplier is linear, reads straight off the Weight column in the
   editor, and its top stop, 1.00x, *is* no decay.

   Half-life is derived back out for everything downstream, and is Infinity at
   the top of the slider -- which the decay maths already handles, since
   0.5 ** (i / Infinity) === 1 for every gameweek. Only two places cannot carry
   an Infinity: JSON (saved drafts, the /api/ask body), where null means no
   decay, and prose, where it is spelled out.

   The "No decay" checkbox overrides the slider rather than moving it, so
   flipping it off returns you to the weighting you had chosen instead of to
   1.00x. It is read here, at the one place every consumer of the setting
   already funnels through, so nothing downstream has to know it exists -- and
   it reaches the transfer-and-chip planner too, whose own discount
   (TRANSFER_HALF_LIFE, 6.5) is otherwise a hardcoded constant with no control
   at all. That discount is worth being able to switch off: over a six-gameweek
   window it weights GW1 at 1.00 and GW6 at 0.59, so a later gameweek has to pay
   1.7x more to win, and on a calendar with no doubles or blanks it is the only
   thing choosing a chip's week.

   It switches off the injury/rotation hazard as well -- see `hazardOf` in
   board.mjs for why the two are one setting. The slider on its own does not:
   at 1.00x it says "these fixtures are all equally far away", which is a claim
   about the calendar, while the checkbox says "stop discounting the far end of
   the horizon at all", which is a claim about what you want ranked. */
export const noDecay = () => !!$("#nodecay")?.checked;
export const gwDecay = () => (noDecay() ? 1 : +$("#gwdecay").value);
export const halfLifeOf = (perGw) => (perGw >= 1 ? Infinity : Math.log(0.5) / Math.log(perGw));
export const halfLife = () => halfLifeOf(gwDecay());
export const hlJSON = (hl) => (Number.isFinite(hl) ? hl : null);
/** The chip planner's own half-life, or null for none. Separate from the
 *  board's because it is a different discount over a different window; shared
 *  only in being switched off by the same checkbox. */
export const transferHalfLife = () =>
  (noDecay() ? null : (S.snapshot?.rules?.TRANSFER_HALF_LIFE ?? null));
/** Everything board.mjs needs to weight a projection, from the controls on
 *  screen. One helper rather than four copies of the same object literal, so a
 *  new weighting knob cannot reach three of the four call sites and be missing
 *  from the fourth -- which is how the pool, the editor and the club panel come
 *  to disagree about what a player is worth. */
export const calibrateOdds = () => $("#oddscalib")?.checked !== false;
export const planOpts = () => ({ horizon: +$("#horizon").value, halfLife: halfLife(),
                          dropout: !noDecay(), calibrateOdds: calibrateOdds() });

/** The decay setting as a sentence fragment. */
export function decayText(perGw) {
  return perGw >= 1 ? "no decay — every fixture counts 1.00×"
    : `${perGw.toFixed(2)}× per gameweek, half-life ${halfLifeOf(perGw).toFixed(1)}`;
}

/** The same thing at slider width. */
export function decayLabel(perGw) {
  return perGw >= 1 ? "1.00× · no decay"
    : `${perGw.toFixed(2)}× · half-life ${halfLifeOf(perGw).toFixed(1)}`;
}

/** What a saved draft was scored under, as a multiplier. Drafts saved before
    the slider changed carry a half-life instead; null when unknown. */
export function ctxDecay(ctx) {
  if (ctx.gw_decay != null) return ctx.gw_decay;
  if (ctx.half_life != null) return Math.pow(0.5, 1 / ctx.half_life);
  return null;
}

export const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
export const fmt = (v, d = 1) => (v === null || v === undefined || isNaN(v)) ? "—" : v.toFixed(d);
export const el = (tag, attrs = {}, parent = null) => {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
};

/* ------------------------------------------------------------------ tooltip */
export const tip = $("#tip");
export function showTip(evt, html) {
  tip.innerHTML = html;
  tip.style.opacity = "1";
  const box = tip.getBoundingClientRect();
  let x = evt.clientX + 14, y = evt.clientY - box.height - 10;
  if (x + box.width > innerWidth - 8) x = evt.clientX - box.width - 14;
  if (y < 8) y = evt.clientY + 16;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
export const hideTip = () => { tip.style.opacity = "0"; };

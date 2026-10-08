"use strict";
import { pushOverrides, markSynced } from "/assets/sync.mjs";

export const $ = (s) => document.querySelector(s);

export const POS_ORDER = ["GKP", "DEF", "MID", "FWD"];
export const SVGNS = "http://www.w3.org/2000/svg";

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
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { }
}

export const EDITS_HISTORY_MAX = 20, EDITS_HISTORY_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
export function snapshotEditsHistory() {
  const cutoff = Date.now() - EDITS_HISTORY_MAX_AGE_MS;
  const history = loadLocal(STORE.editsHistory, []).filter((s) => s.at > cutoff);
  const last = history[history.length - 1];
  if (!Object.keys(S.edits).length && !last) return;
  const snapshot = JSON.stringify(S.edits);
  if (last && last.snapshot === snapshot) return;
  history.push({ at: Date.now(), snapshot });
  saveLocal(STORE.editsHistory, history.slice(-EDITS_HISTORY_MAX));
}

export const saveEdits = () => {
  saveLocal(STORE.edits, S.edits); saveLocal(STORE.editsAt, S.editsAt);
  snapshotEditsHistory(); pushOverrides(); markSynced();
};
export const squadCodes = () => S.squad.map((id) => S.byId.get(id)?.code).filter((c) => c != null);
export const purchaseCodes = () => Object.fromEntries(S.squad
  .filter((id) => S.purchase[id] != null && S.byId.get(id)?.code != null)
  .map((id) => [S.byId.get(id).code, S.purchase[id]]));
export const saveSquad = () => {
  for (const id of Object.keys(S.purchase)) if (!S.squad.includes(+id)) delete S.purchase[id];
  saveLocal(STORE.squad, squadCodes()); saveLocal(STORE.purchase, purchaseCodes()); markSynced();
};
export const resolveSquadCodes = (codes) =>
  (codes || []).map((code) => S.byCode.get(code)).filter((id) => id != null);
export const resolvePurchaseCodes = (byCode) => Object.fromEntries(
  Object.entries(byCode || {}).map(([code, price]) => [S.byCode.get(+code), +price])
    .filter(([id, price]) => id != null && Number.isFinite(price)));

export function newChipPlanState() {
  return { forceChips: [], chipWeek: {}, chipSkip: [], transferMode: "plan", wildcardNow: false };
}

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
const TRANSFER_MODES_VALUES = ["plan", "free", "none"];

export const S = {
  snapshot: null,
  players: [], byId: new Map(), byCode: new Map(), gameweeks: [], meta: null,
  squad: [], optimal: [], optimalPts: null, optimalCost: null, optimalBench: {},
  purchase: {},
  optimalState: "idle", optimalError: "",
  pos: "ALL", search: "", sort: "xpts_plan", dir: -1,
  edits: loadLocal(STORE.edits, {}), editsAt: loadLocal(STORE.editsAt, {}),
  drafts: loadLocal(STORE.drafts, []),
  fixtureEdits: loadLocal(STORE.fixtureEdits, {}),
  posTags: loadLocal(STORE.posTags, {}),
  lineupOrder: loadLocal(STORE.lineupOrder, {}),
  compare: [], draftsPath: "", include: [], exclude: [],
  poolOut: [],
  poolIn: [],
  chipsUsed: [],
  chipPlan: { opt: newChipPlanState(), own: newChipPlanState() },
  views: { gw: "chart", exp: "chart", tl: "chart", plan: "chart" },
  tab: "squad", metrics: ["xpts", "xppg", "price"],
  compareWith: "optimal", versus: [],
  transferPlan: { state: "idle", result: null, error: "", key: null },
  planWeek: 0,
  nearMiss: { state: "idle", rows: [], error: "", key: null, tested: 0,
              progress: { done: 0, total: 0 }, perClub: false },
  weekNearMiss: { state: "idle", rows: [], error: "", idx: null, gw: null,
                  key: null, progress: { done: 0, total: 0 } },
  weekIdeal: { key: null, byWeek: {} },
  ownedPlan: { state: "idle", result: null, error: "", key: null, progress: null },
  ownedPlanWeek: 0,
  chipFolds: { ideal: false, near: false },
};
S.squadCodesPending = loadLocal(STORE.squad, []);
S.purchaseCodesPending = loadLocal(STORE.purchase, {});

(function backfillEditsAt() {
  const now = Date.now();
  let changed = false;
  for (const id of Object.keys(S.edits)) {
    if (!S.editsAt[id]) { S.editsAt[id] = now; changed = true; }
  }
  if (changed) saveLocal(STORE.editsAt, S.editsAt);
})();

globalThis.board = S;

export const SETTING_IDS = ["horizon", "gwdecay", "budget", "ownw", "minstart",
                     "maxclub", "formation", "recency", "lastseason", "freetransfers"];
export const DEFAULT_SETTINGS = { horizon: "8", gwdecay: "0.79", budget: "100", ownw: "0",
                           minstart: "0.3", maxclub: "3", formation: "", freetransfers: "1",
                           lastseason: "0.25" };
export const DEFAULT_BENCH = { GKP: 0.03, "1": 0.24, "2": 0.10, "3": 0.04 };
export const DEFAULT_CHIP_HOLD = { bboost: 14, "3xc": 10, freehit: 12 };
export const DEFAULT_FT_VALUE = 1.5;
export const VIEW_PANES = { gw: ["#gwChart", "#gwTable"], exp: ["#expChart", "#expTable"],
                     tl: ["#tlChart", "#tlTable"],
                     plan: ["#planChart", "#planChartTable"] };

export const noDecay = () => !!$("#nodecay")?.checked;
export const gwDecay = () => (noDecay() ? 1 : +$("#gwdecay").value);
export const halfLifeOf = (perGw) => (perGw >= 1 ? Infinity : Math.log(0.5) / Math.log(perGw));
export const halfLife = () => halfLifeOf(gwDecay());
export const hlJSON = (hl) => (Number.isFinite(hl) ? hl : null);
export const transferHalfLife = () =>
  (noDecay() ? null : (S.snapshot?.rules?.TRANSFER_HALF_LIFE ?? null));
export const calibrateOdds = () => $("#oddscalib")?.checked !== false;
export const planOpts = () => ({ horizon: +$("#horizon").value, halfLife: halfLife(),
                          dropout: !noDecay(), calibrateOdds: calibrateOdds() });

export function decayText(perGw) {
  return perGw >= 1 ? "no decay — every fixture counts 1.00×"
    : `${perGw.toFixed(2)}× per gameweek, half-life ${halfLifeOf(perGw).toFixed(1)}`;
}

export function decayLabel(perGw) {
  return perGw >= 1 ? "1.00× · no decay"
    : `${perGw.toFixed(2)}× · half-life ${halfLifeOf(perGw).toFixed(1)}`;
}

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

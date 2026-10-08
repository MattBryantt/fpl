"use strict";
import { $, S, STORE, loadLocal, saveLocal, SETTING_IDS, applyChipPlanSettings,
         persistableChipPlan, squadCodes, resolveSquadCodes, saveSquad,
         purchaseCodes, resolvePurchaseCodes } from "/assets/state.mjs";
import { fetchLiveTeam } from "/assets/live.mjs";
import {
  BENCH_KEYS, chipHoldValues, ftValueSetting,
  getBenchTouched, setBenchTouched, getChipEconTouched, setChipEconTouched,
  setChipEconDefaults, renderChipsUsed, renderControlValues, renderConstraints,
  readSettings, renderAll,
} from "/assets/squad-view.mjs";
import { recomputeEdited, renderEditBanner } from "/assets/explain-view.mjs";

export const TOKEN = (() => {
  const url = new URL(location.href);
  const fromUrl = url.searchParams.get("t");
  if (fromUrl) {
    localStorage.setItem("fplToken", fromUrl);
    url.searchParams.delete("t");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
    return fromUrl;
  }
  return localStorage.getItem("fplToken") || "";
})();

export const api = (path, init = {}) => fetch(path, {
  ...init,
  headers: { ...(init.headers || {}), ...(TOKEN ? { "X-FPL-Token": TOKEN } : {}) },
});

export let serverPresent = null;

export async function detectServer() {
  try {
    const res = await api("/api/overrides");
    serverPresent = res.ok;
  } catch (_) {
    serverPresent = false;
  }
  document.body.classList.toggle("noserver", !serverPresent);
  $("#lastseason").step = serverPresent ? 0.05 : 0.25;
  return serverPresent;
}

let csvTimer = null, csvPending = false;
export let csvState = "";

export function pushOverrides({ immediate = false } = {}) {
  if (serverPresent === false) return;
  csvPending = true;
  clearTimeout(csvTimer);
  csvTimer = setTimeout(flushOverrides, immediate ? 0 : 1200);
}

async function flushOverrides() {
  clearTimeout(csvTimer);
  if (!csvPending) return;
  const sending = JSON.stringify({ edits: S.edits });
  csvPending = false;
  try {
    const res = await api("/api/overrides", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: sending,
    });
    if (!res.ok) throw new Error(String(res.status));
    const r = await res.json();
    csvState = r.rows ? `saved to ${r.path}` : "";
  } catch (_) {
    csvPending = true;
    csvState = "not yet written to disk — laptop unreachable";
  }
  renderEditBanner();
}

addEventListener("online", () => { if (csvPending) flushOverrides(); });

addEventListener("pagehide", () => {
  if (!csvPending) return;
  clearTimeout(csvTimer);
  try {
    api("/api/overrides", {
      method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edits: S.edits }),
    });
  } catch (_) { }
});

export function syncableSettings() {
  return {
    controls: Object.fromEntries(SETTING_IDS.map((id) => [id, $("#" + id).value])),
    noDecay: !!$("#nodecay")?.checked, calibrateOdds: $("#oddscalib")?.checked !== false,
    chipPlan: persistableChipPlan(),
    bench: Object.fromEntries(BENCH_KEYS.map((k) => [k, $(`#bw_${k}`).value])),
    benchTouched: getBenchTouched(), include: [...S.include], exclude: [...S.exclude], poolOut: [...S.poolOut], poolIn: [...S.poolIn],
    chipsUsed: [...S.chipsUsed],
    chipHold: chipHoldValues(), ftValue: ftValueSetting(), chipEconTouched: getChipEconTouched(),
  };
}

export function applySyncedSettings(saved) {
  if (!saved || typeof saved !== "object") return;
  for (const id of SETTING_IDS) {
    const value = saved.controls?.[id];
    if (value !== undefined && value !== null) $("#" + id).value = value;
  }
  if (saved.noDecay !== undefined) $("#nodecay").checked = !!saved.noDecay;
  if (saved.calibrateOdds !== undefined) $("#oddscalib").checked = !!saved.calibrateOdds;
  if (saved.chipPlan) applyChipPlanSettings(saved.chipPlan);
  else applyChipPlanSettings({ opt: { transferMode: saved.transferMode, chipSkip: saved.chipSkip } });
  if (saved.bench) {
    setBenchTouched(!!saved.benchTouched);
    for (const k of BENCH_KEYS) if (saved.bench[k] != null) $(`#bw_${k}`).value = saved.bench[k];
  }
  if (saved.chipHold || saved.ftValue != null) {
    setChipEconTouched(!!saved.chipEconTouched);
    setChipEconDefaults(saved.chipHold, saved.ftValue);
  }
  if (Array.isArray(saved.include)) S.include = saved.include.map(Number);
  if (Array.isArray(saved.exclude)) S.exclude = saved.exclude.map(Number);
  if (Array.isArray(saved.poolOut)) S.poolOut = saved.poolOut.map(Number);
  if (Array.isArray(saved.poolIn)) S.poolIn = saved.poolIn.map(Number);
  if (Array.isArray(saved.chipsUsed)) S.chipsUsed = saved.chipsUsed.slice();
  renderChipsUsed();
  saveLocal(STORE.settings, readSettings());
  setLastSyncableSettings(JSON.stringify(syncableSettings()));
  renderControlValues();
  renderConstraints();
}

export let lastSyncableSettings = null;
export function setLastSyncableSettings(v) { lastSyncableSettings = v; }

const SYNC_META_KEY = "fpl.sync.meta";
let syncPending = false, syncTimer = null;

const syncMeta = () => loadLocal(SYNC_META_KEY, { updated_at: 0 });
const setSyncMeta = (m) => saveLocal(SYNC_META_KEY, m);

const BOOT_SYNC_STAMP = loadLocal(SYNC_META_KEY, { updated_at: 0 }).updated_at || 0;

let pullSettled = false;

export function markSynced() {
  if (!TOKEN) return;
  setSyncMeta({ updated_at: Date.now() });
  syncPending = true;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(pushSyncState, 1500);
}

export async function pushSyncState() {
  clearTimeout(syncTimer);
  if (!TOKEN || !syncPending) return;
  if (!pullSettled) return;
  const sending = JSON.stringify({
    updated_at: syncMeta().updated_at, drafts: S.drafts, edits: S.edits, editsAt: S.editsAt,
    squad: squadCodes(), purchase: purchaseCodes(), settings: syncableSettings(),
  });
  syncPending = false;
  try {
    const res = await api("/api/sync", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: sending,
    });
    if (!res.ok) throw new Error(String(res.status));
    const body = await res.json().catch(() => ({}));
    if (body && body.stale && typeof body.updated_at === "number") {
      setSyncMeta({ updated_at: body.updated_at });
    }
  } catch (_) {
    syncPending = true;
  }
}

function mergeEdits(localEdits, localAt, remoteEdits, remoteAt) {
  const ids = new Set([
    ...Object.keys(localEdits), ...Object.keys(remoteEdits),
    ...Object.keys(localAt), ...Object.keys(remoteAt),
  ].map(Number));
  const edits = {}, at = {};
  for (const id of ids) {
    const lt = localAt[id] || 0, rt = remoteAt[id] || 0;
    if (lt >= rt) {
      if (localEdits[id]) edits[id] = localEdits[id];
      at[id] = lt;
    } else {
      if (remoteEdits[id]) edits[id] = remoteEdits[id];
      at[id] = rt;
    }
  }
  return { edits, at };
}

export const SKIP_PULL = new URL(location.href).searchParams.get("nopull") === "1";

export const ADOPT_REMOTE = new URL(location.href).searchParams.get("adoptremote") === "1";

export async function pullSyncState(ready) {
  try {
    await attemptPull(ready);
  } finally {
    pullSettled = true;
    if (syncPending) pushSyncState();
  }
}

async function attemptPull(ready) {
  if (!TOKEN) return;
  if (SKIP_PULL) {
    console.warn("nopull=1: keeping this device's own state, not syncing down");
    return;
  }
  try { await ready; } catch (_) { }
  if (!S.snapshot) return;
  try {
    const res = await api("/api/sync");
    if (!res.ok) { console.warn("[sync] pull: GET failed", res.status); return; }
    const remote = await res.json();
    if (!remote.exists) { console.log("[sync] pull: nothing in KV yet"); return; }
    if (!ADOPT_REMOTE && remote.updated_at <= BOOT_SYNC_STAMP) {
      console.log(`[sync] pull: nothing newer (remote ${remote.updated_at}, boot baseline ${BOOT_SYNC_STAMP})`);
      return;
    }
    console.log(`[sync] pull: remote has ${Object.keys(remote.edits || {}).length} edit(s), `
      + `local has ${Object.keys(S.edits).length} before merge`);
    S.drafts = remote.drafts || [];
    const merged = ADOPT_REMOTE
      ? { edits: { ...(remote.edits || {}) }, at: { ...(remote.editsAt || {}) } }
      : mergeEdits(S.edits, S.editsAt, remote.edits || {}, remote.editsAt || {});
    console.log(`[sync] ${ADOPT_REMOTE ? "adopt" : "merge"}: -> ${Object.keys(merged.edits).length} edit(s)`);
    S.edits = merged.edits;
    S.editsAt = merged.at;
    S.squad = resolveSquadCodes(remote.squad);
    S.purchase = resolvePurchaseCodes(remote.purchase);
    saveLocal(STORE.drafts, S.drafts);
    saveLocal(STORE.edits, S.edits);
    saveLocal(STORE.editsAt, S.editsAt);
    saveLocal(STORE.squad, squadCodes());
    saveLocal(STORE.purchase, purchaseCodes());
    if (remote.settings) applySyncedSettings(remote.settings);
    setSyncMeta({ updated_at: remote.updated_at });
    await recomputeEdited(Object.keys(S.edits).map(Number), { sync: false });
    console.log(`[sync] after recompute: ${Object.keys(S.edits).length} edit(s) remain`);
    renderAll();
  } catch (error) {
    console.error("[sync] pull failed:", error);
  }
}

addEventListener("online", () => { if (syncPending) pushSyncState(); });
addEventListener("pagehide", () => {
  if (!syncPending || !TOKEN) return;
  if (!pullSettled) return;
  clearTimeout(syncTimer);
  try {
    api("/api/sync", {
      method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        updated_at: syncMeta().updated_at, drafts: S.drafts, edits: S.edits, editsAt: S.editsAt,
        squad: squadCodes(), purchase: purchaseCodes(), settings: syncableSettings(),
      }),
    });
  } catch (_) { }
});

export async function loadOverrides() {
  const res = await api("/api/overrides");
  const r = await res.json();
  if (!r.exists) return alert(`No saved overrides at ${r.path}`);
  S.edits = {};
  for (const [id, fields] of Object.entries(r.edits)) S.edits[+id] = fields;
  await recomputeEdited(Object.keys(S.edits).map(Number));
  renderAll();
}

export async function loadMyTeam() {
  const input = $("#teamId");
  const teamId = input.value.trim();
  if (!/^\d+$/.test(teamId)) { alert("Enter your FPL team id — the number in the URL of your own “Points” page."); return; }
  localStorage.setItem("fpl.teamId", teamId);

  const btn = $("#loadMyTeam");
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = "Loading…";
  try {
    const gw = Math.max(1, (S.meta?.start_gw || 2) - 1);
    const team = await fetchLiveTeam(api, teamId, gw, (id) => S.byId.get(id)?.price || 0);

    const resolved = team.squadIds.filter((id) => S.byId.has(id));
    if (resolved.length !== team.squadIds.length) {
      console.warn(`[live] ${team.squadIds.length - resolved.length} pick(s) not in the current pool`);
    }
    if (resolved.length !== 15) {
      alert(`FPL returned ${resolved.length} of 15 picks for gameweek ${gw} — `
        + `try again once picks for that gameweek are set.`);
      return;
    }
    S.squad = resolved;
    S.purchase = Object.fromEntries(Object.entries(team.purchasePrices)
      .map(([id, price]) => [+id, price]).filter(([id]) => S.byId.has(id)));
    saveSquad();

    $("#freetransfers").value = String(Math.min(5, Math.max(0, team.freeTransfers)));
    S.chipsUsed = [...team.chipsUsed];
    renderChipsUsed();

    $("#budget").value = String(team.budgetTotal);
    $("#budval").textContent = team.budgetTotal.toFixed(1);

    renderAll();
  } catch (error) {
    alert(`Could not load your team.\n\n${error.message}`);
  } finally {
    btn.disabled = false; btn.textContent = was;
  }
}

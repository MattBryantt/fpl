/* Auth token, the local server's write endpoints, and cross-device sync via
 * Cloudflare KV. See REFACTOR.md for the split this belongs to.
 *
 * Imports from squad-view.mjs for the settings/chrome helpers that
 * syncableSettings()/applySyncedSettings() read and write, and squad-view.mjs
 * imports back from here (TOKEN, api, markSynced, pushOverrides) -- a genuine
 * cycle, safe because every cross-reference is used inside a function body,
 * never at module top-level, so both sides are fully evaluated before either
 * is called. */
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

/* ---------------------------------------------------------------- auth token
   Two write-capable things need this, not one: the old `--lan` server, and now
   /api/sync on the Workers deployment. Both check the same X-FPL-Token header,
   so one token and one storage slot cover both without the client having to
   know which kind of backend it is talking to.

   It arrives once in the URL (from a printed link or a QR code), then moves out
   of it so it stops appearing in the address bar, in history, and in any
   screenshot of the page. localStorage rather than sessionStorage -- sync is
   only worth having if reopening the app tomorrow does not mean typing the
   token in again, and a token that never leaves this device in the first place
   is not meaningfully less safe sitting in localStorage than in sessionStorage. */
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

/* ------------------------------------------------------------ is anything
   behind this page?

   The board has two homes now. One is `fpl.py serve` on the laptop, which has
   write endpoints: it mirrors drafts and overrides to disk, and it can re-run
   the projection. The other is a static host, where the site is a directory of
   files and there is no Python within a thousand miles.

   Everything the board is *for* works in both, because the projection maths and
   the optimiser run here. What differs is a handful of controls that ask the
   laptop to do something, and a control that cannot work is worse than one that
   is not there: it teaches you that the button is broken rather than that the
   feature is elsewhere. So they are hidden rather than left to fail.

   Probed rather than configured, because the same page is served from both and
   a build-time flag would be a third thing to keep in sync. `/api/overrides` is
   the probe because it is a plain GET that reads nothing expensive. */
export let serverPresent = null;

export async function detectServer() {
  try {
    const res = await api("/api/overrides");
    serverPresent = res.ok;
  } catch (_) {
    serverPresent = false;
  }
  document.body.classList.toggle("noserver", !serverPresent);
  return serverPresent;
}

/* --------------------------------------------------------- overrides.csv ---
   There used to be a Save button here, and it was the wrong shape for what it
   did: your edits were already saved -- to localStorage, the moment you made
   them -- and the button only mirrored them to the file the CLI reads. So it
   was a save that felt load-bearing and wasn't, and forgetting it meant the CLI
   quietly planned off stale numbers.

   Now it mirrors itself. Debounced, because a dragged slider commits several
   times; silent, because the laptop being unreachable is the normal state on a
   phone and is not a thing to interrupt anyone about; and retried when the
   connection comes back, so the file catches up rather than staying behind. */
let csvTimer = null, csvPending = false;
export let csvState = "";

export function pushOverrides({ immediate = false } = {}) {
  // A static host has no endpoint to write to, ever. That is different from the
  // laptop being asleep, which is temporary and worth retrying -- so this drops
  // the mirror entirely rather than queueing writes that can never land.
  if (serverPresent === false) return;
  csvPending = true;
  clearTimeout(csvTimer);
  csvTimer = setTimeout(flushOverrides, immediate ? 0 : 1200);
}

async function flushOverrides() {
  clearTimeout(csvTimer);
  if (!csvPending) return;
  // Snapshot what is being sent. An edit made while the request is in flight has
  // to leave the dirty flag set, or it is the one that never reaches disk.
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
    // Offline, or no laptop behind this page at all. Neither is a failure worth
    // a dialog: localStorage already has the edits, and this retries.
    csvPending = true;
    csvState = "not yet written to disk — laptop unreachable";
  }
  renderEditBanner();
}

// The phone comes back to a network far more often than it loses one, and this
// is the moment the file can catch up without anyone asking it to.
addEventListener("online", () => { if (csvPending) flushOverrides(); });

/* pagehide, not beforeunload: on iOS the tab is not closed, it is evicted, and
   beforeunload does not fire for that. This is the last chance a debounce still
   sitting in a timer has to become a file. */
addEventListener("pagehide", () => {
  if (!csvPending) return;
  clearTimeout(csvTimer);
  try {
    api("/api/overrides", {
      method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edits: S.edits }),
    });
  } catch (_) { /* leaving anyway; localStorage already has it */ }
});

/* ------------------------------------------------------- cross-device sync
   overrides.csv answers "does the CLI see what I typed"; this answers "does my
   phone see the squad and settings I changed on my laptop". Different problem,
   different backend (Cloudflare KV via /api/sync rather than a file), same
   shape of solution: local state is authoritative, a push follows every
   change, and a failed push is not an error, just something to retry.

   Drafts, edits and the squad travel as-is; the Settings panel travels through
   syncableSettings()/applySyncedSettings() below, deliberately narrower than
   the full local settings blob -- which tab is open, what the pool is sorted
   by, which chart is showing is how you are currently looking at the board,
   not a fact about the plan, and pulling that from a device that happened to
   sync last would yank the screen around mid-session the same way an
   unwanted theme change would.

   Active only when TOKEN is set -- opened once from a link with ?t=, same as
   the old --lan token. No prompt, no nag if it isn't: a board that has never
   been given a token behaves exactly as it did before this existed.

   Deliberately push-only after boot, not polling. A background pull that
   silently replaced a squad you were mid-edit on would be worse than no sync
   at all -- so the only time this device adopts someone else's state is the
   one moment it has nothing of its own in flight: page load. */

/** The Settings panel's model inputs (horizon, decay, budget, bench weights,
    required/banned players) as one blob. */
export function syncableSettings() {
  return {
    controls: Object.fromEntries(SETTING_IDS.map((id) => [id, $("#" + id).value])),
    noDecay: !!$("#nodecay")?.checked, calibrateOdds: $("#oddscalib")?.checked !== false,
    chipPlan: persistableChipPlan(),
    bench: Object.fromEntries(BENCH_KEYS.map((k) => [k, $(`#bw_${k}`).value])),
    benchTouched: getBenchTouched(), include: [...S.include], exclude: [...S.exclude],
    chipsUsed: [...S.chipsUsed],
    chipHold: chipHoldValues(), ftValue: ftValueSetting(), chipEconTouched: getChipEconTouched(),
  };
}

/** The counterpart to syncableSettings() -- same shape, applied back onto the
    controls, S.include/S.exclude and localStorage, without touching which
    tab, sort or chart mode is on screen. */
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
  if (Array.isArray(saved.chipsUsed)) S.chipsUsed = saved.chipsUsed.slice();
  renderChipsUsed();
  // Folds the pulled controls/bench/include/exclude into this device's own
  // settings blob (tab, sort, views stay whatever they already were) --
  // written straight to storage, not through saveSettings(), so adopting
  // someone else's change does not turn around and re-push it as new.
  saveLocal(STORE.settings, readSettings());
  // Re-baseline so the next unrelated UI tweak (switching tabs, sorting the
  // pool) diffs against what was just adopted, not against whatever this
  // device had before the pull -- otherwise that tweak would see the pulled
  // values as "changed" and re-push them right back out.
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

/* What this device knew before the session started, captured now, at module
   scope, because the boot pull cannot ask the question any other way.

   "Is the remote newer than mine?" has to mean newer than what I last *knew*,
   and `syncMeta()` stops answering that the moment anything calls markSynced()
   -- which stamps the clock for a local change. Anything that saves during
   startup therefore pushes the stamp past the remote's, and the pull then
   decides it has nothing to learn and returns, on a device that in fact has
   nothing. Reading it once, up here, is what keeps the comparison about
   versions instead of about who wrote most recently. */
const BOOT_SYNC_STAMP = loadLocal(SYNC_META_KEY, { updated_at: 0 }).updated_at || 0;

/* And nothing may be pushed until that pull has settled. Without this the same
   startup save races the other way: it uploads this device's state over a
   remote it has not merged yet, which is how an empty board came to overwrite
   a full one seconds after being opened. */
let pullSettled = false;

/** Call after any change to drafts, edits, the squad, or the Settings panel. */
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
  // Held, not dropped: syncPending stays true, and the boot pull flushes it
  // once it has settled. Uploading before merging is how a device that has not
  // yet heard about the other one gets to speak for both.
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
    // The Worker refuses a push whose clock is behind what it already holds,
    // and says so. Taking its number back is what stops this device believing
    // it is ahead for ever: syncMeta would otherwise keep a stamp that was
    // never accepted, sit permanently above the remote's, and every future
    // boot pull would decide there was nothing newer to fetch.
    const body = await res.json().catch(() => ({}));
    if (body && body.stale && typeof body.updated_at === "number") {
      setSyncMeta({ updated_at: body.updated_at });
    }
  } catch (_) {
    syncPending = true; // offline, or no token registered on the server side yet
  }
}

/* Edits merge per player rather than replace wholesale. The rest of the synced
   state (drafts, squad, settings) still replaces outright on whichever side
   pushed last -- those are edited as a whole by one device at a time, so
   last-write-wins on the whole blob is fine for them. Edits are different:
   they accumulate from both a laptop doing team-news sweeps and a phone doing
   one-off tweaks between the same two syncs, and a wholesale replace treats
   "the other device hasn't heard about my edit yet" the same as "the other
   device deliberately has fewer edits than me" -- which is exactly how a
   laptop opened with a stale/empty local copy, touching just one player,
   ended up overwriting every override on every device including the phone
   that actually had them all. Per-id timestamps (S.editsAt) let each id keep
   whichever side touched it more recently, instead of the whole set being
   decided by whoever's blob has the newer wall-clock stamp. */
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

/** Once, at boot. Adopts the other device's state only if it is strictly newer
    than what this device already wrote -- so two devices opened at the same
    moment with nothing pending simply agree, rather than one clobbering the
    other over who asked first. */
/* `?nopull=1` opens the board without adopting anything from the other device.
   The escape hatch for the one situation the merge cannot be trusted in: this
   device holds overrides that may not have reached the other one, and the
   shared copy may already have been overwritten by a smaller set. Pulling
   first would settle that by timestamp and there is no undo, so this makes
   "look before you merge" possible without editing code on a phone. Pushing
   still works, which is what makes it a recovery path rather than just a
   read-only mode: open with it, check the overrides are there, then change
   anything and this device's copy becomes the shared one. */
export const SKIP_PULL = new URL(location.href).searchParams.get("nopull") === "1";

/* `?adoptremote=1` is the other escape hatch: skip the per-id merge entirely
   and take the remote's edits and editsAt verbatim, this device's own local
   copy discarded rather than compared.

   The merge exists to let two devices each keep whichever side touched an id
   more recently, which only works if both sides' timestamps are trustworthy.
   They can stop being that: two independent local corruption events (this
   session's null-snapshot race, and an earlier one before per-id timestamps
   even existed) left this device carrying deletion-stamps on some ids and
   genuine-but-stale-relative-to-the-other-device stamps on others, accumulated
   over days across multiple restores. mergeEdits was doing exactly what it is
   supposed to given those inputs -- correctly picking the newer stamp, id by
   id -- and the result was still wrong, because "newer" and "the device the
   override actually still belongs to" had quietly come apart. No amount of
   smarter tie-breaking recovers that; the only way out is to stop trusting
   this device's history and take the other side's whole. */
export const ADOPT_REMOTE = new URL(location.href).searchParams.get("adoptremote") === "1";

export async function pullSyncState(ready) {
  try {
    await attemptPull(ready);
  } finally {
    // Always, on every path -- no token, nopull, offline, a thrown merge. A
    // push held behind this flag and never released is a change that only ever
    // existed on one device, which is a quieter version of the same bug.
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
  // The snapshot has to be in before anything is merged. Both this and
  // loadPool() are fired unawaited at boot, and this one wins nearly every
  // time -- /api/sync is a few hundred bytes against a 700KB projection. That
  // race quietly ate every incoming override: the merge adopted them, then
  // `checkOverridable(fields, S.snapshot)` threw on a null snapshot, the catch
  // below it read that as "rejected edit" and deleted each one, `rebuildPool()`
  // threw on the same null before the alert could report any of it, and
  // `catch (_) {}` here swallowed the lot. Drafts and squad are assigned before
  // that point, which is exactly why they synced and edits never did.
  try { await ready; } catch (_) { /* loadPool reports its own failure */ }
  if (!S.snapshot) return;   // no projection, nothing to validate an edit against
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
    // adoptremote=1 skips mergeEdits entirely: this device's own edits and
    // editsAt are discarded rather than compared, because the comparison
    // itself is what stopped being trustworthy -- see ADOPT_REMOTE's comment.
    const merged = ADOPT_REMOTE
      ? { edits: { ...(remote.edits || {}) }, at: { ...(remote.editsAt || {}) } }
      : mergeEdits(S.edits, S.editsAt, remote.edits || {}, remote.editsAt || {});
    console.log(`[sync] ${ADOPT_REMOTE ? "adopt" : "merge"}: -> ${Object.keys(merged.edits).length} edit(s)`);
    S.edits = merged.edits;
    S.editsAt = merged.at;
    // remote.squad is codes (see squadCodes()); S.squad stays fpl_id-keyed.
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
    // offline, or the Worker's KV is empty on a fresh setup -- or a genuine
    // bug, which this used to hide as indistinguishably as the other two.
    console.error("[sync] pull failed:", error);
  }
}

addEventListener("online", () => { if (syncPending) pushSyncState(); });
addEventListener("pagehide", () => {
  if (!syncPending || !TOKEN) return;
  // Same bar as pushSyncState: a tab closed before the boot pull settled has
  // not merged anything yet, so what it would send is this device's state
  // standing in for both. The change is already in localStorage and goes up
  // next session; a clobbered remote does not come back.
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
  } catch (_) { /* leaving anyway; localStorage already has it */ }
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

/** Pulls a manager's real squad, bank, free transfers and chips used straight
 *  from FPL's own public API (see live.mjs) and drops them onto the board the
 *  same way a manually-built squad would land: through S.squad, saveSquad(),
 *  the Free transfers select and the Chips-used checkboxes. Nothing
 *  downstream (pitch, transfer planner, chips tab) distinguishes a loaded
 *  squad from a hand-built one, so this does not need to either.
 *
 *  gw is S.meta.start_gw, not left to the backend to default: the deployed
 *  Worker cannot resolve a missing one on its own (see live.mjs's comment on
 *  fetchLiveTeam), so a caller always supplying it is what keeps the local
 *  and deployed paths behaving the same way. */
export async function loadMyTeam() {
  const input = $("#teamId");
  const teamId = input.value.trim();
  if (!/^\d+$/.test(teamId)) { alert("Enter your FPL team id — the number in the URL of your own “Points” page."); return; }
  localStorage.setItem("fpl.teamId", teamId);

  const btn = $("#loadMyTeam");
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = "Loading…";
  try {
    // S.meta.start_gw is the projection horizon's first gameweek -- the next
    // one to plan for, not the last one a manager's picks actually exist for.
    // Those are one apart (the same relationship fpl_api.live_squad() uses
    // via next_gameweek() - 1 for its own default), which is what a squad
    // saved for a gameweek that has not opened yet would otherwise 404 on.
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

"use strict";
import {
  $, S, POS_ORDER, STORE, loadLocal, saveLocal, saveSquad, resolveSquadCodes, resolvePurchaseCodes,
  newChipPlanState, persistableChipPlan, applyChipPlanSettings,
  SETTING_IDS, DEFAULT_SETTINGS, DEFAULT_BENCH, DEFAULT_CHIP_HOLD, DEFAULT_FT_VALUE,
  VIEW_PANES, noDecay, gwDecay, halfLife, halfLifeOf, hlJSON, calibrateOdds, planOpts,
  decayText, decayLabel, ctxDecay, css, fmt, el,
} from "/assets/state.mjs";
import { sellPrice as sellPriceOf } from "/assets/live.mjs";
import {
  TOKEN, api, markSynced, pushOverrides, syncableSettings, applySyncedSettings,
  lastSyncableSettings, setLastSyncableSettings, SKIP_PULL, ADOPT_REMOTE, serverPresent,
} from "/assets/sync.mjs";
import { derivePool, staleness } from "/assets/board.mjs";
import { pitchHTML, squadLayout, wireShirts, METRICS, METRIC_KEYS, MAX_METRICS,
         cleanMetrics } from "/assets/pitch.mjs";
import { POSITION_TAG_LABELS, SEED_TAGS } from "/assets/position-tags.mjs";
import { renderGwChart, renderExposure, renderTimeline, renderFixtures,
         renderNearMisses } from "/assets/analysis-view.mjs";
import { scheduleSolve } from "/assets/transfer-view.mjs";
import { renderLineup, renderEditBanner } from "/assets/explain-view.mjs";
import { renderChips, markVersus, versusOpen, renderChipConstraints } from "/assets/compare-view.mjs";

let settingsOpen = false, moreOpen = false;

export function renderChrome() {
  $("#settingsRow").classList.toggle("hidden", !settingsOpen);
  $("#moreRow").classList.toggle("hidden", !(settingsOpen && moreOpen));
  $("#benchRow").classList.toggle("hidden", !(settingsOpen && moreOpen));
  $("#settingsBtn").setAttribute("aria-expanded", String(settingsOpen));
  $("#settingsBtn").textContent = settingsOpen ? "Settings ▴" : "Settings ▾";
  $("#moreBtn").setAttribute("aria-expanded", String(moreOpen));
  $("#moreBtn").textContent = moreOpen ? "Fewer options ▴" : "More options ▾";
}

export function toggleSettingsPanel() {
  settingsOpen = !settingsOpen;
  renderChrome(); saveSettings();
}
export function toggleMoreOptions() {
  moreOpen = !moreOpen;
  renderChrome(); saveSettings();
}

const TABS = ["squad", "analysis", "chips", "drafts"];

export function setTab(name) {
  if (!TABS.includes(name)) name = "squad";
  S.tab = name;
  for (const tab of TABS) {
    $(`#panel-${tab}`).hidden = tab !== name;
  }
  document.querySelectorAll(".tab[data-tab]").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  if (name === "analysis" && S.meta) {
    renderGwChart(); renderExposure(); renderTimeline(); renderFixtures(); renderNearMisses();
  }
  if (name === "chips" && S.meta) { renderChips(); }
}

export function openPool(pos = null) {
  if (pos && pos !== S.pos) { setPosFilter(pos); renderPool(); saveSettings(); }
  $("#poolDrawer").classList.add("open");
  $("#poolDrawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.remove("hidden");
  $("#search").focus({ preventScroll: true });
}

export function closePool() {
  $("#poolDrawer").classList.remove("open");
  $("#poolDrawer").setAttribute("aria-hidden", "true");
  if (!$("#drawer").classList.contains("open") && !versusOpen()) {
    $("#scrim").classList.add("hidden");
  }
}

export const poolOpen = () => $("#poolDrawer").classList.contains("open");

export function renderMetricBar() {
  $("#metricBar").innerHTML = METRIC_KEYS.map((key) => `
    <button data-metric="${key}" aria-pressed="${S.metrics.includes(key)}"
            title="${METRICS[key].head}">${METRICS[key].label}</button>`).join("");
  const [lead, ...rest] = S.metrics;
  $("#metricHint").textContent = rest.length
    ? `${METRICS[lead].label} large, then ${rest.map((k) => METRICS[k].label).join(" and ")}`
    : `${METRICS[lead].label} only — pick up to ${MAX_METRICS}`;
}

export function toggleMetric(key) {
  const has = S.metrics.includes(key);
  let next = has ? S.metrics.filter((k) => k !== key) : [...S.metrics, key];
  if (!next.length) next = [key];
  if (next.length > MAX_METRICS) next = next.slice(next.length - MAX_METRICS);
  S.metrics = cleanMetrics(next);
}

export function renderControlValues() {
  const off = noDecay();
  $("#gwdecay").disabled = off;
  $("#nodecayWrap").classList.toggle("on", off);
  $("#oddscalibWrap").classList.toggle("on", calibrateOdds());
  $("#hlval").textContent = off
    ? `off — 1.00×, no dropout (slider: ${(+$("#gwdecay").value).toFixed(2)}×)`
    : decayLabel(gwDecay());
  $("#budval").textContent = (+$("#budget").value).toFixed(1);
  $("#ownval").textContent = (+$("#ownw").value).toFixed(2);
  $("#msval").textContent = (+$("#minstart").value).toFixed(2);
  $("#mcval").textContent = $("#maxclub").value;
  const recency = +$("#recency").value;
  $("#recval").textContent = recency ? recency + " gw" : "off";
  $("#recency").classList.toggle("needsync", recency !== (S.meta?.recency || 0));
  const lastSeason = +$("#lastseason").value;
  $("#lsval").textContent = lastSeason.toFixed(2);
  $("#lastseason").classList.toggle("needsync", lastSeason !== (S.meta?.previous ?? 1));
  renderBenchWeights();
  renderChipEconValues();
}

export function readSettings() {
  return {
    controls: Object.fromEntries(SETTING_IDS.map((id) => [id, $("#" + id).value])),
    noDecay: noDecay(), calibrateOdds: calibrateOdds(),
    chipPlan: persistableChipPlan(),
    bench: Object.fromEntries(BENCH_KEYS.map((k) => [k, $(`#bw_${k}`).value])),
    benchTouched,
    chipHold: chipHoldValues(), ftValue: ftValueSetting(), chipEconTouched,
    theme: document.documentElement.getAttribute("data-theme") || "",
    settingsOpen, moreOpen,
    views: { ...S.views },
    tab: S.tab, metrics: [...S.metrics], compareWith: S.compareWith,
    pos: S.pos, sort: S.sort, dir: S.dir,
    include: [...S.include], exclude: [...S.exclude], poolOut: [...S.poolOut], poolIn: [...S.poolIn],
    chipsUsed: [...S.chipsUsed],
  };
}

export function renderChipsUsed() {
  document.querySelectorAll("[data-chipused]").forEach((cb) => {
    cb.checked = S.chipsUsed.includes(cb.dataset.chipused);
  });
}

let settingsTimer = null;
export function saveSettings() {
  clearTimeout(settingsTimer);
  settingsTimer = setTimeout(() => saveLocal(STORE.settings, readSettings()), 250);
  const syncable = JSON.stringify(syncableSettings());
  if (syncable !== lastSyncableSettings) {
    setLastSyncableSettings(syncable);
    markSynced();
  }
}

export function applySettings(saved) {
  if (!saved || typeof saved !== "object") return;
  for (const id of SETTING_IDS) {
    const value = saved.controls?.[id];
    if (value !== undefined && value !== null) $("#" + id).value = value;
  }
  const legacyHl = +saved.controls?.halflife;
  if (saved.controls?.gwdecay === undefined && legacyHl > 0) {
    $("#gwdecay").value = Math.pow(0.5, 1 / legacyHl).toFixed(2);
  }
  $("#nodecay").checked = !!saved.noDecay;
  if (saved.calibrateOdds !== undefined) $("#oddscalib").checked = !!saved.calibrateOdds;
  if (saved.chipPlan) applyChipPlanSettings(saved.chipPlan);
  else applyChipPlanSettings({ opt: { transferMode: saved.transferMode, chipSkip: saved.chipSkip } });
  if (saved.benchTouched && saved.bench) {
    benchTouched = true;
    for (const k of BENCH_KEYS) {
      if (saved.bench[k] != null) $(`#bw_${k}`).value = saved.bench[k];
    }
  }
  if (saved.chipEconTouched) {
    chipEconTouched = true;
    setChipEconDefaults(saved.chipHold, saved.ftValue);
  }
  settingsOpen = !!saved.settingsOpen;
  moreOpen = !!saved.moreOpen;
  renderChrome();
  if (saved.metrics || saved.metric) S.metrics = cleanMetrics(saved.metrics || saved.metric);
  if (typeof saved.compareWith === "string") S.compareWith = saved.compareWith;
  renderMetricBar();
  setTab(saved.tab || "squad");
  for (const [which, mode] of Object.entries(saved.views || {})) setView(which, mode);
  if (saved.pos) setPosFilter(saved.pos);
  if (saved.sort) { S.sort = saved.sort; S.dir = saved.dir === 1 ? 1 : -1; }
  markSortedHeader();
  S.include = Array.isArray(saved.include) ? saved.include.map(Number) : [];
  S.exclude = Array.isArray(saved.exclude) ? saved.exclude.map(Number) : [];
  S.poolOut = Array.isArray(saved.poolOut) ? saved.poolOut.map(Number) : [];
  S.poolIn = Array.isArray(saved.poolIn) ? saved.poolIn.map(Number) : [];
  S.chipsUsed = Array.isArray(saved.chipsUsed) ? saved.chipsUsed.slice() : [];
  renderChipsUsed();
  renderControlValues();
}

export function setView(which, mode) {
  const panes = VIEW_PANES[which];
  if (!panes) return;
  const table = mode === "table";
  const chart = $(panes[0]), tbl = $(panes[1]);
  chart?.classList.toggle("hidden", table);
  tbl?.classList.toggle("hidden", !table);
  const btn = document.querySelector(`.toggle[data-view="${which}"]`);
  if (btn) btn.textContent = table ? "Chart" : "Table";
  S.views[which] = table ? "table" : "chart";
}

export function setPosFilter(pos) {
  S.pos = pos;
  document.querySelectorAll(".chip[data-pos]").forEach((c) =>
    c.setAttribute("aria-pressed", String(c.dataset.pos === pos)));
}

export function markSortedHeader() {
  document.querySelectorAll("#pool th").forEach((th) =>
    th.classList.toggle("sorted", th.dataset.sort === S.sort));
}

export function restoreDefaults() {
  for (const [id, value] of Object.entries(DEFAULT_SETTINGS)) $("#" + id).value = value;
  $("#nodecay").checked = false;
  $("#oddscalib").checked = true;
  S.chipPlan = { opt: newChipPlanState(), own: newChipPlanState() };
  $("#recency").value = String(S.meta?.recency || 0);
  $("#lastseason").value = String(S.meta?.previous ?? 1);
  benchTouched = false;
  const bench = S.meta?.bench_slot_weights || DEFAULT_BENCH;
  for (const k of BENCH_KEYS) $(`#bw_${k}`).value = bench[k] ?? DEFAULT_BENCH[k];
  chipEconTouched = false;
  const rules = S.snapshot?.rules;
  setChipEconDefaults(rules?.CHIP_HOLD_VALUE || DEFAULT_CHIP_HOLD, rules?.FT_VALUE ?? DEFAULT_FT_VALUE);
  document.documentElement.removeAttribute("data-theme");
  S.include = []; S.exclude = []; S.poolOut = []; S.poolIn = [];
  S.chipsUsed = []; renderChipsUsed();
  setPosFilter("ALL");
  S.search = ""; $("#search").value = "";
  S.sort = "xpts_plan"; S.dir = -1; markSortedHeader();
  for (const which of Object.keys(VIEW_PANES)) setView(which, "chart");
  settingsOpen = moreOpen = false;
  renderChrome();
  S.metrics = ["xpts", "xppg", "price"]; renderMetricBar();
  S.compareWith = "optimal"; S.versus = [];
  setTab("squad");
  renderControlValues();
  try { localStorage.removeItem(STORE.settings); } catch (_) { }
  clampHorizon();
  rebuildPool(); renderAll(); scheduleSolve(0);
}

export function clampHorizon() {
  const max = S.snapshot?.gameweeks?.length;
  if (!max) return;
  const sel = $("#horizon");
  if (+sel.value <= max) return;
  const allowed = [...sel.options].map((o) => +o.value).filter((v) => v <= max);
  sel.value = String(allowed.length ? Math.max(...allowed) : max);
}

export function playerTags(player) {
  const own = S.posTags[player.id];
  if (own) return own;
  return SEED_TAGS[player.full_name] || [];
}
export function savePosTags() { saveLocal(STORE.posTags, S.posTags); }
export function togglePosTag(id, tag) {
  const current = new Set(S.posTags[id] ?? playerTags(S.byId.get(id)));
  if (current.has(tag)) current.delete(tag); else current.add(tag);
  S.posTags[id] = [...current];
  savePosTags();
}
export const posTagBadges = (tags) => tags.map((t) =>
  `<span class="postagbadge" title="${POSITION_TAG_LABELS[t] || t}">${t}</span>`).join("");

export const FORMATIONS = (() => {
  const out = [];
  for (let d = 3; d <= 5; d++) for (let m = 2; m <= 5; m++) for (let f = 1; f <= 3; f++)
    if (d + m + f === 10) out.push([d, m, f]);
  return out;
})();

export function bestXI(ids, scoreOf) {
  const by = { GKP: [], DEF: [], MID: [], FWD: [] };
  for (const id of ids) {
    const p = S.byId.get(id);
    if (p) by[p.pos].push({ id, s: scoreOf(p) });
  }
  for (const k of POS_ORDER) by[k].sort((a, b) => b.s - a.s);
  if (!by.GKP.length) return { ids: [], total: 0 };

  let best = null;
  for (const [d, m, f] of FORMATIONS) {
    if (by.DEF.length < d || by.MID.length < m || by.FWD.length < f) continue;
    const chosen = [by.GKP[0], ...by.DEF.slice(0, d), ...by.MID.slice(0, m), ...by.FWD.slice(0, f)];
    const total = chosen.reduce((a, c) => a + c.s, 0);
    if (!best || total > best.total) best = { total, ids: chosen.map((c) => c.id), shape: [d, m, f] };
  }
  return best || { ids: [], total: 0 };
}

export const gwPoints = (ids, index) => {
  const xi = bestXI(ids, (p) => p.gw[index] || 0);
  if (!xi.ids.length) return 0;
  const captain = Math.max(...ids.map((id) => (S.byId.get(id)?.gw[index]) || 0));
  return xi.total + captain;
};

export function gwLineup(ids, index) {
  const xi = bestXI(ids, (p) => p.gw[index] || 0);
  if (!xi.ids.length) return null;
  const scoreAt = (id) => S.byId.get(id)?.gw[index] || 0;
  const xiSet = new Set(xi.ids);
  const bench = ids.filter((id) => !xiSet.has(id)).sort((a, b) => scoreAt(b) - scoreAt(a));
  const ranked = xi.ids.slice().sort((a, b) => scoreAt(b) - scoreAt(a));
  const captain = ranked[0], vice = ranked[1] ?? null;
  return { shape: xi.shape, starters: xi.ids, bench, captain, vice,
           total: xi.total + scoreAt(captain) };
}

export const planXI = (ids) => bestXI(ids, (p) => p.xpts_plan || 0);

export function squadTotal(ids) {
  const xi = planXI(ids);
  if (!xi.ids.length) return 0;
  const captain = Math.max(...xi.ids.map((id) => S.byId.get(id).xpts_plan || 0));
  return xi.total + captain;
}

export function validity(ids) {
  const need = S.meta.squad_by_pos, issues = [];
  const counts = { GKP: 0, DEF: 0, MID: 0, FWD: 0 };
  const clubs = {};
  let cost = 0;
  for (const id of ids) {
    const p = S.byId.get(id);
    counts[p.pos]++; clubs[p.team] = (clubs[p.team] || 0) + 1; cost += p.price;
  }
  for (const k of POS_ORDER) {
    if (counts[k] > need[k]) issues.push({ bad: true, text: `${counts[k]} ${k} — max ${need[k]}` });
    else if (counts[k] < need[k]) issues.push({ bad: false, text: `${need[k] - counts[k]} more ${k} needed` });
  }
  const budget = +$("#budget").value;
  if (cost > budget + 1e-9) issues.push({ bad: true, text: `£${cost.toFixed(1)}m spent — £${(cost - budget).toFixed(1)}m over budget` });
  for (const [club, n] of Object.entries(clubs))
    if (n > S.meta.max_per_club) issues.push({ bad: true, text: `${n} from ${club} — max ${S.meta.max_per_club}` });
  return { issues, cost, counts, complete: ids.length === 15 && !issues.some((i) => i.bad) && !issues.length };
}

export function exposure(ids) {
  const owned = new Set(ids);
  const rows = S.players
    .filter((p) => !owned.has(p.id) && p.owned > 0)
    .map((p) => ({ ...p, exposure: (p.owned / 100) * (p.xpts_plan || 0) }))
    .sort((a, b) => b.exposure - a.exposure);
  const total = S.players.reduce((a, p) => a + (p.owned / 100) * (p.xpts_plan || 0), 0);
  const held = S.players.filter((p) => owned.has(p.id))
    .reduce((a, p) => a + (p.owned / 100) * (p.xpts_plan || 0), 0);
  return { top: rows.slice(0, 8), share: total ? held / total : 0 };
}

const thinMinutes = () =>
  (S.meta?.season_minutes ?? 38 * 90) * (S.meta?.established_share ?? 0.26);
const ppgClass = (p) => (!p.ppg ? "muted-cell"
  : (p.minutes_last || 0) < thinMinutes() ? "thin-cell" : "");

const ppgTitle = (p) => !p.ppg ? "no appearances last season"
  : `${fmt(p.ppg, 1)} per appearance over ${p.apps_last} games\n`
    + `${fmt(p.ppg_fixture, 2)} per fixture — same basis as xPPG (${fmt(p.xppg, 2)})\n`
    + `${Math.round(p.minutes_last)} minutes`;

export function renderPool() {
  const term = S.search.toLowerCase();
  let rows = S.players.filter((p) =>
    (S.pos === "ALL" || p.pos === S.pos) &&
    (!term || p.full_name.toLowerCase().includes(term) || p.team.toLowerCase().includes(term)));

  const key = S.sort;
  rows.sort((a, b) => {
    const av = key === "value" ? (a.xpts_plan || 0) / a.price : a[key];
    const bv = key === "value" ? (b.xpts_plan || 0) / b.price : b[key];
    if (typeof av === "string") return S.dir * av.localeCompare(bv);
    return S.dir * ((av || 0) - (bv || 0));
  });
  rows = rows.slice(0, 200);

  const picked = new Set(S.squad);
  const body = $("#pool tbody");
  body.innerHTML = "";
  for (const p of rows) {
    const tr = document.createElement("tr");
    if (picked.has(p.id)) tr.className = "picked";
    const flags = [];
    if (S.edits[p.id]) flags.push(`<span class="flag" title="You have edited this player's inputs"><span class="edited-dot">●</span> edited</span>`);
    if (p.moved) flags.push(`<span class="flag" title="Numbers recorded at ${p.previous_club}">moved</span>`);
    if (p.status !== "a") flags.push(`<span class="flag" title="${p.news || "not fully available"}">${p.status === "d" ? "doubt" : "out"}</span>`);
    tr.innerHTML = `
      <td><div class="namecell">
        <button class="add" data-id="${p.id}" ${picked.has(p.id) ? "disabled" : ""} aria-label="Add ${p.name}">+</button>
        <button class="editbtn" data-edit="${p.id}" title="Edit this player's stats" aria-label="Edit ${p.name}">✎</button>
        <button class="editbtn ${S.include.includes(p.id) ? "on" : ""}" data-req="${p.id}"
                title="Require this player in the optimal squad" aria-label="Require ${p.name}">⊕</button>
        <button class="editbtn ${S.exclude.includes(p.id) ? "off" : ""}" data-ban="${p.id}"
                title="Bar this player from the optimal squad" aria-label="Bar ${p.name}">⊘</button>
        <button class="editbtn ${S.versus.includes(p.id) ? "on" : ""}" data-vs="${p.id}"
                title="Compare this player head to head" aria-label="Compare ${p.name}">⇄</button>
        <span>${p.name}</span>${flags.join("")}
      </div></td>
      <td>${p.pos}${posTagBadges(playerTags(p))}</td><td>${p.team_short}</td><td>${fmt(p.price, 1)}</td>
      <td>${fmt(p.xpts_plan, 1)}</td>
      <td>${fmt(p.xppg, 2)}</td>
      <td class="${ppgClass(p)}" title="${ppgTitle(p)}">${p.ppg ? fmt(p.ppg, 1) : "—"}</td>
      <td>${fmt((p.xpts_plan || 0) / p.price, 2)}</td>
      <td>${fmt(p.p_start, 2)}</td><td>${fmt(p.cs, 2)}</td><td>${fmt(p.owned, 1)}</td>`;
    body.appendChild(tr);
  }
  $("#poolcount").textContent = `${rows.length} shown`;
}

export function captaincy(ids) {
  const ranked = planXI(ids).ids
    .map((id) => S.byId.get(id))
    .filter(Boolean)
    .sort((a, b) => (b.xpts_plan || 0) - (a.xpts_plan || 0));
  return { captain: ranked[0]?.id ?? null, vice: ranked[1]?.id ?? null };
}

export function renderSquad() {
  const layout = squadLayout({
    ids: S.squad, lookup: S.byId, need: S.meta.squad_by_pos,
    xiIds: planXI(S.squad).ids, benchOrder: benchSlots(S.squad),
  });
  const { captain, vice } = captaincy(S.squad);

  const other = comparisonSquad();
  const tags = {};
  if (other && other.ids.length && S.squad.length) {
    const theirs = new Set(other.ids);
    for (const id of S.squad) if (!theirs.has(id)) tags[id] = { kind: "out", text: "out" };
  }

  const box = $("#squadPitch");
  box.innerHTML = pitchHTML({
    layout, teams: S.snapshot?.teams, metrics: S.metrics,
    captain, vice, tags, remove: true, versus: true,
  });
  wireShirts(box);
  markVersus(box);

  $("#shapeTag").textContent = layout.complete
    ? `${layout.shape} · ${S.squad.length}/15`
    : `${S.squad.length}/15 picked`;
  $("#sideMinePts").textContent = S.squad.length ? fmt(squadTotal(S.squad), 1) : "—";
  $("#sideMineCost").textContent = "£" + fmt(squadCost(S.squad), 1);

  const v = validity(S.squad);
  const budget = +$("#budget").value;
  const worth = squadSellValue(S.squad);
  $("#tCost").textContent = "£" + fmt(worth, 1);
  $("#tCost").title = worth < v.cost - 0.05
    ? `lists at £${fmt(v.cost, 1)}m; sells for £${fmt(worth, 1)}m after the sell-on fee` : "";
  const meter = $("#budMeter");
  meter.classList.toggle("over", worth > budget);
  meter.firstElementChild.style.width = Math.min(100, (worth / budget) * 100) + "%";
  const bank = budget - worth;
  $("#tBank").textContent = S.squad.length
    ? (bank < 0 ? `£${fmt(-bank, 1)}m over budget` : `£${fmt(bank, 1)}m in the bank`)
    : `£${fmt(budget, 1)}m to spend`;

  const total = squadTotal(S.squad);
  const hero = $("#heroPts");
  hero.classList.toggle("empty", !S.squad.length);
  hero.textContent = S.squad.length ? fmt(total, 1) : "no players yet";

  const delta = $("#heroDelta");
  if (S.optimalPts && S.squad.length === 15) {
    const d = total - S.optimalPts;
    delta.className = "delta " + (d >= -0.05 ? "up" : "down");
    delta.textContent = `${d >= 0 ? "+" : ""}${d.toFixed(1)} vs the optimal squad`;
  } else if (S.squad.length && S.squad.length < 15) {
    delta.className = "delta"; delta.style.color = "var(--muted)";
    delta.textContent = `${15 - S.squad.length} slots still empty`;
  } else { delta.textContent = ""; }

  const xiIds = planXI(S.squad).ids;
  const xiGames = Math.max(...xiIds.map((id) => S.byId.get(id)?.games || 0), 0);
  const xiRate = xiIds.reduce((a, id) => a + (S.byId.get(id)?.xppg || 0), 0)
    + (captain ? S.byId.get(captain).xppg || 0 : 0);
  $("#tXppg").textContent = xiIds.length && xiGames ? fmt(xiRate, 1) : "—";

  renderSquadMoney(bank);

  const exp = exposure(S.squad);
  $("#tCov").textContent = S.squad.length ? Math.round(exp.share * 100) + "%" : "—";

  const list = $("#issues");
  list.innerHTML = "";
  if (!S.squad.length) {
    list.innerHTML = `<div class="issue" style="color:var(--muted)"><span class="ico">·</span><span>Tap an empty shirt to pick for that position, or copy the optimal squad across and argue with it.</span></div>`;
  } else if (!v.issues.length) {
    list.innerHTML = `<div class="issue ok"><span class="ico">✓</span><span>Legal squad — 15 players, budget and club limits satisfied.</span></div>`;
  } else {
    for (const i of v.issues) {
      const d = document.createElement("div");
      d.className = "issue " + (i.bad ? "bad" : "");
      if (!i.bad) d.style.color = "var(--muted)";
      d.innerHTML = `<span class="ico">${i.bad ? "!" : "·"}</span><span>${i.text}</span>`;
      list.appendChild(d);
    }
  }
}
function renderSquadMoney(bank) {
  const box = $("#squadMoney");
  box.hidden = !S.squad.length;
  if (!S.squad.length) return;
  const rows = S.squad.map((id) => S.byId.get(id)).filter(Boolean).map((p) => `
    <label class="sub">${p.name} <input type="number" step="0.1" min="3.5" max="16" style="width:4.5rem"
      data-buy="${p.id}" value="${fmt(S.purchase[p.id] ?? p.price, 1)}"
      title="Price paid; now £${fmt(p.price, 1)}m, sells for £${fmt(sellPrice(p.id), 1)}m"></label>`).join(" ");
  if (box.contains(document.activeElement)) return;
  box.innerHTML = `<details><summary class="sub">Squad money — buy prices and bank</summary>
    <label class="sub">Bank £m <input type="number" step="0.1" min="0" style="width:4.5rem"
      id="bankInput" value="${fmt(Math.max(0, bank), 1)}"></label>
    <div>${rows}</div></details>`;
}
document.addEventListener("change", (e) => {
  const buy = e.target.closest?.("[data-buy]");
  const isBank = e.target.id === "bankInput";
  if (!buy && !isBank) return;
  const bank = $("#budget").value - squadSellValue(S.squad);
  if (buy) S.purchase[+buy.dataset.buy] = +buy.value;
  const kept = isBank ? +e.target.value : bank;
  $("#budget").value = String(Math.round((squadSellValue(S.squad) + kept) * 10) / 10);
  $("#budval").textContent = (+$("#budget").value).toFixed(1);
  saveSquad(); scheduleSolve(); renderAll();
});

export function benchSlots(ids) {
  const xi = new Set(planXI(ids).ids);
  const rest = ids.filter((id) => !xi.has(id)).map((id) => S.byId.get(id)).filter(Boolean);
  const order = {};
  for (const p of rest.filter((p) => p.pos === "GKP")) order[p.id] = "GKP";
  rest.filter((p) => p.pos !== "GKP")
    .sort((a, b) => (b.xpts_plan || 0) - (a.xpts_plan || 0))
    .forEach((p, i) => { order[p.id] = String(Math.min(3, i + 1)); });
  return order;
}

export const BENCH_PROFILE = { GKP: 0.25, "1": 2.0, "2": 0.85, "3": 0.35 };
export const BENCH_KEYS = Object.keys(BENCH_PROFILE);
const PROFILE_SUM = BENCH_KEYS.reduce((a, k) => a + BENCH_PROFILE[k], 0);

export const benchWeights = () =>
  Object.fromEntries(BENCH_KEYS.map((k) => [k, +$(`#bw_${k}`).value]));

export function renderBenchWeights() {
  const w = benchWeights();
  for (const k of BENCH_KEYS) $(`#bwv_${k}`).textContent = w[k].toFixed(3);
  const scale = BENCH_KEYS.reduce((a, k) => a + w[k], 0) / PROFILE_SUM;
  $("#benchw").value = Math.min(1, scale);
  $("#bwval").textContent = scale.toFixed(2) + (scale > 1 ? "+" : "");
}

export function setBenchScale(scale) {
  for (const k of BENCH_KEYS) {
    const input = $(`#bw_${k}`);
    input.value = Math.min(+input.max, scale * BENCH_PROFILE[k]);
  }
  for (const k of BENCH_KEYS) $(`#bwv_${k}`).textContent = (+$(`#bw_${k}`).value).toFixed(3);
  $("#bwval").textContent = (+scale).toFixed(2);
}

let benchTouched = false;
export function getBenchTouched() { return benchTouched; }
export function setBenchTouched(v) { benchTouched = v; }

export const HOLD_KEYS = ["bboost", "3xc", "freehit"];
export const HOLD_INPUT_ID = { bboost: "holdBboost", "3xc": "holdTc", freehit: "holdFreehit" };

export const chipHoldValues = () =>
  Object.fromEntries(HOLD_KEYS.map((k) => [k, +$(`#${HOLD_INPUT_ID[k]}`).value]));
export const ftValueSetting = () => +$("#ftValue").value;

export function renderChipEconValues() {
  for (const k of HOLD_KEYS) $(`#${HOLD_INPUT_ID[k]}Val`).textContent = (+$(`#${HOLD_INPUT_ID[k]}`).value).toFixed(1);
  $("#ftValueVal").textContent = ftValueSetting().toFixed(1);
}

let chipEconTouched = false;
export function getChipEconTouched() { return chipEconTouched; }
export function setChipEconTouched(v) { chipEconTouched = v; }
export function setChipEconDefaults(hold, ftValue) {
  for (const k of HOLD_KEYS) if (hold?.[k] != null) $(`#${HOLD_INPUT_ID[k]}`).value = hold[k];
  if (ftValue != null) $("#ftValue").value = ftValue;
  renderChipEconValues();
}

export const startGw = () => $("#startgw").value ? +$("#startgw").value : null;

export const parseFormation = (text) => {
  if (!text) return null;
  const [d, m, f] = text.split("-").map(Number);
  return { DEF: d, MID: m, FWD: f };
};

export function toggleConstraint(id, which) {
  const other = which === "include" ? "exclude" : "include";
  S[other] = S[other].filter((x) => x !== id);
  S[which] = S[which].includes(id) ? S[which].filter((x) => x !== id) : [...S[which], id];
  saveSettings();
  renderPool(); renderConstraints(); renderChipConstraints(); scheduleSolve(0);
}

export function dropConstraint(id) {
  S.include = S.include.filter((x) => x !== id);
  S.exclude = S.exclude.filter((x) => x !== id);
  saveSettings();
  renderPool(); renderConstraints(); renderChipConstraints(); scheduleSolve(0);
}

export function renderConstraints() {
  const box = $("#constraintChips");
  const chip = (id, kind) => {
    const p = S.byId.get(id);
    return `<button class="chip" data-drop="${id}" title="Remove this constraint"
      style="border-color:${kind === "include" ? "var(--good)" : "var(--critical)"}">
      ${kind === "include" ? "⊕" : "⊘"} ${p ? p.name : id} ×</button>`;
  };
  const parts = [...S.include.map((i) => chip(i, "include")),
                 ...S.exclude.map((i) => chip(i, "exclude"))];
  box.innerHTML = parts.length
    ? `<div style="display:flex;gap:5px;flex-wrap:wrap">${parts.join("")}</div>`
    : `<span class="sub">none set</span>`;
}

export const SERIES_SLOTS = [
  { light: "#2a78d6", dark: "#3987e5" },
  { light: "#eb6834", dark: "#d95926" },
  { light: "#1baf7a", dark: "#199e70" },
  { light: "#eda100", dark: "#c98500" },
];
export const MAX_COMPARE = 3;

export const squadSig = (ids) => (ids || []).filter((id) => S.byId.has(id)).slice().sort().join(",");

export function slotMap() {
  const map = new Map([[squadSig(S.squad), 0]]);
  let next = 1;
  for (const name of S.compare) {
    const d = S.drafts.find((x) => x.name === name);
    if (!d) continue;
    const key = squadSig(d.squad);
    if (!map.has(key)) map.set(key, next++);
  }
  return map;
}

export const draftSlot = (ids, map) => map.get(squadSig(ids));
export const isDark = () => {
  const t = document.documentElement.getAttribute("data-theme");
  return t === "dark" || (!t && matchMedia("(prefers-color-scheme: dark)").matches);
};
export const slotColor = (i) => SERIES_SLOTS[i % SERIES_SLOTS.length][isDark() ? "dark" : "light"];

export function draftMetrics(ids) {
  const live = ids.filter((id) => S.byId.has(id));
  const v = validity(live);
  return {
    ids: live, cost: v.cost, issues: v.issues,
    total: squadTotal(live),
    coverage: exposure(live).share,
    legal: live.length === 15 && !v.issues.some((i) => i.bad),
    missing: 15 - live.length,
  };
}

const persistDrafts = () => { saveLocal(STORE.drafts, S.drafts); markSynced(); };

export async function syncDrafts() {
  let remote = [];
  try {
    const res = await api("/api/drafts");
    if (!res.ok) return;
    const data = await res.json();
    remote = data.drafts || [];
    S.draftsPath = data.path;
  } catch (_) { return; }

  const merged = new Map(S.drafts.map((d) => [d.name, d]));
  for (const d of remote) {
    const mine = merged.get(d.name);
    if (!mine || (d.saved_at || "") > (mine.saved_at || "")) merged.set(d.name, d);
  }
  S.drafts = [...merged.values()].sort((a, b) => a.name.localeCompare(b.name));
  persistDrafts();

  for (const draft of S.drafts) {
    try {
      await api("/api/drafts", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
    } catch (_) { break; }
  }
  renderDrafts();
}

export function saveDraftAs(name) {
  name = name.trim();
  if (!name) return alert("Give the draft a name first.");
  if (!S.squad.length) return alert("Nothing to save — the squad is empty.");
  S.drafts = S.drafts.filter((d) => d.name !== name);
  S.drafts.push({
    name, squad: [...S.squad], notes: "",
    saved_at: new Date().toISOString().slice(0, 19),
    context: {
      horizon: +$("#horizon").value, gw_decay: gwDecay(),
      half_life: hlJSON(halfLife()),
      budget: +$("#budget").value, edited_players: Object.keys(S.edits).length,
    },
  });
  S.drafts.sort((a, b) => a.name.localeCompare(b.name));
  persistDrafts();
  renderDrafts();
}

export function saveDraft() {
  saveDraftAs($("#draftName").value);
  $("#draftName").value = "";
}

export async function deleteDraft(name) {
  if (!confirm(`Delete the draft “${name}”?`)) return;
  S.drafts = S.drafts.filter((d) => d.name !== name);
  S.compare = S.compare.filter((n) => n !== name);
  persistDrafts();
  try {
    await api(`/api/drafts/${encodeURIComponent(name)}`, { method: "DELETE" });
  } catch (_) { }
  if (S.compareWith === `draft:${name}`) S.compareWith = "optimal";
  renderDrafts(); renderSquad(); renderCompare(); renderGwChart();
}

export function loadDraft(name) {
  const d = S.drafts.find((x) => x.name === name);
  if (!d) return;
  S.squad = d.squad.filter((id) => S.byId.has(id));
  saveSquad(); renderAll();
}

export function toggleCompare(name) {
  if (S.compare.includes(name)) S.compare = S.compare.filter((n) => n !== name);
  else {
    if (S.compare.length >= MAX_COMPARE) {
      alert(`Compare up to ${MAX_COMPARE} saved drafts at once — beyond that the `
        + `chart's colours stop being reliably distinguishable.`);
      return;
    }
    S.compare.push(name);
  }
  renderDrafts(); renderGwChart();
}

export function renderDrafts() {
  renderCompareOptions();
  if (!S.meta) return;
  const box = $("#draftList");
  box.innerHTML = "";
  $("#draftHint").textContent = S.drafts.length
    ? `${S.drafts.length} saved · ${S.draftsPath || ""}`
    : "No saved drafts yet.";

  const slots = slotMap();
  for (const d of S.drafts) {
    const m = draftMetrics(d.squad);
    const selected = S.compare.includes(d.name);
    const slot = selected ? draftSlot(d.squad, slots) : null;
    const row = document.createElement("div");
    row.className = "draftrow" + (selected ? " sel" : "");
    const ctx = d.context || {};
    const savedDecay = ctxDecay(ctx);
    const drift = ctx.horizon && (ctx.horizon !== +$("#horizon").value
      || (savedDecay !== null && Math.abs(savedDecay - gwDecay()) > 0.005));
    row.innerHTML = `
      <input type="checkbox" ${selected ? "checked" : ""} data-cmp="${encodeURIComponent(d.name)}"
             aria-label="Compare ${d.name}">
      ${slot !== undefined && slot !== null ? `<span class="swatch" style="background:${slotColor(slot)}"></span>` : ""}
      <span class="dname">${d.name}</span>
      <span class="dnum">${m.total.toFixed(1)} xPts</span>
      <span class="dnum">£${m.cost.toFixed(1)}m</span>
      <span class="dmeta">${m.legal ? "legal" : (m.missing > 0 ? `${m.missing} short` : "illegal")}
        · ${Math.round(m.coverage * 100)}% cover</span>
      ${drift ? `<span class="flag" title="Saved at horizon ${ctx.horizon}, ${savedDecay === null ? "unknown decay" : decayText(savedDecay)};
        shown re-scored under the current settings">re-scored</span>` : ""}
      <span class="spacer"></span>
      <span class="dmeta">${(d.saved_at || "").replace("T", " ").slice(0, 16)}</span>
      <button class="mini" data-load="${encodeURIComponent(d.name)}">Load</button>
      <button class="mini danger" data-del="${encodeURIComponent(d.name)}">Delete</button>`;
    box.appendChild(row);
  }
  renderComparison();
}

export function renderComparison() {
  const box = $("#cmpBox");
  if (S.compare.length < 1) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");

  const slots = slotMap();
  const entries = [{ name: "Current draft", ids: S.squad, slot: 0 }];
  S.compare.forEach((name) => {
    const d = S.drafts.find((x) => x.name === name);
    if (d) entries.push({ name: d.name, ids: d.squad, slot: draftSlot(d.squad, slots) });
  });
  const rows = entries.map((e) => ({ ...e, m: draftMetrics(e.ids) }));
  const best = Math.max(...rows.map((r) => r.m.total));

  const cells = (r) => `
    <td style="text-align:left"><span class="swatch" style="background:${slotColor(r.slot)}"></span>
      &nbsp;${r.name}</td>
    <td>${r.m.total.toFixed(1)}</td>
    <td>${r.m.total === best ? "—" : (r.m.total - best).toFixed(1)}</td>
    <td>£${r.m.cost.toFixed(1)}</td>
    <td>${Math.round(r.m.coverage * 100)}%</td>
    <td>${r.m.legal ? "✓" : (r.m.missing > 0 ? `${r.m.missing} short` : "illegal")}</td>`;

  const baseline = new Set(S.squad);
  const diffs = rows.slice(1).map((r) => {
    const theirs = new Set(r.m.ids);
    const onlyThem = r.m.ids.filter((id) => !baseline.has(id));
    const onlyMine = [...baseline].filter((id) => !theirs.has(id));
    const nm = (id) => S.byId.get(id)?.name || id;
    return `<div class="diffcol">
      <h4><span class="swatch" style="background:${slotColor(r.slot)}"></span>${r.name}</h4>
      ${onlyThem.length ? `<div class="dmeta">only in this draft</div><ul>${onlyThem.map((id) => `<li>${nm(id)}</li>`).join("")}</ul>` : `<div class="none">nothing it has that you don't</div>`}
      ${onlyMine.length ? `<div class="dmeta" style="margin-top:6px">missing vs current</div><ul>${onlyMine.map((id) => `<li>${nm(id)}</li>`).join("")}</ul>` : ""}
    </div>`;
  }).join("");

  box.innerHTML = `
    <table>
      <thead><tr><th style="text-align:left">Draft</th><th>xPts</th><th>vs best</th>
        <th>Cost</th><th>Cover</th><th>Legal</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>${cells(r)}</tr>`).join("")}</tbody>
    </table>
    ${diffs ? `<div class="diffgrid">${diffs}</div>` : ""}`;
}

const DATA_CACHE = "fpl-data-v1";

export async function loadSnapshot(path = "/snapshot.json") {
  const res = await api(path);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.error || detail.detail || `snapshot unavailable (${res.status})`);
  }
  try {
    (await caches.open(DATA_CACHE)).put(path, res.clone());
  } catch (_) { }
  return res.json();
}

export function rebuildPool(keepSquad = true) {
  const data = derivePool(S.snapshot, S.edits, planOpts(), S.fixtureEdits);

  S.players = data.players; S.gameweeks = data.gameweeks; S.meta = data.meta;
  S.byId = new Map(data.players.map((p) => [p.id, p]));
  S.byCode = new Map(data.players.map((p) => [p.code, p.id]));
  if (S.squadCodesPending) {
    S.squad = resolveSquadCodes(S.squadCodesPending);
    S.purchase = resolvePurchaseCodes(S.purchaseCodesPending);
    S.squadCodesPending = S.purchaseCodesPending = null;
  }
  if (!keepSquad) S.squad = [];
  S.squad = S.squad.filter((id) => S.byId.has(id));
  S.optimal = S.optimal.filter((id) => S.byId.has(id));
  S.include = S.include.filter((id) => S.byId.has(id));
  S.exclude = S.exclude.filter((id) => S.byId.has(id));
  S.poolOut = S.poolOut.filter((id) => S.byId.has(id));
  S.poolIn = S.poolIn.filter((id) => S.byId.has(id));
  S.optimalPts = S.optimal.length === 15 ? squadTotal(S.optimal) : null;

  const m = data.meta;
  $("#meta").textContent = `GW${m.start_gw}–GW${m.start_gw + m.horizon - 1} · `
    + `${m.priced_gws.length} of ${m.horizon} gameweeks bookmaker-priced · `
    + `${data.players.length} players`;
  renderFreshness();

  const sel = $("#startgw");
  if (sel.options.length <= 1) {
    for (let gw = m.start_gw; gw <= 38; gw++) {
      const option = document.createElement("option");
      option.value = gw; option.textContent = "GW" + gw;
      sel.appendChild(option);
    }
  }
  sel.value = String(m.start_gw);

  if (!benchTouched && m.bench_slot_weights) {
    for (const k of BENCH_KEYS) {
      if (m.bench_slot_weights[k] != null) $(`#bw_${k}`).value = m.bench_slot_weights[k];
    }
  }
  if (!chipEconTouched) setChipEconDefaults(S.snapshot.rules.CHIP_HOLD_VALUE, S.snapshot.rules.FT_VALUE);
  renderControlValues();
  renderEditBanner();
}

export function renderFreshness() {
  const m = S.meta;
  const { label, level } = staleness(m.generated_at);
  const el = $("#freshness");
  el.classList.toggle("warn", level === "warn");
  el.classList.toggle("bad", level === "bad");
  el.innerHTML =
    `Snapshot <b>${label}</b> · frozen at GW${m.start_gw}, ${m.snapshot_horizon} gameweeks`
    + (m.recency ? ` · recency ${m.recency}gw` : "")
    + (m.previous != null && m.previous !== 1 ? ` · last season ×${(+m.previous).toFixed(2)}` : "")
    + (SKIP_PULL ? ` · <b>not syncing down</b> (nopull=1)` : "")
    + (ADOPT_REMOTE ? ` · <b>adopted the remote copy wholesale</b> (adoptremote=1)` : "");
  for (const option of $("#horizon").options) {
    option.disabled = +option.value > m.snapshot_horizon;
  }
}

export async function loadPool(keepSquad = true) {
  const wrap = $(".wrap");
  wrap.classList.add("stale");
  try {
    if (!S.snapshot) S.snapshot = await loadSnapshot();
    clampHorizon();
    rebuildPool(keepSquad);
  } catch (error) {
    $("#loading").innerHTML = `<div style="max-width:30rem;text-align:center">`
      + `<p><b>No projection available.</b></p><p class="sub">${error.message}</p>`
      + `<p class="sub">Open this once while the laptop is running and it will be `
      + `cached on the phone from then on.</p></div>`;
    wrap.classList.remove("stale");
    return;
  }
  wrap.classList.remove("stale");
  $("#loading").classList.add("hidden");
  renderAll();
  scheduleSolve(0);
}

export async function setLastSeason() {
  if (serverPresent !== false) return sync(false);
  const pct = Math.round(+$("#lastseason").value * 4) * 25;
  try {
    S.snapshot = await loadSnapshot(pct === 25 ? "/snapshot.json" : `/snapshots/prev-${pct}.json`);
  } catch (error) {
    alert(`Could not load that weight — ${error.message}`);
  }
  rebuildPool();
  renderAll();
  scheduleSolve(0);
}

export async function sync(refresh = false) {
  const btn = $("#syncBtn");
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = refresh ? "Refetching…" : "Syncing…";
  try {
    const res = await api("/api/snapshot", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recency: +$("#recency").value, previous: +$("#lastseason").value,
                             start_gw: startGw(), refresh }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `sync failed (${res.status})`);
    }
    S.snapshot = await loadSnapshot();
    rebuildPool();
    renderAll();
    scheduleSolve(0);
    await syncDrafts();
  } catch (error) {
    alert(`Could not sync — is the laptop running \`fpl.py serve\`?\n\n${error.message}`);
  } finally {
    btn.disabled = false; btn.textContent = was;
  }
}

export function comparisonSquad() {
  if (S.compareWith === "none") return null;
  if (S.compareWith.startsWith("draft:")) {
    const name = S.compareWith.slice(6);
    const draft = S.drafts.find((d) => d.name === name);
    if (!draft) return null;
    const ids = draft.squad.filter((id) => S.byId.has(id));
    return { ids, label: draft.name, kind: "draft",
             bench: benchSlots(ids), cost: ids.reduce((a, id) => a + S.byId.get(id).price, 0) };
  }
  return { ids: S.optimal, label: "Optimal squad", kind: "optimal",
           bench: S.optimalBench,
           cost: S.optimalCost ?? S.optimal.reduce((a, id) => a + (S.byId.get(id)?.price || 0), 0) };
}

export function renderCompareOptions() {
  const select = $("#compareWith");
  const options = [
    ["optimal", "Optimal squad"],
    ...S.drafts.map((d) => [`draft:${d.name}`, d.name]),
    ["none", "Nothing — one pitch"],
  ];
  const wanted = options.some(([v]) => v === S.compareWith) ? S.compareWith : "optimal";
  S.compareWith = wanted;
  select.innerHTML = options.map(([value, label]) =>
    `<option value="${value}"${value === wanted ? " selected" : ""}>${label}</option>`).join("");
}

export const squadCost = (ids) => ids.reduce((a, id) => a + (S.byId.get(id)?.price || 0), 0);
export const sellPrice = (id) => {
  const now = S.byId.get(id)?.price || 0;
  const bought = S.purchase[id];
  return bought == null ? now : sellPriceOf(bought, now);
};
export const squadSellValue = (ids) => ids.reduce((a, id) => a + sellPrice(id), 0);

const optDiff = () => {
  const mine = new Set(S.squad), theirs = new Set(S.optimal);
  return {
    incoming: S.optimal.filter((id) => !mine.has(id)),
    outgoing: S.squad.filter((id) => !theirs.has(id)),
  };
};
export { optDiff };

export function renderOptStatus() {
  const box = $("#optStatus");
  const showing = S.compareWith === "optimal";
  box.classList.toggle("err", showing && S.optimalState === "error");
  box.textContent = !showing ? "" : {
    idle: "", pending: "settings changed…", solving: "solving…",
    ready: "up to date", error: "no solution",
  }[S.optimalState] || "";
  $("#comparePitch").classList.toggle("pending", showing && S.optimalState !== "ready");
  const ready = S.optimal.length === 15;
  $("#optResolve").disabled = !showing;
  $("#optimise").disabled = !ready;
  const diff = ready ? optDiff() : null;
  $("#tabOptCount").textContent =
    !diff || !S.squad.length ? "" : diff.incoming.length ? String(diff.incoming.length) : "✓";
}

export function optSettingsText() {
  const bits = [
    `${$("#horizon").value} gameweeks`,
    `£${(+$("#budget").value).toFixed(1)}m`,
    decayText(gwDecay()),
    `max ${$("#maxclub").value}/club`,
  ];
  if (+$("#ownw").value) bits.push(`template tilt ${(+$("#ownw").value).toFixed(2)}`);
  if ($("#formation").value) bits.push($("#formation").value);
  if (S.include.length) bits.push(`${S.include.length} required`);
  if (S.exclude.length) bits.push(`${S.exclude.length} barred`);
  return "Solved for: " + bits.join(" · ");
}

export function renderCompare() {
  renderCompareOptions();
  renderOptStatus();
  const box = $("#comparePitch");
  box.innerHTML = "";
  $("#optSettings").textContent = S.compareWith === "optimal" ? optSettingsText() : "";

  const other = comparisonSquad();
  $("#pitchPair").classList.toggle("solo", !other);
  $("#compareSide").hidden = !other;
  $("#swapCard").hidden = !other;
  $("#optCopy").disabled = !other || other.ids.length !== 15;

  if (!other) { renderSwap(null); return; }

  if (S.compareWith === "optimal" && S.optimalState === "error") {
    $("#optShapeTag").textContent = "—";
    $("#sideThemPts").textContent = "—"; $("#sideThemCost").textContent = "";
    box.innerHTML = `<div class="issue bad"><span class="ico">!</span><span>${S.optimalError}</span></div>`;
    renderSwap(null);
    return;
  }
  if (!other.ids.length) {
    $("#optShapeTag").textContent = "—";
    $("#sideThemPts").textContent = "—"; $("#sideThemCost").textContent = "";
    renderSwap(null);
    return;
  }

  const mine = new Set(S.squad);
  const tags = {};
  if (S.squad.length) {
    for (const id of other.ids) if (!mine.has(id)) tags[id] = { kind: "in", text: "in" };
  }

  const layout = squadLayout({
    ids: other.ids, lookup: S.byId, need: S.meta.squad_by_pos,
    xiIds: planXI(other.ids).ids, benchOrder: other.bench,
  });
  const { captain, vice } = captaincy(other.ids);
  box.innerHTML = pitchHTML({
    layout, teams: S.snapshot?.teams, metrics: S.metrics,
    captain, vice, tags, swap: true, versus: true,
  });
  wireShirts(box);
  markVersus(box);

  $("#optShapeTag").textContent = layout.shape || `${other.ids.length}/15`;
  const total = S.compareWith === "optimal" && S.optimalPts !== null
    ? S.optimalPts : squadTotal(other.ids);
  $("#sideThemPts").textContent = other.ids.length ? fmt(total, 1) : "—";
  $("#sideThemCost").textContent = "£" + fmt(other.cost ?? squadCost(other.ids), 1);

  renderSwap(other);
}

export function renderSwap(other) {
  const box = $("#swapBox");
  if (!other || !other.ids.length) {
    box.innerHTML = `<div class="empty-note">Pick a squad to compare against.</div>`;
    $("#swapTitle").textContent = "The difference";
    $("#swapNote").textContent = "";
    return;
  }
  if (!S.squad.length) {
    box.innerHTML = `<div class="empty-note">Draft a squad, or copy this one across,
      and every change between the two will be priced here.</div>`;
    $("#swapTitle").textContent = `${other.label} — nothing to compare yet`;
    $("#swapNote").textContent = "";
    return;
  }

  const mine = new Set(S.squad), theirs = new Set(other.ids);
  const rank = (p) => POS_ORDER.indexOf(p.pos);
  const byPos = (a, b) => rank(a) - rank(b) || (b.xpts_plan || 0) - (a.xpts_plan || 0);
  const outs = S.squad.filter((id) => !theirs.has(id)).map((id) => S.byId.get(id))
    .filter(Boolean).sort(byPos);
  const ins = other.ids.filter((id) => !mine.has(id)).map((id) => S.byId.get(id))
    .filter(Boolean).sort(byPos);

  $("#swapTitle").textContent = outs.length || ins.length
    ? `${ins.length} in, ${outs.length} out vs ${other.label}`
    : `Identical to ${other.label}`;
  $("#swapNote").textContent = outs.length || ins.length
    ? "Paired by position — the swap you would actually make."
    : "Same fifteen players.";

  if (!outs.length && !ins.length) {
    box.innerHTML = `<div class="issue ok"><span class="ico">✓</span><span>The two squads
      hold the same fifteen players.</span></div>`;
    return;
  }

  const pairs = [];
  const pool = [...ins];
  for (const out of outs) {
    const match = pool.findIndex((p) => p.pos === out.pos);
    pairs.push({ out, in: match >= 0 ? pool.splice(match, 1)[0] : null });
  }
  for (const left of pool) pairs.push({ out: null, in: left });
  pairs.sort((a, b) => rank(a.out || a.in) - rank(b.out || b.in));

  const cell = (p, kind) => p
    ? `<span class="who"><span class="dirtag ${kind}">${kind}</span>
         <b>${p.name}</b> <span class="sub">${p.team_short}</span></span>`
    : `<span class="sub">—</span>`;
  const delta = (value, digits = 1) => {
    if (!isFinite(value)) return `<span class="sub">—</span>`;
    const cls = value > 0.049 ? "gain" : value < -0.049 ? "loss" : "";
    const sign = value > 0 ? "+" : "";
    return `<span class="${cls}">${sign}${value.toFixed(digits)}</span>`;
  };
  const num = (p, pick, digits) => p ? fmt(pick(p), digits) : "—";

  const rows = pairs.map((pair) => {
    const gainPts = (pair.in?.xpts_plan || 0) - (pair.out?.xpts_plan || 0);
    const gainCost = (pair.in?.price || 0) - (pair.out?.price || 0);
    return `<tr>
      <td>${cell(pair.out, "out")}</td>
      <td>${cell(pair.in, "in")}</td>
      <td>${num(pair.out, (p) => p.xpts_plan, 1)} → ${num(pair.in, (p) => p.xpts_plan, 1)}</td>
      <td>${delta(gainPts)}</td>
      <td>${num(pair.out, (p) => p.xppg, 2)} → ${num(pair.in, (p) => p.xppg, 2)}</td>
      <td>£${num(pair.out, (p) => p.price, 1)} → £${num(pair.in, (p) => p.price, 1)}</td>
      <td>${delta(gainCost)}</td>
    </tr>`;
  }).join("");

  const sum = (list, pick) => list.reduce((a, p) => a + (pick(p) || 0), 0);
  const totalPts = sum(ins, (p) => p.xpts_plan) - sum(outs, (p) => p.xpts_plan);
  const totalCost = sum(ins, (p) => p.price) - sum(outs, (p) => p.price);

  box.innerHTML = `
    <div class="chartbox">
      <table class="swaptable">
        <thead><tr>
          <th>Out of yours</th><th>In from ${other.label}</th>
          <th>xPts</th><th>Δ xPts</th><th>xPPG</th><th>Price</th><th>Δ £m</th>
        </tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr>
          <td colspan="2">All ${pairs.length} change${pairs.length === 1 ? "" : "s"}</td>
          <td></td><td>${delta(totalPts)}</td><td></td><td></td><td>${delta(totalCost)}</td>
        </tr></tfoot>
      </table>
    </div>`;
}

export function swapIn(id) {
  const p = S.byId.get(id);
  if (!p || S.squad.includes(id)) return;
  const need = S.meta.squad_by_pos[p.pos];
  const holding = S.squad.map((x) => S.byId.get(x)).filter((x) => x && x.pos === p.pos);

  if (holding.length >= need || S.squad.length >= 15) {
    const wanted = new Set(S.optimal);
    const droppable = holding.filter((x) => !wanted.has(x.id))
      .sort((a, b) => (a.xpts_plan || 0) - (b.xpts_plan || 0));
    if (!droppable.length) {
      alert(`No ${p.pos} to drop — your draft already holds every ${p.pos} the solver picked.`);
      return;
    }
    S.squad = S.squad.filter((x) => x !== droppable[0].id);
  }
  S.squad.push(id);
  saveSquad(); renderAll();
}

export function renderAll() {
  renderCompareOptions();
  renderPool(); renderSquad(); renderCompare(); renderConstraints();
  renderLineup();
  renderDrafts();
  $("#tabDraftCount").textContent = S.drafts.length ? String(S.drafts.length) : "";
  if (S.tab === "analysis") {
    renderGwChart(); renderExposure(); renderTimeline(); renderFixtures(); renderNearMisses();
  }
  if (S.tab === "chips") { renderChips(); }
}

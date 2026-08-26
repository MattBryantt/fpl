/* The Analysis tab: the gw line chart, exposure bars, the timeline heatmap,
 * the fixtures card, and the near-miss card (UI presentation only -- the
 * sweep itself runs in transfer-view.mjs, which shares the squad optimiser's
 * worker). See REFACTOR.md for the split this belongs to. */
"use strict";
import { $, S, css, fmt, el, showTip, hideTip, calibrateOdds, saveLocal, STORE } from "/assets/state.mjs";
import { fixtureKey, withOddsCalibration } from "/assets/board.mjs";
import { cleanSheetProb } from "/assets/poisson.mjs";
import {
  gwPoints, gwLineup, exposure, slotMap, slotColor, draftSlot,
  rebuildPool, renderAll,
} from "/assets/squad-view.mjs";
import { scheduleSolve, nearMissCount, nearMissKey } from "/assets/transfer-view.mjs";

/* -------------------------------------------------------------- gw line chart */
export function renderGwChart() {
  const host = $("#gwChart");
  host.innerHTML = "";
  if (!S.squad.length) { host.innerHTML = `<div class="empty-note">Draft a squad to see its weekly profile.</div>`; return; }

  const gws = S.gameweeks;
  const mine = gws.map((_, i) => gwPoints(S.squad, i));
  // Once you have pressed "Fill optimal" and not yet edited, the two squads are
  // the same and the second line would sit exactly under the first -- two keys
  // in the legend for one visible line reads as a bug. Say so instead.
  const identical = S.optimal.length === 15 && S.squad.length === 15 &&
    S.optimal.slice().sort().join() === S.squad.slice().sort().join();
  const comparing = S.compare.length > 0;
  const series = [{
    name: comparing ? "Current draft"
      : identical ? "Your draft (identical to optimal)" : "Your draft",
    data: mine, color: slotColor(0), ids: S.squad,
  }];
  if (comparing) {
    // Ticked drafts replace the optimal overlay: showing both at once would put
    // five lines on a chart whose palette is validated for four.
    const slots = slotMap();
    S.compare.forEach((name) => {
      const d = S.drafts.find((x) => x.name === name);
      if (d) {
        const ids = d.squad.filter((id) => S.byId.has(id));
        series.push({ name: d.name, color: slotColor(draftSlot(ids, slots)), ids,
                      data: gws.map((_, g) => gwPoints(ids, g)) });
      }
    });
  } else if (S.optimal.length && !identical) {
    series.push({ name: "Optimal", data: gws.map((_, i) => gwPoints(S.optimal, i)),
                  color: slotColor(1), ids: S.optimal });
  }

  // Two drafts with the same fifteen players draw the same line, and the one
  // underneath is invisible -- a legend listing three series over two visible
  // lines reads as a broken chart. Fold duplicates into one key that names both.
  const seen = new Map();
  for (const s of series) {
    const key = (s.ids || []).slice().sort().join(",") || s.data.map((v) => v.toFixed(3)).join(",");
    if (seen.has(key)) seen.get(key).name += ` = ${s.name}`;
    else seen.set(key, s);
  }
  series.length = 0;
  series.push(...seen.values());

  // A legend is mandatory for two or more series and pointless for one -- with a
  // single line the heading already names what is plotted, so a lone swatch just
  // restates it. Use the space for the note instead.
  $("#gwLegend").innerHTML = series.length > 1
    ? series.map((s) => `<span class="key"><i style="background:${s.color}"></i>${s.name}</span>`).join("")
    : `<span class="sub">${identical
        ? "Your draft is currently identical to the optimal squad — edit it to compare."
        : "Press ‘Fill optimal’, or tick a saved draft below, to overlay a comparison."}</span>`;

  const W = Math.max(300, host.clientWidth || 460), H = 250;
  const M = { t: 14, r: 54, b: 30, l: 40 };
  const all = series.flatMap((s) => s.data);
  const lo = Math.min(...all) * 0.94, hi = Math.max(...all) * 1.04;
  const x = (i) => M.l + (i * (W - M.l - M.r)) / Math.max(1, gws.length - 1);
  const y = (v) => H - M.b - ((v - lo) / (hi - lo || 1)) * (H - M.t - M.b);

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" }, host);
  el("title", {}, svg).textContent = "Projected starting XI points for each gameweek";

  // Recessive solid hairline grid, rounded to clean values.
  const step = Math.max(1, Math.round((hi - lo) / 4));
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    el("line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v), stroke: css("--grid"), "stroke-width": 1 }, svg);
    const t = el("text", { x: M.l - 8, y: y(v) + 4, "text-anchor": "end", fill: css("--muted"),
      "font-size": 11, style: "font-variant-numeric:tabular-nums" }, svg);
    t.textContent = v;
  }
  gws.forEach((gw, i) => {
    const t = el("text", { x: x(i), y: H - 10, "text-anchor": "middle", fill: css("--muted"), "font-size": 11 }, svg);
    t.textContent = "GW" + gw;
  });
  el("line", { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, stroke: css("--axis"), "stroke-width": 1 }, svg);

  for (const s of series) {
    el("path", {
      d: s.data.map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join(" "),
      fill: "none", stroke: s.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }, svg);
    // End marker with a 2px surface ring, plus a direct end-label so identity
    // never rests on colour alone.
    const last = s.data.length - 1;
    el("circle", { cx: x(last), cy: y(s.data[last]), r: 4.5, fill: s.color,
      stroke: css("--surface-1"), "stroke-width": 2 }, svg);
    const lab = el("text", { x: x(last) + 9, y: y(s.data[last]) + 4, fill: css("--text-secondary"),
      "font-size": 11.5, style: "font-variant-numeric:tabular-nums" }, svg);
    lab.textContent = s.data[last].toFixed(1);
  }

  // Crosshair band — hit targets span the whole column, never the 2px line.
  const cross = el("line", { y1: M.t, y2: H - M.b, stroke: css("--axis"), "stroke-width": 1, opacity: 0 }, svg);
  gws.forEach((gw, i) => {
    const half = (W - M.l - M.r) / Math.max(1, gws.length - 1) / 2;
    const band = el("rect", { x: x(i) - half, y: M.t, width: half * 2, height: H - M.t - M.b,
      fill: "transparent", style: "cursor:crosshair" }, svg);
    band.addEventListener("mousemove", (e) => {
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 1);
      showTip(e, `<b>GW${gw}</b><br>` + series.map((s) =>
        `<span class="key"><i style="background:${s.color};width:9px;height:9px;border-radius:50%"></i>${s.name} <span class="r">${s.data[i].toFixed(1)}</span></span>`).join("<br>"));
    });
    band.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  });

  $("#gwTable").innerHTML = `<table><thead><tr><th>GW</th>${series.map((s) => `<th>${s.name}</th>`).join("")}</tr></thead>
    <tbody>${gws.map((gw, i) => `<tr><td>GW${gw}</td>${series.map((s) => `<td>${s.data[i].toFixed(1)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

/* --------------------------------------------------------------- exposure bars */
export function renderExposure() {
  const host = $("#expChart");
  host.innerHTML = "";
  const exp = exposure(S.squad);
  if (!exp.top.length) { host.innerHTML = `<div class="empty-note">No data.</div>`; return; }

  const rowH = 27, W = Math.max(300, host.clientWidth || 460), M = { l: 116, r: 46, t: 4, b: 4 };
  const H = exp.top.length * rowH + M.t + M.b;
  const max = Math.max(...exp.top.map((p) => p.exposure));
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" }, host);
  el("title", {}, svg).textContent = "Players you do not own, ranked by ownership times projected points";

  exp.top.forEach((p, i) => {
    const y = M.t + i * rowH;
    // 24px bar in a 27px band: the leftover is the surface gap between bars.
    const w = (p.exposure / max) * (W - M.l - M.r);
    const label = el("text", { x: M.l - 9, y: y + 17, "text-anchor": "end",
      fill: css("--text-secondary"), "font-size": 12 }, svg);
    label.textContent = p.name;
    el("rect", { x: M.l, y: y + 3, width: Math.max(2, w), height: 18, rx: 4,
      fill: css("--series-1") }, svg);
    // Square the baseline end: only the data-end is rounded.
    el("rect", { x: M.l, y: y + 3, width: Math.min(4, Math.max(2, w)), height: 18, fill: css("--series-1") }, svg);
    const val = el("text", { x: M.l + w + 8, y: y + 17, fill: css("--text-secondary"),
      "font-size": 11.5, style: "font-variant-numeric:tabular-nums" }, svg);
    val.textContent = p.exposure.toFixed(1);
    const hit = el("rect", { x: 0, y, width: W, height: rowH, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", (e) => showTip(e,
      `<b>${p.name}</b> · ${p.team_short} £${p.price.toFixed(1)}m<br>
       <span class="r">${p.owned.toFixed(1)}% owned · ${p.xpts_plan.toFixed(1)} xPts</span><br>
       <span class="r">exposure ${p.exposure.toFixed(2)}</span>`));
    hit.addEventListener("mouseleave", hideTip);
  });

  $("#expTable").innerHTML = `<table><thead><tr><th>Player</th><th>Team</th><th>£</th><th>Own%</th><th>xPts</th><th>Exposure</th></tr></thead>
    <tbody>${exp.top.map((p) => `<tr><td>${p.name}</td><td>${p.team_short}</td><td>${p.price.toFixed(1)}</td><td>${p.owned.toFixed(1)}</td><td>${p.xpts_plan.toFixed(1)}</td><td>${p.exposure.toFixed(2)}</td></tr>`).join("")}</tbody></table>`;
}

/* ------------------------------------------------------------ timeline heatmap
   Diverging, because the question is polarity: is this gameweek above or below
   what this player normally does? Blue and red poles, neutral at his own mean. */
export function renderTimeline() {
  const host = $("#tlChart");
  host.innerHTML = "";
  if (!S.squad.length) {
    host.innerHTML = `<div class="empty-note">Draft a squad to see when its fixtures turn.</div>`;
    $("#tlLineup").innerHTML = "";
    return;
  }

  const gws = S.gameweeks;
  const rows = S.squad.map((id) => S.byId.get(id))
    .map((p) => {
      const mean = p.gw.reduce((a, b) => a + b, 0) / (p.gw.length || 1);
      const swing = Math.max(...p.gw) - Math.min(...p.gw);
      return { p, mean, swing };
    })
    .sort((a, b) => b.swing - a.swing);

  const cw = 62, rh = 27, M = { l: 150, t: 26, r: 8, b: 6 };
  const W = M.l + gws.length * cw + M.r, H = M.t + rows.length * rh + M.b;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" }, host);
  el("title", {}, svg).textContent = "Each player's projected points per gameweek relative to his own average";

  gws.forEach((gw, i) => {
    const t = el("text", { x: M.l + i * cw + cw / 2, y: 16, "text-anchor": "middle",
      fill: css("--muted"), "font-size": 11 }, svg);
    t.textContent = "GW" + gw;
  });

  rows.forEach((r, ri) => {
    const y = M.t + ri * rh;
    const name = el("text", { x: M.l - 10, y: y + 18, "text-anchor": "end",
      fill: css("--text-secondary"), "font-size": 12 }, svg);
    name.textContent = `${r.p.name}`;
    const pos = el("text", { x: 4, y: y + 18, fill: css("--muted"), "font-size": 11 }, svg);
    pos.textContent = `${r.p.pos} ${r.p.team_short}`;

    gws.forEach((gw, i) => {
      const v = r.p.gw[i] || 0;
      const rel = r.mean ? (v - r.mean) / r.mean : 0;
      const mag = Math.min(1, Math.abs(rel) / 0.3);
      const hue = rel >= 0 ? css("--pos") : css("--neg");
      // 2px surface gap on every side does the separating — no borders.
      el("rect", { x: M.l + i * cw + 1, y: y + 2, width: cw - 3, height: rh - 4, rx: 4,
        fill: Math.abs(rel) < 0.02 ? css("--mid") : hue,
        "fill-opacity": Math.abs(rel) < 0.02 ? 1 : (0.13 + mag * 0.42) }, svg);
      const t = el("text", { x: M.l + i * cw + cw / 2, y: y + 18, "text-anchor": "middle",
        fill: css("--text-primary"), "font-size": 11.5,
        style: "font-variant-numeric:tabular-nums" }, svg);
      t.textContent = v.toFixed(1);
      const hit = el("rect", { x: M.l + i * cw, y, width: cw, height: rh, fill: "transparent" }, svg);
      hit.addEventListener("mousemove", (e) => showTip(e,
        `<b>${r.p.name}</b> · GW${gw}<br><span class="r">${r.p.opp[i] || "no fixture"}</span><br>
         <span class="r">${v.toFixed(2)} xPts · his average ${r.mean.toFixed(2)}</span><br>
         <span class="r">${rel >= 0 ? "+" : ""}${Math.round(rel * 100)}% vs his own average</span>`));
      hit.addEventListener("mouseleave", hideTip);
    });
  });

  $("#tlTable").innerHTML = `<table><thead><tr><th>Player</th><th>Pos</th>${gws.map((g) => `<th>GW${g}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr><td>${r.p.name}</td><td>${r.p.pos}</td>${r.p.gw.map((v) => `<td>${v.toFixed(1)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;

  renderLineupStrip(gws);
}

// Same 15-man squad throughout the heatmap above; this is who from it actually
// starts, who's benched (autosub order) and who wears the armband each of
// those gameweeks — the CLI's `plan.lineups`, ported.
function renderLineupStrip(gws) {
  const host = $("#tlLineup");
  const rows = gws.map((gw, i) => {
    const lu = gwLineup(S.squad, i);
    return lu && { gw, lu };
  }).filter(Boolean);
  if (!rows.length) { host.innerHTML = ""; return; }

  const name = (id) => S.byId.get(id)?.name ?? "—";
  host.innerHTML = `
    <p class="lineupfoot" style="margin-top:14px">Weekly captain &amp; bench — same squad, picked fresh each gameweek. Starting XI is the squad minus bench order.</p>
    <div class="tlstrip"><table class="lineup nowrap">
      <thead><tr><th>GW</th><th>Shape</th><th>Captain</th><th>Vice</th><th>Bench order</th><th>XI pts</th></tr></thead>
      <tbody>${rows.map(({ gw, lu }) => `<tr>
        <td>GW${gw}</td>
        <td>${lu.shape.join("-")}</td>
        <td>${name(lu.captain)}</td>
        <td>${lu.vice === null ? "—" : name(lu.vice)}</td>
        <td>${lu.bench.map(name).join(", ")}</td>
        <td>${fmt(lu.total, 1)}</td>
      </tr>`).join("")}</tbody>
    </table></div>`;
}

/* ------------------------------------------------------------------ fixtures
   The model's own view of every match in the horizon -- what each side is
   expected to score, and the clean sheet chance that implies -- with the two
   xG figures editable in place. An override here is an opinion about a match,
   not about any one player, so it is applied in derivePool() and reaches every
   player of both clubs the moment it lands, the same way a bookmaker's price
   would have. See board.mjs's fixtureKey/applyFixtureEdits for the mechanism. */
export function fixtureOverrideCount() { return Object.keys(S.fixtureEdits || {}).length; }

export function renderFixtures() {
  const host = $("#fixturesBox");
  const resetAll = $("#fixResetAll");
  if (!host) return;
  if (!S.snapshot || !S.gameweeks.length) { host.innerHTML = ""; resetAll.classList.add("hidden"); return; }

  const n = fixtureOverrideCount();
  resetAll.classList.toggle("hidden", n === 0);
  resetAll.textContent = n === 1 ? "Reset 1 override" : `Reset ${n} overrides`;

  const teamShort = (t) => S.snapshot.teams?.[t]?.short || t;
  const inHorizon = new Set(S.gameweeks);
  const byGw = new Map();
  for (const f of withOddsCalibration(S.snapshot.fixtures, calibrateOdds())) {
    if (!inHorizon.has(f.gw)) continue;
    if (!byGw.has(f.gw)) byGw.set(f.gw, []);
    byGw.get(f.gw).push(f);
  }
  if (!byGw.size) { host.innerHTML = `<div class="empty-note">No fixtures in this horizon.</div>`; return; }

  const rows = [];
  for (const gw of S.gameweeks) {
    const fixtures = byGw.get(gw);
    if (!fixtures?.length) continue;
    rows.push(`<tr class="fixgrp"><td colspan="7">GW${gw}</td></tr>`);
    for (const f of fixtures) {
      const key = fixtureKey(f);
      const override = S.fixtureEdits[key];
      const lamHome = override?.lam_home ?? f.lam_home;
      const lamAway = override?.lam_away ?? f.lam_away;
      const csHome = cleanSheetProb(lamAway), csAway = cleanSheetProb(lamHome);
      rows.push(`<tr${override ? ' class="fixedited"' : ""}>
        <td>${teamShort(f.home_team)} vs ${teamShort(f.away_team)}</td>
        <td><span class="pill${f.source === "odds" ? " ok" : ""}">${f.source === "odds" ? "priced" : "modelled"}</span></td>
        <td><input class="mininput" type="number" inputmode="decimal" min="0" max="6" step="0.05"
            data-fixkey="${key}" data-fixfield="lam_home" value="${lamHome.toFixed(2)}"
            aria-label="${teamShort(f.home_team)} expected goals"></td>
        <td>${(csHome * 100).toFixed(0)}%</td>
        <td><input class="mininput" type="number" inputmode="decimal" min="0" max="6" step="0.05"
            data-fixkey="${key}" data-fixfield="lam_away" value="${lamAway.toFixed(2)}"
            aria-label="${teamShort(f.away_team)} expected goals"></td>
        <td>${(csAway * 100).toFixed(0)}%</td>
        <td>${override ? `<button class="editbtn" data-fixreset="${key}" title="Reset to the model's own figures">×</button>` : ""}</td>
      </tr>`);
    }
  }

  host.innerHTML = `<table class="lineup fixtable">
    <thead><tr>
      <th>Match</th><th>Source</th><th>Home xG</th><th>Home CS%</th><th>Away xG</th><th>Away CS%</th><th></th>
    </tr></thead>
    <tbody>${rows.join("")}</tbody>
  </table>`;
}

/** One fixture's xG typed over, in either direction. Rebuilds the whole pool --
 *  every player of both clubs is affected, not just one row -- the same way
 *  committing a player override does. */
export async function setFixtureLambda(key, field, raw) {
  if (raw === "" || Number.isNaN(+raw)) { renderFixtures(); return; }
  const value = Math.min(6, Math.max(0, +raw));
  const raw_f = S.snapshot.fixtures.find((x) => fixtureKey(x) === key);
  if (!raw_f) return;
  const f = withOddsCalibration([raw_f], calibrateOdds())[0];
  const model = field === "lam_home" ? f.lam_home : f.lam_away;
  const entry = { ...(S.fixtureEdits[key] || {}) };
  if (Math.abs(value - model) < 1e-9) delete entry[field];
  else entry[field] = value;
  if (Object.keys(entry).length) S.fixtureEdits[key] = entry;
  else delete S.fixtureEdits[key];
  saveLocal(STORE.fixtureEdits, S.fixtureEdits);
  rebuildPool();
  S.optimal = []; S.optimalPts = null; S.optimalCost = null; S.optimalBench = {};
  renderAll();
  scheduleSolve(400);
}

export function resetFixtureOverride(key) {
  if (!(key in S.fixtureEdits)) return;
  delete S.fixtureEdits[key];
  saveLocal(STORE.fixtureEdits, S.fixtureEdits);
  rebuildPool();
  S.optimal = []; S.optimalPts = null; S.optimalCost = null; S.optimalBench = {};
  renderAll();
  scheduleSolve(400);
}

export function resetAllFixtureOverrides() {
  if (!fixtureOverrideCount()) return;
  S.fixtureEdits = {};
  saveLocal(STORE.fixtureEdits, S.fixtureEdits);
  rebuildPool();
  S.optimal = []; S.optimalPts = null; S.optimalCost = null; S.optimalBench = {};
  renderAll();
  scheduleSolve(400);
}

/* ------------------------------------------------------------------ nearly in
   Ranking players cannot answer "who nearly made the squad". A player can be
   fourth in his position on points and nowhere near it because everyone above
   him is cheaper, and another can be twentieth and one swap away because he
   frees exactly the money the other fourteen wanted. The only honest answer is
   the solver's: force him in, rebuild everything around him, read the drop.

   That is a full re-solve per candidate, so it runs on a button rather than on
   every settings change -- and it reports each candidate as it lands, because
   several seconds of spinner is indistinguishable from a hang. */
export function renderNearMisses() {
  const box = $("#nearBox");
  if (!box) return;
  const near = S.nearMiss;
  const solving = near.state === "solving";
  const count = solving ? 0 : nearMissCount();

  const run = $("#nearRun");
  run.disabled = solving || !S.optimal.length;
  run.textContent = solving ? "Solving…"
    : count ? `Test ${count} candidates` : "Find near misses";
  $("#nearClub").setAttribute("aria-pressed", String(near.perClub));
  $("#nearClub").disabled = solving;

  if (solving) {
    const { done, total } = near.progress;
    box.innerHTML = `<div class="sub">Rebuilding the squad around each candidate —
        ${done} of ${total || "?"} solved. The order settles as it goes.</div>
      <div class="meter" style="margin-bottom:12px"><div style="width:${
        total ? (done / total) * 100 : 0}%"></div></div>`
      + (near.rows.length ? nearMissTableHTML(near.rows, (p) => p.xpts_plan, "xPts") : "");
    return;
  }
  if (near.state === "error") {
    box.innerHTML = `<div class="issue bad"><span class="ico">!</span><span>${near.error}</span></div>`;
    return;
  }
  if (near.state === "idle" || !near.rows.length) {
    box.innerHTML = `<div class="empty-note">${
      !S.optimal.length ? "Waiting for the optimal squad."
      : near.state === "ready" ? "Nobody to test — every player the solver can see is in the squad."
      : "One full re-solve per candidate. On a full pool that is a second or two each, "
        + "so the ranking fills in as it goes rather than arriving all at once."
    }</div>`;
    return;
  }

  box.innerHTML = (near.key === nearMissKey() ? "" : `<div class="issue bad" style="margin-bottom:10px">
      <span class="ico">!</span><span>The settings have moved since this ran — every
      gap below was measured against a squad that is no longer the answer.</span></div>`)
    + nearMissTableHTML(near.rows, (p) => p.xpts_plan, "xPts")
    + `<p class="sub" style="margin:10px 0 0">${near.tested} candidates re-solved in
      ${((near.ms || 0) / 1000).toFixed(1)}s. ${near.perClub
        ? "Every club's own price/points frontier."
        : "The price/points frontier: anyone left out is beaten outright by a player at the same price or less, so he cannot be closer than that player is."}</p>`;
}

/** The ranking itself, shared by the board's answer and the Chips tab's. Only
 *  the column of points differs — the horizon there, one gameweek here — and
 *  the header says which, because a number labelled xPts that turns out to be a
 *  single week is worse than no number. */
export function nearMissTableHTML(rows, pointsOf, pointsLabel) {
  // The bar is scaled to the widest gap on show rather than to an absolute
  // number: the question is which of these men is closest, and a fixed scale
  // would flatten a tightly packed list into a column of identical stubs.
  const worst = Math.max(...rows.map((r) => r.gap ?? 0), 0.01);
  const name = (id) => S.byId.get(id)?.name ?? "—";

  return `<div class="chartbox"><table class="nearmiss"><thead><tr>
      <th class="l">Player</th><th class="l">Pos</th><th class="l">Team</th><th>£</th>
      <th>${pointsLabel}</th><th>Gap</th><th class="barh"></th><th class="l">Would be</th>
      <th class="l">In place of</th></tr></thead><tbody>`
    + rows.map((r) => {
        const p = S.byId.get(r.id);
        if (!p) return "";
        const tie = r.gap !== null && r.gap < 0.005;
        return `<tr data-near="${r.id}" title="Open ${p.name}">
          <td class="l"><b>${p.name}</b></td><td class="l">${p.pos}</td>
          <td class="l">${p.team_short}</td><td>${p.price.toFixed(1)}</td>
          <td>${fmt(pointsOf(p), 1)}</td>
          <td class="${tie ? "tie" : ""}">${
            r.gap === null ? "—" : tie ? "tie" : r.gap.toFixed(2)}</td>
          <td><span class="nearbar${r.gap === null ? " none" : ""}" style="width:${
            r.gap === null ? 100 : Math.max(2, (r.gap / worst) * 100)}%"></span></td>
          <td class="l">${r.gap === null ? "not holdable"
            : r.captain ? "XI, captain" : r.starting ? "in the XI" : "on the bench"}</td>
          <td class="l">${r.replaces.map(name).join(", ") || "—"}</td></tr>`;
      }).join("")
    + `</tbody></table></div>`;
}

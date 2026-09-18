/* The Chips tab's own chrome (both pitches, the merged compare chart, the
 * strategy folds) and the head-to-head drawer. The actual solving happens in
 * transfer-view.mjs; this module calls into it and draws what comes back.
 * See REFACTOR.md for the split this belongs to. */
"use strict";
import { $, S, fmt, noDecay, css, el, showTip, hideTip } from "/assets/state.mjs";
import { saveSquad } from "/assets/state.mjs";
import { squadLayout, pitchHTML, wireShirts, shirtUrl } from "/assets/pitch.mjs";
import { chipSlots, pairMoves } from "/assets/chips.mjs";
import {
  BENCH_KEYS, planXI, benchSlots, captaincy, squadCost, squadSellValue, saveDraftAs, loadDraft, renderAll,
} from "/assets/squad-view.mjs";
import {
  planTransfersAndChips, planOwnedSquad, transferInputKey, ownedInputKey,
  renderChipTableHTML, solveWeekIdeal, runWeekNearMisses, weekIdealNote,
} from "/assets/transfer-view.mjs";
import { nearMissTableHTML } from "/assets/analysis-view.mjs";
import { openEditor } from "/assets/explain-view.mjs";

/* ---------------------------------------------------- Your squad vs Optimal
   One chart for both plans, rather than one each: what a reader opens this tab
   to compare is the two totals side by side, and two separate charts made that
   a subtraction the eye had to do across a scroll. Keyed by gameweek rather
   than array index, so the two plans can be overlaid even if one was solved
   under a since-changed horizon and no longer shares the other's week count. */
function compareChartSeries() {
  const series = [];
  if (S.ownedPlan.state === "ready" && S.ownedPlan.result) {
    series.push({ name: "Your squad", color: slotColorRef(0), plan: S.ownedPlan.result });
  }
  if (S.transferPlan.state === "ready" && S.transferPlan.result) {
    series.push({ name: "Optimal", color: slotColorRef(1), plan: S.transferPlan.result });
  }
  const gws = [...new Set(series.flatMap((s) => s.plan.weeks.map((w) => w.gw)))].sort((a, b) => a - b);
  for (const s of series) {
    const byGw = new Map(s.plan.weeks.map((w) => [w.gw, w]));
    s.data = gws.map((gw) => byGw.get(gw)?.xiPoints ?? null);
    s.chipAt = (gw) => s.plan.chipByGw.get(gw) || null;
  }
  return { series, gws };
}
import { slotColor as slotColorRef } from "/assets/squad-view.mjs";

/** What each plan is worth over the window, and where it spends a chip -- the
 *  one sentence a reader wants under a chart with two lines on it. */
function compareChartNoteHTML(series) {
  if (!series.length) {
    return `<div class="sub" style="margin-top:8px">Solve "Your squad" or "Optimal" below to see
      their weekly points here.</div>`;
  }
  const totals = series.map((s) => s.data.reduce((a, v) => a + (v || 0), 0));
  const totalBits = series.map((s, i) => `<b>${s.name}</b> ${fmt(totals[i], 1)}`).join(" · ");
  const chipBits = series.map((s) => {
    const weeks = [...s.plan.chipByGw.entries()];
    return weeks.length
      ? `${s.name} plays ${weeks.map(([gw, c]) => `${s.plan.chipLabels[c]} in GW${gw}`).join(", ")}`
      : `${s.name} plays no chip`;
  }).join("; ");
  return `<div class="sub" style="margin-top:8px">Undecayed totals over the window: ${totalBits}.
    ${chipBits}. Each plan is ranked by the solver on its own discounted objective, which also
    credits a chip held back at its reserve price and reserves against future transfers -- so this
    raw total is not what either was actually maximising, and is not itself proof one plan beats the
    other. Click a gameweek to put that week on the pitch${series.length > 1 ? "es" : ""} below.</div>`;
}

export function renderComparePlanChart() {
  const host = $("#cmpPlanChart");
  if (!host) return;
  host.innerHTML = "";
  const { series, gws } = compareChartSeries();

  $("#cmpPlanLegend").innerHTML = series.length
    ? series.map((s) => `<span class="key"><i style="background:${s.color}"></i>${s.name}</span>`).join("")
    : "";
  $("#cmpPlanNote").innerHTML = compareChartNoteHTML(series);

  if (!series.length || !gws.length) {
    host.innerHTML = `<div class="empty-note">Solve "Your squad" or "Optimal" below to see their
      weekly points here.</div>`;
    $("#cmpPlanChartTable").innerHTML = "";
    return;
  }

  const W = Math.max(300, host.clientWidth || 460), H = 250;
  const M = { t: 22, r: 58, b: 30, l: 40 };
  const all = series.flatMap((s) => s.data.filter((v) => v != null));
  const lo = Math.min(...all) * 0.94, hi = Math.max(...all) * 1.04;
  const x = (i) => M.l + (i * (W - M.l - M.r)) / Math.max(1, gws.length - 1);
  const y = (v) => H - M.b - ((v - lo) / (hi - lo || 1)) * (H - M.t - M.b);

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" }, host);
  el("title", {}, svg).textContent =
    "Projected points for each gameweek, your squad's plan against the from-scratch optimum";

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

  const last = gws.length - 1;
  const placed = [];
  for (const s of series) {
    // Broken at any gap -- a gameweek one plan has no answer for should read as
    // a gap in its line, not a lie drawn straight through it.
    let d = "";
    s.data.forEach((v, i) => { if (v != null) d += (d ? "L" : "M") + x(i) + "," + y(v) + " "; });
    el("path", { d: d.trim(), fill: "none", stroke: s.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    let li = last;
    while (li >= 0 && s.data[li] == null) li--;
    if (li < 0) continue;
    el("circle", { cx: x(li), cy: y(s.data[li]), r: 4.5, fill: s.color,
      stroke: css("--surface-1"), "stroke-width": 2 }, svg);
    let ly = y(s.data[li]) + 4;
    while (placed.some((p) => Math.abs(p - ly) < 12)) ly += 12;
    placed.push(ly);
    const lab = el("text", { x: x(li) + 9, y: ly, fill: css("--text-secondary"),
      "font-size": 11.5, style: "font-variant-numeric:tabular-nums" }, svg);
    lab.textContent = s.data[li].toFixed(1);
  }

  const cross = el("line", { y1: M.t, y2: H - M.b, stroke: css("--axis"), "stroke-width": 1, opacity: 0 }, svg);
  const half = (W - M.l - M.r) / Math.max(1, gws.length - 1) / 2;
  gws.forEach((gw, i) => {
    const band = el("rect", { x: x(i) - half, y: M.t, width: half * 2, height: H - M.t - M.b,
      fill: "transparent", style: "cursor:crosshair" }, svg);
    band.addEventListener("mousemove", (e) => {
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 1);
      showTip(e, `<b>GW${gw}</b><br>` + series.map((s) => {
        const chip = s.chipAt(gw);
        const v = s.data[i];
        return `<span class="key"><i style="background:${s.color};width:9px;height:9px;border-radius:50%"></i>${s.name}${
          chip ? ` (${s.plan.chipLabels[chip]})` : ""} <span class="r">${v == null ? "—" : v.toFixed(1)}</span></span>`;
      }).join("<br>"));
    });
    band.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
    // Puts that gameweek on whichever pitch(es) below actually have it.
    band.addEventListener("click", () => {
      const ownIdx = S.ownedPlan.result?.weeks.findIndex((w) => w.gw === gw);
      if (ownIdx != null && ownIdx !== -1) S.ownedPlanWeek = ownIdx;
      const optIdx = S.transferPlan.result?.weeks.findIndex((w) => w.gw === gw);
      const hadOpt = optIdx != null && optIdx !== -1;
      if (hadOpt) S.planWeek = optIdx;
      renderChips();
      if (hadOpt) solveWeekIdeal(optIdx);
    });
  });

  const paired = series.length > 1;
  $("#cmpPlanChartTable").innerHTML = `<table><thead><tr><th>GW</th>${
      series.map((s) => `<th>${s.name}</th>`).join("")}${paired ? "<th>Difference</th>" : ""}</tr></thead>
    <tbody>${gws.map((gw, i) => `<tr><td>GW${gw}</td>
      ${series.map((s) => `<td>${s.data[i] == null ? "—" : fmt(s.data[i], 1)}</td>`).join("")}
      ${paired ? `<td>${series[0].data[i] == null || series[1].data[i] == null ? "—"
        : (series[0].data[i] - series[1].data[i] >= 0 ? "+" : "−")
          + fmt(Math.abs(series[0].data[i] - series[1].data[i]), 1)}</td>` : ""}
    </tr>`).join("")}</tbody></table>`;
}

/** The card that holds the merged chart -- static except for its toggle, so it
 *  is written once here rather than duplicated at both call sites in
 *  renderChips(). */
function renderCompareChartHTML() {
  const open = S.chipFolds.chart;
  return `
  <details class="chipfold" style="max-width:640px;margin-top:16px"${open ? " open" : ""}>
    <summary data-fold="chart">Projected points by gameweek
      <span class="scope">— "Your squad" against "Optimal"</span></summary>
    <div class="foldbody">
      <div class="cardhead">
        <div class="legend" id="cmpPlanLegend"></div>
        <button class="toggle" data-view="plan" data-prefix="cmp">${S.views.plan === "table" ? "Chart" : "Table"}</button>
      </div>
      <div class="chartbox${S.views.plan === "table" ? " hidden" : ""}" id="cmpPlanChart"></div>
      <div class="${S.views.plan === "table" ? "" : "hidden"}" id="cmpPlanChartTable"></div>
      <div id="cmpPlanNote"></div>
    </div>
  </details>`;
}

/** The whole plan, one gameweek at a time, with the pitch for whichever week
 *  is selected.
 *
 *  This used to render `weeks[0]` and nothing else, which is why a forced
 *  bench boost looked like it had changed nothing: the chip usually lands in
 *  GW2 or later, and the squad built for it — along with the transfers that
 *  assemble it and the free transfer banked to pay for them — was solved,
 *  returned, and then never drawn. The selector defaults to the first week
 *  with a chip in it, because that is the week you pressed the button to see;
 *  with no chip it stays on the first, which is still "what do I do now". */
/** `owned` plans (anchored to a fifteen you already hold) and from-scratch
 *  plans differ in what week 0 means: a from-scratch plan buys its opening
 *  fifteen outright, so there is nothing to call a transfer there; an owned
 *  plan's week 0 is exactly like every other week, just possibly a hold.
 *  `prefix` namespaces every element id, so the from-scratch plan and the
 *  owned plan can share the page; `weekIdx` overrides the module-level
 *  S.planWeek for whichever plan is not the live one, and `live` gates the
 *  near-miss / ideal-squad boxes, which only exist for the plan actually
 *  being solved on this worker cycle (see weekIdealHTML's own note). */
function renderPlanHTML(plan, { prefix = "", weekIdx = null, live = true, showPitch = true } = {}) {
  const owned = (plan.opt.squad || []).length === 15;
  const weeks = plan.weeks;
  const idx = Math.min(Math.max(weekIdx ?? S.planWeek, 0), weeks.length - 1);
  const week = weeks[idx];
  const name = (id) => S.byId.get(id)?.name ?? "—";
  const pairs = pairMoves(week.soldIds, week.boughtIds, plan.positions);
  const gainOf = (id) => {
    if (id == null) return 0;
    const pts = plan.pointsByPlayer.get(id);
    const from = plan.gameweeks.indexOf(week.gw);
    return pts ? pts.slice(from).reduce((a, b) => a + b, 0) : 0;
  };
  const movesHTML = pairs.length
    ? `<table class="movetable"><tbody>${pairs.map(([out, inn]) => {
        const gain = gainOf(inn) - gainOf(out);
        return `<tr>
          <td>${out != null ? name(out) : "—"}</td><td>→</td><td>${inn != null ? name(inn) : "—"}</td>
          <td class="${gain >= 0 ? "gain" : "loss"}">${gain >= 0 ? "+" : ""}${fmt(gain, 1)}</td>
        </tr>`;
      }).join("")}</tbody></table>`
    : `<div class="sub">${owned ? "No transfers this gameweek — hold."
        : "No changes this gameweek — the same fifteen."}</div>`;

  // The first gameweek of a from-scratch plan has no transfers by construction:
  // the opening fifteen is bought, not transferred into. An owned plan's week 0
  // is not special that way -- it is just the fifteen you hold, maybe moved.
  const opening = !owned && idx === 0;
  const mine = new Set(S.squad);
  const shared = week.squad.filter((id) => mine.has(id)).length;
  const squadDiffers = owned && week.squad.slice().sort().join() !== S.squad.slice().sort().join();
  const chipTag = (w) => (w.chip ? CHIP_SHORT[w.chip] || "chip" : "");

  return `
  <div class="card" style="margin-top:14px">
    <h4 style="margin:0 0 6px">Week by week</h4>
    <div class="weekpick">${weeks.map((w, i) => `
      <button class="mini ${i === idx ? "on" : ""} ${w.chip ? "haschip" : ""}" data-planweek="${i}" data-prefix="${prefix}">
        GW${w.gw}${w.chip ? `<span class="wtag">${chipTag(w)}</span>` : ""}
      </button>`).join("")}
    </div>
    <div class="cardhead" style="margin-top:12px">
      <div>
        <h3 style="margin:0">GW${week.gw}${week.chip ? " — " + plan.chipLabels[week.chip] : ""}</h3>
        <div class="sub">${week.hits ? `${week.hits} hit${week.hits > 1 ? "s" : ""} (−${week.hits * 4}) · ` : ""}${week.freeTransfers} FT banked · £${fmt(week.bank, 1)}m in the bank · ${fmt(week.xiPoints, 1)} xI points${week.chip === "bboost" ? " · all 15 score" : ""}</div>
      </div>
    </div>
    ${showPitch ? `<div id="${prefix}thisWeekPitch"></div>` : ""}
    <h4 style="margin:14px 0 6px">${opening ? `The opening fifteen`
      : `Transfers in GW${week.gw}`}</h4>
    ${opening
      ? `<div class="sub">Bought outright on £${fmt(plan.opt.budget, 1)}m — this plan starts
         from nothing, so there are no transfers to make here.${S.squad.length === 15
           ? ` You already own <b>${shared}</b> of these fifteen.` : ""}</div>`
      : movesHTML}
    ${squadDiffers ? `<div class="sub" style="margin-top:8px">This differs from the squad on
      screen${idx ? ` — it is where the moves from GW${weeks[0].gw} to GW${week.gw} leave you`
        : " — the moves above are what would get you there"}.</div>` : ""}
    ${renderPlanPathHTML(plan, idx, { prefix })}
    ${live ? `<div id="weekIdealBox">${weekIdealHTML(plan, idx)}</div>` : ""}
    ${live ? `<div id="weekNearBox">${weekNearMissHTML(plan, idx)}</div>` : ""}
  </div>`;
}

/** All six weeks at a glance, under the pitch. The selector above shows one
 *  week properly; this shows the shape of the whole thing, which is what says
 *  whether a chip week was paid for by banking a transfer three weeks earlier
 *  or by taking a hit on the day. */
function renderPlanPathHTML(plan, idx, { prefix = "" } = {}) {
  const name = (id) => S.byId.get(id)?.name ?? "—";
  const total = plan.weeks.reduce((a, w) => a + w.xiPoints, 0);
  const hits = plan.weeks.reduce((a, w) => a + w.hits, 0);
  return `
  <h4 style="margin:18px 0 6px">The whole plan</h4>
  <table class="chiptable path"><thead><tr>
      <th>GW</th><th>Chip</th><th>In</th><th>Out</th><th>FT</th><th>Hits</th><th>Bank</th><th>xI pts</th>
    </tr></thead><tbody>${plan.weeks.map((w, i) => `
      <tr class="${i === idx ? "picked" : ""} ${w.chip ? "haschip" : ""}" data-planweek="${i}" data-prefix="${prefix}">
        <td>GW${w.gw}</td>
        <td>${w.chip ? plan.chipLabels[w.chip] : "—"}</td>
        <td>${w.boughtIds.map(name).join(", ") || "—"}</td>
        <td>${w.soldIds.map(name).join(", ") || "—"}</td>
        <td>${w.freeTransfers}</td>
        <td>${w.hits || "—"}</td>
        <td>£${fmt(w.bank, 1)}m</td>
        <td>${fmt(w.xiPoints, 1)}</td>
      </tr>`).join("")}</tbody>
    <tfoot><tr><td colspan="5">Over the window</td><td>${hits || "—"}</td><td></td>
      <td>${fmt(total, 1)}</td></tr></tfoot></table>
  <div class="sub" style="margin-top:6px">Click a row or a gameweek above to put that week's
    eleven on the pitch. xI points are undecayed and do not subtract hits; the solver ranks
    plans on a discounted objective that does.</div>`;
}

/** Short badges for the week selector — the full labels are too wide to sit
 *  inside a button that also has to say which gameweek it is. */
const CHIP_SHORT = { bboost: "BB", "3xc": "TC", freehit: "FH", wildcard: "WC" };

/** How much rope the plan gets on transfers. All three run through knobs the
 *  LP already had (`noTransferGws`, `hitLimit`) and nothing here was reachable
 *  from the UI before. "None" is the one worth explaining: it answers "what are
 *  my chips worth to the squad I already own?", which is the honest question
 *  when you have no intention of churning the side to chase a chip week. */
/** How much the plan may change the fifteen it opened with. The opening squad
 *  itself is always free -- it is bought from scratch -- so these govern only
 *  what happens from the second gameweek on. "None" is the useful one for
 *  reading a chip: it asks "what is one fifteen, never touched, worth with this
 *  chip in it?", which is the cleanest possible view of the chip's own value. */
const TRANSFER_MODES = [
  ["plan", "as the plan likes", "Transfers and hits from GW2 on are the solver's to spend."],
  ["free", "free transfers only", "No points hits — the plan may only use transfers it has earned."],
  ["none", "never change the side", "One fifteen for the whole window. The cleanest read on a chip, since nothing but the chip can move. A free hit still fields its own side, because that chip does not spend transfers."],
];

/** The chips a solve could be made to play: legal in this window and not
 *  already used. The free hit is offered even on a flat calendar — a solve
 *  drops it there on its own, and forcing it is precisely the question "what
 *  would it buy me anyway?". */
function forcibleChipSlots() {
  if (!S.meta || !S.snapshot) return {};
  const gameweeks = S.gameweeks.slice(0, transferGwCountRef());
  return chipSlots(S.meta.chip_windows || {}, gameweeks, S.chipsUsed, S.snapshot.rules.CHIPS);
}
import { transferGwCount as transferGwCountRef } from "/assets/transfer-view.mjs";

/** The Settings controls the plan honours, spelled out on the tab that obeys
 *  them. Each of these was a hardcoded constant in buildTransferPayload until
 *  the plan was wired to the same knobs the pitch uses, so saying which ones
 *  apply is the difference between a shared setting and a coincidence.
 *
 *  Template tilt is deliberately absent and named as absent: it is an
 *  ownership term in the squad optimiser's objective, and this LP has no
 *  ownership term to tilt. Silently ignoring it is what the rest of this list
 *  exists to stop. `owned` is true for the plan anchored to your squad, which
 *  reads your banked free transfers where the from-scratch one cannot. */
function chipScopeText(owned) {
  const bits = [
    `budget £${(+$("#budget").value).toFixed(1)}m`,
    `max ${$("#maxclub").value}/club`,
    `min start ${(+$("#minstart").value).toFixed(2)}`,
    `bench ${BENCH_KEYS.map((k) => (+$(`#bw_${k}`).value).toFixed(2)).join("/")}`,
  ];
  if ($("#formation").value) bits.push(`formation ${$("#formation").value}`);
  if (S.chipsUsed.length) bits.push(`${S.chipsUsed.length} chip${S.chipsUsed.length > 1 ? "s" : ""} already used`);
  if (owned) bits.push(`${$("#freetransfers").value} FT banked`);
  bits.push(noDecay() ? "no decay, no dropout risk" : "decay and dropout risk on");
  // Two settings are named as *not* applying, because both look like they should
  // and both used to. Banked free transfers cannot apply to a squad bought from
  // scratch -- there is nothing to transfer from. Template tilt is an ownership
  // term in the squad optimiser's objective and this LP has none.
  return `${bits.join(" · ")}. Player edits apply too.${owned ? "" : ` Your squad and your banked
    free transfers do not — this plan buys its own fifteen.`} Template tilt does not: it is an
    ownership term the squad optimiser has and this model does not.`;
}

/** Required and barred players, on the Chips tab rather than only in Settings.
 *  They already fed the solve; what was missing was any way to see from here
 *  that a plan had been told to avoid somebody, or to take it back. */
export function renderChipConstraints() {
  const box = $("#chipConstraints");
  if (!box) return;
  const chip = (id, kind) => {
    const p = S.byId.get(id);
    return `<button class="chip" data-drop="${id}" title="Remove this constraint"
      style="border-color:${kind === "include" ? "var(--good)" : "var(--critical)"}">
      ${kind === "include" ? "⊕" : "⊘"} ${p ? p.name : id} ×</button>`;
  };
  const parts = [...S.include.map((i) => chip(i, "include")),
                 ...S.exclude.map((i) => chip(i, "exclude"))];
  box.innerHTML = parts.length
    ? `<div class="cchips">${parts.join("")}</div>`
    : `<span class="sub">No required or barred players — set them with ⊕ / ⊘ in the pool.</span>`;
  box.querySelectorAll("[data-drop]").forEach((b) => b.addEventListener("click", () => {
    dropConstraintRef(+b.dataset.drop);
    replanReadySides();
  }));
}
import { dropConstraint as dropConstraintRef } from "/assets/squad-view.mjs";

/** Constraints (include/exclude) are the one thing both sides still share, so
 *  dropping one can leave either plan's answer stale. Re-solves whichever
 *  side already has an answer to update, and only falls back to a plain
 *  render when neither does. */
function replanReadySides() {
  let did = false;
  if (S.transferPlan.state === "ready") { planTransfersAndChips(); did = true; }
  if (S.ownedPlan.state === "ready") { planOwnedSquad(); did = true; }
  if (!did) renderChips();
}

/** One chip's setting, as a single value the select round-trips.
 *
 *  Was a checkbox plus a select, which could express states the model has no
 *  meaning for ("play it, but in no particular week" read the same as "consider
 *  it") and had no way at all to say "do not spend a solve on this". One control
 *  per chip, three states, and the solve cost of each is on the label. Which
 *  weeks are even in play is a separate question now (see toggleChipWeek) --
 *  narrowing to one week is what a "pin" means, independent of skip/force.
 *
 *  `side` is "opt" (the from-scratch plan) or "own" (anchored to your squad)
 *  -- each keeps its own strategy in S.chipPlan, so forcing a bench boost for
 *  one says nothing about the other. */
const chipModeOf = (chip, side) =>
  S.chipPlan[side].chipSkip.includes(chip) ? "skip"
  : S.chipPlan[side].forceChips.includes(chip) ? "force" : "consider";

/** The gameweeks a chip is even allowed to land in, honouring whatever subset
 *  you have narrowed it to below -- the full legal window when you have not. */
function chipCandidateWeeks(chip, legal, side) {
  const chosen = S.chipPlan[side].chipWeek[chip];
  return Array.isArray(chosen) && chosen.length ? legal.filter((gw) => chosen.includes(gw)) : legal;
}

/** Add or drop one gameweek from a chip's candidate set. Narrowing to exactly
 *  one week is what "pin" means to buildTransferPayload; narrowing to any
 *  other non-empty subset still restricts the solve without forcing the chip
 *  to be played at all. Never allowed down to zero -- that is "leave it out",
 *  which already has its own control. */
function toggleChipWeek(chip, gw, side) {
  const legal = forcibleChipSlots()[chip] || [];
  const current = chipCandidateWeeks(chip, legal, side);
  const next = current.includes(gw) ? current.filter((g) => g !== gw) : [...current, gw];
  if (!next.length) return;
  const plan = S.chipPlan[side];
  if (next.length === legal.length) {
    const { [chip]: _dropped, ...rest } = plan.chipWeek;
    plan.chipWeek = rest;
  } else {
    plan.chipWeek = { ...plan.chipWeek, [chip]: next };
  }
  saveSettingsRef();
}
import { saveSettings as saveSettingsRef } from "/assets/squad-view.mjs";

function resetChipWeeks(chip, side) {
  const plan = S.chipPlan[side];
  const { [chip]: _dropped, ...rest } = plan.chipWeek;
  plan.chipWeek = rest;
  saveSettingsRef();
}

/** Apply a chip's select back onto the two pieces of state it spans. Written
 *  as a full reset of both rather than as a patch, because the states are
 *  mutually exclusive and a patch is how "skip" used to leave a chip forced
 *  behind it. Leaves chipWeek alone -- which weeks are in play does not
 *  reset just because you changed your mind about forcing it. */
function setChipMode(chip, mode, side) {
  const plan = S.chipPlan[side];
  plan.chipSkip = plan.chipSkip.filter((c) => c !== chip);
  plan.forceChips = plan.forceChips.filter((c) => c !== chip);
  if (mode === "skip") plan.chipSkip = [...plan.chipSkip, chip];
  else if (mode === "force") plan.forceChips = [...plan.forceChips, chip];
  saveSettingsRef();
}

/** Whether a chip's sweep (the per-week re-solve) will actually run, matching
 *  buildTransferPayload's own forceChips derivation exactly: explicitly forced,
 *  or narrowed down to the one week that counts as a pin. */
const chipWillSweep = (chip, weeks, side) => S.chipPlan[side].forceChips.includes(chip) || weeks.length === 1;

/** What the next press will cost, counted the way buildTransferJobs builds it:
 *  the plan, the chip-free baseline, one per chip still in, and a gameweek's
 *  worth for each chip you have committed to (or narrowed to a single week).
 *  Shown before the press, because a five-minute wait is worth being able to
 *  see coming -- and because the only way to shorten it is a control sitting
 *  right beside the number. `owned` is the leaner owned-plan job count (see
 *  buildPlanOnlyJobs): no baseline, no per-chip worth solve. */
function chipSolveCount(slots, side, owned = false) {
  const live = Object.keys(slots).filter((c) => !S.chipPlan[side].chipSkip.includes(c));
  const sweeps = live.reduce((a, c) => {
    const weeks = chipCandidateWeeks(c, slots[c] || [], side);
    return a + (chipWillSweep(c, weeks, side) ? weeks.length : 0);
  }, 0);
  return (owned ? 1 : 2 + live.length) + sweeps;
}

/** The pitch for one week of a solved plan -- starters, bench in solver order,
 *  captain and vice -- shared by the from-scratch plan and the owned-squad
 *  plan, which each keep their own copy at `#${prefix}thisWeekPitch`. */
function renderWeekPitchInto(prefix, plan, weekIdx) {
  const box = $(`#${prefix}thisWeekPitch`);
  if (!box) return;
  const weeks = plan.weeks;
  const week = weeks[Math.min(Math.max(weekIdx, 0), weeks.length - 1)];
  const fielded = [...week.starters, ...week.bench.map((b) => b.id)];
  const benchOrder = Object.fromEntries(week.bench.map((b) => [b.id, b.slot]));
  const layout = squadLayout({ ids: fielded, lookup: S.byId, need: S.meta.squad_by_pos,
                               xiIds: week.starters, benchOrder });
  box.innerHTML = pitchHTML({ layout, teams: S.snapshot?.teams, metrics: S.metrics,
                             captain: week.captain, vice: week.vice, versus: true });
  wireShirts(box);
  // Rebuilt inside host.innerHTML on every render, so a fresh listener each
  // time is correct rather than a leak.
  box.addEventListener("click", (e) => {
    const vs = e.target.closest("[data-vs]");
    if (vs) return toggleVersus(+vs.dataset.vs);
    const edit = e.target.closest("[data-edit]");
    if (edit) openEditor(+edit.dataset.edit);
  });
}

/** The editable pitch for the fifteen you actually own -- the anchor the
 *  owned-squad plan solves against. Add/remove go through the same
 *  data-add-pos / data-rm contract as the Squad tab's own pitch, because they
 *  are the same S.squad. */
function ownedPitchHTML() {
  const layout = squadLayout({
    ids: S.squad, lookup: S.byId, need: S.meta.squad_by_pos,
    xiIds: planXI(S.squad).ids, benchOrder: benchSlots(S.squad),
  });
  return { html: pitchHTML({ layout, teams: S.snapshot?.teams, metrics: S.metrics,
                             ...captaincy(S.squad), remove: true }), layout };
}

/** Pick one of your saved drafts into S.squad, or bank the current one under a
 *  name -- both without leaving the Chips tab for the Drafts one, since the
 *  point of editing here is to see the chip/transfer read on it immediately. */
function ownedDraftControlsHTML() {
  const options = S.drafts.map((d) =>
    `<option value="${encodeURIComponent(d.name)}">${d.name}</option>`).join("");
  return `
  <div class="chipbar" style="margin:10px 0 4px">
    <select id="ownedDraftPick" ${S.drafts.length ? "" : "disabled"}>
      <option value="">${S.drafts.length ? "Load a saved draft…" : "No saved drafts yet"}</option>
      ${options}
    </select>
    <input type="text" id="ownedDraftName" placeholder="Name this squad…" maxlength="40">
    <button class="btn ghost" id="ownedDraftSave">Save as draft</button>
  </div>`;
}

/** The owned-squad plan's own controls and, once solved, its full week-by-week
 *  panel -- chart, week picker, pitch, moves, the lot, same as the from-scratch
 *  plan's, generalized through renderPlanHTML's prefix/live options. */
/** One chip's row inside a strategy fold -- mode select plus the per-week
 *  candidate toggles. `side` is "opt" or "own"; each reads and writes its own
 *  bucket in S.chipPlan, and the element ids carry the side so the two folds
 *  can sit on the page at once without colliding. */
function chipRowHTML(chip, side, slots, busy) {
  const mode = chipModeOf(chip, side);
  const legal = slots[chip];
  const chosen = chipCandidateWeeks(chip, legal, side);
  const narrowed = chosen.length !== legal.length;
  const weeks = chipWillSweep(chip, chosen, side) ? chosen.length : 0;
  const labels = S.snapshot.rules.CHIP_LABELS;
  return `<div class="chiprow">
    <label class="chipname" for="chipmode_${side}_${chip}">${labels[chip]}</label>
    <select id="chipmode_${side}_${chip}" data-chipmode="${chip}" data-side="${side}" ${busy ? "disabled" : ""}>
      <option value="consider" ${mode === "consider" ? "selected" : ""}>consider it (1 solve)</option>
      <option value="skip" ${mode === "skip" ? "selected" : ""}>leave it out (0 solves)</option>
      <option value="force" ${mode === "force" ? "selected" : ""}>must play, best of these weeks${weeks ? ` (+${weeks})` : ""}</option>
    </select>
    <div class="weekpick chipgws">${legal.map((gw) => `
      <button type="button" class="mini ${chosen.includes(gw) ? "on" : ""}" data-chipgw="${chip}" data-side="${side}"
              data-gw="${gw}" ${busy ? "disabled" : ""}
              title="${chosen.includes(gw) ? "In the candidate set — click to drop it"
                : "Not being considered — click to add it back"}">GW${gw}</button>`).join("")}
      ${narrowed ? `<button type="button" class="mini ghost" data-chipgwall="${chip}" data-side="${side}"
        ${busy ? "disabled" : ""}>All weeks</button>` : ""}
    </div>
  </div>`;
}

/** Which chips this side's next solve will play or price, and how much rope
 *  it has to change the squad along the way -- folded away by default, since
 *  it is a control panel a reader opens on purpose rather than something to
 *  scroll past to reach the team underneath it. */
function renderChipStrategyFoldHTML(side, slots, forcible, busy) {
  const plan = S.chipPlan[side];
  const cost = chipSolveCount(slots, side, side === "own");
  const skipped = forcible.filter((c) => plan.chipSkip.includes(c)).length;
  const narrowed = forcible.filter((c) => chipCandidateWeeks(c, slots[c], side).length !== slots[c].length).length;
  const flags = [skipped ? `${skipped} left out` : "", narrowed ? `${narrowed} narrowed` : ""].filter(Boolean);
  const open = S.chipFolds[`strategy:${side}`];
  return `
  <details class="chipfold"${open ? " open" : ""}>
    <summary data-fold="strategy:${side}">Chip &amp; transfer strategy
      <span class="scope">${flags.length ? "— " + flags.join(", ") : "— every legal chip, as it likes"}</span></summary>
    <div class="foldbody">
      ${forcible.length ? `
        <div class="chiprows">${forcible.map((c) => chipRowHTML(c, side, slots, busy)).join("")}</div>
        <div class="solvecost ${busy ? "busy" : ""}">Next press: <b>${cost} solve${cost === 1 ? "" : "s"}</b>${
          skipped ? ` · ${skipped} left out` : ""}</div>
        <span class="sub"><b>Consider</b> prices the chip: one solve that builds a fifteen around it.
          <b>Leave it out</b> drops it entirely. <b>Must play</b> also re-solves every gameweek left in
          its candidate set. Click a <b>GW</b> button to drop it from consideration — down to one week
          and that becomes a pin, forcing the chip there.</span>` : `
        <div class="sub">No chips are legal in this window.</div>`}
      <div class="ctl" style="margin-top:10px">
        <label for="transferMode_${side}">Transfers after GW${S.gameweeks[0] ?? ""}</label>
        <select id="transferMode_${side}" data-transfermode="${side}" ${busy ? "disabled" : ""}>
          ${TRANSFER_MODES.map(([v, label]) => `<option value="${v}"
            ${plan.transferMode === v ? "selected" : ""}>${label}</option>`).join("")}
        </select>
      </div>
      <div class="sub" style="margin-top:6px">${TRANSFER_MODES.find(([v]) => v === plan.transferMode)?.[2] || ""}</div>
      <div class="sub" style="margin-top:10px">${chipScopeText(side === "own")}</div>
    </div>
  </details>`;
}

function renderOwnedPanelHTML(slots, forcible) {
  const op = S.ownedPlan;
  const missing = 15 - S.squad.length;
  const busy = op.state === "solving";
  const strategy = renderChipStrategyFoldHTML("own", slots, forcible, busy);
  if (missing > 0) {
    return `${strategy}<div class="sub" style="margin-top:10px">Add ${missing} more player${missing === 1 ? "" : "s"}
      to plan a chip and transfer strategy anchored to this squad.</div>`;
  }
  const stale = op.state === "ready" && op.key !== ownedInputKey();
  const progress = op.progress;
  const statusText = busy
    ? (progress && progress.total > 1 ? `solving — ${progress.done} of ${progress.total} done…`
        : "solving — this builds the whole horizon around your squad, it can take a while…")
    : op.state === "error" ? op.error : "";
  return `
  ${strategy}
  <div class="chipbar" style="margin-top:10px">
    <button class="btn" id="planOwnedBtn" ${busy ? "disabled" : ""}>
      ${op.state === "ready" ? "Re-solve" : "Solve my squad"}
    </button>
    <span class="optstatus ${op.state === "error" ? "err" : ""}">${statusText}</span>
  </div>
  ${stale ? `<div class="planstale">Your squad or the settings above have changed since this
    was solved — it answers the old ones.
    <button class="mini" id="ownedReplanBtn">Re-solve</button></div>` : ""}
  ${op.state === "ready" ? renderPlanHTML(op.result,
      { prefix: "own", weekIdx: S.ownedPlanWeek, live: false, showPitch: false }) : ""}`;
}

export function renderChips() {
  const host = $("#chipsBody");
  if (!host) return;
  if (!S.meta || !S.snapshot) {
    host.innerHTML = `<div class="empty-note">Load or sync a snapshot to plan chips.</div>`;
    return;
  }
  const tp = S.transferPlan;
  const ready = tp.state === "ready" && tp.result;
  // A plan is an answer to the settings it was solved under. Nothing pushes an
  // invalidation at this tab, so it checks: the plan stays on screen (it is
  // still the best answer available) but says out loud that it is answering an
  // older question.
  const stale = ready && tp.key !== transferInputKey();

  const progress = tp.progress;
  const statusText = tp.state === "solving"
    ? (progress && progress.total > 1
        ? `building a squad for each chip — ${progress.done} of ${progress.total} solves done…`
        : "solving — this builds a whole fifteen over the horizon, it can take a while…")
    : tp.state === "error" ? tp.error : "";
  const slots = forcibleChipSlots();
  const forcible = Object.keys(slots);
  const busy = tp.state === "solving";

  // The teams come first: everything above them used to be a page of controls
  // and explanation a reader had to clear before reaching the thing the tab is
  // named for. What is left up here is one shared line -- constraints apply to
  // both sides the same way -- and it is one line, not a card.
  host.innerHTML = `
    <div class="sub" id="chipConstraints" style="margin-bottom:10px"></div>

    <div class="pitchpair">
      <div class="pitchside">
        <div class="sidehead">
          <h2>Your squad</h2>
          <span class="shapetag" id="ownedShapeTag"></span>
          <span class="grow"></span>
          <button class="btn ghost" id="addPlayerOwned">Add players</button>
        </div>
        <div class="sub">Anchored to the fifteen you own -- pays a transfer and a hit for every
          move away from it, same as FPL charges you.</div>
        ${ownedDraftControlsHTML()}
        <div id="ownedPitch"></div>
        ${renderOwnedPanelHTML(slots, forcible)}
      </div>
      <div class="pitchside">
        <div class="sidehead">
          <h2>Optimal</h2>
          <span class="grow"></span>
          <button class="btn" id="planBtn" ${busy ? "disabled" : ""}>
            ${tp.state === "ready" ? "Re-plan" : "Plan chips"}
          </button>
        </div>
        <div class="sub">Built from scratch on the settings below, ignoring what you own.</div>
        <span class="optstatus ${tp.state === "error" ? "err" : ""}">${statusText}</span>
        ${stale ? `<div class="planstale">Settings have changed since this plan was solved — it
          answers the old ones. <button class="mini" id="replanBtn">Re-plan</button></div>` : ""}
        ${renderChipStrategyFoldHTML("opt", slots, forcible, busy)}
        ${ready ? renderPlanHTML(tp.result) : `<div class="sub" style="margin-top:10px">Press
          Plan chips to see the from-scratch build.</div>`}
        <details class="chipfold"${S.chipFolds.worth ? " open" : ""}>
          <summary data-fold="worth">What each chip is worth, built for it</summary>
          <div class="foldbody">${ready ? renderChipTableHTML(tp.result)
            : `<div class="sub">Solve to see this.</div>`}</div>
        </details>
      </div>
    </div>

    ${(() => {
      // "Optimal" is capped at Budget; your own squad is not -- it is whatever
      // it is worth. If the two disagree, Optimal is playing on less money and
      // a gap between the lines can be entirely that, not chips or transfers.
      const budget = +$("#budget").value;
      const ownedCost = S.squad.length ? squadSellValue(S.squad) : 0;
      return ownedCost > budget + 0.05 ? `<div class="planstale" style="margin-top:16px">Your squad
        costs £${fmt(ownedCost, 1)}m, more than the £${fmt(budget, 1)}m Budget below — "Optimal" is
        capped at that budget, so it is playing on less money than you actually have. Raise
        Budget to at least £${fmt(ownedCost, 1)}m for the two lines to mean the same thing.
        </div>` : "";
    })()}

    ${renderCompareChartHTML()}`;

  $("#planBtn").addEventListener("click", planTransfersAndChips);
  $("#replanBtn")?.addEventListener("click", planTransfersAndChips);
  renderChipConstraints();
  wireChipFolds("#chipsBody");

  // The chart's toggle and its fold both live inside #chipsBody, which this
  // function rewrites on every render, so neither can use a load-time
  // listener. Opening the fold has to redraw the chart itself: an SVG sized
  // while its <details> was closed sizes to zero and never corrects itself.
  host.querySelector('.toggle[data-view="plan"]')?.addEventListener("click", () => {
    setViewRef("plan", S.views.plan === "table" ? "chart" : "table");
    saveSettingsRef();
    renderChips();
  });
  const chartFold = $("#cmpPlanChart")?.closest("details");
  chartFold?.addEventListener("toggle", () => {
    if (!chartFold.open) return;
    // <details> applies its layout change asynchronously, so measuring
    // clientWidth in the same tick that opened it can still catch the closed
    // (zero) size and draw a chart that never corrects itself. Both a
    // double-rAF and a plain timeout, because which one actually lands after
    // layout has settled varies by how busy the main thread is; a second,
    // harmless redraw is cheaper than a chart that stays blank.
    requestAnimationFrame(() => requestAnimationFrame(renderComparePlanChart));
    setTimeout(renderComparePlanChart, 60);
  });

  // Every control here changes that side's answer only, so each re-solves
  // that side rather than leaving a plan on screen that no longer matches the
  // controls above it -- the other side's plan is untouched.
  const changed = (side) => {
    const plan = side === "own" ? S.ownedPlan : S.transferPlan;
    const solve = side === "own" ? planOwnedSquad : planTransfersAndChips;
    if (plan.state === "ready") solve(); else renderChips();
  };

  host.querySelectorAll("[data-transfermode]").forEach((sel) => sel.addEventListener("change", (e) => {
    S.chipPlan[sel.dataset.transfermode].transferMode = e.target.value;
    saveSettingsRef();
    changed(sel.dataset.transfermode);
  }));

  host.querySelectorAll("[data-chipmode]").forEach((sel) => sel.addEventListener("change", () => {
    setChipMode(sel.dataset.chipmode, sel.value, sel.dataset.side);
    changed(sel.dataset.side);
  }));

  host.querySelectorAll("[data-chipgw]").forEach((btn) => btn.addEventListener("click", () => {
    toggleChipWeek(btn.dataset.chipgw, +btn.dataset.gw, btn.dataset.side);
    changed(btn.dataset.side);
  }));
  host.querySelectorAll("[data-chipgwall]").forEach((btn) => btn.addEventListener("click", () => {
    resetChipWeeks(btn.dataset.chipgwall, btn.dataset.side);
    changed(btn.dataset.side);
  }));

  // The from-scratch plan's week buttons re-solve who nearly made that week's
  // squad, same as before; the owned plan has no such cache and just switches
  // the week -- a full re-render is cheap, nothing here re-solves.
  host.querySelectorAll("[data-planweek]").forEach((el) => el.addEventListener("click", () => {
    const i = +el.dataset.planweek;
    if (el.dataset.prefix === "own") { S.ownedPlanWeek = i; renderChips(); }
    else { S.planWeek = i; renderChips(); solveWeekIdeal(S.planWeek); }
  }));

  wireWeekNearMiss();
  wireWeekIdeal();

  // "Your squad" -- the editable pitch, the draft picker and the anchored
  // solve. All of it operates on S.squad, the same fifteen the Squad tab
  // edits, so adding a player here is exactly adding one there.
  $("#addPlayerOwned").addEventListener("click", () => openPoolRef());
  const ownedBox = $("#ownedPitch");
  if (ownedBox) {
    const { html, layout } = ownedPitchHTML();
    ownedBox.innerHTML = html;
    wireShirts(ownedBox);
    $("#ownedShapeTag").textContent = layout.complete
      ? `${layout.shape} · ${S.squad.length}/15` : `${S.squad.length}/15 picked`;
    ownedBox.addEventListener("click", (e) => {
      const add = e.target.closest("[data-add-pos]");
      if (add) return openPoolRef(add.dataset.addPos || null);
      const rm = e.target.closest("[data-rm]");
      if (rm) { S.squad = S.squad.filter((id) => id !== +rm.dataset.rm); saveSquad(); renderAll(); return; }
      const edit = e.target.closest("[data-edit]");
      if (edit) openEditor(+edit.dataset.edit);
    });
  }
  $("#ownedDraftPick")?.addEventListener("change", (e) => {
    const name = decodeURIComponent(e.target.value || "");
    if (name) loadDraft(name);
  });
  $("#ownedDraftSave")?.addEventListener("click", () => {
    saveDraftAs($("#ownedDraftName").value);
    $("#ownedDraftName").value = "";
  });
  $("#planOwnedBtn")?.addEventListener("click", planOwnedSquad);
  $("#ownedReplanBtn")?.addEventListener("click", planOwnedSquad);

  // After the markup, never inside it: an SVG sized against a container that
  // is not in the document has no width to size against. One chart for both
  // plans (see renderCompareChartHTML), drawn whenever either has an answer.
  renderComparePlanChart();
  if (ready) renderWeekPitchInto("", tp.result, S.planWeek);
}
import { setView as setViewRef, openPool as openPoolRef, poolOpen as poolOpenRef } from "/assets/squad-view.mjs";

/** Redraw only the rebuild block. Same reason renderWeekNearMisses exists: the
 *  week card holds a pitch whose shirts are images, and rebuilding the tab
 *  around a solve that takes a second would flash all of it. */
export function renderWeekIdeal() {
  const box = $("#weekIdealBox"), plan = S.transferPlan.result;
  if (!box || !plan) return;
  const idx = Math.min(Math.max(S.planWeek, 0), plan.weeks.length - 1);
  box.innerHTML = weekIdealHTML(plan, idx);
  wireWeekIdeal();
}

/** The state of the rebuild for the week on screen, or null when the plan it
 *  answered has been re-solved since. */
function weekIdealState(idx) {
  return S.weekIdeal.key === S.transferPlan.key ? (S.weekIdeal.byWeek[idx] || null) : null;
}

function wireWeekIdeal() {
  const plan = S.transferPlan.result;
  if (!plan) return;
  const idx = Math.min(Math.max(S.planWeek, 0), plan.weeks.length - 1);
  $("#weekIdealBtn")?.addEventListener("click", () => solveWeekIdeal(idx, { force: true }));
  wireChipFolds("#weekIdealBox");

  const pitch = $("#weekIdealPitch");
  const st = weekIdealState(idx);
  if (!pitch || st?.state !== "ready") return;

  // Marked against the fifteen the plan holds that week, not against the squad
  // you own today: the pitch directly above this one is the plan's, so "in" has
  // to mean "the rebuild wants him and the plan does not" or the two pitches
  // are marked in two different languages.
  const held = new Set(plan.weeks[idx].squad);
  const tags = {};
  for (const id of st.squad) if (!held.has(id)) tags[id] = { kind: "in", text: "in" };

  // A bench boost has no bench: all fifteen score, so all fifteen go on the
  // pitch and the substitutes' band is dropped — the same shape the plan's own
  // pitch directly above takes for that week, and drawing one of them with an
  // "auto-subs run down this order" band and the other without would say the
  // two squads are playing under different rules.
  const boosted = plan.weeks[idx].chip === "bboost";
  const layout = squadLayout({ ids: st.squad, lookup: S.byId, need: S.meta.squad_by_pos,
                               xiIds: boosted ? st.squad : st.starting,
                               benchOrder: st.bench });
  pitch.innerHTML = pitchHTML({ layout, teams: S.snapshot?.teams, metrics: S.metrics,
                               captain: st.captain, tags, versus: true });
  wireShirts(pitch);
  pitch.addEventListener("click", (e) => {
    const vs = e.target.closest("[data-vs]");
    if (vs) return toggleVersus(+vs.dataset.vs);
    const edit = e.target.closest("[data-edit]");
    if (edit) openEditor(+edit.dataset.edit);
  });
}

/** The squad the chip actually wants, against the squad the plan can afford to
 *  build towards.
 *
 *  This block exists because the plan alone answers the wrong question once a
 *  chip is committed. Ask it to bench boost and it plays the chip over the
 *  bench you happen to own, because reaching a better one costs transfers and
 *  hits it has to charge for. The rebuild pays none of that: it is the best
 *  fifteen that week's money could buy, under that week's scoring, and the gap
 *  between the two is exactly what the ownership and the transfer costs are
 *  taking off you. Cheapest-four is quoted alongside the totals because it is
 *  where a bench boost shows up in money — a £4.0m fourth keeper and three
 *  bodies become four players bought to play. */
function weekIdealHTML(plan, idx) {
  const week = plan.weeks[idx];
  const chipName = week.chip ? plan.chipLabels[week.chip] : "";
  const st = weekIdealState(idx);
  const solving = st?.state === "solving";

  // Shut unless you opened it, and nothing else may open it. This block is
  // solved automatically for the selected week, so keying the fold off "is there
  // an answer" would leave it permanently expanded -- which is the state that
  // made it read as the tab's headline when it is nothing of the kind: it is one
  // gameweek, solved on its own, and next to a pitch it looks like a squad to go
  // and buy.
  const open = S.chipFolds.ideal;
  const head = `<summary data-fold="ideal">Best fifteen for GW${week.gw} alone${
    chipName ? `, ${chipName.toLowerCase()}` : ""}
    <span class="scope">— this gameweek only, ignores the rest of the window</span></summary>`;

  const wrap = (body) => `<details class="chipfold"${open ? " open" : ""}>${head}
    <div class="foldbody">${body}</div></details>`;

  const btn = `<button class="mini" id="weekIdealBtn" ${solving ? "disabled" : ""}>${
    solving ? "Solving…" : st?.state === "ready" ? "Solve again" : "Rebuild this week"}</button>`;

  const premise = `<div class="sub" style="margin-bottom:8px">The best fifteen this week's
    money could buy if it were picked for <b>GW${week.gw} on its own</b>${week.chip
      ? `, with the ${chipName.toLowerCase()} in force` : ""}. It maximises that one gameweek
    and nothing after it, so it is not a squad to keep — the plan above has to be right about
    every week in the window at once, and the gap between the two is exactly what being right
    about the others costs it here. The chart is the horizon answer; this is the week.</div>`;

  if (!st) return wrap(premise + btn);
  if (solving) return wrap(premise + btn + `<div class="sub" style="margin-top:6px">Rebuilding
    GW${week.gw} on £${fmt(st.budget, 1)}m…</div>`);
  if (st.state === "error") {
    return wrap(premise + btn
      + `<div class="issue bad"><span class="ico">!</span><span>${st.error}</span></div>`);
  }

  const price = (id) => S.byId.get(id)?.price || 0;
  const cheapestFour = (ids) => ids.map(price).sort((a, b) => a - b).slice(0, 4)
    .reduce((a, b) => a + b, 0);
  const name = (id) => S.byId.get(id)?.name ?? "—";

  const idealSet = new Set(st.squad), heldSet = new Set(week.squad);
  const outIds = week.squad.filter((id) => !idealSet.has(id));
  const inIds = st.squad.filter((id) => !heldSet.has(id));
  const pairs = pairMoves(outIds, inIds, plan.positions);

  const gain = st.points - st.held;
  const note = weekIdealNote(week, st.budget);
  const summary = `<div class="sub" style="margin-bottom:8px">Scores
    <b>${fmt(st.points, 1)}</b> in GW${week.gw} against <b>${fmt(st.held, 1)}</b> for the
    fifteen the plan fields that week — ${gain > 0.05
      ? `<b>${fmt(gain, 1)}</b> more, which is what the plan gives up in this one week to be
         the best side across the <i>whole</i> window`
      : "the same, so nothing in the horizon is pulling the squad away from this week"}.
    Costs £${fmt(st.cost, 1)}m of £${fmt(st.budget, 1)}m,
    £${fmt(cheapestFour(st.squad), 1)}m of it on the cheapest four against
    £${fmt(cheapestFour(week.squad), 1)}m in the plan's squad.${
      note ? " " + note.charAt(0).toUpperCase() + note.slice(1) + "." : ""}</div>`;

  const spend = (out, inn) => {
    if (inn == null) return "";
    const d = price(inn) - (out != null ? price(out) : 0);
    return `${d < 0 ? "−" : "+"}£${fmt(Math.abs(d), 1)}m`;
  };
  const moves = pairs.length
    ? `<table class="movetable"><tbody>${pairs.map(([out, inn]) => `<tr>
        <td>${out != null ? name(out) : "—"}</td><td>→</td>
        <td>${inn != null ? name(inn) : "—"}</td>
        <td class="${inn != null && price(inn) >= (out != null ? price(out) : 0) ? "loss" : "gain"}">${spend(out, inn)}</td>
      </tr>`).join("")}</tbody></table>
      <div class="sub" style="margin-top:6px">${pairs.length} change${pairs.length > 1 ? "s" : ""}
        from the fifteen the plan fields in GW${week.gw}. Every one of them is a player the
        plan gave up to be better in the <i>other</i> gameweeks — this rebuild is charged for
        none of them, because it only has to be right about this week.</div>`
    : `<div class="sub">No changes — the plan's fifteen is already the best for this week on
        its own, so the horizon cost it nothing here.</div>`;

  return wrap(premise + btn + `<div id="weekIdealPitch" style="margin-top:10px"></div>`
    + `<h4 style="margin:14px 0 6px">What it would take</h4>` + summary + moves);
}

/** Redraw only the near-miss block, not the tab around it. A sweep ticks about
 *  once a second for half a minute, and renderChips rebuilds the week's pitch
 *  from scratch — including its shirts — which would flash the whole card on
 *  every candidate that landed. */
export function renderWeekNearMisses() {
  const box = $("#weekNearBox"), plan = S.transferPlan.result;
  if (!box || !plan) { renderChips(); return; }
  box.innerHTML = weekNearMissHTML(plan, S.planWeek);
  wireWeekNearMiss();
}

function wireWeekNearMiss() {
  $("#weekNearBtn")?.addEventListener("click", () => runWeekNearMisses(S.planWeek));
  wireChipFolds("#weekNearBox");
  document.querySelectorAll("#weekNearBox [data-near]").forEach((row) =>
    row.addEventListener("click", () => openEditor(+row.dataset.near)));
}

/** Remember which folds are open across the re-renders their own solves cause.
 *  Listens on the summary rather than on `toggle`, because the toggle event
 *  fires when the element is recreated with the `open` attribute we just set
 *  from state, which would flip it straight back. */
function wireChipFolds(scope) {
  document.querySelectorAll(`${scope} [data-fold]`).forEach((sum) =>
    sum.addEventListener("click", () => {
      const which = sum.dataset.fold;
      S.chipFolds = { ...S.chipFolds, [which]: !sum.parentElement.open };
    }));
}

/** The near-miss block inside one gameweek of a transfer plan.
 *
 *  Deliberately a *different* answer from the board's, and says so: it is one
 *  gameweek rather than a discounted horizon, it spends what that week's squad
 *  and bank are worth rather than the opening budget, and it scores the chip
 *  the plan chose to play there. A bench boost week ranks bench-worthy players
 *  the board would never buy; a triple-captain week pays for the armband. */
function weekNearMissHTML(plan, idx) {
  const near = S.weekNearMiss;
  const week = plan.weeks[idx];
  const mine = near.state !== "idle" && near.idx === idx && near.key === S.transferPlan.key;
  const solving = mine && near.state === "solving";
  const chipName = week.chip ? plan.chipLabels[week.chip] : "";

  const open = S.chipFolds.near || (mine && near.state !== "idle");
  const head = `<summary data-fold="near">Who nearly made GW${week.gw}${
    chipName ? `, ${chipName.toLowerCase()}` : ""}
    <span class="scope">— this gameweek only, one re-solve per candidate</span></summary>`;
  const wrap = (body) => `<details class="chipfold"${open ? " open" : ""}>${head}
    <div class="foldbody">${body}</div></details>`;

  const btn = `<button class="mini" id="weekNearBtn" ${solving ? "disabled" : ""}>${
    solving ? "Solving…" : mine && near.state === "ready" ? "Solve again" : "Who else?"}</button>`;

  if (!mine) {
    return wrap(`<div class="sub" style="margin-bottom:8px">Who the best fifteen this week's
      money could buy leaves out, and by how little — scored on GW${week.gw} alone${week.chip
        ? ` with the ${chipName.toLowerCase()} in force` : ""}, not on the horizon the
      board ranks on. One re-solve per candidate.</div>` + btn);
  }
  if (solving) {
    const { done, total } = near.progress;
    const at = plan.gameweeks.indexOf(week.gw);
    return wrap(`<div class="sub">Rebuilding GW${week.gw} around each candidate —
      ${done} of ${total || "?"}. The order settles as it goes.</div>
      <div class="meter" style="margin-bottom:12px"><div style="width:${
        total ? (done / total) * 100 : 0}%"></div></div>`
      + (near.rows.length ? nearMissTableHTML(near.rows,
          (p) => (plan.pointsByPlayer.get(p.id) || [])[at] || 0, `GW${week.gw}`) : ""));
  }
  if (near.state === "error") {
    return wrap(btn
      + `<div class="issue bad"><span class="ico">!</span><span>${near.error}</span></div>`);
  }
  if (!near.rows.length) {
    return wrap(`<div class="sub" style="margin-bottom:8px">Nobody to test — every player the
      solver can see is already in this week's fifteen.</div>` + btn);
  }

  // What that ideal fifteen *is*, and what holding the plan's one instead
  // costs, is the block above this one -- solved on its own and scored the way
  // the week scores, weights and armband included. Repeating it here from a raw
  // fifteen-man total would put two different numbers for one claim on one
  // card, so this block sticks to its own question: who just missed out.
  const summary = `<div class="sub" style="margin-bottom:8px">Ranked against the same
     rebuild shown above, on £${fmt(near.budget, 1)}m — each row is what the squad gives up
     to hold that player instead.</div>`;

  return wrap(summary + btn
    + nearMissTableHTML(near.rows, (p) => (plan.pointsByPlayer.get(p.id)
        || [])[plan.gameweeks.indexOf(week.gw)] || 0, `GW${week.gw}`)
    + `<p class="sub" style="margin:8px 0 0">${near.tested} candidates re-solved in
       ${((near.ms || 0) / 1000).toFixed(1)}s, each one a full rebuild of the other
       fourteen around him.</p>`);
}

/* ------------------------------------------------------------- head to head
   `fpl.py compare`, in the browser. Two players, every number the board holds
   on either, and the gap signed so the better one reads as the better one --
   which for price and ownership is the smaller figure, and the table has to
   know that or it would call the expensive player the winner every time. */
const VS_ROWS = [
  { label: "Position", get: (p) => p.pos, text: true },
  { label: "Club", get: (p) => p.team, text: true },
  { label: "Price", get: (p) => p.price, digits: 1, prefix: "£", lower: true },
  { label: "xPts over the horizon", get: (p) => p.xpts_plan, digits: 1, lead: true },
  { label: "xPPG, per match", get: (p) => p.xppg, digits: 2 },
  { label: "xPts per £m", get: (p) => (p.xpts_plan || 0) / (p.price || 1), digits: 2 },
  { label: "PPG last season", get: (p) => p.ppg, digits: 1 },
  { label: "Minutes last season", get: (p) => p.minutes_last, digits: 0 },
  { label: "Start probability", get: (p) => p.p_start, digits: 2 },
  { label: "Expected minutes", get: (p) => p.exp_minutes, digits: 0 },
  { label: "Expected clean sheets", get: (p) => p.cs, digits: 1 },
  { label: "Owned by", get: (p) => p.owned, digits: 1, suffix: "%", lower: true },
  { label: "Fixtures in the horizon", get: (p) => p.games, digits: 0 },
];

export function toggleVersus(id) {
  if (!S.byId.has(id)) return;
  if (S.versus.includes(id)) S.versus = S.versus.filter((x) => x !== id);
  // Two is the comparison; a third replaces the older of the pair, which is
  // what clicking a third shirt means.
  else S.versus = [...S.versus, id].slice(-2);
  renderSquadRef(); renderCompareRef(); renderLineupRef(); renderVersus();
  if (S.versus.length === 2) openVersus();
}
import { renderSquad as renderSquadRef, renderCompare as renderCompareRef } from "/assets/squad-view.mjs";
import { renderLineup as renderLineupRef } from "/assets/explain-view.mjs";

/** Mark the shirts currently being compared, wherever they are drawn. */
export function markVersus(root) {
  for (const id of S.versus) {
    root.querySelector(`.pcard[data-id="${id}"]`)?.classList.add("vspicked");
  }
}

export const versusOpen = () => $("#vsDrawer").classList.contains("open");

export function openVersus() {
  renderVersus();
  $("#vsDrawer").classList.add("open");
  $("#vsDrawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.remove("hidden");
}

export function closeVersus() {
  $("#vsDrawer").classList.remove("open");
  $("#vsDrawer").setAttribute("aria-hidden", "true");
  if (!poolOpenRef() && !$("#drawer").classList.contains("open")) {
    $("#scrim").classList.add("hidden");
  }
}

export function renderVersus() {
  const box = $("#vsBox");
  const players = S.versus.map((id) => S.byId.get(id)).filter(Boolean);
  if (players.length < 2) {
    box.innerHTML = `<div class="empty-note">Pick the ⇄ on two shirts, or two rows in
      the pool, and every number the board holds on both appears here.
      ${players.length === 1 ? `<br><br>Holding <b>${players[0].name}</b> — pick one more.` : ""}
    </div>`;
    return;
  }

  const [a, b] = players;
  const head = (p) => {
    const url = shirtUrl(S.snapshot?.teams, p);
    return `<span class="vshead">${url ? `<img src="${url}" alt="">` : ""}
      <span><b>${p.name}</b><br><span class="sub">${p.team_short} · ${p.pos} · £${fmt(p.price, 1)}</span></span></span>`;
  };

  const rows = VS_ROWS.map((row) => {
    const av = row.get(a), bv = row.get(b);
    if (row.text) {
      return `<tr><td class="lab">${row.label}</td><td>${av}</td><td>${bv}</td><td></td></tr>`;
    }
    const gap = (av || 0) - (bv || 0);
    // "Better" is the bigger number except where it is the smaller one: paying
    // less and being less owned are both wins, and a table that did not know
    // that would bold the wrong column half the time.
    const aBetter = row.lower ? gap < -1e-9 : gap > 1e-9;
    const bBetter = row.lower ? gap > 1e-9 : gap < -1e-9;
    const show = (v) => (row.prefix || "") + fmt(v, row.digits) + (row.suffix || "");
    const sign = gap > 0 ? "+" : "";
    return `<tr${row.lead ? ` class="lead"` : ""}>
      <td class="lab">${row.label}</td>
      <td class="${aBetter ? "better" : ""}">${show(av)}</td>
      <td class="${bBetter ? "better" : ""}">${show(bv)}</td>
      <td class="gap">${sign}${fmt(gap, row.digits)}</td>
    </tr>`;
  }).join("");

  // The horizon, gameweek by gameweek, because two players with the same total
  // can have it arranged very differently -- and the arrangement is what decides
  // when you buy which.
  const weekly = S.gameweeks.map((gw, i) => {
    const av = a.gw[i] || 0, bv = b.gw[i] || 0;
    const lead = av - bv;
    return `<tr>
      <td class="lab">GW${gw}</td>
      <td class="${lead > 1e-9 ? "better" : ""}">${fmt(av, 1)}</td>
      <td class="${lead < -1e-9 ? "better" : ""}">${fmt(bv, 1)}</td>
      <td class="gap">${(a.opp || [])[i] ? shortFixture(a, i) : "—"} / ${(b.opp || [])[i] ? shortFixture(b, i) : "—"}</td>
    </tr>`;
  }).join("");

  box.innerHTML = `
    <table class="vstable">
      <thead><tr><th></th><th>${head(a)}</th><th>${head(b)}</th><th>Gap</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="exsec">
      <div class="exhead"><h4>Gameweek by gameweek</h4>
        <span class="sub">points, and who each plays</span></div>
      <table class="vstable"><tbody>${weekly}</tbody></table>
    </div>
    <div class="drawerfoot">
      <button class="btn ghost" data-edit="${a.id}">Open ${a.name}</button>
      <button class="btn ghost" data-edit="${b.id}">Open ${b.name}</button>
    </div>`;
}

/* An opponent label written for a table, shortened for a cell. */
function shortFixture(player, index) {
  const label = (player.opp || [])[index] || "";
  const match = /^(.*?)\s*\((H|A)\)/.exec(label);
  if (!match) return label.slice(0, 6);
  const short = S.snapshot?.teams?.[match[1]]?.short || match[1].slice(0, 3).toUpperCase();
  return match[2] === "H" ? short : short.toLowerCase();
}

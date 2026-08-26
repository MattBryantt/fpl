/* The player drawer: the stat editor, the explain panel, the per-club lineup
 * view, and the AI "ask" chat. Also owns commit/undo/recompute for edits and
 * the edit banner, since those are the drawer's own persistence machinery.
 * See REFACTOR.md for the split this belongs to. */
"use strict";
import { $, S, saveLocal, STORE, planOpts, fmt, loadLocal } from "/assets/state.mjs";
import { saveEdits, snapshotEditsHistory } from "/assets/state.mjs";
import {
  derivePool, editPlayer, explainPlayer, clubLineup, checkOverridable, POINT_SOURCES,
} from "/assets/board.mjs";
import { squadLayout, pitchHTML, wireShirts } from "/assets/pitch.mjs";
import { POSITION_TAGS, POSITION_TAG_LABELS } from "/assets/position-tags.mjs";
import { playerTags, posTagBadges, bestXI, rebuildPool, renderAll, poolOpen } from "/assets/squad-view.mjs";
import { scheduleSolve } from "/assets/transfer-view.mjs";
import { pushOverrides, api } from "/assets/sync.mjs";
import { markVersus, versusOpen } from "/assets/compare-view.mjs";

/* --------------------------------------------------------------- stat editor
   Every model input a user can reasonably disagree with. `only` restricts a
   field to the positions it means anything for -- saves per 90 on a striker is
   noise, not a setting. */
export const EDIT_FIELDS = [
  { k: "p_start", label: "Start probability", min: 0, max: 1, step: 0.01, dp: 2,
    help: "The biggest lever in the model. Last season's starts cannot see a transfer or a new manager." },
  { k: "mins_if_start", label: "Minutes when he starts", min: 0, max: 90, step: 1, dp: 0,
    help: "How long his shift is, on the weeks he is in the side — independent of how often that is. "
        + "The league average is 78. Drop it for a striker hooked on the hour and the second appearance "
        + "point and the clean sheet go with him, which is the part a start probability cannot say." },
  { k: "exp_minutes", label: "Expected minutes", min: 0, max: 90, step: 1, dp: 0,
    help: "Minutes per match across starts and cameos both, for when you trust one figure more than the "
        + "two above it. Setting it lengthens or shortens his shift first, and only reaches for start "
        + "probability when ninety minutes still is not enough. Setting either of those back clears it." },
  { k: "npxg_per90", label: "Non-penalty xG per 90", min: 0, max: 1.5, step: 0.01, dp: 3,
    help: "Penalties are modelled separately, so this is open play only." },
  { k: "xa_per90", label: "Expected assists per 90", min: 0, max: 1.2, step: 0.01, dp: 3 },
  { k: "dc_per90", label: "Defensive contribution per 90", min: 0, max: 25, step: 0.1, dp: 2,
    help: "Threshold is 10 for defenders, 12 for everyone else." },
  { k: "bonus_per90", label: "Bonus points per 90", min: 0, max: 1.5, step: 0.01, dp: 3 },
  { k: "saves_per90", label: "Saves per 90", min: 0, max: 8, step: 0.1, dp: 2, only: ["GKP"] },
  { k: "penalties_order", label: "Penalty order (1 = takes them)", min: 0, max: 5, step: 1, dp: 0 },
  { k: "price", label: "Price (£m)", min: 3.5, max: 16, step: 0.1, dp: 1,
    help: "For hypotheticals — it does not change what FPL charges you." },
];

/* The drawer still edits a copy, but the copy is now committed for you.
   It used to require Apply, and the reason was real: writing straight through on
   every keystroke, without recomputing the player, left the board showing his old
   points while the optimiser was quietly handed the new ones. That is a reason to
   recompute on commit, not a reason to make you press a button — so the commit is
   debounced and *always* runs the recompute, and closing the drawer by any route
   keeps what you typed rather than silently binning it.

   `editBaseline` is what makes that safe: it is the player's edits as they were
   when the drawer opened, so Undo changes is a real escape hatch rather than
   Reset player, which throws away edits you made a week ago too. */
export let editingId = null, editBuffer = {}, previewTimer = null, commitTimer = null;
export let editBaseline = null;

export function openEditor(id) {
  editingId = id;
  editBuffer = { ...(S.edits[id] || {}) };
  // Deep, because the per-match block is nested and a shallow copy would hand
  // Undo the same object the sliders are busy mutating.
  editBaseline = JSON.parse(JSON.stringify(S.edits[id] || null));
  const p = S.byId.get(id);
  $("#edName").textContent = p.name;
  $("#edWho").textContent = `${p.full_name} · ${p.pos} · ${p.team} · £${p.price.toFixed(1)}m`
    + (p.moved ? ` · numbers recorded at ${p.previous_club}` : "");
  $("#edWhy").innerHTML = p.moved
    ? `These rates were produced at <b>${p.previous_club}</b> and already carry a team-context
       adjustment and a heavier prior. If you think the model still has him wrong, say so here.`
    : `Rates are last season's, shrunk toward the positional average. An override replaces
       that number outright — the model uses exactly what you type.`;
  renderPosTags(id);
  expandedMatches.clear();
  renderEditorFields();
  renderMatches();
  renderEditorConstraints();
  renderExplain();
  resetAsk(p);
  previewEdit();
  $("#drawer").classList.add("open");
  $("#scrim").classList.remove("hidden");
}

/* Detailed position, edited as a set of toggle chips rather than a slider --
   it is not a number the model reads, just a tag the board shows back next to
   the base FPL position wherever it lists him. */
export function renderPosTags(id) {
  const p = S.byId.get(id);
  const active = new Set(playerTags(p));
  $("#edPosTags").innerHTML = POSITION_TAGS.map((t) =>
    `<button type="button" class="postagchip${active.has(t) ? " on" : ""}"
             data-postag="${t}" title="${POSITION_TAG_LABELS[t]}">${t}</button>`).join("");
}

/** The drawer's "Reset player" button: drop every override on the player
 *  currently open, season and per-match both. */
export async function resetEditor() {
  if (editingId === null) return;
  const id = editingId;
  editBuffer = {};
  expandedMatches.clear();
  delete S.edits[id];
  renderEditorFields(); renderMatches(); previewEdit();
  await recomputeEdited([id]);
  renderAll();
}

export function closeEditor() {
  // Flush before dropping the buffer. The debounce means a slider moved half a
  // second before Escape has a commit still in flight, and losing that is
  // exactly the bug autosave is supposed to remove.
  commitEdit({ immediate: true });
  clearTimeout(previewTimer);
  clearTimeout(commitTimer);
  editingId = null;
  editBuffer = {};
  editBaseline = null;
  $("#drawer").classList.remove("open");
  // The pool or the comparison can be open underneath — a player is usually
  // edited on the way to deciding whether to buy him — so the scrim belongs to
  // whichever is left rather than to whoever closed last.
  if (!poolOpen() && !versusOpen()) $("#scrim").classList.add("hidden");
}

/* --------------------------------------------------------------- explain ---
   Three questions, in the order people actually ask them: what is he paid for,
   why is a gameweek eight fixture worth less than a gameweek one fixture, and
   how does any of that reach the single number in the table.

   The last one is the whole reason this panel exists. The board ranks on
   plan-weighted points and the eye reads raw ones, and nothing on screen ever
   said they were different quantities -- so the honest thing is to show both
   ends and the arithmetic between them rather than pick one and hope. */
export let explainOpen = true;
export function setExplainOpen(v) { explainOpen = v; }

export function renderExplain() {
  const box = $("#edExplain");
  if (!explainOpen) { box.innerHTML = ""; return; }
  const p = S.byId.get(editingId);
  if (!p) return;

  let x;
  try {
    x = explainPlayer(S.snapshot, S.snapshot.players.find((r) => r.id === editingId),
      scoringEdit(p, editBuffer), planOpts(), S.fixtureEdits);
  } catch {
    box.innerHTML = `<p class="exnote">Cannot break these points down right now.</p>`;
    return;
  }

  const shown = POINT_SOURCES
    .map((s) => ({ ...s, value: x.breakdown[s.key] || 0 }))
    .filter((s) => Math.abs(s.value) > 0.005);
  const scale = Math.max(...shown.map((s) => Math.abs(s.value)), 0.01);

  const bars = shown.map((s) => `
    <div>
      <div class="exrow">
        <span class="exlabel">${s.label}${s.note ? `<span class="note">${s.note}</span>` : ""}</span>
        <span class="exval">${s.value > 0 ? "" : "−"}${Math.abs(s.value).toFixed(2)}</span>
      </div>
      <div class="exbar${s.value < 0 ? " neg" : ""}"
           style="width:${Math.max(2, (Math.abs(s.value) / scale) * 100)}%"></div>
    </div>`).join("");

  // The bridge. Each gameweek's own points, then the two multipliers that turn
  // them into what the ranking uses, then the product -- laid out so the last
  // column visibly sums to the headline figure.
  const rows = x.gw.map((r) => `
    <tr class="${r.weight < 0.35 ? "faded" : ""}">
      <td>GW${r.gw}${r.opp ? ` <span style="color:var(--muted)">${r.opp}</span>` : ""}</td>
      <td>${r.points.toFixed(2)}</td>
      <td>${r.weight.toFixed(2)}</td>
      <td>${r.weighted.toFixed(2)}</td>
    </tr>`).join("");

  const perGame = x.games ? x.raw_total / x.games : 0;
  const hazardPct = (x.hazard * 100).toFixed(1);

  box.innerHTML = `
    ${bars}
    <div class="exsum"><span>Over ${x.games} fixture${x.games === 1 ? "" : "s"}</span>
      <span>${x.raw_total.toFixed(2)}</span></div>
    <p class="exnote">That is <b>${perGame.toFixed(2)} per fixture</b> — the xPPG column,
      and the number to compare against another player on a different fixture run.</p>

    <h4 style="margin:14px 0 2px;font-size:12.5px">Why the table shows ${x.plan_total.toFixed(1)}</h4>
    <p class="exnote">${Number.isFinite(x.half_life)
      ? `A gameweek you may never field him in is worth less than this Saturday.
         Each gameweek of distance is worth <b>${(+$("#gwdecay").value).toFixed(2)}×</b> the one
         before it — halving every ${x.half_life.toFixed(1)} gameweeks.`
      : `Fixture decay is off, so all ${x.gw.length} gameweeks count their full
         value regardless of distance.`}
      ${x.hazard
        ? `Every gameweek is then multiplied by his chance of still being available —
           ${hazardPct}% risk of dropping out per gameweek, compounding.`
        : `Nothing further comes off them either: the injury and rotation dropout is
           switched off with the decay, so the weight column is a column of 1.00s and
           the total below is the fixtures added up.`}</p>
    <table class="extable">
      <thead><tr><th>Fixture</th><th>Points</th><th>Weight</th><th>Counts as</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="exsum"><span>Plan-weighted total — what the board ranks on</span>
      <span>${x.plan_total.toFixed(2)}</span></div>`;
}

export function renderEditorConstraints() {
  const req = $("#edReq"), ban = $("#edBan");
  const on = S.include.includes(editingId), off = S.exclude.includes(editingId);
  req.textContent = on ? "⊕ Required" : "⊕ Require";
  ban.textContent = off ? "⊘ Barred" : "⊘ Bar";
  req.style.background = on ? "var(--good)" : "";
  req.style.color = on ? "#fff" : "";
  ban.style.background = off ? "var(--critical)" : "";
  ban.style.color = off ? "#fff" : "";
}

/* --------------------------------------------------------------- lineups ---
   The view for feeding real team news into the model. Minutes are the model's
   least reliable input and much its most powerful, so this is where an hour of
   your own knowledge is worth more than any amount of xG modelling.

   It renders from the derived pool rather than recomputing, so it cannot
   disagree with the rest of the board -- and because the pool already carries
   the club rebalance, promoting one player visibly pushes his team-mates down
   the moment you apply it. */
export let lineupTeam = null, lineupOnlyEdited = false;
export function setLineupTeam(v) { lineupTeam = v; }
export function toggleLineupOnlyEdited() { lineupOnlyEdited = !lineupOnlyEdited; renderLineup(); }

export function teamsInPool() {
  return [...new Set(S.players.map((p) => p.team))].sort();
}

/* ---------------------------------------------------------- lineup pitch shape
   FPL's own row template is one line per GKP/DEF/MID/FWD, which says nothing
   about whether a back four's flanks are full-backs or wing-backs, or whether
   the front line is two wingers round a striker or a front two. Left-to-right
   order within a row is the one thing that can say it without a redesign, so
   a row sorts by lane -- left, centre, right, read off the detailed tags
   (position-tags.mjs) -- rather than by points. A player without a tag has no
   lane opinion and sits centre, same as before this existed.

   That default is a guess, and the whole point of tagging positions yourself
   is that a guess should be correctable: the ‹ › buttons on each shirt swap it
   with its neighbour, and the result is remembered per club so it does not
   reset the next time the fixtures refresh. */
const TAG_LANE = { LB: 0, LM: 0, LW: 0, RB: 2, RM: 2, RW: 2 };
function playerLane(player) {
  const lanes = playerTags(player).map((t) => TAG_LANE[t]).filter((v) => v !== undefined);
  return lanes.length ? lanes.reduce((a, b) => a + b, 0) / lanes.length : 1;
}
function defaultRowOrder(members) {
  return members.slice().sort((a, b) =>
    playerLane(a) - playerLane(b) || (b.xpts_plan || 0) - (a.xpts_plan || 0));
}
function effectiveRowOrder(team, pos, members) {
  const custom = S.lineupOrder[team]?.[pos];
  const ids = new Set(members.map((p) => p.id));
  if (custom && custom.length === ids.size && custom.every((id) => ids.has(id))) {
    return custom.map((id) => members.find((p) => p.id === id));
  }
  return defaultRowOrder(members);
}
export function nudgeLineupPlayer(id, dir) {
  if (!lineupPitchState) return;
  const { team, layout } = lineupPitchState;
  const row = layout.rows.find((r) => r.slots.some((s) => s.player?.id === id));
  if (!row) return;
  const order = row.slots.map((s) => s.player).filter(Boolean);
  const i = order.findIndex((p) => p.id === id);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  const byTeam = S.lineupOrder[team] || (S.lineupOrder[team] = {});
  byTeam[row.pos] = order.map((p) => p.id);
  saveLocal(STORE.lineupOrder, S.lineupOrder);
  renderLineupPitch(clubLineup(S.players, team));
}

/* The club as a pitch: the eleven it is likeliest to field, in a legal shape,
   with the next four beneath. Same renderer as a squad, because it is the same
   question asked of a different fifteen -- and expected minutes is exactly the
   sort of thing a column of numbers hides and a shape does not.

   Ranked on start probability rather than points: this is who plays, not who
   scores. `bestXI` enumerates the same formations the optimiser is allowed, so
   the shape on screen is one a manager could actually field. */
let lineupPitchState = null;
export function renderLineupPitch(line) {
  const host = $("#lineupPitch");
  const ids = line.players.map((p) => p.id);
  const xi = bestXI(ids, (p) => p.p_start || 0);
  if (!xi.ids.length) { host.innerHTML = ""; lineupPitchState = null; return; }

  const chosen = new Set(xi.ids);
  const next = line.players.filter((p) => !chosen.has(p.id))
    .sort((a, b) => (b.p_start || 0) - (a.p_start || 0));
  const keeper = next.find((p) => p.pos === "GKP");
  const outfield = next.filter((p) => p.pos !== "GKP").slice(0, 3);
  const bench = [keeper, ...outfield].filter(Boolean);
  const benchOrder = {};
  if (keeper) benchOrder[keeper.id] = "GKP";
  outfield.forEach((p, i) => { benchOrder[p.id] = String(i + 1); });

  const layout = squadLayout({
    ids: [...xi.ids, ...bench.map((p) => p.id)], lookup: S.byId,
    need: S.meta.squad_by_pos, xiIds: xi.ids, benchOrder,
    rowOrder: (pos, members) => effectiveRowOrder(line.team, pos, members),
  });
  lineupPitchState = { team: line.team, layout };
  // Fixed, and not the squad's metrics: this section is about who plays and for
  // how long, so those lead whatever you happen to be reading a squad by.
  const metrics = ["start", "mins", "xppg"];
  host.innerHTML = pitchHTML({
    layout, teams: S.snapshot?.teams, metrics, versus: true, benchLabels: false,
    reorder: true,
  });
  wireShirts(host);
  markVersus(host);
}

export function renderLineup() {
  const box = $("#lineupBox"), select = $("#lineupTeam");
  if (!S.players?.length) { box.innerHTML = ""; return; }

  const edited = new Set(S.players.filter((p) => p.edited || p.adjusted).map((p) => p.team));
  let teams = teamsInPool();
  if (lineupOnlyEdited && edited.size) teams = teams.filter((t) => edited.has(t));
  if (!teams.includes(lineupTeam)) lineupTeam = teams[0] ?? null;

  const want = teams.map((t) => t + (edited.has(t) ? " •" : "")).join("|");
  if (select.dataset.built !== want) {
    select.innerHTML = teams.map((t) =>
      `<option value="${t}"${t === lineupTeam ? " selected" : ""}>`
      + `${t}${edited.has(t) ? " •" : ""}</option>`).join("");
    select.dataset.built = want;
  }
  select.value = lineupTeam ?? "";
  $("#lineupEdited").setAttribute("aria-pressed", String(lineupOnlyEdited));

  if (!lineupTeam) {
    box.innerHTML = `<p class="lineupfoot">No clubs to show.</p>`;
    $("#lineupPitch").innerHTML = "";
    return;
  }
  const line = clubLineup(S.players, lineupTeam);
  renderLineupPitch(line);

  // A club fields eleven. Say so plainly when the overrides in force do not.
  const off = line.starters - 11;
  const pill = line.balanced
    ? `<span class="pill ok">${line.starters.toFixed(2)} starters</span>`
    : `<span class="pill warn">${line.starters.toFixed(2)} starters, ${off > 0 ? "+" : ""}${off.toFixed(2)}</span>`;

  const rows = line.players.filter((p) => p.exp_minutes > 0.05).map((p) => {
    const tag = p.edited ? `<span class="tag you">yours</span>`
      : p.adjusted ? `<span class="tag adj">adjusted</span>` : "";
    const starter = p.p_start >= 0.5;
    return `<tr class="${starter ? "" : "benchrow"}">
      <td>
        <div class="who2">${p.name}<span class="posn">${p.pos}</span>${posTagBadges(playerTags(p))}${tag}</div>
        <div class="minbar${starter ? "" : " sub"}" style="width:${Math.max(2, (p.exp_minutes / 90) * 100)}%"></div>
      </td>
      <td><input class="mininput" type="number" inputmode="decimal"
          data-mininput="${p.id}" data-field="p_start"
          min="0" max="1" step="0.01" value="${p.p_start.toFixed(2)}"
          aria-label="Start probability for ${p.name}"></td>
      <td><input class="mininput" type="number" inputmode="numeric"
          data-mininput="${p.id}" data-field="exp_minutes"
          min="0" max="90" step="1" value="${p.exp_minutes.toFixed(0)}"
          aria-label="Expected minutes for ${p.name}"></td>
      <td>${fmt(p.xppg, 2)}</td>
      <td><button class="editbtn" data-edit="${p.id}" title="Edit ${p.name}">✎</button></td>
    </tr>`;
  }).join("");

  box.innerHTML = `
    <div class="lineuphead">${pill}
      <span>${line.minutes.toFixed(0)} of 990 minutes accounted for</span>
      ${line.edited ? `<span>· you have overridden someone here</span>` : ""}</div>
    <div class="scroll"><table class="lineup">
      <thead><tr><th>Player</th><th>Start</th><th>Mins</th><th>xPPG</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <p class="lineupfoot">${line.balanced
      ? "Eleven starters, so this club's books balance."
      : off > 0
        ? "More than eleven start. Your overrides assert more minutes than a team has "
          + "to give, so this club will out-score what its fixtures say — lower somebody "
          + "to bring it back."
        : "Fewer than eleven start, so this club is under-projected. Raise whoever you "
          + "expect to fill the gap."}</p>`;
}

/* Editing Start or Mins straight from the lineup table, without opening the
   drawer for a one-number change. Writes to S.edits exactly as the drawer's
   commitEdit does, so a number typed here and a slider dragged there are the
   same override -- neither shadows the other, and the drawer (if open on this
   same player) is kept in step rather than going stale. */
export async function inlineMinutesEdit(id, field, raw) {
  const p = S.byId.get(id);
  const f = EDIT_FIELDS.find((x) => x.k === field);
  if (!p || !f || raw === "" || Number.isNaN(+raw)) { renderLineup(); return; }
  const n = Math.min(f.max, Math.max(f.min, +raw));
  const entry = editableFields(p).find((x) => x.f.k === field);
  const base = entry ? entry.base : p.inputs?.[field];
  const store = tidy(S.edits[id] ? JSON.parse(JSON.stringify(S.edits[id])) : {});
  if (Math.abs(n - base) < 1e-9) delete store[field];
  else { store[field] = n; for (const k of MINUTES_CLEARS[field] || []) delete store[k]; }
  const tidied = tidy(store);
  if (Object.keys(tidied).length) S.edits[id] = tidied;
  else delete S.edits[id];
  await recomputeEdited([id], { solveDelay: 400 });
  renderAll();
  if (editingId === id) {
    editBuffer = S.edits[id] ? JSON.parse(JSON.stringify(S.edits[id])) : {};
    renderEditorFields(); renderMatches(); previewEdit();
  }
}

/* The same rebalance, previewed live while the slider is still moving. It costs
   a full derivePool over the pool -- a couple of milliseconds for 573 players,
   and already behind the same debounce the points preview uses -- which buys
   the thing that makes team news enterable at all: you can see who pays for the
   minutes you are handing out, before you commit to handing them out. */
export function renderDrawerClub() {
  const box = $("#clubBox"), label = $("#clubStarters");
  const me = S.byId.get(editingId);
  if (!me) { box.innerHTML = ""; label.textContent = ""; return; }

  let after;
  try {
    const pending = { ...S.edits, [editingId]: tidy(editBuffer) };
    after = derivePool(S.snapshot, pending, planOpts(), S.fixtureEdits).players;
  } catch {
    box.innerHTML = ""; label.textContent = ""; return;
  }

  const line = clubLineup(after, me.team);
  const before = new Map(S.players.map((p) => [p.id, p.exp_minutes]));

  label.innerHTML = line.balanced
    ? `<span class="pill ok">${line.starters.toFixed(2)} starters</span>`
    : `<span class="pill warn">${line.starters.toFixed(2)} starters</span>`;

  const rows = line.players
    .filter((p) => p.exp_minutes > 0.05 || (before.get(p.id) || 0) > 0.05)
    .map((p) => ({ p, was: before.get(p.id) ?? p.exp_minutes }))
    .map((r) => ({ ...r, moved: r.p.exp_minutes - r.was }))
    .sort((a, b) => Math.abs(b.moved) - Math.abs(a.moved) || b.p.exp_minutes - a.p.exp_minutes)
    .slice(0, 8);

  box.innerHTML = `<table class="extable">
    <thead><tr><th>Player</th><th>Mins now</th><th>Was</th><th>Δ</th></tr></thead>
    <tbody>${rows.map((r) => `
      <tr class="${Math.abs(r.moved) < 0.05 ? "faded" : ""}">
        <td>${r.p.id === editingId ? "<b>" + r.p.name + "</b>" : r.p.name}</td>
        <td>${r.p.exp_minutes.toFixed(0)}</td>
        <td>${r.was.toFixed(0)}</td>
        <td class="${r.moved > 0.05 ? "up" : r.moved < -0.05 ? "down" : ""}">${
          Math.abs(r.moved) < 0.05 ? "—" : (r.moved > 0 ? "+" : "") + r.moved.toFixed(1)}</td>
      </tr>`).join("")}</tbody></table>
    <p class="exnote">${line.balanced
      ? "Eleven still start — the minutes you gave him came off his team-mates."
      : "This club no longer fields eleven. The overrides in force claim more "
        + "minutes than a team has to give, so it will out-score its own fixtures."}</p>`;
}

/* ------------------------------------------------------------------- ask ---
   A chat box over a projection is a good way to produce confident nonsense, so
   the server does not hand the model a question and a hope: it rebuilds the
   player's dossier from the same scoring code the board runs on, and the answer
   is grounded in that. Which also means this is the one part of the board that
   genuinely needs the laptop -- there is no model on the phone, and pretending
   otherwise would just fail slowly. */
let askAvailable = null, askHistory = [], askBusy = false;

const ASK_SUGGESTIONS = [
  "Why is he rated this highly?",
  "What is he actually being paid for?",
  "How much of this depends on him starting?",
  "Should I trust this number?",
];

async function loadAskStatus() {
  if (askAvailable !== null) return askAvailable;
  try {
    const res = await api("/api/ai");
    askAvailable = res.ok ? await res.json() : { available: false };
  } catch {
    // No server behind us -- on the phone with the laptop shut, which is the
    // normal way to use this board and not an error worth shouting about.
    askAvailable = { available: false };
  }
  return askAvailable;
}

export function resetAsk(player) {
  askHistory = [];
  $("#askLog").innerHTML = "";
  $("#askInput").value = "";
  $("#askSuggest").innerHTML = ASK_SUGGESTIONS
    .map((q) => `<button type="button" data-ask="${q.replace(/"/g, "&quot;")}">${q}</button>`)
    .join("");
  loadAskStatus().then((status) => {
    const sec = $("#askSec");
    if (!status.available) {
      sec.classList.add("off");
      $("#askStatus").textContent = "unavailable";
      const why = document.createElement("div");
      why.className = "askmsg err";
      why.textContent = (status.reason ? `${status.reason}. ` : "")
        + "See .env.example — a local Ollama needs no key and no account, or point "
        + "FPL_AI_BASE_URL at a free hosted tier.";
      $("#askLog").replaceChildren(why);
      return;
    }
    sec.classList.remove("off");
    $("#askStatus").textContent = status.local
      ? `${status.model} · on this machine`
      : `${status.model} · sent to ${new URL(status.base_url).host}`;
  });
}

function appendAsk(role, text) {
  const div = document.createElement("div");
  div.className = `askmsg ${role}`;
  if (role === "ai") {
    // The model writes prose and the occasional bullet list. Rendered as text
    // in paragraphs rather than as HTML: nothing it returns should ever be able
    // to put markup into this page.
    div.append(...String(text).split(/\n{2,}/).map((para) => {
      const p = document.createElement("p");
      p.textContent = para.replace(/\n/g, " ").trim();
      return p;
    }));
  } else {
    div.textContent = text;
  }
  $("#askLog").appendChild(div);
  div.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return div;
}

export async function sendAsk(question) {
  if (askBusy || !question.trim() || editingId === null) return;
  const status = await loadAskStatus();
  if (!status.available) return;

  askBusy = true;
  $("#askSend").disabled = true;
  $("#askInput").value = "";
  $("#askSuggest").innerHTML = "";
  appendAsk("me", question);
  const pending = appendAsk("ai", "…");

  try {
    const res = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fpl_id: editingId,
        question,
        horizon: +$("#horizon").value,
        half_life: hlJSONRef(halfLifeRef()),
        start_gw: +$("#startgw").value || null,
        recency: +$("#recency").value || 0,
        history: askHistory,
        edits: tidy(editBuffer),
      }),
    });
    const out = await res.json().catch(() => ({}));
    pending.remove();
    if (!res.ok) throw new Error(out.detail || `The AI endpoint returned ${res.status}.`);
    appendAsk("ai", out.answer);
    askHistory.push({ role: "user", content: question },
                    { role: "assistant", content: out.answer });
  } catch (error) {
    pending.remove();
    appendAsk("err", error.message || "Could not reach the AI endpoint.");
  } finally {
    askBusy = false;
    $("#askSend").disabled = false;
  }
}
import { hlJSON as hlJSONRef, halfLife as halfLifeRef } from "/assets/state.mjs";

/* One slider-plus-number control. `store` is whichever dict the value belongs
   in — the season-level buffer, or one gameweek's — so the same code renders
   both and they cannot drift in behaviour. `idPrefix` keeps element ids unique
   once the same field appears twelve times over. */
/* How often he starts, how long he stays on, and the two multiplied out.
   p_start and mins_if_start are genuinely independent -- that is the point of
   having both -- but exp_minutes is their product, so it cannot be stated
   alongside either without one of them quietly losing. Setting a component
   clears the aggregate; setting the aggregate clears both components and lets
   the solve decide where the minutes came from. Leaving them all stored would
   show three sliders as "changed" while only some of them shape the score. */
const MINUTES_CLEARS = {
  p_start: ["exp_minutes"],
  mins_if_start: ["exp_minutes"],
  exp_minutes: ["p_start", "mins_if_start"],
};

function renderField(box, f, base, store, idPrefix, onChange, hint = "", prevSeason) {
  const value = store[f.k] !== undefined ? store[f.k] : base;
  const changed = store[f.k] !== undefined && Math.abs(store[f.k] - base) > 1e-9;
  const rid = `${idPrefix}${f.k}`, nid = `${idPrefix}n_${f.k}`;
  const div = document.createElement("div");
  div.className = "field" + (changed ? " changed" : "");
  // Last season's number as it actually was, before shrinkage pulled it toward
  // the positional average -- shown once, on the season control only, since a
  // per-match override has no season of its own to compare against.
  const showPrev = idPrefix === "ed_" && prevSeason !== undefined && prevSeason !== null;
  div.innerHTML = `
    <div class="row">
      <label for="${rid}">${f.label}</label>
      <span class="orig">${hint || `${idPrefix === "ed_" ? "model" : "season"}: ${base.toFixed(f.dp)}`}</span>
    </div>
    <div class="inp">
      <input type="range" id="${rid}" min="${f.min}" max="${f.max}" step="${f.step}" value="${value}">
      <input type="number" id="${nid}" min="${f.min}" max="${f.max}" step="${f.step}" value="${(+value).toFixed(f.dp)}">
    </div>
    ${showPrev ? `<div class="orig" style="margin-top:3px">last season (unweighted): ${prevSeason.toFixed(f.dp)}</div>` : ""}
    ${f.help && idPrefix === "ed_" ? `<div class="orig" style="margin-top:3px">${f.help}</div>` : ""}`;
  box.appendChild(div);

  const range = div.querySelector(`#${CSS.escape(rid)}`);
  const num = div.querySelector(`#${CSS.escape(nid)}`);
  const sync = (v) => {
    const n = Math.min(f.max, Math.max(f.min, +v));
    range.value = n; num.value = n.toFixed(f.dp);
    if (Math.abs(n - base) < 1e-9) delete store[f.k];
    else { store[f.k] = n; for (const k of MINUTES_CLEARS[f.k] || []) delete store[k]; }
    div.classList.toggle("changed", Math.abs(n - base) > 1e-9);
    onChange();
  };
  range.addEventListener("input", (e) => sync(e.target.value));
  num.addEventListener("change", (e) => sync(e.target.value));
  return div;
}

/** Fields that mean something for this player, with the model's value. */
/* The one field the club rebalance moves on its own. Overriding a team-mate's
   minutes takes minutes off everyone else at that club, and the board scores,
   lists and draws the player at the moved value -- so the editor has to open on
   the moved value too. It did not, and a shirt reading 0.31 next to a slider
   reading 0.49 is the board contradicting itself about the number that matters
   most. */
const REBALANCED_FIELD = "p_start";
// exp_minutes moves with p_start under a rebalance -- it is derived from it,
// not an independent consequence -- so the edit tab has to open on the same
// pair the squad view already shows or the two disagree about a player
// neither the user nor the board ever asked to look different. mins_if_start
// is not on the list: a rebalance redistributes shirts, not shift lengths, and
// it leaves that field exactly where it found it.
const REBALANCED_FIELDS = new Set([REBALANCED_FIELD, "exp_minutes"]);

/** The overrides this player is actually being scored with: yours, plus the
 *  start probability his club's books owe him. `renormaliseMinutes` produced it;
 *  everything in the drawer has to agree with it. */
function scoringEdit(player, buffer) {
  const edit = tidy(buffer || {});
  if (player?.adjusted && edit[REBALANCED_FIELD] === undefined) {
    edit[REBALANCED_FIELD] = player[REBALANCED_FIELD];
  }
  return edit;
}

/* Why p_start is what it is. The model blends a long-run start rate with a
   recency-weighted one, weighted by how far ahead the horizon looks, and the
   blend is the single number the slider opens on -- so when the two halves
   disagree, saying so is the difference between "the model has him at 0.41" and
   "the model has him at 0.41 because he sat out August, and he has started
   every match since". Only shown when they actually disagree; a settled starter
   whose two numbers match does not need a sentence about it. */
function startFormHint(player) {
  const long = player.start_long_run, recent = player.start_recent;
  if (long === null || long === undefined || recent === null || recent === undefined) return "";
  if (Math.abs(recent - long) < 0.05) return "";
  return `season-long ${long.toFixed(2)}, lately ${recent.toFixed(2)}`;
}

function editableFields(player) {
  const out = [];
  for (const f of EDIT_FIELDS) {
    if (f.only && !f.only.includes(player.pos)) continue;
    // A player who does not take penalties has no penalty order at all, so the
    // field would vanish for exactly the players you might want to promote.
    // Treat "no order" as zero so it stays editable.
    let base = player.inputs?.[f.k];
    if ((base === null || base === undefined) && f.k === "penalties_order") base = 0;
    if (base === null || base === undefined) continue;
    // An adjusted player opens on what he is being scored at, with the model's
    // own figure kept beside it -- the slider is the live value, and the note
    // says where it came from.
    const prevSeason = player.raw_inputs?.[f.k];
    const form = f.k === "p_start" ? startFormHint(player) : "";
    if (REBALANCED_FIELDS.has(f.k) && player.adjusted) {
      const hint = [`rebalanced from ${base.toFixed(f.dp)}`, form].filter(Boolean).join(" · ");
      out.push({ f, base: player[f.k], hint });
      continue;
    }
    out.push({ f, base, prevSeason, hint: form || undefined });
  }
  return out;
}

export function renderEditorFields() {
  const p = S.byId.get(editingId);
  const box = $("#edFields");
  box.innerHTML = "";
  for (const { f, base, hint, prevSeason } of editableFields(p)) {
    renderField(box, f, base, editBuffer, "ed_",
                () => { previewEdit(); renderMatches(); }, hint, prevSeason);
  }
}

/* Per-match rows. The season value is the baseline each row is measured
   against, not the model's — a match override is an exception to what you have
   already said about the player, and showing it against the model would report
   "changed" for a week you never touched. */
const expandedMatches = new Set();

function seasonValue(player, key, base) {
  return editBuffer[key] !== undefined ? editBuffer[key] : base;
}

export function renderMatches() {
  const p = S.byId.get(editingId);
  const box = $("#edMatches");
  box.innerHTML = "";
  editBuffer.gw = editBuffer.gw || {};
  const fields = editableFields(p);

  let touched = 0;
  S.gameweeks.forEach((gw, index) => {
    const key = String(gw);
    const store = editBuffer.gw[key] || {};
    const opponent = p.opp[index] || "";
    const set = Object.keys(store).length > 0;
    if (set) touched++;

    const row = document.createElement("div");
    row.className = "mrow" + (set ? " set" : "");
    row.innerHTML = `
      <div class="mtop">
        <span class="mgw">GW${gw}</span>
        <span class="mopp">${opponent || `<span class="mblank">no fixture</span>`}</span>
        <span class="mpts">${fmt(p.gw[index], 1)}</span>
        <button class="more" aria-expanded="false" title="All fields for GW${gw}"
                aria-label="All fields for GW${gw}">⋯</button>
      </div>`;
    box.appendChild(row);
    if (!opponent) return;   // nothing to override in a blank gameweek

    const commit = () => {
      if (!Object.keys(store).length) delete editBuffer.gw[key];
      else editBuffer.gw[key] = store;
      previewEdit();
      row.classList.toggle("set", Object.keys(store).length > 0);
      $("#edMatchCount").textContent = matchCountLabel();
    };

    // The inline lever: start probability, which is what a per-match opinion
    // almost always is. Baseline is the season value, so dragging it to the
    // same number as the season setting clears the override rather than
    // recording a redundant one.
    const pStartBase = fields.find((x) => x.f.k === "p_start");
    if (pStartBase) {
      const base = seasonValue(p, "p_start", pStartBase.base);
      const value = store.p_start !== undefined ? store.p_start : base;
      const line = document.createElement("div");
      line.className = "mstart";
      line.innerHTML = `
        <span class="lab">start</span>
        <input type="range" min="0" max="1" step="0.01" value="${value}"
               aria-label="Start probability in GW${gw}">
        <span class="val">${(+value).toFixed(2)}</span>`;
      row.appendChild(line);
      const range = line.querySelector("input"), out = line.querySelector(".val");
      range.addEventListener("input", (e) => {
        const n = +e.target.value;
        out.textContent = n.toFixed(2);
        if (Math.abs(n - base) < 1e-9) delete store.p_start;
        else { store.p_start = n; for (const k of MINUTES_CLEARS.p_start) delete store[k]; }
        commit();
        // exp_minutes lives in the collapsible extra fields, not this inline
        // row -- if it was showing "changed" it just went stale, so refresh it.
        if (expandedMatches.has(key)) fill();
      });
    }

    const more = row.querySelector(".more");
    const extra = document.createElement("div");
    extra.className = "mextra hidden";
    row.appendChild(extra);
    if (expandedMatches.has(key)) { extra.classList.remove("hidden"); more.setAttribute("aria-expanded", "true"); }

    const fill = () => {
      extra.innerHTML = "";
      for (const { f, base } of fields) {
        if (f.k === "p_start") continue;   // already inline above
        renderField(extra, f, seasonValue(p, f.k, base), store, `m${gw}_`, commit);
      }
      const reset = document.createElement("button");
      reset.className = "mini";
      reset.textContent = `Clear GW${gw}`;
      reset.addEventListener("click", () => {
        for (const k of Object.keys(store)) delete store[k];
        delete editBuffer.gw[key];
        renderMatches(); previewEdit();
      });
      extra.appendChild(reset);
    };
    if (expandedMatches.has(key)) fill();

    more.addEventListener("click", () => {
      const open = extra.classList.toggle("hidden");
      more.setAttribute("aria-expanded", String(!open));
      if (open) expandedMatches.delete(key);
      else { expandedMatches.add(key); fill(); }
    });
  });

  $("#edMatchCount").textContent = matchCountLabel();
}

function matchCountLabel() {
  const n = Object.values(editBuffer.gw || {}).filter((v) => Object.keys(v).length).length;
  return n ? `${n} match${n === 1 ? "" : "es"} overridden` : "none set";
}

// Still debounced, though the recompute is now local and takes under a
// millisecond: without it, dragging a slider would rerender the drawer on every
// pixel of travel, and that is the part that costs.
export function previewEdit() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    if (editingId === null) return;
    const p = S.byId.get(editingId);
    // Against the model's own number, not against whatever this player is
    // currently showing -- once an edit is applied, comparing to the applied
    // value reports every override as "unchanged from the model".
    const before = p.model_xpts_plan ?? p.xpts_plan;
    let r;
    try {
      checkOverridable(editBuffer, S.snapshot);
      r = editPlayer(S.snapshot, editingId, scoringEdit(p, editBuffer), planOpts());
    } catch (error) {
      // An illegal buffer is shown, not committed. Committing it would send it
      // straight to recomputeEdited, which drops rejected edits and alerts --
      // so half-typed nonsense would fire a dialog at you mid-drag.
      clearTimeout(commitTimer);
      $("#edPts").innerHTML = `<span class="down">${error.message}</span>`;
      return;
    }
    // Legal, so it is a real edit: schedule the commit that used to be Apply.
    scheduleCommit();
    const d = r.xpts_plan - before;
    $("#edPts").innerHTML = `<b>${r.xpts_plan.toFixed(1)}</b> projected points`
      + (Math.abs(d) > 0.05
        ? ` <span class="${d > 0 ? "up" : "down"}">${d > 0 ? "+" : ""}${d.toFixed(1)} vs the model</span>`
        : ` <span class="orig">unchanged from the model</span>`);
    // Both of these are of the *edited* player, so they move with the sliders.
    renderExplain();
    renderDrawerClub();
    // Repaint the per-match rows so each gameweek shows what it is now worth.
    const live = S.byId.get(editingId);
    if (live) {
      live.gw.forEach((_, i) => {
        const cell = $("#edMatches").children[i]?.querySelector(".mpts");
        if (cell) cell.textContent = fmt(r.gw[i], 1);
      });
    }
  }, 140);
}

/** Strip the bookkeeping an empty per-match map leaves behind, so `{gw:{}}`
    never counts as an edit and never reaches the CSV. */
function tidy(buffer) {
  const out = {};
  for (const [k, v] of Object.entries(buffer)) {
    if (k !== "gw") { out[k] = v; continue; }
    const gw = {};
    for (const [week, fields] of Object.entries(v || {})) {
      if (fields && Object.keys(fields).length) gw[week] = fields;
    }
    if (Object.keys(gw).length) out.gw = gw;
  }
  return out;
}

/* Commit the drawer's buffer into S.edits, which persists it and rebuilds the
   pool. Debounced separately from previewEdit and more slowly on purpose: the
   preview is a few numbers inside the drawer and is worth redrawing the instant
   a slider settles, while a commit repaints the whole board and re-runs the
   MILP, and doing that mid-drag is how a phone starts to feel like treacle. */
export function scheduleCommit() {
  clearTimeout(commitTimer);
  commitTimer = setTimeout(() => commitEdit(), 550);
}

export async function commitEdit({ immediate = false } = {}) {
  clearTimeout(commitTimer);
  if (editingId === null) return;
  const id = editingId;
  const tidied = tidy(editBuffer);
  // Nothing changed — skip the rebuild rather than repaint the board because a
  // drawer was opened and closed.
  if (JSON.stringify(S.edits[id] ?? null) === JSON.stringify(
      Object.keys(tidied).length ? tidied : null)) return;
  if (Object.keys(tidied).length) S.edits[id] = tidied;
  else delete S.edits[id];
  await recomputeEdited([id], { solveDelay: immediate ? 0 : 400 });
  renderAll();
}

/* Undo is against the drawer's opening state, not against the model. It is the
   button that makes live commit safe to have: you can drag a slider to see what
   it does, decide it was wrong, and get back exactly what you had. */
export async function undoEdit() {
  if (editingId === null) return;
  const id = editingId;
  clearTimeout(commitTimer);
  editBuffer = editBaseline ? JSON.parse(JSON.stringify(editBaseline)) : {};
  if (editBaseline) S.edits[id] = JSON.parse(JSON.stringify(editBaseline));
  else delete S.edits[id];
  expandedMatches.clear();
  renderEditorFields(); renderMatches(); previewEdit();
  await recomputeEdited([id], { solveDelay: 0 });
  renderAll();
}

/* Edits are applied by rebuilding the derived pool, which is the same code path
   that produced it in the first place. There is no second "recompute one
   player" route that could disagree with the first — which is exactly how the
   server version came to show one number and hand the optimiser another. */
export function recomputeEdited(ids, { solveDelay = 0, sync = true } = {}) {
  const failed = [];
  console.log(`[recompute] ${ids.length} id(s), sync=${sync}, snapshot=${!!S.snapshot}`);
  for (const id of ids) {
    // A local edit (sync !== false) touches this id right now, whether it
    // ended up set or cleared -- that timestamp is what lets a future pull's
    // merge tell "this device's state for this id is newer" from "it just
    // never heard about the other device's change". Edits adopted from a pull
    // already carry the merge's own timestamp and must not be re-stamped
    // here, or every pull would look locally-authored on the next merge.
    if (sync) S.editsAt[id] = Date.now();
    const fields = S.edits[id];
    if (!fields) continue;
    // Without a snapshot there is nothing to validate against, and "cannot
    // check" must never be answered the same way as "checked and rejected" --
    // that conflation is what deleted a whole set of overrides.
    if (!S.snapshot) continue;
    try {
      checkOverridable(fields, S.snapshot);
    } catch (error) {
      // A rejected edit is dropped so the board and the solver keep looking at
      // the same numbers. What it must NOT do is stamp the drop as a deletion.
      //
      // "This device cannot validate that field" is a fact about this device's
      // snapshot, not about the edit. An override naming a field a newer
      // snapshot added -- mins_if_start, say -- fails here on any device still
      // holding an older one, and stamping it made that local incompatibility
      // outrank the edit itself: the id then read as a deliberate deletion,
      // beat every older copy on every device, and the override was gone
      // everywhere. That is not hypothetical; it is how thirty of them were
      // lost twice over.
      //
      // Leaving the timestamp alone means the edit stays live wherever it
      // still validates, and this device simply carries none for that id --
      // "no opinion" rather than "removed". A real deletion is still stamped,
      // because that goes through the sync branch above.
      failed.push(`${S.byId.get(id)?.name || id}: ${error.message}`);
      delete S.edits[id];
      if (sync) S.editsAt[id] = Date.now();
    }
  }
  // sync: false is for edits just adopted from a pullSyncState() pull -- they
  // came from another device's push, so writing them back to localStorage is
  // right, but re-marking them via markSynced() would re-timestamp them with
  // this device's clock and immediately push them straight back out, which
  // both is a pointless round trip and -- if this device's checkOverridable
  // above dropped one on a stale/mismatched local snapshot -- would broadcast
  // that smaller set as authoritative and erase the edit for everyone.
  if (sync) saveEdits();
  else {
    saveLocal(STORE.edits, S.edits); saveLocal(STORE.editsAt, S.editsAt);
    snapshotEditsHistory(); pushOverrides();
  }
  // Logged unconditionally, before the alert -- an alert only ever reaches
  // someone sitting at the screen when it fires, and this whole investigation
  // has been guessing at what a `catch` swallowed. The console line survives
  // even when nothing is watching, and even when rebuildPool() below throws.
  if (failed.length) console.warn("[recompute] rejected:", failed);
  // Reported before rebuilding, not after. rebuildPool() can throw, and when it
  // did the alert never ran -- so the one message that would have named the
  // dropped overrides was lost to the very failure that dropped them, and the
  // loss looked silent from the outside.
  if (failed.length) {
    alert("These edits were rejected and have been dropped:\n\n" + failed.join("\n"));
  }
  try {
    rebuildPool();
  } catch (error) {
    console.error("[recompute] rebuildPool() threw:", error);
    throw error;
  }
  // The solver's answer was computed against the old numbers, so it is no
  // longer a fair comparison. Drop it and solve again.
  S.optimal = []; S.optimalPts = null; S.optimalCost = null; S.optimalBench = {};
  renderEditBanner();
  scheduleSolve(solveDelay);
}

export function renderEditBanner() {
  const n = Object.keys(S.edits).length;
  // The History button lives in this banner, and the banner used to be hidden
  // whenever there were no edits -- which is precisely when you need it. An
  // override set wiped by a bad merge left no edits, so the banner vanished,
  // so the one control that could restore it was unreachable. If this device
  // has a rolling backup, the banner stays up and says so.
  // Only sets with something in them are worth offering to restore -- history
  // now records the moment a set went empty too, and "3 earlier sets" reading
  // as an offer when one of them is the wipe itself would be a lie.
  const restorable = loadLocal(STORE.editsHistory, [])
    .filter((s) => s.snapshot && s.snapshot !== "{}").length;
  $("#editBanner").classList.toggle("hidden", n === 0 && !restorable);
  $("#clearEdits").classList.toggle("hidden", n === 0);
  if (!n) {
    $("#editCount").innerHTML = `No overrides on this device — `
      + `<b>${restorable}</b> earlier ${restorable === 1 ? "set is" : "sets are"} `
      + `saved here and can be restored.`;
    $("#editSaved").textContent = "";
    return;
  }
  const names = Object.keys(S.edits).map((id) => S.byId.get(+id)?.name).filter(Boolean);
  $("#editCount").innerHTML = `<span class="edited-dot">●</span> <b>${n}</b> edited `
    + `${n === 1 ? "player" : "players"}: ${names.join(", ")}`;
  // Says where the edits are, not whether you remembered to save them: they are
  // always saved on this device, and this line is only about the CLI's copy.
  $("#editSaved").textContent = csvStateRef() ? `· ${csvStateRef()}` : "";
}
// csvState lives in sync.mjs as a live-updating export; read through a getter
// so this file's own import block does not need to track its every mutation.
import { csvState as _csvState } from "/assets/sync.mjs";
function csvStateRef() { return _csvState; }

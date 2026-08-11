/* Preparation and presentation for the transfer-and-chip planner: everything
 * around the solve that isn't the solve itself.
 *
 * `transfers.js` (a classic script, not a module — see its own header for why)
 * builds and solves the MILP; this module reduces the full player pool to
 * something that MILP can afford to consider, and turns its solved output
 * back into the numbers a person reads. Split this way so the LP engine stays
 * pure mechanics and the "which players are even worth modelling" and "was
 * this chip's gameweek actually a good one" judgment calls — the parts that
 * change if the model's reasoning changes — live in one place, in plain JS.
 *
 * `pointsByPlayer` throughout is a `Map<id, number[]>`: one player's raw
 * points survival-adjusted (not decayed) across the solve window, index-
 * aligned to the `gameweeks` array in scope — the same quantity
 * `transfers.expected_points` computes in Python (`raw * survival`, distinct
 * from `planning.weighted_points`'s decayed version).
 *
 * Ports `transfers.py`'s `candidate_pool`, the `captain_pool` line in
 * `plan_transfers`, `free_transfer_value`, `chip_slots`, `_chip_report` and
 * `_pair_moves`. Verified against the Python by scripts/verify-transfer-port.mjs.
 */

const POSITIONS = ["GKP", "DEF", "MID", "FWD"];

const FORMATIONS = (() => {
  const out = [];
  for (let d = 3; d <= 5; d++) for (let m = 2; m <= 5; m++) for (let f = 1; f <= 3; f++)
    if (d + m + f === 10) out.push([d, m, f]);
  return out;
})();

/** Best legal XI from `rows` ({id, pos, score}), same tie-break as the board's
 *  own `bestXI` and `planning._best_xi_ids`: sort each position by score
 *  descending, then take the highest-scoring legal formation. */
function bestXI(rows) {
  const by = { GKP: [], DEF: [], MID: [], FWD: [] };
  for (const r of rows) by[r.pos]?.push(r);
  for (const k of POSITIONS) by[k].sort((a, b) => b.score - a.score);
  if (!by.GKP.length) return { ids: [], total: 0 };

  let best = null;
  for (const [d, m, f] of FORMATIONS) {
    if (by.DEF.length < d || by.MID.length < m || by.FWD.length < f) continue;
    const chosen = [by.GKP[0], ...by.DEF.slice(0, d), ...by.MID.slice(0, m), ...by.FWD.slice(0, f)];
    const total = chosen.reduce((a, c) => a + c.score, 0);
    if (!best || total > best.total) best = { total, ids: chosen.map((c) => c.id) };
  }
  return best || { ids: [], total: 0 };
}

/** One player's raw per-gameweek points, discounted by his own survival curve
 *  only — no plan decay. Port of `transfers.expected_points`'s per-player
 *  term. The first gameweek is never discounted, matching
 *  `planning.survival_curve`. */
export function survivalAdjusted(player, gwCount) {
  const hazard = player.hazard || 0;
  const n = Math.min(gwCount, player.gw.length);
  const out = new Array(n);
  for (let i = 0; i < n; i++) out[i] = (player.gw[i] || 0) * Math.pow(1 - hazard, i);
  return out;
}

const sum = (arr) => arr.reduce((a, b) => a + b, 0);

/** Shrink the full player list to what the transfer planner can afford to
 *  consider. Port of `transfers.candidate_pool`: per position, the union of
 *  the top `cap` players by total points over the window and the top
 *  `max(cap/2, 6)` by points-per-million, plus anyone in `keep`
 *  unconditionally — a plan that cannot see your own squad cannot tell you to
 *  sell it. `caps` is `rules.POOL_BY_POS`. */
export function candidatePool(players, pointsByPlayer, { keep = [], minMinutesProb = 0,
                                                          exclude = [], caps } = {}) {
  const keepSet = new Set(keep);
  const excludeSet = new Set(exclude);
  const windowPoints = new Map();
  for (const p of players) if (pointsByPlayer.has(p.id)) windowPoints.set(p.id, sum(pointsByPlayer.get(p.id)));

  let pool = players.filter((p) => windowPoints.has(p.id));
  pool = pool.filter((p) => keepSet.has(p.id) || minMinutesProb <= 0 || (p.p_play ?? 0) >= minMinutesProb);
  if (excludeSet.size) pool = pool.filter((p) => keepSet.has(p.id) || !excludeSet.has(p.id));

  const chosen = new Map();
  for (const [pos, cap] of Object.entries(caps)) {
    const block = pool.filter((p) => p.pos === pos);
    const byPoints = block.slice()
      .sort((a, b) => windowPoints.get(b.id) - windowPoints.get(a.id))
      .slice(0, cap);
    const value = (p) => (p.price > 0 ? windowPoints.get(p.id) / p.price : -Infinity);
    const byValue = block.slice()
      .sort((a, b) => value(b) - value(a))
      .slice(0, Math.max(Math.floor(cap / 2), 6));
    const forced = block.filter((p) => keepSet.has(p.id));
    for (const p of [...byPoints, ...byValue, ...forced]) chosen.set(p.id, p);
  }
  return [...chosen.values()];
}

/** The players a captain binary is worth modelling for — the top `n` by total
 *  window points. Port of the `captain_pool` line in `plan_transfers`;
 *  generous enough the constraint never binds on anyone the model would
 *  actually pick. */
export function captainPool(pool, pointsByPlayer, n) {
  return pool.slice()
    .sort((a, b) => sum(pointsByPlayer.get(b.id) || []) - sum(pointsByPlayer.get(a.id) || []))
    .slice(0, n)
    .map((p) => p.id);
}

/** Cumulative worth of holding s banked transfers, s in 0..maxFreeTransfers.
 *  Port of `transfers.free_transfer_value`. `ftValueByState` is
 *  `rules.FT_VALUE_BY_STATE` (JSON-keyed by string, read here with numeric
 *  keys — JS coerces both the same way on plain-object access). */
export function freeTransferValue(ftValue, ftValueByState, maxFreeTransfers) {
  const value = { 0: 0 };
  let running = 0;
  for (let state = 1; state <= maxFreeTransfers; state++) {
    running += ftValueByState[state] ?? ftValue;
    value[state] = running;
  }
  return value;
}

/** Gameweeks in the horizon where each chip may legally be played, keyed by
 *  chip. A chip with no legal gameweek is dropped entirely. Port of
 *  `transfers.chip_slots`. `chips` is `rules.CHIPS`. */
export function chipSlots(windows, gameweeks, chipsUsed, chips) {
  const used = new Set(chipsUsed || []);
  const slots = {};
  for (const [chip, window] of Object.entries(windows || {})) {
    if (used.has(chip) || !chips.includes(chip)) continue;
    const [start, stop] = window;
    const allowed = gameweeks.filter((gw) => gw >= start && gw <= stop);
    if (allowed.length) slots[chip] = allowed;
  }
  return slots;
}

/** Which gameweeks in the window contain a double or a blank, e.g.
 *  `{7: "2 double, 3 blank"}`. Port of `transfers.fixture_variation`. */
export function fixtureVariation(fixtures, gameweeks) {
  const teams = new Set();
  for (const f of fixtures) { teams.add(f.home_team); teams.add(f.away_team); }
  const counts = new Map();
  for (const team of teams) counts.set(team, new Map(gameweeks.map((g) => [g, 0])));
  for (const f of fixtures) {
    if (!gameweeks.includes(f.gw)) continue;
    const home = counts.get(f.home_team), away = counts.get(f.away_team);
    home.set(f.gw, (home.get(f.gw) || 0) + 1);
    away.set(f.gw, (away.get(f.gw) || 0) + 1);
  }
  const marks = {};
  for (const gw of gameweeks) {
    let doubles = 0, blanks = 0;
    for (const [, byGw] of counts) {
      const n = byGw.get(gw) || 0;
      if (n >= 2) doubles++;
      else if (n === 0) blanks++;
    }
    if (doubles || blanks) marks[gw] = `${doubles} double, ${blanks} blank`;
  }
  return marks;
}

function median(values) {
  if (!values.length) return NaN;
  const sorted = values.slice().sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** Match sales to purchases within a position, then whatever is left over —
 *  presentation only, the solver books a set of sales and a set of purchases
 *  with a pooled budget, not "which sale funded which buy". Port of
 *  `transfers._pair_moves`. Returns `[[outId|null, inId|null], ...]`. */
export function pairMoves(outIds, inIds, positions) {
  const pairs = [];
  const leftoverOut = [];
  const remaining = inIds.slice();
  for (const outId of outIds.slice().sort((a, b) => a - b)) {
    const idx = remaining.findIndex((id) => positions.get(id) === positions.get(outId));
    if (idx === -1) { leftoverOut.push(outId); continue; }
    pairs.push([outId, remaining[idx]]);
    remaining.splice(idx, 1);
  }
  while (leftoverOut.length || remaining.length) {
    pairs.push([leftoverOut.length ? leftoverOut.shift() : null,
               remaining.length ? remaining.shift() : null]);
  }
  return pairs;
}

/** What each chip is worth, and whether the gameweek it wants (if any) is a
 *  real choice — read off the *solved* path, not decided here. Port of
 *  `transfers._chip_report`.
 *
 *  `chips`: `{chip: [allowed gws]}` from `chipSlots`.
 *  `chipByGw`: `Map<gw, chip>` — which chip (if any) the solve played each
 *  gameweek.
 *  `squads`: `Map<gw, id[]>` — the fifteen the solve held that gameweek.
 *  `pointsByPlayer`, `gameweeks`: as elsewhere in this module.
 *  `positions`: `Map<id, pos>`.
 *  `variation`: from `fixtureVariation`.
 *  `skipped`: `{chip: reason}` for chips dropped before the solve (e.g. a
 *  free hit with no blank or double to hit).
 *  `chipLabels`: `rules.CHIP_LABELS`.
 */
export function chipReport({ chips, chipByGw, squads, pointsByPlayer, gameweeks, positions,
                            variation, skipped, chipLabels }) {
  const scoreAt = (id, gw) => {
    const arr = pointsByPlayer.get(id);
    return arr ? (arr[gameweeks.indexOf(gw)] || 0) : 0;
  };

  const payouts = { bboost: {}, "3xc": {} };
  for (const gw of gameweeks) {
    const held = (squads.get(gw) || []).filter((id) => pointsByPlayer.has(id));
    const rows = held.map((id) => ({ id, pos: positions.get(id), score: scoreAt(id, gw) }));
    const xi = bestXI(rows);
    const xiSet = new Set(xi.ids);
    const heldTotal = sum(rows.map((r) => r.score));
    const xiTotal = sum(rows.filter((r) => xiSet.has(r.id)).map((r) => r.score));
    payouts.bboost[gw] = heldTotal - xiTotal;
    payouts["3xc"][gw] = xi.ids.length ? Math.max(...xi.ids.map((id) => scoreAt(id, gw))) : 0;
  }

  const rows = [];
  for (const [chip, why] of Object.entries(skipped || {})) {
    rows.push({ chip, label: chipLabels[chip], gw: null, worth: null, edge: null, verdict: why });
  }
  for (const [chip, allowed] of Object.entries(chips)) {
    const playedGw = [...chipByGw.entries()].find(([, c]) => c === chip)?.[0] ?? null;
    const series = payouts[chip] || {};
    const windowVals = allowed.filter((g) => g in series).map((g) => series[g]);

    if (playedGw === null) {
      rows.push({ chip, label: chipLabels[chip], gw: null, worth: null, edge: null,
                 verdict: "hold — beaten by keeping it" });
      continue;
    }
    if (!Object.keys(series).length) {
      rows.push({ chip, label: chipLabels[chip], gw: playedGw, worth: null, edge: null,
                 verdict: "structural — priced through the squad it lets you buy, not a per-gameweek payout" });
      continue;
    }
    const worth = series[playedGw];
    const edge = windowVals.length ? worth - median(windowVals) : null;
    let verdict;
    if (!variation || !Object.keys(variation).length) verdict = "hold — nothing to time against";
    else if (edge === null || !Number.isFinite(edge) || edge < 1.0) verdict = "weak — no better than any other week";
    else verdict = "timed against a double or blank";
    rows.push({ chip, label: chipLabels[chip], gw: playedGw, worth, edge, verdict });
  }
  return rows;
}

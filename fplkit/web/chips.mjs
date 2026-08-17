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

/** What a bench boost and a triple captain would pay in every gameweek of the
 *  window, measured against the squad the solve *actually holds* that week —
 *  `{chip: {gw: points}}`.
 *
 *  Read the caveat before using these as a comparison across gameweeks: only
 *  the week the chip was played has a squad built for it. Every other column
 *  prices the chip against a fifteen assembled for a different purpose, so the
 *  played week is flattered and the alternatives are understated — on a real
 *  snapshot the gap ran to 2.0 points, enough to flip the sign of the `edge`
 *  in `chipReport`. Answering "which week is best?" honestly needs one solve
 *  per candidate week with the chip pinned there; that is what `chipReport`'s
 *  `resolved` argument carries, and this function is the cheap estimate used
 *  only when no such sweep has been run.
 *
 *  A bench boost is worth the bench *net of what the bench already earns*. A
 *  benched player is already scored at his slot's weight — he is the one who
 *  comes on when someone does not play — so the chip only buys the remaining
 *  `1 - weight` of him. Port of the payout block in `transfers._chip_report`.
 */
export function chipPayouts({ squads, pointsByPlayer, gameweeks, positions, slotWeight }) {
  const scoreAt = (id, gw) => {
    const arr = pointsByPlayer.get(id);
    return arr ? (arr[gameweeks.indexOf(gw)] || 0) : 0;
  };
  const outfield = Object.keys(slotWeight).filter((s) => s !== "GKP");

  const payouts = { bboost: {}, "3xc": {} };
  for (const gw of gameweeks) {
    const held = (squads.get(gw) || []).filter((id) => pointsByPlayer.has(id));
    const rows = held.map((id) => ({ id, pos: positions.get(id), score: scoreAt(id, gw) }));
    const xi = bestXI(rows);
    const xiSet = new Set(xi.ids);
    const benched = rows.filter((r) => !xiSet.has(r.id));

    // What the bench already earns from its slot weights: the chip only buys
    // the remaining 1 - weight of each, and the slot order is the one the
    // objective gives them — reserve keeper apart, then best-first.
    const spareGk = benched.filter((r) => r.pos === "GKP");
    const rest = benched.filter((r) => r.pos !== "GKP").sort((a, b) => b.score - a.score);
    let earned = sum(spareGk.map((r) => (slotWeight.GKP || 0) * r.score));
    rest.forEach((r, i) => { earned += (slotWeight[outfield[i]] || 0) * r.score; });

    payouts.bboost[gw] = sum(benched.map((r) => r.score)) - earned;
    payouts["3xc"][gw] = xi.ids.length ? Math.max(...xi.ids.map((id) => scoreAt(id, gw))) : 0;
  }
  return payouts;
}

/** What each chip is worth, and whether the gameweek it wants (if any) is a
 *  real choice — read off the *solved* path, not decided here. Port of
 *  `transfers._chip_report`, extended with the `resolved` sweep the Python
 *  side does not have.
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
 *  `forced`: chips the solve was made to play, which changes what the verdict
 *  claims — a forced chip's gameweek is the best one for it, not evidence that
 *  playing it beat holding it.
 *  `slotWeight`: `rules.DEFAULT_BENCH_SLOT_WEIGHTS`, for the netting above.
 *  Required rather than defaulted: defaulting it to nothing would quietly
 *  report gross bench points, which is the overstatement the netting exists to
 *  remove, and the caller has the snapshot's rules to hand either way.
 *  `resolved`: `{chip: {gw: {payout, objective}}}` from a sweep that re-solved
 *  the whole plan once per candidate gameweek with the chip pinned there —
 *  see `chipPayouts`'s caveat for why nothing else can honestly rank weeks.
 *  Absent for a chip means no sweep was run for it, and the row says so
 *  rather than quoting an `edge` computed from numbers that cannot bear it.
 *
 *  Each row carries `checked` (was a sweep run), and when it was, `bestObjGw`
 *  (the week the plan actually scores highest with) and `bestRawGw` (the week
 *  the chip pays most in undecayed points). The two differ whenever the decay
 *  is doing the choosing, which is the single most useful thing a reader can
 *  know about a chip's gameweek.
 */
export function chipReport({ chips, chipByGw, squads, pointsByPlayer, gameweeks, positions,
                            variation, skipped, chipLabels, forced = [], slotWeight,
                            resolved = {} }) {
  const forcedSet = new Set(forced);
  const payouts = chipPayouts({ squads, pointsByPlayer, gameweeks, positions, slotWeight });

  const rows = [];
  for (const [chip, why] of Object.entries(skipped || {})) {
    rows.push({ chip, label: chipLabels[chip], gw: null, worth: null, edge: null,
               checked: false, verdict: why });
  }
  for (const [chip, allowed] of Object.entries(chips)) {
    const playedGw = [...chipByGw.entries()].find(([, c]) => c === chip)?.[0] ?? null;
    const sweep = resolved[chip] || null;
    const checked = !!sweep && Object.keys(sweep).length > 1;
    // A swept chip is priced from its own pinned solves; an unswept one falls
    // back to the estimate, which is only ever quoted for the played week --
    // the one week it is not biased for.
    const series = sweep
      ? Object.fromEntries(Object.entries(sweep).map(([gw, v]) => [gw, v.payout]))
      : (payouts[chip] || {});
    const windowVals = allowed.filter((g) => g in series).map((g) => series[g]);

    const argmax = (pick) => {
      if (!sweep) return null;
      let bestGw = null, bestVal = -Infinity;
      for (const [gw, v] of Object.entries(sweep)) {
        const x = pick(v);
        if (Number.isFinite(x) && x > bestVal) { bestVal = x; bestGw = Number(gw); }
      }
      return bestGw;
    };
    const bestObjGw = argmax((v) => v.objective);
    const bestRawGw = argmax((v) => v.payout);

    if (playedGw === null) {
      rows.push({ chip, label: chipLabels[chip], gw: null, worth: null, edge: null,
                 checked, bestObjGw, bestRawGw, verdict: "hold — beaten by keeping it" });
      continue;
    }
    // The verdict leads with what the plan did, because a row that names a
    // gameweek and then reads "hold" is a contradiction a reader has to unpick.
    const lead = forcedSet.has(chip) ? "forced" : "play";
    if (!Object.keys(series).length) {
      rows.push({ chip, label: chipLabels[chip], gw: playedGw, worth: null, edge: null,
                 checked, bestObjGw, bestRawGw,
                 verdict: `${lead} — structural, priced through the squad it buys for that one week` });
      continue;
    }
    const worth = series[playedGw];

    // Without a sweep there is no honest cross-week comparison to make, so the
    // row makes none. Quoting an edge here is what let a flat calendar read as
    // "timed on a double or blank": every rival week was measured against a
    // squad built for a different one.
    if (!checked) {
      rows.push({ chip, label: chipLabels[chip], gw: playedGw, worth, edge: null,
                 checked: false, bestObjGw: null, bestRawGw: null,
                 verdict: `${lead} — other weeks not re-solved, so no timing claim` });
      continue;
    }

    const edge = windowVals.length ? worth - median(windowVals) : null;
    let timing;
    if (bestRawGw !== null && bestRawGw !== playedGw) {
      // The decay, not the fixtures, moved it. Say so plainly and name the
      // week that pays most before discounting -- it is the number a person
      // is actually asking for when they force a chip.
      timing = `earlier than its raw peak (GW${bestRawGw}) — the decay chose this week`;
    } else if (!variation || !Object.keys(variation).length) {
      timing = `best of ${Object.keys(sweep).length} weeks re-solved, on a flat calendar`;
    } else if (edge === null || !Number.isFinite(edge) || edge < 1.0) {
      timing = "no better than any other week";
    } else {
      timing = "timed on a double or blank";
    }
    rows.push({ chip, label: chipLabels[chip], gw: playedGw, worth, edge,
               checked: true, bestObjGw, bestRawGw, verdict: `${lead} — ${timing}` });
  }
  return rows;
}

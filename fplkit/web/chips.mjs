
const POSITIONS = ["GKP", "DEF", "MID", "FWD"];

const FORMATIONS = (() => {
  const out = [];
  for (let d = 3; d <= 5; d++) for (let m = 2; m <= 5; m++) for (let f = 1; f <= 3; f++)
    if (d + m + f === 10) out.push([d, m, f]);
  return out;
})();

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

export function survivalAdjusted(player, gwCount) {
  const hazard = player.hazard || 0;
  const n = Math.min(gwCount, player.gw.length);
  const out = new Array(n);
  for (let i = 0; i < n; i++) out[i] = (player.gw[i] || 0) * Math.pow(1 - hazard, i);
  return out;
}

const sum = (arr) => arr.reduce((a, b) => a + b, 0);

export function candidatePool(players, pointsByPlayer, { keep = [], minMinutesProb = 0,
                                                          exclude = [], caps,
                                                          pricePointCandidates = 3 } = {}) {
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
    const byPriceGroups = new Map();
    for (const p of block) {
      if (!byPriceGroups.has(p.price)) byPriceGroups.set(p.price, []);
      byPriceGroups.get(p.price).push(p);
    }
    const byPrice = [];
    for (const group of byPriceGroups.values()) {
      group.sort((a, b) => windowPoints.get(b.id) - windowPoints.get(a.id));
      byPrice.push(...group.slice(0, pricePointCandidates));
    }
    const forced = block.filter((p) => keepSet.has(p.id));
    for (const p of [...byPoints, ...byValue, ...byPrice, ...forced]) chosen.set(p.id, p);
  }
  return [...chosen.values()];
}

export function captainPool(pool, pointsByPlayer, n) {
  return pool.slice()
    .sort((a, b) => sum(pointsByPlayer.get(b.id) || []) - sum(pointsByPlayer.get(a.id) || []))
    .slice(0, n)
    .map((p) => p.id);
}

export function freeTransferValue(ftValue, ftValueByState, maxFreeTransfers) {
  const value = { 0: 0 };
  let running = 0;
  for (let state = 1; state <= maxFreeTransfers; state++) {
    running += ftValueByState[state] ?? ftValue;
    value[state] = running;
  }
  return value;
}

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

    const spareGk = benched.filter((r) => r.pos === "GKP");
    const rest = benched.filter((r) => r.pos !== "GKP").sort((a, b) => b.score - a.score);
    let earned = sum(spareGk.map((r) => (slotWeight.GKP || 0) * r.score));
    rest.forEach((r, i) => { earned += (slotWeight[outfield[i]] || 0) * r.score; });

    payouts.bboost[gw] = sum(benched.map((r) => r.score)) - earned;
    payouts["3xc"][gw] = xi.ids.length ? Math.max(...xi.ids.map((id) => scoreAt(id, gw))) : 0;
  }
  return payouts;
}

export function chipReport({ chips, chipByGw, squads, pointsByPlayer, gameweeks, positions,
                            variation, skipped, chipLabels, forced = [], slotWeight,
                            resolved = {}, pinned = {} }) {
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
    const pin = pinned[chip] ?? null;
    const lead = pin !== null ? "your week" : forcedSet.has(chip) ? "forced" : "play";
    if (!Object.keys(series).length) {
      rows.push({ chip, label: chipLabels[chip], gw: playedGw, worth: null, edge: null,
                 checked, bestObjGw, bestRawGw,
                 verdict: `${lead} — structural, priced through the squad it buys for that one week` });
      continue;
    }
    const worth = series[playedGw];

    if (!checked) {
      rows.push({ chip, label: chipLabels[chip], gw: playedGw, worth, edge: null,
                 checked: false, bestObjGw: null, bestRawGw: null,
                 verdict: `${lead} — other weeks not re-solved, so no timing claim` });
      continue;
    }

    const edge = windowVals.length ? worth - median(windowVals) : null;
    const weeks = Object.keys(sweep).length;
    let timing;
    if (pin !== null) {
      const cost = (sweep[bestObjGw]?.objective ?? 0) - (sweep[pin]?.objective ?? 0);
      timing = bestObjGw === pin
        ? `also the best of ${weeks} weeks re-solved`
        : `GW${bestObjGw} scores ${cost.toFixed(1)} more over the window`;
    } else if (bestRawGw !== null && bestRawGw !== playedGw) {
      timing = `${bestRawGw > playedGw ? "earlier" : "later"} than its raw peak (GW${bestRawGw})`
        + " — the decay chose this week";
    } else if (!variation || !Object.keys(variation).length) {
      timing = `best of ${weeks} weeks re-solved, on a flat calendar`;
    } else if (edge === null || !Number.isFinite(edge) || edge < 1.0) {
      timing = "no better than any other week";
    } else {
      timing = "timed on a double or blank";
    }
    rows.push({ chip, label: chipLabels[chip], gw: playedGw, worth, edge,
               checked: true, bestObjGw, bestRawGw, pinned: pin,
               verdict: `${lead} — ${timing}` });
  }
  return rows;
}

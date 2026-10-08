
(function (root) {
  "use strict";

  const SLOTS = ["GKP", "1", "2", "3"];

  const n = (v) => String(Math.round(v * 1e6) / 1e6);
  const term = (coef, name) => `${coef < 0 ? "-" : "+"} ${n(Math.abs(coef))} ${name}`;

  function buildLp(pool, opt) {
    opt = opt || {};
    const budget = opt.budget ?? 100;
    const benchSlotWeights = opt.benchSlotWeights || null;
    const benchWeight = opt.benchWeight ?? 0.12;
    const profile = opt.benchSlotProfile || { GKP: 0.25, "1": 2.0, "2": 0.85, "3": 0.35 };
    const ownershipWeight = opt.ownershipWeight ?? 0;
    const captainMultiplier = opt.captainMultiplier ?? 2;
    const minStart = opt.minStart ?? 0;
    const include = opt.include || [], exclude = opt.exclude || [];
    const maxPerClub = opt.maxPerClub ?? 3;
    const formation = opt.formation || null;
    const squadByPos = opt.squadByPos || { GKP: 2, DEF: 5, MID: 5, FWD: 3 };
    const xiMin = opt.xiMin || { GKP: 1, DEF: 3, MID: 2, FWD: 1 };
    const xiMax = opt.xiMax || { GKP: 1, DEF: 5, MID: 5, FWD: 3 };
    const squadSize = opt.squadSize ?? 15, xiSize = opt.xiSize ?? 11;

    const inc = new Set(include), exc = new Set(exclude);
    let players = pool.filter((p) => !exc.has(p.id));
    if (minStart > 0) players = players.filter((p) => p.p_play >= minStart || inc.has(p.id));

    const have = new Set(players.map((p) => p.id));
    const missing = include.filter((id) => !have.has(id));
    if (missing.length) {
      throw new Error(`Forced-in players are not in the pool: ${missing.join(", ")}`);
    }

    const slotWeight = {};
    for (const s of SLOTS) {
      const given = benchSlotWeights ? benchSlotWeights[s] : undefined;
      slotWeight[s] = given === undefined || given === null
        ? benchWeight * profile[s] : Number(given);
    }

    const S = (p) => `s_${p.id}`, X = (p) => `x_${p.id}`, C = (p) => `c_${p.id}`;
    const B = (p, s) => `b${s}_${p.id}`;
    const eligible = (p, s) => (p.pos === "GKP") === (s === "GKP");

    const obj = [];
    for (const p of players) {
      const pts = p.pts || 0;
      if (pts) {
        obj.push(term(pts, X(p)));
        if (captainMultiplier !== 1) obj.push(term(pts * (captainMultiplier - 1), C(p)));
      }
      if (ownershipWeight) {
        const tilt = ownershipWeight * ((p.own || 0) / 100) * pts;
        if (tilt) obj.push(term(tilt, S(p)));
      }
      for (const s of SLOTS) {
        if (!eligible(p, s)) continue;
        const w = slotWeight[s] * pts;
        if (w) obj.push(term(w, B(p, s)));
      }
    }
    if (!obj.length) obj.push("0 zero_obj");

    const cons = [];
    for (const s of SLOTS) {
      const elig = players.filter((p) => eligible(p, s));
      cons.push(`slot${s}: ${elig.map((p) => `+ ${B(p, s)}`).join(" ")} = 1`);
    }
    for (const p of players) {
      const bs = SLOTS.filter((s) => eligible(p, s)).map((s) => `+ ${B(p, s)}`).join(" ");
      cons.push(`bn_${p.id}: ${bs} - ${S(p)} + ${X(p)} = 0`);
    }
    cons.push(`size: ${players.map((p) => `+ ${S(p)}`).join(" ")} = ${squadSize}`);
    cons.push(`cost: ${players.map((p) => term(p.price, S(p))).join(" ")} <= ${n(budget)}`);
    cons.push(`xisize: ${players.map((p) => `+ ${X(p)}`).join(" ")} = ${xiSize}`);
    cons.push(`capt: ${players.map((p) => `+ ${C(p)}`).join(" ")} = 1`);
    for (const p of players) {
      cons.push(`xi_${p.id}: + ${X(p)} - ${S(p)} <= 0`);
      cons.push(`cp_${p.id}: + ${C(p)} - ${X(p)} <= 0`);
    }
    for (const pos of Object.keys(squadByPos)) {
      const members = players.filter((p) => p.pos === pos);
      cons.push(`sq${pos}: ${members.map((p) => `+ ${S(p)}`).join(" ")} = ${squadByPos[pos]}`);
      const xi = members.map((p) => `+ ${X(p)}`).join(" ");
      if (formation && formation[pos] !== undefined && formation[pos] !== null) {
        cons.push(`fm${pos}: ${xi} = ${formation[pos]}`);
      } else {
        cons.push(`xmn${pos}: ${xi} >= ${xiMin[pos]}`);
        cons.push(`xmx${pos}: ${xi} <= ${xiMax[pos]}`);
      }
    }
    const teams = [...new Set(players.map((p) => p.team))];
    teams.forEach((team, i) => {
      const members = players.filter((p) => p.team === team);
      cons.push(`cl${i}: ${members.map((p) => `+ ${S(p)}`).join(" ")} <= ${maxPerClub}`);
    });
    include.forEach((id, i) => cons.push(`inc${i}: + s_${id} = 1`));

    const bin = [];
    for (const p of players) {
      bin.push(S(p), X(p), C(p));
      for (const s of SLOTS) if (eligible(p, s)) bin.push(B(p, s));
    }

    return {
      lp: `Maximize\n obj: ${obj.join(" ")}\nSubject To\n ${cons.join("\n ")}\n`
        + `Binary\n ${bin.join(" ")}\nEnd`,
      players,
      slotWeight,
      captainMultiplier,
    };
  }

  function readSolution(result, players, slotWeight, captainMultiplier = 2) {
    const on = (name) => (result.Columns[name]?.Primal ?? 0) > 0.5;
    const squad = players.filter((p) => on(`s_${p.id}`));
    const bench = {};
    for (const p of squad) {
      for (const s of SLOTS) if (on(`b${s}_${p.id}`)) bench[p.id] = s;
    }
    const starting = squad.filter((p) => on(`x_${p.id}`));
    const captain = squad.find((p) => on(`c_${p.id}`)) || null;
    const xiPoints = starting.reduce((a, p) => a + (p.pts || 0), 0)
                   + (captain ? (captainMultiplier - 1) * (captain.pts || 0) : 0);
    return {
      squad: squad.map((p) => p.id),
      starting: starting.map((p) => p.id),
      captain: captain ? captain.id : null,
      bench,
      cost: Math.round(squad.reduce((a, p) => a + p.price, 0) * 10) / 10,
      xi_points: xiPoints,
      bench_points: squad.filter((p) => !on(`x_${p.id}`)).reduce((a, p) => a + (p.pts || 0), 0),
      objective: result.ObjectiveValue,
      slot_weight: slotWeight,
    };
  }

  let highsPromise = null;

  function loadHighs(base) {
    if (highsPromise) return highsPromise;
    base = base || "./vendor/";
    highsPromise = (async () => {
      let factory = null;

      if (typeof module === "object" && module.exports && typeof require === "function") {
        factory = require(base + "highs.js");
      } else if (typeof importScripts === "function") {
        importScripts(base + "highs.js");
        factory = root.Module;
      } else {
        await new Promise((resolve, reject) => {
          const tag = root.document.createElement("script");
          tag.src = base + "highs.js";
          tag.onload = resolve;
          tag.onerror = () => reject(new Error(`could not load ${base}highs.js`));
          root.document.head.appendChild(tag);
        });
        factory = root.Module;
      }

      if (root.Module) { try { delete root.Module; } catch (_) { root.Module = undefined; } }
      if (typeof factory !== "function") throw new Error("HiGHS did not expose a factory");
      return factory({ locateFile: (file) => base + file });
    })();
    return highsPromise;
  }

  async function solveSquad(pool, opt, base) {
    const highs = await loadHighs(base);
    const { lp, players, slotWeight, captainMultiplier } = buildLp(pool, opt);
    const result = highs.solve(lp, {});
    if (result.Status !== "Optimal") {
      throw new Error(
        `No legal squad found (solver status: ${result.Status}). `
        + "Budget too low, or too many players excluded?");
    }
    return readSolution(result, players, slotWeight, captainMultiplier);
  }

  function nearMissCandidates(pool, opt, squadIds, perClub) {
    opt = opt || {};
    const held = new Set(squadIds || []);
    const exc = new Set(opt.exclude || []);
    const minStart = opt.minStart ?? 0;

    const groups = new Map();
    for (const p of pool) {
      if (held.has(p.id) || exc.has(p.id) || p.p_play < minStart) continue;
      const key = perClub ? `${p.pos}|${p.team}` : p.pos;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(p);
    }

    const chosen = [];
    for (const key of [...groups.keys()].sort()) {
      const ordered = groups.get(key).slice()
        .sort((a, b) => a.price - b.price || (b.pts || 0) - (a.pts || 0));
      let best = -Infinity;
      for (const p of ordered) {
        if ((p.pts || 0) > best) { chosen.push(p.id); best = p.pts || 0; }
      }
    }
    return chosen;
  }

  async function nearMisses(pool, opt, settings, base) {
    settings = settings || {};
    const optimal = await solveSquad(pool, opt, base);
    const held = new Set(optimal.squad);
    const byId = new Map(pool.map((p) => [p.id, p]));
    const candidates = nearMissCandidates(pool, opt, optimal.squad, settings.perClub);
    const include = opt.include || [];

    const rows = [];
    for (let i = 0; i < candidates.length; i++) {
      if (settings.stopped && settings.stopped()) return null;
      const id = candidates[i];
      try {
        const forced = await solveSquad(pool, { ...opt, include: [...include, id] }, base);
        const got = new Set(forced.squad);
        const pos = byId.get(id)?.pos;
        const out = [...held].filter((x) => !got.has(x))
          .sort((a, b) => (byId.get(a)?.pos === pos ? 0 : 1) - (byId.get(b)?.pos === pos ? 0 : 1));
        rows.push({
          id,
          gap: Math.max(0, optimal.objective - forced.objective),
          starting: forced.starting.includes(id),
          captain: forced.captain === id,
          replaces: out,
        });
      } catch (error) {
        if (!/No legal squad found/.test(String(error && error.message))) throw error;
        rows.push({ id, gap: null, starting: false, captain: false, replaces: [] });
      }
      if (settings.onProgress) {
        settings.onProgress(i + 1, candidates.length, rows[rows.length - 1]);
      }
    }

    rows.sort((a, b) => (a.gap === null) - (b.gap === null) || a.gap - b.gap);
    return { rows, tested: candidates.length, objective: optimal.objective,
             squad: optimal.squad };
  }

  const api = { buildLp, readSolution, loadHighs, solveSquad, SLOTS,
                nearMissCandidates, nearMisses };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FplSolver = api;
})(typeof self !== "undefined" ? self : globalThis);

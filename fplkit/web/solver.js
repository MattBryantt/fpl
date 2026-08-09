/* Squad selection as a mixed-integer program — the browser port of
 * fplkit/optimise.py, solved by HiGHS compiled to WebAssembly.
 *
 * Same variables, same constraints, same objective as the Python. That is the
 * whole point: the board's premise is that the answer is *exact*, so that "is
 * he worth the money?" can be answered by forcing a player in and re-optimising
 * everything around him. A heuristic that gets close would quietly break the
 * one question the tool exists to answer. Verified squad-for-squad against CBC
 * over randomised settings by scripts/verify-solver-port.mjs.
 *
 * A classic script, not an ES module, so that one file can be importScripts'd
 * into the worker and required by node. The solve is 0.2-3s at full pool size,
 * which is a visible freeze on a phone, so it always runs off the main thread.
 */

(function (root) {
  "use strict";

  const SLOTS = ["GKP", "1", "2", "3"];

  // LP format reads exactly what you print, so print enough. Six decimals is
  // far inside the noise on a projection and keeps the file ~90KB.
  const n = (v) => String(Math.round(v * 1e6) / 1e6);
  const term = (coef, name) => `${coef < 0 ? "-" : "+"} ${n(Math.abs(coef))} ${name}`;

  /** Build the CPLEX LP text for one set of settings.
   *  `pool` rows need: id, pos, team, price, pts, own, p_play. */
  function buildLp(pool, opt) {
    opt = opt || {};
    const budget = opt.budget ?? 100;
    const benchSlotWeights = opt.benchSlotWeights || null;
    const benchWeight = opt.benchWeight ?? 0.12;
    const profile = opt.benchSlotProfile || { GKP: 0.25, "1": 2.0, "2": 0.85, "3": 0.35 };
    const ownershipWeight = opt.ownershipWeight ?? 0;
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
    // Keep fringe players out of the "free" bench slots, but never drop one the
    // user has explicitly required — the Python makes the same exception.
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
      if (pts) { obj.push(term(pts, X(p))); obj.push(term(pts, C(p))); }
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
    // An all-zero objective is not valid LP text, and happens the moment every
    // bench weight is zeroed on an empty pool.
    if (!obj.length) obj.push("0 zero_obj");

    const cons = [];
    for (const s of SLOTS) {
      const elig = players.filter((p) => eligible(p, s));
      cons.push(`slot${s}: ${elig.map((p) => `+ ${B(p, s)}`).join(" ")} = 1`);
    }
    // On exactly one bench slot iff in the squad and not in the XI.
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
    // Constraint names must be unique and LP-safe; club names are neither.
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
    };
  }

  function readSolution(result, players, slotWeight) {
    const on = (name) => (result.Columns[name]?.Primal ?? 0) > 0.5;
    const squad = players.filter((p) => on(`s_${p.id}`));
    const bench = {};
    for (const p of squad) {
      for (const s of SLOTS) if (on(`b${s}_${p.id}`)) bench[p.id] = s;
    }
    const starting = squad.filter((p) => on(`x_${p.id}`));
    const captain = squad.find((p) => on(`c_${p.id}`)) || null;
    const xiPoints = starting.reduce((a, p) => a + (p.pts || 0), 0)
                   + (captain ? captain.pts || 0 : 0);
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

  /** Load the WASM solver once and keep it. `base` is where highs.js and
   *  highs.wasm live, relative to whoever is loading them.
   *
   *  Three ways in, because highs.js is a UMD bundle and each environment
   *  reaches it differently: node requires it, a worker importScripts it, and a
   *  page has to inject a tag. The verifier runs the first, the board runs the
   *  second, and the third exists so a solve still works if the worker ever
   *  cannot start. */
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

      // `Module` is far too generic a name to leave on the global object, and
      // emscripten only needs it long enough to be called once.
      if (root.Module) { try { delete root.Module; } catch (_) { root.Module = undefined; } }
      if (typeof factory !== "function") throw new Error("HiGHS did not expose a factory");
      return factory({ locateFile: (file) => base + file });
    })();
    return highsPromise;
  }

  async function solveSquad(pool, opt, base) {
    const highs = await loadHighs(base);
    const { lp, players, slotWeight } = buildLp(pool, opt);
    const result = highs.solve(lp, {});
    if (result.Status !== "Optimal") {
      throw new Error(
        `No legal squad found (solver status: ${result.Status}). `
        + "Budget too low, or too many players excluded?");
    }
    return readSolution(result, players, slotWeight);
  }

  const api = { buildLp, readSolution, loadHighs, solveSquad, SLOTS };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FplSolver = api;
})(typeof self !== "undefined" ? self : globalThis);

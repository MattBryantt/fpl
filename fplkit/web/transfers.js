/* Transfers and chips, solved together, as one mixed-integer program — the
 * browser port of fplkit/transfers.py's `plan_transfers`.
 *
 * A classic script, not an ES module, for the same reason solver.js is one:
 * so it can be `importScripts`'d into a worker (which cannot `import` a UMD
 * factory like highs.js) and `require`'d by node for verification. Loads
 * HiGHS through `FplSolver.loadHighs`, already vendored by solver.js — one
 * WASM instance, shared with the squad optimiser rather than duplicated.
 *
 * This file is mechanics only: given a pool of players (each carrying
 * `pts[]`, survival-adjusted points per gameweek — see chips.mjs), an owned
 * squad, and the rule constants read off the snapshot, it builds the LP,
 * solves it, and reads back what was decided. Every judgment call about
 * *which* players belong in the pool, *what* a chip's gameweek is worth, or
 * how to present a transfer as "X → Y" lives in chips.mjs instead, which runs
 * on the main thread where it has room to import things. Nothing here decides
 * what to show a person; it decides what is legal and what scores best.
 *
 * Ported constraint-for-constraint from `plan_transfers` (see that function's
 * docstring for the reasoning behind each piece — the free-transfer state
 * machine, the friction charge, why chips share the objective rather than a
 * separate pass). Verified against CBC-solved reference cases by
 * scripts/verify-transfer-port.mjs.
 */

(function (root) {
  "use strict";

  const SLOTS_DEFAULT = ["GKP", "1", "2", "3"];

  const n = (v) => String(Math.round(v * 1e6) / 1e6);
  const term = (coef, name) => `${coef < 0 ? "-" : "+"} ${n(Math.abs(coef))} ${name}`;
  const T = (coef, name) => ({ coef, name });
  const renderTerms = (terms) => {
    const kept = terms.filter((t) => t.coef !== 0);
    return kept.length ? kept.map((t) => term(t.coef, t.name)).join(" ") : "0 zero";
  };
  const negTerms = (terms) => terms.map((t) => T(-t.coef, t.name));

  // LP variable names. `gw` is a plain gameweek number (small int), always a
  // valid LP-token suffix; `id` is an FPL player id, likewise.
  const SQ = (id, gw) => `sq_${id}_${gw}`;
  const XI = (id, gw) => `xi_${id}_${gw}`;
  const BN = (slot, id, gw) => `bn${slot}_${id}_${gw}`;
  const CP = (id, gw) => `cp_${id}_${gw}`;
  const TC = (id, gw) => `tc_${id}_${gw}`;
  const FH = (id, gw) => `fh_${id}_${gw}`;
  const BU = (id, gw) => `bu_${id}_${gw}`;
  const SO = (id, gw) => `so_${id}_${gw}`;
  const BANK = (gw) => `bank_${gw}`;
  const FT = (gw) => `ft_${gw}`;
  const FTS = (gw, s) => `fts_${gw}_${s}`;
  const SPENT = (gw) => `sp_${gw}`;
  const HITS = (gw) => `hits_${gw}`;
  const OVER = (gw) => `ov_${gw}`;
  const UNDER = (gw) => `un_${gw}`;
  const USE = (chip, gw) => `use_${chip}_${gw}`;

  /** Geometric discount per gameweek. Port of `transfers.decay_factors`. */
  function decayFactors(gameweeks, halfLife) {
    const decay = {};
    gameweeks.forEach((gw, step) => {
      decay[gw] = halfLife == null || !Number.isFinite(halfLife)
        ? 1 : Math.pow(0.5, step / halfLife);
    });
    return decay;
  }

  /** Build the LP text for one solve. `pool` rows need: id, pos, team, price,
   *  pts[] (index-aligned to `opt.gameweeks`). `opt` carries every rule
   *  constant and every ownership fact — see the module docstring; nothing
   *  here has a default, so a missing field fails loudly rather than quietly
   *  drifting from the rule it was supposed to mirror. */
  function buildLp(pool, opt) {
    const {
      gameweeks, budget, squad = [], bank = 0, freeTransfers = 1,
      chips = {}, captainPool = [], slotWeight, squadByPos, xiMinByPos, xiMaxByPos,
      squadSize, xiSize, maxPerClub, include = [], exclude = [],
      halfLife, holdValue, friction, ftWorth, maxFreeTransfers,
      hitCost, bankValue, freeTransfersPerGw, idleMovePenalty,
      noTransferGws = [], banFirstGwTransfers = false, hitLimit = null,
    } = opt;

    const poolIds = new Set(pool.map((p) => p.id));
    for (const id of include) if (!poolIds.has(id)) throw new Error(`forced-in player ${id} is not in the pool`);
    const owned = squad.slice();
    const missing = owned.filter((id) => !poolIds.has(id));
    if (missing.length) throw new Error(`owned players are not in the pool: ${missing.join(", ")}`);
    const ownedSet = new Set(owned);
    const preseason = owned.length === 0;

    const first = gameweeks[0], last = gameweeks[gameweeks.length - 1];
    const terminal = last + 1;
    const allGw = [...gameweeks, terminal];
    const slots = Object.keys(slotWeight);
    const teams = [...new Set(pool.map((p) => p.team))];
    const captainSet = new Set(captainPool);
    const hasTriple = "3xc" in chips;
    const hasFreeHit = "freehit" in chips;
    const bigM = 2 * maxFreeTransfers + squadSize;

    const cons = [];
    const addCon = (name, terms, op, rhs) => cons.push({ name, terms, op, rhs });
    const bin = new Set();
    const genBounds = new Map(); // name -> [lo, hi]

    const idxOf = (p, gw) => gameweeks.indexOf(gw);
    const ptsAt = (p, gw) => p.pts[idxOf(p, gw)] || 0;

    /** 1 if `chip` may be played in `gw` — a real variable term, or null. */
    const playedVar = (chip, gw) => (chips[chip] && chips[chip].includes(gw)) ? USE(chip, gw) : null;
    const playedTerm = (chip, gw, coef = 1) => {
      const v = playedVar(chip, gw);
      return v ? [T(coef, v)] : [];
    };

    // --- squad legality ----------------------------------------------------
    for (const gw of gameweeks) {
      addCon(`sqsize_${gw}`, pool.map((p) => T(1, SQ(p.id, gw))), "=", squadSize);
      for (const [pos, count] of Object.entries(squadByPos)) {
        const members = pool.filter((p) => p.pos === pos);
        addCon(`sqpos_${pos}_${gw}`, members.map((p) => T(1, SQ(p.id, gw))), "=", count);
      }
      teams.forEach((tm, ti) => {
        const members = pool.filter((p) => p.team === tm);
        addCon(`sqclub_${ti}_${gw}`, members.map((p) => T(1, SQ(p.id, gw))), "<=", maxPerClub);
      });
      for (const id of include) addCon(`inc_${id}_${gw}`, [T(1, SQ(id, gw))], "=", 1);
      for (const id of exclude) if (poolIds.has(id)) addCon(`exc_${id}_${gw}`, [T(1, SQ(id, gw))], "=", 0);
      for (const p of pool) bin.add(SQ(p.id, gw));
    }

    // --- the free-hit squad --------------------------------------------------
    if (hasFreeHit) {
      for (const gw of chips.freehit) {
        const flag = USE("freehit", gw);
        addCon(`fhsize_${gw}`, [...pool.map((p) => T(1, FH(p.id, gw))), T(-squadSize, flag)], "=", 0);
        for (const [pos, count] of Object.entries(squadByPos)) {
          const members = pool.filter((p) => p.pos === pos);
          addCon(`fhpos_${pos}_${gw}`, [...members.map((p) => T(1, FH(p.id, gw))), T(-count, flag)], "=", 0);
        }
        teams.forEach((tm, ti) => {
          const members = pool.filter((p) => p.team === tm);
          addCon(`fhclub_${ti}_${gw}`, [...members.map((p) => T(1, FH(p.id, gw))), T(-maxPerClub, flag)], "<=", 0);
        });
        // Affordable out of what selling the real squad that week would
        // raise, plus the bank -- sell price is always current price here
        // (this tool has never tracked what a player was bought for).
        const afford = [
          ...pool.map((p) => T(p.price, FH(p.id, gw))),
          ...pool.map((p) => T(-p.price, SQ(p.id, gw))),
          T(-1, BANK(gw)),
        ];
        addCon(`fhafford_${gw}`, afford, "<=", 0);
      }
      for (const gw of gameweeks) {
        for (const p of pool) {
          const flag = playedVar("freehit", gw);
          const terms = flag ? [T(1, FH(p.id, gw)), T(-1, flag)] : [T(1, FH(p.id, gw))];
          addCon(`fhgate_${p.id}_${gw}`, terms, "<=", 0);
          bin.add(FH(p.id, gw));
        }
      }
    }

    // --- lineup --------------------------------------------------------------
    for (const gw of gameweeks) {
      const boost = playedVar("bboost", gw);
      const hit = playedVar("freehit", gw);

      addCon(`xisize_${gw}`,
        [...pool.map((p) => T(1, XI(p.id, gw))), ...(boost ? [T(-(squadSize - xiSize), boost)] : [])],
        "=", xiSize);

      for (const slot of slots) {
        const eligible = pool.filter((p) => (p.pos === "GKP") === (slot === "GKP"));
        addCon(`slot_${slot}_${gw}`,
          [...eligible.map((p) => T(1, BN(slot, p.id, gw))), ...(boost ? [T(1, boost)] : [])],
          "=", 1);
        const ineligible = pool.filter((p) => (p.pos === "GKP") !== (slot === "GKP"));
        for (const p of ineligible) addCon(`slotoff_${slot}_${p.id}_${gw}`, [T(1, BN(slot, p.id, gw))], "=", 0);
        for (const p of pool) bin.add(BN(slot, p.id, gw));
      }

      for (const p of pool) {
        const benchTerms = slots.map((s) => T(1, BN(s, p.id, gw)));
        // xi <= squad + hit
        addCon(`xicap_${p.id}_${gw}`,
          [T(1, XI(p.id, gw)), T(-1, SQ(p.id, gw)), ...(hit ? [T(-1, hit)] : [])], "<=", 0);
        // benched <= squad + hit
        addCon(`bncap_${p.id}_${gw}`,
          [...benchTerms, T(-1, SQ(p.id, gw)), ...(hit ? [T(-1, hit)] : [])], "<=", 0);
        if (hasFreeHit) {
          // xi <= free_hit_squad + (1 - hit)  ->  xi - fh + hit <= 1 (RHS is 1
          // whether or not `hit` exists this gw -- outside the chip's window
          // it is the constant 0, and 1 - 0 is still 1).
          addCon(`xifh_${p.id}_${gw}`,
            [T(1, XI(p.id, gw)), T(-1, FH(p.id, gw)), ...(hit ? [T(1, hit)] : [])],
            "<=", 1);
          addCon(`bnfh_${p.id}_${gw}`,
            [...benchTerms, T(-1, FH(p.id, gw)), ...(hit ? [T(1, hit)] : [])],
            "<=", 1);
        }
        addCon(`xibn_${p.id}_${gw}`, [T(1, XI(p.id, gw)), ...benchTerms], "<=", 1);
      }

      for (const pos of ["GKP", "DEF", "MID", "FWD"]) {
        const members = pool.filter((p) => p.pos === pos);
        addCon(`ximin_${pos}_${gw}`, members.map((p) => T(1, XI(p.id, gw))), ">=", xiMinByPos[pos]);
        addCon(`ximax_${pos}_${gw}`,
          [...members.map((p) => T(1, XI(p.id, gw))), ...(boost ? [T(-1, boost)] : [])],
          "<=", xiMaxByPos[pos]);
      }

      addCon(`onecapt_${gw}`, captainPool.map((id) => T(1, CP(id, gw))), "=", 1);
      for (const id of captainPool) {
        addCon(`captxi_${id}_${gw}`, [T(1, CP(id, gw)), T(-1, XI(id, gw))], "<=", 0);
        bin.add(CP(id, gw));
      }
      if (hasTriple) {
        const tcFlag = playedVar("3xc", gw);
        addCon(`tc_${gw}`,
          [...captainPool.map((id) => T(1, TC(id, gw))), ...(tcFlag ? [T(-1, tcFlag)] : [])],
          "=", 0);
        for (const id of captainPool) {
          addCon(`tccap_${id}_${gw}`, [T(1, TC(id, gw)), T(-1, CP(id, gw))], "<=", 0);
          bin.add(TC(id, gw));
        }
      }
    }

    // --- transfers -------------------------------------------------------
    gameweeks.forEach((gw, step) => {
      const hit = playedVar("freehit", gw);
      for (const p of pool) {
        bin.add(BU(p.id, gw)); bin.add(SO(p.id, gw));
        if (preseason && step === 0) {
          addCon(`nobuy0_${p.id}`, [T(1, BU(p.id, gw))], "=", 0);
          addCon(`nosell0_${p.id}`, [T(1, SO(p.id, gw))], "=", 0);
          continue;
        }
        const previous = step
          ? [T(1, SQ(p.id, gameweeks[step - 1]))]
          : [];
        const previousConst = (!step && ownedSet.has(p.id)) ? 1 : 0;
        // squad[gw] == previous + bought - sold
        addCon(`tr_${p.id}_${gw}`,
          [T(1, SQ(p.id, gw)), ...negTerms(previous), T(-1, BU(p.id, gw)), T(1, SO(p.id, gw))],
          "=", previousConst);
        addCon(`buhit_${p.id}_${gw}`, [T(1, BU(p.id, gw)), ...(hit ? [T(1, hit)] : [])], "<=", 1);
        addCon(`sohit_${p.id}_${gw}`, [T(1, SO(p.id, gw)), ...(hit ? [T(1, hit)] : [])], "<=", 1);
        addCon(`nobuyback_${p.id}_${gw}`, [T(1, BU(p.id, gw)), T(1, SO(p.id, gw))], "<=", 1);
      }
    });

    const banned = new Set(noTransferGws);
    if (banFirstGwTransfers) banned.add(first);
    for (const gw of banned) {
      if (!gameweeks.includes(gw)) continue;
      addCon(`noTr_${gw}`, pool.map((p) => T(1, BU(p.id, gw))), "=", 0);
    }

    // --- money -------------------------------------------------------------
    gameweeks.forEach((gw, step) => {
      const raised = pool.map((p) => T(p.price, SO(p.id, gw)));
      const outlay = pool.map((p) => T(-p.price, BU(p.id, gw)));
      if (preseason && step === 0) {
        addCon(`bank_${gw}`,
          [T(1, BANK(gw)), ...pool.map((p) => T(p.price, SQ(p.id, gw)))],
          "=", budget);
      } else if (step === 0) {
        addCon(`bank_${gw}`, [T(1, BANK(gw)), ...negTerms(raised), ...negTerms(outlay)], "=", bank);
      } else {
        addCon(`bank_${gw}`,
          [T(1, BANK(gw)), T(-1, BANK(gameweeks[step - 1])), ...negTerms(raised), ...negTerms(outlay)],
          "=", 0);
      }
    });

    // --- the free-transfer state machine ------------------------------------
    for (const gw of gameweeks) {
      const moves = pool.map((p) => T(1, BU(p.id, gw)));
      const card = playedVar("wildcard", gw);
      // spent >= moves - squadSize*card
      addCon(`spmin_${gw}`,
        [T(1, SPENT(gw)), ...negTerms(moves), ...(card ? [T(squadSize, card)] : [])],
        ">=", 0);
      // spent <= squadSize*(1-card)  ->  spent + squadSize*card <= squadSize
      addCon(`spmax_${gw}`, [T(1, SPENT(gw)), ...(card ? [T(squadSize, card)] : [])], "<=", squadSize);
      addCon(`spmoves_${gw}`, [T(1, SPENT(gw)), ...negTerms(moves)], "<=", 0);
      // paid >= spent - ft
      addCon(`paidmin_${gw}`, [T(1, HITS(gw)), T(-1, SPENT(gw)), T(1, FT(gw))], ">=", 0);
      genBounds.set(SPENT(gw), [0, squadSize]);
      genBounds.set(HITS(gw), [0, squadSize]);
      bin.add(OVER(gw)); bin.add(UNDER(gw));
    }

    addCon(`ftopen`, [T(1, FT(first))], "=", preseason ? 0 : freeTransfers);

    gameweeks.forEach((gw, step) => {
      const nxt = step + 1 < gameweeks.length ? gameweeks[step + 1] : terminal;
      // raw = ft[gw] - spent[gw] + earned, earned = freeTransfersPerGw - wildcard? - freehit?
      const rv = [T(1, FT(gw)), T(-1, SPENT(gw)),
                 ...playedTerm("wildcard", gw, -1), ...playedTerm("freehit", gw, -1)];
      const rc = freeTransfersPerGw;

      // raw >= (max+1) - bigM*(1-over)  ->  rv - bigM*over >= (max+1) - bigM - rc
      addCon(`ftover1_${gw}`, [...rv, T(-bigM, OVER(gw))], ">=", (maxFreeTransfers + 1) - bigM - rc);
      // raw <= max + bigM*over  ->  rv - bigM*over <= max - rc
      addCon(`ftover2_${gw}`, [...rv, T(-bigM, OVER(gw))], "<=", maxFreeTransfers - rc);
      // raw <= bigM*(1-under)  ->  rv + bigM*under <= bigM - rc
      addCon(`ftunder1_${gw}`, [...rv, T(bigM, UNDER(gw))], "<=", bigM - rc);
      // raw >= 1 - bigM*under  ->  rv + bigM*under >= 1 - rc
      addCon(`ftunder2_${gw}`, [...rv, T(bigM, UNDER(gw))], ">=", 1 - rc);
      addCon(`ftexcl_${gw}`, [T(1, OVER(gw)), T(1, UNDER(gw))], "<=", 1);

      // ft[nxt] <= max + bigM*(1-over)  ->  ft[nxt] + bigM*over <= max + bigM
      addCon(`ftnxt1_${gw}`, [T(1, FT(nxt)), T(bigM, OVER(gw))], "<=", maxFreeTransfers + bigM);
      // ft[nxt] >= max - bigM*(1-over)  ->  ft[nxt] - bigM*over >= max - bigM
      addCon(`ftnxt2_${gw}`, [T(1, FT(nxt)), T(-bigM, OVER(gw))], ">=", maxFreeTransfers - bigM);
      // ft[nxt] <= 1 + bigM*(1-under)  ->  ft[nxt] + bigM*under <= 1 + bigM
      addCon(`ftnxt3_${gw}`, [T(1, FT(nxt)), T(bigM, UNDER(gw))], "<=", 1 + bigM);
      // ft[nxt] >= 1 - bigM*(1-under)  ->  ft[nxt] - bigM*under >= 1 - bigM
      addCon(`ftnxt4_${gw}`, [T(1, FT(nxt)), T(-bigM, UNDER(gw))], ">=", 1 - bigM);
      // ft[nxt] - raw <= bigM*(over+under)  ->  ft[nxt] - rv - bigM*over - bigM*under <= rc
      addCon(`fteq1_${gw}`,
        [T(1, FT(nxt)), ...negTerms(rv), T(-bigM, OVER(gw)), T(-bigM, UNDER(gw))], "<=", rc);
      // raw - ft[nxt] <= bigM*(over+under)  ->  rv - ft[nxt] - bigM*over - bigM*under <= -rc
      addCon(`fteq2_${gw}`,
        [...rv, T(-1, FT(nxt)), T(-bigM, OVER(gw)), T(-bigM, UNDER(gw))], "<=", -rc);
    });

    for (const gw of allGw) {
      const stateTerms = [];
      for (let s = 0; s <= maxFreeTransfers; s++) { stateTerms.push(T(1, FTS(gw, s))); bin.add(FTS(gw, s)); }
      addCon(`ftsimplex_${gw}`, stateTerms, "=", 1);
      const weighted = [];
      for (let s = 0; s <= maxFreeTransfers; s++) weighted.push(T(s, FTS(gw, s)));
      addCon(`ftlink_${gw}`, [T(1, FT(gw)), ...negTerms(weighted)], "=", 0);
      genBounds.set(FT(gw), [0, maxFreeTransfers]);
    }

    // --- chips ---------------------------------------------------------------
    for (const [chip, allowed] of Object.entries(chips)) {
      addCon(`chiponce_${chip}`, allowed.map((gw) => T(1, USE(chip, gw))), "<=", 1);
      for (const gw of allowed) bin.add(USE(chip, gw));
    }
    for (const gw of gameweeks) {
      const active = Object.keys(chips)
        .map((chip) => playedVar(chip, gw))
        .filter(Boolean)
        .map((v) => T(1, v));
      if (active.length) addCon(`onechip_${gw}`, active, "<=", 1);
    }
    if (hitLimit != null) {
      addCon(`hitlimit`, gameweeks.map((gw) => T(1, HITS(gw))), "<=", hitLimit);
    }

    // --- objective -----------------------------------------------------------
    const decay = decayFactors(gameweeks, halfLife);
    const obj = new Map();
    const addObj = (name, coef) => { if (coef !== 0) obj.set(name, (obj.get(name) || 0) + coef); };

    const ftWorthOf = (s) => ftWorth[s] ?? ftWorth[String(s)] ?? 0;
    const bankedTerms = (gw) => {
      const t = [];
      for (let s = 0; s <= maxFreeTransfers; s++) t.push(T(ftWorthOf(s), FTS(gw, s)));
      return t;
    };

    let objConstant = 0;
    const openingState = preseason ? 0 : freeTransfers;
    const opening = ftWorthOf(openingState);

    gameweeks.forEach((gw, step) => {
      const dw = decay[gw];
      for (const p of pool) {
        const pts = ptsAt(p, gw);
        if (!pts) continue;
        addObj(XI(p.id, gw), dw * pts);
        for (const s of slots) addObj(BN(s, p.id, gw), dw * pts * slotWeight[s]);
      }
      for (const id of captainPool) {
        const p = pool.find((x) => x.id === id);
        const pts = p ? ptsAt(p, gw) : 0;
        addObj(CP(id, gw), dw * pts);
        if (hasTriple) addObj(TC(id, gw), dw * pts);
      }
      addObj(HITS(gw), dw * -hitCost);
      addObj(SPENT(gw), dw * -friction);
      for (const p of pool) addObj(BU(p.id, gw), dw * -idleMovePenalty);
      for (const chip of Object.keys(chips)) {
        const v = playedVar(chip, gw);
        if (v) addObj(v, dw * -(holdValue[chip] || 0));
      }
      for (const t of bankedTerms(gw)) addObj(t.name, dw * t.coef);
      if (step) {
        for (const t of negTerms(bankedTerms(gameweeks[step - 1]))) addObj(t.name, dw * t.coef);
      } else {
        objConstant += dw * -opening;
      }
      addObj(BANK(gw), dw * bankValue);
    });
    const dLast = decay[last];
    for (const t of bankedTerms(terminal)) addObj(t.name, dLast * t.coef);
    for (const t of negTerms(bankedTerms(last))) addObj(t.name, dLast * t.coef);

    const objTerms = [...obj.entries()].filter(([, c]) => c !== 0).map(([name, c]) => T(c, name));
    const objLine = objTerms.length ? renderTerms(objTerms) : "0 zero_obj";

    // --- render --------------------------------------------------------------
    const consText = cons.map((c) => `${c.name}: ${renderTerms(c.terms)} ${c.op} ${n(c.rhs)}`).join("\n ");
    const boundsText = [...genBounds.entries()]
      .map(([name, [lo, hi]]) => `${n(lo)} <= ${name} <= ${n(hi)}`).join("\n ");
    const generalText = [...genBounds.keys()].join(" ");
    const binaryText = [...bin].join(" ");

    const lp = `Maximize\n obj: ${objLine}\nSubject To\n ${consText}\n`
      + `Bounds\n ${boundsText}\nGeneral\n ${generalText}\nBinary\n ${binaryText}\nEnd`;

    return { lp, objConstant, meta: { gameweeks, terminal, decay, slots, captainPool, chips, hasTriple, hasFreeHit } };
  }

  /** Turn a solved LP back into one result object per gameweek. Presentation
   *  (pairing sales to purchases, chip verdicts, formatting) happens in
   *  chips.mjs on the main thread; this only reads variables. */
  function readSolution(result, pool, opt, meta) {
    const on = (name) => (result.Columns[name]?.Primal ?? 0) > 0.5;
    const val = (name) => result.Columns[name]?.Primal ?? 0;
    const byId = new Map(pool.map((p) => [p.id, p]));
    const { gameweeks, slots, captainPool, chips, hasTriple, hasFreeHit } = meta;

    const chipByGw = new Map();
    for (const [chip, allowed] of Object.entries(chips)) {
      for (const gw of allowed) if (on(USE(chip, gw))) chipByGw.set(gw, chip);
    }

    const weeks = [];
    let horizonPoints = 0;
    for (const gw of gameweeks) {
      const chip = chipByGw.get(gw) || null;
      const squadIds = pool.filter((p) => on(SQ(p.id, gw))).map((p) => p.id);
      const starters = pool.filter((p) => on(XI(p.id, gw))).map((p) => p.id);
      const bench = [];
      for (const s of slots) for (const p of pool) if (on(BN(s, p.id, gw))) bench.push({ slot: s, id: p.id });
      bench.sort((a, b) => {
        const ag = a.slot === "GKP" ? 1 : 0, bg = b.slot === "GKP" ? 1 : 0;
        return ag !== bg ? ag - bg : slots.indexOf(a.slot) - slots.indexOf(b.slot);
      });
      const captain = captainPool.find((id) => on(CP(id, gw))) ?? null;
      const tripled = hasTriple && captainPool.some((id) => on(TC(id, gw)));
      const idx = gameweeks.indexOf(gw);
      const ptsOf = (id) => byId.get(id)?.pts[idx] || 0;
      const vice = starters.filter((id) => id !== captain)
        .reduce((best, id) => (best === null || ptsOf(id) > ptsOf(best) ? id : best), null);

      const multiplier = tripled ? 3 : 2;
      let weekPoints = starters.reduce((a, id) => a + ptsOf(id), 0);
      if (captain !== null) weekPoints += (multiplier - 1) * ptsOf(captain);
      horizonPoints += weekPoints;

      const formation = ["DEF", "MID", "FWD"]
        .map((pos) => starters.filter((id) => byId.get(id)?.pos === pos).length).join("-");

      const boughtIds = pool.filter((p) => on(BU(p.id, gw))).map((p) => p.id);
      const soldIds = pool.filter((p) => on(SO(p.id, gw))).map((p) => p.id);

      weeks.push({
        gw, chip, squad: squadIds, starters, bench, captain, vice, tripled,
        formation, xiPoints: weekPoints,
        boughtIds, soldIds,
        freeTransfers: Math.round(val(FT(gw))),
        spent: Math.round(val(SPENT(gw))),
        hits: Math.round(val(HITS(gw))),
        bank: val(BANK(gw)),
        decay: meta.decay[gw],
      });
    }

    return {
      gameweeks, weeks, chipByGw,
      objective: result.ObjectiveValue + opt.__objConstant,
      status: result.Status,
      horizonPoints,
    };
  }

  let highsPromise = null;
  function loadHighs(base) {
    if (root.FplSolver) return root.FplSolver.loadHighs(base);
    throw new Error("solver.js must be loaded before transfers.js");
  }

  /** Build, solve, and read back one transfer-and-chip plan. `pool` rows need
   *  id/pos/team/price/pts[]; `opt` is documented at `buildLp`. */
  async function planTransfers(pool, opt, base) {
    const highs = await loadHighs(base);
    const { lp, objConstant, meta } = buildLp(pool, opt);
    const result = highs.solve(lp, {});
    if (result.Status !== "Optimal") {
      throw new Error(
        `no legal transfer plan found (solver status: ${result.Status}). `
        + "Budget too low, squad illegal, or too many players excluded?");
    }
    return readSolution(result, pool, { ...opt, __objConstant: objConstant }, meta);
  }

  const api = { buildLp, readSolution, planTransfers, decayFactors, SLOTS_DEFAULT };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FplTransfers = api;
})(typeof self !== "undefined" ? self : globalThis);

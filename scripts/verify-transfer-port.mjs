/* Proves the browser's transfer-and-chip MILP is the same MILP.
 *
 * scripts/transfer-cases.json holds synthetic scenarios solved by CBC through
 * fplkit/transfers.py -- the pool, points and settings captured verbatim, so
 * a disagreement here can only be the LP translation, not a different pool
 * getting built on each side (that risk belongs to candidatePool, which this
 * script does not exercise). Re-solved by the vendored HiGHS WASM build, same
 * relationship scripts/make-solver-cases.py has to verify-solver-port.mjs.
 *
 * The bar is the same one that script uses, for the same reason: the
 * objective, not the squad. This model has more room for genuine ties than
 * the single-period one (an idle-move penalty just above zero means a handful
 * of economically-identical fifteens can share an optimum), so squad and chip
 * agreement are reported but only the objective gates a failure.
 *
 * The chip payouts do gate, because they are a pure function of a squad and
 * are compared on the same squads on both sides -- no tie can excuse a
 * difference there.
 *
 * Run: node scripts/verify-transfer-port.mjs
 */

import fs from "fs";
import path from "path";
import { createRequire } from "module";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");
const require = createRequire(import.meta.url);

global.self = global; // solver.js's loadHighs checks `typeof self`
const FplSolver = require(path.join(ROOT, "fplkit/web/solver.js"));
global.FplSolver = FplSolver;
const FplTransfers = require(path.join(ROOT, "fplkit/web/transfers.js"));
const { chipPayouts } = await import(path.join(ROOT, "fplkit/web/chips.mjs"));

// The board's own default, which is what make-transfer-cases.py prices with.
const SLOT_WEIGHT = { GKP: 0.03, 1: 0.24, 2: 0.10, 3: 0.04 };

// The cases are solved at seconds=30 in Python but rounded to 4dp on the way
// out; HiGHS's own MIP gap is comparable to CBC's, so a few 1e-3 of slack
// covers both without hiding a real disagreement, which on this model's
// objective scale (~150-350) would be an order of magnitude larger.
const TOL = 5e-3;

const cases = JSON.parse(fs.readFileSync(path.join(ROOT, "scripts/transfer-cases.json")));
const VENDOR = path.join(ROOT, "fplkit/web/vendor/");

const sortedIds = (ids) => ids.slice().sort((a, b) => a - b);

let worstObj = 0, objOk = 0, squadMismatches = [], chipMismatches = [], failures = [];
let worstPayout = 0;
let slowest = 0;

/** The reported chip payouts, priced off the *CBC* squads on both sides. The
 *  numbers a person reads are what say a chip was weighed in every gameweek of
 *  the window, so a port that solves identically and then reports different
 *  payouts is still broken -- and pricing both sides off the same squads keeps
 *  a legitimate tie between two fifteens out of the comparison. */
function checkPayouts(c) {
  if (!c.chipPayouts) return;
  const squads = new Map(Object.entries(c.squads).map(([gw, ids]) => [Number(gw), ids]));
  const mine = chipPayouts({
    squads,
    pointsByPlayer: new Map(c.pool.map((p) => [p.id, p.pts])),
    gameweeks: c.opt.gameweeks,
    positions: new Map(c.pool.map((p) => [p.id, p.pos])),
    slotWeight: SLOT_WEIGHT,
  });
  for (const [chip, series] of Object.entries(c.chipPayouts)) {
    for (const [gw, expected] of Object.entries(series)) {
      const got = mine[chip]?.[Number(gw)];
      if (got === undefined) {
        failures.push(`${c.name}: no JS payout for ${chip} GW${gw}`);
        continue;
      }
      const diff = Math.abs(got - expected);
      worstPayout = Math.max(worstPayout, diff);
      if (diff > 1e-6) {
        failures.push(`${c.name}: ${chip} GW${gw} payout ${got.toFixed(4)} vs CBC ${expected.toFixed(4)}`);
      }
    }
  }
}

for (const c of cases) {
  checkPayouts(c);
  const started = performance.now();
  let result;
  try {
    result = await FplTransfers.planTransfers(c.pool, c.opt, VENDOR);
  } catch (error) {
    failures.push(`${c.name}: JS threw "${error.message}" but CBC found ${c.objective}`);
    continue;
  }
  const ms = performance.now() - started;
  slowest = Math.max(slowest, ms);

  const diff = Math.abs(result.objective - c.objective);
  worstObj = Math.max(worstObj, diff);
  if (diff <= TOL) objOk++;
  else failures.push(`${c.name}: obj ${result.objective.toFixed(4)} vs CBC ${c.objective.toFixed(4)} `
    + `(Δ${diff.toExponential(2)})`);

  for (const week of result.weeks) {
    const expected = c.squads[String(week.gw)];
    if (!expected) continue;
    if (JSON.stringify(sortedIds(week.squad)) !== JSON.stringify(sortedIds(expected))) {
      squadMismatches.push(`${c.name} GW${week.gw}`);
    }
    const expectedChip = c.chipByGw[String(week.gw)] || null;
    if (week.chip !== expectedChip) {
      chipMismatches.push(`${c.name} GW${week.gw}: JS ${week.chip || "—"} vs CBC ${expectedChip || "—"}`);
    }
  }
}

console.log(`${cases.length} cases · ${objOk} within tolerance on objective`);
console.log(`worst objective |Δ| ${worstObj.toExponential(2)} (limit ${TOL.toExponential(2)}) · `
  + `slowest solve ${slowest.toFixed(0)}ms`);
console.log(`squad agreement: ${cases.reduce((a, c) => a + Object.keys(c.squads).length, 0) - squadMismatches.length}`
  + `/${cases.reduce((a, c) => a + Object.keys(c.squads).length, 0)} gameweeks identical`
  + (squadMismatches.length ? ` (differs: ${squadMismatches.join(", ")})` : ""));
console.log(`chip agreement: ${chipMismatches.length ? chipMismatches.join("; ") : "every gameweek's chip matches"}`);
console.log(`chip payouts: worst |Δ| ${worstPayout.toExponential(2)} across every chip in every gameweek`);

if (failures.length) {
  console.log("\nFAIL\n" + failures.map((f) => "  " + f).join("\n"));
  process.exit(1);
}
console.log("\nPASS — the WASM transfer-and-chip planner reaches the same optimum as CBC on every case.");

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

// The cases are solved at seconds=30 in Python but rounded to 4dp on the way
// out; HiGHS's own MIP gap is comparable to CBC's, so a few 1e-3 of slack
// covers both without hiding a real disagreement, which on this model's
// objective scale (~150-350) would be an order of magnitude larger.
const TOL = 5e-3;

const cases = JSON.parse(fs.readFileSync(path.join(ROOT, "scripts/transfer-cases.json")));
const VENDOR = path.join(ROOT, "fplkit/web/vendor/");

const sortedIds = (ids) => ids.slice().sort((a, b) => a - b);

let worstObj = 0, objOk = 0, squadMismatches = [], chipMismatches = [], failures = [];
let slowest = 0;

for (const c of cases) {
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

if (failures.length) {
  console.log("\nFAIL\n" + failures.map((f) => "  " + f).join("\n"));
  process.exit(1);
}
console.log("\nPASS — the WASM transfer-and-chip planner reaches the same optimum as CBC on every case.");

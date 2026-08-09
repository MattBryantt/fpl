/* Proves the browser's MILP is the same MILP.
 *
 * scripts/solver-cases.json holds randomised settings solved by CBC through
 * fplkit/optimise.py. Every one is re-solved here by the vendored HiGHS WASM
 * build — the exact file the phone loads — and the two must agree.
 *
 * The bar is the *objective*, not the squad. Ties are real: two different
 * fifteens can score identically, and which one a solver returns is an
 * implementation detail neither of them promises. An objective that matches to
 * within the LP's own printed precision means the browser found an optimum, and
 * that is the property the board actually depends on. Squad agreement is
 * reported too, because in practice ties are rare and a sudden crop of them
 * would be worth knowing about.
 *
 * Run: node scripts/verify-solver-port.mjs
 */

import fs from "fs";
import path from "path";
import { createRequire } from "module";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");
const require = createRequire(import.meta.url);

// The vendored copy, not the one in node_modules: testing a different build
// from the one that ships would prove nothing about what the phone runs.
const FplSolver = require(path.join(ROOT, "fplkit/web/solver.js"));

// LP text is printed to 6dp and HiGHS's own MIP gap is ~1e-6 relative, so an
// objective around 200 can legitimately land a few 1e-5 away from CBC's.
const TOL = 5e-4;

const cases = JSON.parse(fs.readFileSync(path.join(ROOT, "scripts/solver-cases.json")));
const pool = JSON.parse(fs.readFileSync(path.join(ROOT, "scripts/solver-pool.json")));

let worst = 0, identical = 0, tied = 0, failures = [], slowest = 0;

for (const c of cases) {
  const started = performance.now();
  let out;
  try {
    out = await FplSolver.solveSquad(pool, c.options, path.join(ROOT, "fplkit/web/vendor/"));
  } catch (error) {
    if (c.infeasible) { identical++; continue; }
    failures.push(`${c.label}: JS threw "${error.message}" but CBC found ${c.objective}`);
    continue;
  }
  const ms = performance.now() - started;
  slowest = Math.max(slowest, ms);

  if (c.infeasible) {
    failures.push(`${c.label}: CBC found it infeasible, JS returned a squad`);
    continue;
  }
  const diff = Math.abs(out.objective - c.objective);
  worst = Math.max(worst, diff);
  const same = JSON.stringify(out.squad.slice().sort((a, b) => a - b))
             === JSON.stringify(c.squad.slice().sort((a, b) => a - b));
  if (same) identical++; else if (diff <= TOL) tied++;

  if (diff > TOL) {
    failures.push(`${c.label}: obj ${out.objective.toFixed(6)} vs CBC ${c.objective.toFixed(6)} `
      + `(Δ${diff.toExponential(2)})`);
  }
}

console.log(`${cases.length} cases · ${identical} identical squads · ${tied} tied on objective `
  + `with a different fifteen`);
console.log(`worst objective |Δ| ${worst.toExponential(2)} (limit ${TOL.toExponential(2)}) · `
  + `slowest solve ${slowest.toFixed(0)}ms`);

if (failures.length) {
  console.log("\nFAIL\n" + failures.map((f) => "  " + f).join("\n"));
  process.exit(1);
}
console.log("\nPASS — the WASM solver reaches the same optimum as CBC on every case.");

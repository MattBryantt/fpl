
import fs from "fs";
import path from "path";
import { createRequire } from "module";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");
const require = createRequire(import.meta.url);

const FplSolver = require(path.join(ROOT, "fplkit/web/solver.js"));

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

/* Proves the browser-side maths is the same maths.
 *
 * Two checks, both against numbers Python produced:
 *
 *  1. Baseline. The snapshot's per-gameweek points came out of the pandas
 *     pipeline. Recomputing them with points.js from the same inputs must give
 *     the same answer, for every player and every gameweek. This exercises
 *     playerFixturePoints and all four Poisson helpers across the full range of
 *     real inputs, which no hand-written case would cover.
 *
 *  2. Overrides. scripts/override-cases.json holds (player, overrides) pairs
 *     scored by model.reproject_player. applyOverrides + reprojectPlayer must
 *     match, including the minutes coupling and the `_mult` form.
 *
 * Run: node scripts/verify-js-port.mjs
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { reprojectPlayer, planWeight } from "../fplkit/web/points.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");

/* Both tolerances are set by rounding in the *reference* data, not by any
   slack in the port — which is why they are this tight and named separately.
   A real disagreement in the maths lands orders of magnitude above them.

   The snapshot stores per-gameweek points at 6dp (DISPLAY_DP), so a value that
   agrees perfectly can still read half a unit in the last place out. The
   scoring inputs are stored at 10dp precisely so they do not add to this. */
const TOL_BASELINE = 5.01e-7;
/* model.reproject_player rounds its own return to 4dp before it leaves Python
   (fplkit/model.py:832), so the override fixtures cannot be more precise than
   that however they are generated. */
const TOL_OVERRIDE = 5.01e-5;

const snap = JSON.parse(fs.readFileSync(path.join(ROOT, "out/snapshot.json")));
const byId = new Map(snap.players.map((p) => [p.id, p]));

let checked = 0, worst = 0, failures = [];

/* ---------------------------------------------------------- 1. baseline */
for (const p of snap.players) {
  // project() skips players who cannot appear, so their snapshot row is zeros
  // by omission rather than by calculation. Nothing to compare against.
  if (p.p_play <= 0) continue;
  const got = reprojectPlayer(snap, p, null).gw;
  for (let i = 0; i < p.gw.length; i++) {
    const diff = Math.abs(got[i] - p.gw[i]);
    worst = Math.max(worst, diff);
    checked++;
    if (diff > TOL_BASELINE && failures.length < 8) {
      failures.push(`${p.name} GW${snap.gameweeks[i]}: js ${got[i].toFixed(9)} vs py ${p.gw[i]}`);
    }
  }
}
console.log(`baseline : ${checked} player-gameweeks, worst |Δ| ${worst.toExponential(2)}  (limit ${TOL_BASELINE.toExponential(2)})`);
if (failures.length) { console.log(failures.map((f) => "  " + f).join("\n")); }

/* --------------------------------------------------------- 2. overrides */
const casesPath = path.join(ROOT, "scripts/override-cases.json");
let caseWorst = 0, caseCount = 0;
if (fs.existsSync(casesPath)) {
  for (const c of JSON.parse(fs.readFileSync(casesPath))) {
    const p = byId.get(c.fpl_id);
    if (!p) continue;
    const got = reprojectPlayer(snap, p, c.overrides);
    for (let i = 0; i < c.gw.length; i++) {
      const diff = Math.abs(got.gw[i] - c.gw[i]);
      caseWorst = Math.max(caseWorst, diff);
      caseCount++;
      if (diff > TOL_OVERRIDE && failures.length < 16) {
        failures.push(`${p.name} ${JSON.stringify(c.overrides)} GW${snap.gameweeks[i]}: `
          + `js ${got.gw[i].toFixed(9)} vs py ${c.gw[i]}`);
      }
    }
    // The post-override inputs must agree too, or the minutes coupling is wrong
    // in a way the points happen not to expose.
    for (const [k, v] of Object.entries(c.inputs)) {
      const diff = Math.abs((got.inputs[k] ?? 0) - v);
      caseWorst = Math.max(caseWorst, diff);
      caseCount++;
      if (diff > TOL_OVERRIDE && failures.length < 16) {
        failures.push(`${p.name} ${JSON.stringify(c.overrides)} input ${k}: `
          + `js ${got.inputs[k]} vs py ${v}`);
      }
    }
  }
  console.log(`overrides: ${caseCount} values, worst |Δ| ${caseWorst.toExponential(2)}  (limit ${TOL_OVERRIDE.toExponential(2)})`);
} else {
  console.log("overrides: no cases file — run scripts/make-override-cases.py");
}

/* ------------------------------------------- 3. the derived minutes family
   p_play is not in OVERRIDABLE, so the checks above cannot see it: it feeds the
   points (through the appearance term) but it is never compared directly. It is
   also what the optimiser filters its pool on, so a p_start override that fails
   to carry it makes an edited player invisible to the solver while his points
   visibly move — which is exactly the bug this whole exercise started from, and
   it reappeared once in the JS. Assert the coupling explicitly. */
{
  const subject = snap.players.find((p) => p.p_play > 0 && p.p_play < 0.4 && p.p_sub > 0);
  const boosted = reprojectPlayer(snap, subject, { p_start: 0.95 });
  const d = boosted.derived;
  const problems = [];
  if (!(d.p_play > subject.p_play)) {
    problems.push(`p_play did not follow p_start (${subject.p_play} -> ${d.p_play})`);
  }
  const expectedPlay = 0.95 + (1 - 0.95) * subject.p_sub;
  if (Math.abs(d.p_play - expectedPlay) > 1e-9) {
    problems.push(`p_play ${d.p_play} != ${expectedPlay}`);
  }
  if (Math.abs(d.p60 - 0.95 * snap.rules.P60_GIVEN_START) > 1e-9) {
    problems.push(`p60 ${d.p60} did not follow p_start`);
  }
  // exp_minutes given explicitly is the more specific claim and must survive
  // the re-derivation that a p_start override triggers.
  const pinned = reprojectPlayer(snap, subject, { exp_minutes: 90, p_start: 0.1 });
  if (Math.abs(pinned.derived.exp_minutes - 90) > 1e-9) {
    problems.push(`explicit exp_minutes was overwritten (${pinned.derived.exp_minutes})`);
  }
  console.log(`minutes  : ${problems.length ? "BROKEN" : "coupled"} `
    + `(${subject.name}: p_play ${subject.p_play.toFixed(3)} -> ${d.p_play.toFixed(3)})`);
  failures.push(...problems);
}

/* --------------------------------------------------- 4. plan weighting */
const saka = byId.get(snap.players[0].id);
const pw = planWeight(saka.gw, saka.hazard, 3.0, 8);
console.log(`planWeight sanity: ${saka.name} 8gw @hl3 = ${pw.toFixed(4)}`);

const ok = worst <= TOL_BASELINE && caseWorst <= TOL_OVERRIDE && !failures.length;
console.log(ok ? "\nPASS — the JS port matches Python everywhere."
               : `\nFAIL — ${failures.length} mismatches:\n` + failures.map((f) => "  " + f).join("\n"));
process.exit(ok ? 0 : 1);

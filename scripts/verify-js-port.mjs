
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { reprojectPlayer, planWeight } from "../fplkit/web/points.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");

const TOL_BASELINE = 5.01e-7;
const TOL_OVERRIDE = 5.01e-5;

const snap = JSON.parse(fs.readFileSync(path.join(ROOT, "out/snapshot.json")));
const byId = new Map(snap.players.map((p) => [p.id, p]));

let checked = 0, worst = 0, failures = [];

for (const p of snap.players) {
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
  const pinned = reprojectPlayer(snap, subject, { exp_minutes: 90, p_start: 0.1 });
  if (Math.abs(pinned.derived.exp_minutes - 90) > 1e-9) {
    problems.push(`explicit exp_minutes was overwritten (${pinned.derived.exp_minutes})`);
  }

  const hooked = reprojectPlayer(snap, subject, { p_start: 0.95, mins_if_start: 55 });
  const nailed = reprojectPlayer(snap, subject, { p_start: 0.95 });
  if (Math.abs(hooked.derived.p_play - nailed.derived.p_play) > 1e-9) {
    problems.push(`mins_if_start moved p_play (${nailed.derived.p_play} -> ${hooked.derived.p_play})`);
  }
  if (!(hooked.derived.p60 < nailed.derived.p60 - 1e-6)) {
    problems.push(`a shorter shift did not lower p60 (${nailed.derived.p60} -> ${hooked.derived.p60})`);
  }
  if (!(hooked.derived.exp_minutes < nailed.derived.exp_minutes - 1e-6)) {
    problems.push(`a shorter shift did not lower exp_minutes`);
  }
  const shortened = reprojectPlayer(snap, subject, { p_start: 0.9, exp_minutes: 45 });
  if (Math.abs(shortened.inputs.p_start - 0.9) > 1e-9) {
    problems.push(`exp_minutes moved p_start when the shift had room `
      + `(0.9 -> ${shortened.inputs.p_start})`);
  }
  console.log(`minutes  : ${problems.length ? "BROKEN" : "coupled"} `
    + `(${subject.name}: p_play ${subject.p_play.toFixed(3)} -> ${d.p_play.toFixed(3)})`);
  failures.push(...problems);
}

const saka = byId.get(snap.players[0].id);
const pw = planWeight(saka.gw, saka.hazard, 3.0, 8);
console.log(`planWeight sanity: ${saka.name} 8gw @hl3 = ${pw.toFixed(4)}`);

const ok = worst <= TOL_BASELINE && caseWorst <= TOL_OVERRIDE && !failures.length;
console.log(ok ? "\nPASS — the JS port matches Python everywhere."
               : `\nFAIL — ${failures.length} mismatches:\n` + failures.map((f) => "  " + f).join("\n"));
process.exit(ok ? 0 : 1);

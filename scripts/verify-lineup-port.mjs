/* Proves the browser rebalances a club the same way the model does.
 *
 * Overriding a player's minutes obliges his club to give those minutes up from
 * somewhere. Python does that in `model.renormalise_minutes`; the board does it
 * in `board.mjs:renormaliseMinutes`, because on a phone there is nothing else to
 * do it. If the two disagree, the lineup on screen is not the lineup the
 * projection was built from, and every number downstream of it is quietly
 * describing a different team.
 *
 * Run: node scripts/verify-lineup-port.mjs
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { derivePool, clubLineup } from "../fplkit/web/board.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");

/* Two tolerances, tracked apart, because they are set by different things and
   reporting one against the other's limit is how a real failure hides behind a
   rounding artefact.

   Per player: the reference stores p_start at 9dp, so agreement cannot read
   tighter than that. The bisection itself converges to machine precision.

   Per club: a sum over ~30 players, each carried at the snapshot's 10dp, so the
   accumulated rounding is a couple of orders of magnitude looser. */
const TOL_PLAYER = 5.01e-9;
const TOL_CLUB = 1e-6;

const snap = JSON.parse(fs.readFileSync(path.join(ROOT, "out/snapshot.json")));
const cases = JSON.parse(fs.readFileSync(path.join(ROOT, "scripts/lineup-cases.json")));

let checked = 0, worstPlayer = 0, worstClub = 0;
const failures = [];

for (const testCase of cases) {
  const edits = {};
  for (const [id, fields] of Object.entries(testCase.edits)) edits[+id] = fields;

  const pool = derivePool(snap, edits, { horizon: snap.gameweeks.length, halfLife: 3 });
  const byId = new Map(pool.players.map((p) => [p.id, p]));

  for (const [club, expected] of Object.entries(testCase.expected)) {
    const line = clubLineup(pool.players, club);
    const gap = Math.abs(line.starters - expected.starters);
    worstClub = Math.max(worstClub, gap);
    checked++;
    if (gap > TOL_CLUB) {
      failures.push(`${testCase.name} / ${club}: starters js `
        + `${line.starters.toFixed(6)} vs py ${expected.starters}`);
    }

    for (const [id, pStart] of Object.entries(expected.players)) {
      const got = byId.get(+id);
      if (!got) { failures.push(`${testCase.name}: player ${id} missing from pool`); continue; }
      const diff = Math.abs(got.p_start - pStart);
      worstPlayer = Math.max(worstPlayer, diff);
      checked++;
      if (diff > TOL_PLAYER && failures.length < 10) {
        failures.push(`${testCase.name} / ${got.name}: p_start js `
          + `${got.p_start.toFixed(10)} vs py ${pStart}`);
      }
    }
  }
}

console.log(`lineup  : ${checked} values over ${cases.length} cases`);
console.log(`  per player : worst |Δ| ${worstPlayer.toExponential(2)}  `
  + `(limit ${TOL_PLAYER.toExponential(2)})`);
console.log(`  per club   : worst |Δ| ${worstClub.toExponential(2)}  `
  + `(limit ${TOL_CLUB.toExponential(2)})`);

if (failures.length) {
  console.log("\nFAIL — the browser rebalances differently from the model:");
  console.log(failures.map((f) => "  " + f).join("\n"));
  process.exit(1);
}
console.log("\nPASS — the browser puts a club back together the way the model does.");

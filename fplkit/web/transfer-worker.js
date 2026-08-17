/* The transfer-and-chip MILP, off the main thread. Same shape as
 * solver-worker.js -- a request carries a sequence number and only the
 * newest one gets an answer, so dragging a setting while a solve is in
 * flight cannot land a stale plan on screen. This model is far bigger than
 * the squad optimiser's (a season's worth of players times a multi-gameweek
 * horizon, not one gameweek), so the wait this protects the UI from is
 * measured in seconds to tens of seconds, not a couple of hundred
 * milliseconds.
 *
 * A request carries a *list* of jobs, not one. Ranking a chip's gameweeks
 * honestly means re-solving the whole plan once per candidate week with the
 * chip pinned there -- nothing cheaper can do it, because the point of the
 * exercise is that each week gets to build its own squad. Six of those is
 * minutes, not seconds, so each job reports back as it lands: the caller can
 * show the first answer and a running count rather than a spinner that looks
 * indistinguishable from a hang.
 *
 * The seq check sits between jobs as well as around them, so a superseded
 * sweep stops at the next job boundary instead of running all six out.
 */

importScripts("./solver.js", "./transfers.js");

let newest = 0;

self.onmessage = async (event) => {
  const { seq, jobs } = event.data;
  newest = Math.max(newest, seq);
  if (seq < newest) return;

  const started = performance.now();
  const results = [];
  for (let i = 0; i < jobs.length; i++) {
    if (seq < newest) return; // superseded -- drop the rest of the sweep
    const { pool, opt, tag } = jobs[i];
    try {
      const result = await FplTransfers.planTransfers(pool, opt, "./vendor/");
      results.push({ tag, ok: true, result });
    } catch (error) {
      // One pinned week being infeasible is not a failed sweep: it means the
      // chip cannot be played that week given the constraints, which is a
      // legitimate answer about that week. Only a failed *first* job (the
      // plan itself) is fatal, and the caller decides that from `tag`.
      results.push({ tag, ok: false, error: String(error.message || error) });
    }
    if (seq < newest) return;
    self.postMessage({ seq, kind: "progress", done: i + 1, total: jobs.length,
                       tag, ms: Math.round(performance.now() - started) });
  }
  if (seq < newest) return;
  self.postMessage({ seq, kind: "done", results,
                     ms: Math.round(performance.now() - started) });
};

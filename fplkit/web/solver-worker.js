/* The MILP, off the main thread.
 *
 * HiGHS takes 0.2-3s on the full pool, and on a phone that is the upper end. On
 * the main thread that is a frozen page every time a slider moves — no scroll,
 * no tap, no spinner animating, because the spinner cannot animate either. Here
 * it is a spinner that actually spins.
 *
 * Requests carry a sequence number and the worker only ever answers the newest
 * one it has been given. Dragging a slider queues several solves and all but
 * the last describe settings that are already gone; the page filters stale
 * replies too, but dropping them here also saves the work.
 *
 * Two jobs come through here, because both are the same MILP under the same
 * settings and letting them share the counter is the point: a "nearmiss" sweep
 * is one solve per candidate and takes seconds, so a settings change arriving
 * mid-sweep must cancel it rather than leave twenty solves grinding away on an
 * answer to a question nobody is asking any more.
 */

importScripts("./solver.js");

let newest = 0;

self.onmessage = async (event) => {
  const { seq, kind, pool, options, settings } = event.data;
  newest = Math.max(newest, seq);
  if (seq < newest) return;

  const started = performance.now();
  const stopped = () => seq < newest;
  try {
    const result = kind === "nearmiss"
      ? await FplSolver.nearMisses(pool, options, {
          ...settings,
          stopped,
          onProgress: (done, total, row) => {
            if (stopped()) return;
            self.postMessage({ seq, kind: "progress", done, total, row });
          },
        }, "./vendor/")
      : await FplSolver.solveSquad(pool, options, "./vendor/");
    // `null` is a sweep that gave up part-way because it was superseded, and
    // the seq check below would drop the reply anyway.
    if (stopped() || result === null) return;
    self.postMessage({ seq, ok: true, result, ms: Math.round(performance.now() - started) });
  } catch (error) {
    if (stopped()) return;
    self.postMessage({ seq, ok: false, error: String(error.message || error) });
  }
};

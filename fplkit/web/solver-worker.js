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
 */

importScripts("./solver.js");

let newest = 0;

self.onmessage = async (event) => {
  const { seq, pool, options } = event.data;
  newest = Math.max(newest, seq);
  if (seq < newest) return;

  const started = performance.now();
  try {
    const result = await FplSolver.solveSquad(pool, options, "./vendor/");
    if (seq < newest) return;  // superseded while the solver was running
    self.postMessage({ seq, ok: true, result, ms: Math.round(performance.now() - started) });
  } catch (error) {
    if (seq < newest) return;
    self.postMessage({ seq, ok: false, error: String(error.message || error) });
  }
};

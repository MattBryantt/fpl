/* The transfer-and-chip MILP, off the main thread. Same shape as
 * solver-worker.js -- a request carries a sequence number and only the
 * newest one gets an answer, so dragging a setting while a solve is in
 * flight cannot land a stale plan on screen. This model is far bigger than
 * the squad optimiser's (a season's worth of players times a multi-gameweek
 * horizon, not one gameweek), so the wait this protects the UI from is
 * measured in seconds to tens of seconds, not a couple of hundred
 * milliseconds.
 */

importScripts("./solver.js", "./transfers.js");

let newest = 0;

self.onmessage = async (event) => {
  const { seq, pool, opt } = event.data;
  newest = Math.max(newest, seq);
  if (seq < newest) return;

  const started = performance.now();
  try {
    const result = await FplTransfers.planTransfers(pool, opt, "./vendor/");
    if (seq < newest) return;
    self.postMessage({ seq, ok: true, result, ms: Math.round(performance.now() - started) });
  } catch (error) {
    if (seq < newest) return;
    self.postMessage({ seq, ok: false, error: String(error.message || error) });
  }
};

/* One transfer-and-chip MILP, off the main thread. This model is far bigger
 * than the squad optimiser's (a season's worth of players times a
 * multi-gameweek horizon), so a solve is measured in seconds, not
 * milliseconds. transfer-view.mjs runs several of these workers side by side,
 * one job each at a time, and terminates them when a request is superseded.
 *
 * A job failing is not a failed sweep: one pinned week being infeasible means
 * the chip cannot be played that week given the constraints, which is a
 * legitimate answer about that week. The caller decides from the job's tag
 * whether a failure is fatal.
 */

importScripts("./solver.js", "./transfers.js");

self.onmessage = async (event) => {
  const { pool, opt } = event.data;
  try {
    const result = await FplTransfers.planTransfers(pool, opt, "./vendor/");
    self.postMessage({ ok: true, result });
  } catch (error) {
    self.postMessage({ ok: false, error: String(error.message || error) });
  }
};


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
    if (stopped() || result === null) return;
    self.postMessage({ seq, ok: true, result, ms: Math.round(performance.now() - started) });
  } catch (error) {
    if (stopped()) return;
    self.postMessage({ seq, ok: false, error: String(error.message || error) });
  }
};

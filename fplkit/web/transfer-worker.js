
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

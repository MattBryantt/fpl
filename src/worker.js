/**
 * The one thing a static host cannot do on its own: hold state that two
 * devices both need to see. Everything else about the board -- the
 * projection, the optimiser, browsing the pool -- runs entirely in the
 * browser off snapshot.json, which is why the site can be static at all. But
 * a squad picked on the phone and a squad picked on the laptop are the same
 * squad, and localStorage is per-device by definition, so *something* has to
 * sit between them. This is that something, and it is deliberately as small
 * as the job allows: one KV key, one route, one shape of request.
 *
 * Routing is set up in wrangler.jsonc so that only /api/* reaches this
 * script at all -- every other request is served straight from the `dist/`
 * assets binding without the Worker being invoked, which is what keeps a
 * board that is mostly static actually free to run.
 *
 * Auth is a single shared token, not a login system. There is one user. The
 * token is a Wrangler secret (`wrangler secret put FPL_TOKEN`), so it never
 * sits in the repo or in `dist/`, and the client sends it as `X-FPL-Token` --
 * the same header and the same `api()` helper the board already used for the
 * old `--lan` server, which is why nothing on the client had to learn a new
 * auth mechanism to gain this one.
 */

const STATE_KEY = "state:v1";
const MAX_BODY_BYTES = 2 * 1024 * 1024; // a real squad's worth of JSON is a few
                                        // hundred KB; anything past 2 MB is not
                                        // a sync request, it is a mistake or abuse

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Constant-time-ish comparison so a leaked response time cannot leak the
 *  token one character at a time. Hashing first also means two different-
 *  length tokens compare in the same number of operations. */
async function tokensMatch(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || !a || !b) return false;
  const enc = new TextEncoder();
  const [ah, bh] = await Promise.all(
    [a, b].map((s) => crypto.subtle.digest("SHA-256", enc.encode(s))));
  const av = new Uint8Array(ah), bv = new Uint8Array(bh);
  let diff = 0;
  for (let i = 0; i < av.length; i++) diff |= av[i] ^ bv[i];
  return diff === 0;
}

async function handleSync(request, env) {
  if (!env.FPL_TOKEN) {
    // A misconfigured deployment should fail loudly, not silently accept
    // every request because there was nothing to check against.
    return json({ error: "sync is not configured: FPL_TOKEN secret is unset" }, 500);
  }
  const supplied = request.headers.get("X-FPL-Token") || "";
  if (!(await tokensMatch(supplied, env.FPL_TOKEN))) {
    return json({ error: "bad or missing token" }, 401);
  }

  if (request.method === "GET") {
    const stored = await env.FPL_STATE.get(STATE_KEY, "json");
    return json(stored ? { exists: true, ...stored } : { exists: false });
  }

  if (request.method === "PUT" || request.method === "POST") {
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return json({ error: "body too large" }, 413);

    let body;
    try {
      body = JSON.parse(raw);
    } catch {
      return json({ error: "invalid JSON" }, 400);
    }
    if (typeof body !== "object" || body === null || typeof body.updated_at !== "number") {
      return json({ error: "expected {updated_at: number, drafts?, edits?, squad?, settings?}" }, 400);
    }

    // Last write wins, and it is the *pusher's* clock that decides that, not
    // this Worker's -- two devices comparing against a shared server time
    // would need to agree on drift, and they never will. What each client
    // needs is a number it recognises as its own, so it can tell "this is
    // what I sent" from "this is newer than what I sent" on the next pull.
    //
    // That comparison has to happen here too, not just on the client's pull:
    // two devices can push within the same round trip, and the network is
    // free to deliver them out of order. Without this check, a push that left
    // a client earlier but arrives here later would win outright and silently
    // erase a newer edit -- exactly the kind of loss this endpoint exists to
    // prevent.
    const existing = await env.FPL_STATE.get(STATE_KEY, "json");
    if (existing && typeof existing.updated_at === "number" && existing.updated_at >= body.updated_at) {
      return json({ ok: true, stale: true, updated_at: existing.updated_at });
    }

    const record = {
      updated_at: body.updated_at,
      drafts: Array.isArray(body.drafts) ? body.drafts : [],
      edits: (body.edits && typeof body.edits === "object") ? body.edits : {},
      squad: Array.isArray(body.squad) ? body.squad : [],
      // Opaque to the Worker -- the Settings panel's controls and bench
      // weights, in whatever shape the client's syncableSettings() produces.
      // A new control does not need a matching change here.
      settings: (body.settings && typeof body.settings === "object") ? body.settings : {},
    };
    await env.FPL_STATE.put(STATE_KEY, JSON.stringify(record));
    return json({ ok: true, updated_at: record.updated_at });
  }

  return json({ error: "method not allowed" }, 405);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/sync") return handleSync(request, env);

    // Nothing else should reach this script -- wrangler.jsonc scopes
    // run_worker_first to /api/*, and everything outside it is served
    // straight from the assets binding without the Worker being invoked at
    // all. This is a fallback for that assumption being wrong, not the
    // normal path.
    if (env.ASSETS) return env.ASSETS.fetch(request);
    return json({ error: "not found" }, 404);
  },
};

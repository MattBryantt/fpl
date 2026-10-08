
const STATE_KEY = "state:v1";
const MAX_BODY_BYTES = 2 * 1024 * 1024;

const FPL_BASE = "https://fantasy.premierleague.com/api";
const FPL_HEADERS = { "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" };

async function handleLive(request, env, teamId) {
  if (!env.FPL_TOKEN) {
    return json({ error: "live team lookup is not configured: FPL_TOKEN secret is unset" }, 500);
  }
  const supplied = request.headers.get("X-FPL-Token") || "";
  if (!(await tokensMatch(supplied, env.FPL_TOKEN))) {
    return json({ error: "bad or missing token" }, 401);
  }
  if (!/^\d+$/.test(teamId || "")) return json({ error: "bad team id" }, 400);

  const gw = new URL(request.url).searchParams.get("gw");
  if (gw !== null && !/^\d+$/.test(gw)) return json({ error: "bad gw" }, 400);

  try {
    const get = (path) => fetch(`${FPL_BASE}/${path}`, { headers: FPL_HEADERS });
    const picksUrl = gw ? `entry/${teamId}/event/${gw}/picks/` : null;
    const [historyRes, picksRes, transfersRes] = await Promise.all([
      get(`entry/${teamId}/history/`),
      picksUrl ? get(picksUrl) : Promise.resolve(null),
      get(`entry/${teamId}/transfers/`),
    ]);
    if (!historyRes.ok) {
      return json({ error: `FPL history lookup failed (${historyRes.status})` }, historyRes.status);
    }
    if (picksRes && !picksRes.ok) {
      return json({ error: `FPL picks lookup failed (${picksRes.status})` }, picksRes.status);
    }
    const picks = picksRes ? await picksRes.json() : null;
    const summaries = {};
    await Promise.all((picks?.picks || []).map(async (p) => {
      const res = await get(`element-summary/${p.element}/`);
      if (res.ok) summaries[p.element] = (await res.json()).history || [];
    }));
    return json({
      history: await historyRes.json(),
      picks,
      transfers: transfersRes.ok ? await transfersRes.json() : [],
      summaries,
    });
  } catch (error) {
    return json({ error: `upstream fetch failed: ${error.message}` }, 502);
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

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
      return json({ error: "expected {updated_at: number, drafts?, edits?, squad?, purchase?, settings?}" }, 400);
    }

    const existing = await env.FPL_STATE.get(STATE_KEY, "json");
    if (existing && typeof existing.updated_at === "number" && existing.updated_at >= body.updated_at) {
      return json({ ok: true, stale: true, updated_at: existing.updated_at });
    }

    const record = {
      updated_at: body.updated_at,
      drafts: Array.isArray(body.drafts) ? body.drafts : [],
      edits: (body.edits && typeof body.edits === "object") ? body.edits : {},
      editsAt: (body.editsAt && typeof body.editsAt === "object") ? body.editsAt : {},
      squad: Array.isArray(body.squad) ? body.squad : [],
      purchase: (body.purchase && typeof body.purchase === "object") ? body.purchase : {},
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
    const liveMatch = url.pathname.match(/^\/api\/live\/([^/]+)$/);
    if (liveMatch) return handleLive(request, env, liveMatch[1]);

    if (env.ASSETS) return env.ASSETS.fetch(request);
    return json({ error: "not found" }, 404);
  },
};

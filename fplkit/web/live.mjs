/* A manager's live team, read from FPL's own public API -- no login, so no
 * per-player selling price (see fpl_api.live_squad's docstring for what that
 * means; the same fallback -- current listed price -- applies here).
 *
 * `/api/live/{id}` answers two different ways depending on what is running
 * behind it. The local dev server has Python do the composition
 * (fpl_api.live_squad(): element -> this season's fpl_id is already true, so
 * only bank/value/the free-transfer replay need computing) and returns that
 * directly. The deployed board has no Python at all -- its /api/live route
 * is a deliberately dumb Cloudflare Worker proxy (see src/worker.js) that
 * only bundles FPL's raw picks+history JSON, because CORS blocks the browser
 * from reaching FPL's API on its own. This module is what turns that raw
 * bundle into the same shape Python already produces, so the one thing that
 * calls fetchLiveTeam() does not need to know which backend answered.
 */

const FREE_TRANSFERS_PER_GW = 1;
const MAX_FREE_TRANSFERS = 5;

/** JS port of transfers.py's next_free_transfers() -- kept in lockstep with
 *  the LP it matches by scripts/verify-transfer-rules.py on the Python side;
 *  there is no MILP on this side to check it against directly, so this is
 *  intentionally a direct, literal translation rather than a reimplementation
 *  from the rule description. */
export function nextFreeTransfers(ft, spent, playedFreehit = false) {
  const earned = FREE_TRANSFERS_PER_GW - (playedFreehit ? 1 : 0);
  const raw = ft - spent + earned;
  return Math.max(1, Math.min(MAX_FREE_TRANSFERS, raw));
}

/** Turns FPL's raw {picks, history} bundle into the canonical shape --
 *  mirrors fpl_api.live_squad() field for field, differing only in
 *  camelCase vs snake_case. */
export function composeLiveSquad({ picks, history }, gw) {
  const rows = picks?.picks || [];
  const squadIds = rows.map((p) => p.element);
  const captainId = rows.find((p) => p.is_captain)?.element ?? null;

  const eh = picks?.entry_history || {};
  const bank = (eh.bank ?? 0) / 10;
  const value = (eh.value ?? 0) / 10;

  const chips = history?.chips || [];
  const chipsUsed = chips.map((c) => c.name);

  let ft = 1; // the first gameweek after preseason always opens on exactly one
  const current = [...(history?.current || [])].sort((a, b) => a.event - b.event);
  for (const row of current) {
    if (row.event < 2 || row.event > gw) continue;
    const freehit = chips.some((c) => c.event === row.event && c.name === "freehit");
    ft = nextFreeTransfers(ft, row.event_transfers, freehit);
  }

  return {
    gw, squadIds, captainId, bank, value,
    budgetTotal: Math.round((bank + value) * 10) / 10,
    chipsUsed, activeChip: picks?.active_chip ?? null, freeTransfers: ft,
  };
}

/** Renames Python's already-composed snake_case response onto the same
 *  shape composeLiveSquad() produces -- a naming bridge, not a second
 *  implementation of the composition itself. */
function normalizeComposed(data) {
  return {
    gw: data.gw, squadIds: data.squad_ids, captainId: data.captain_id,
    bank: data.bank, value: data.value, budgetTotal: data.budget_total,
    chipsUsed: data.chips_used, activeChip: data.active_chip,
    freeTransfers: data.free_transfers,
  };
}

/** Fetches and normalizes a manager's live team, however `/api/live/{id}` is
 *  actually being served this session. `apiFetch` is the board's existing
 *  `api()` helper (adds the sync token header, works unmodified against
 *  either backend since both mount the route at the same relative path).
 *
 *  `gw` is required, not defaulted: the local server can resolve a missing
 *  one itself (fpl_api.live_squad falls back to next_gameweek() - 1), but
 *  the deployed Worker cannot -- it is deliberately dumb and has no bootstrap
 *  data to work that out from, so it fetches nothing at all for `picks`
 *  without one. Callers should pass `S.meta.start_gw - 1` (clamped to >= 1)
 *  -- the projection horizon's first gameweek is the *next* one to plan for,
 *  one ahead of the last gameweek a manager's picks actually exist for. */
export async function fetchLiveTeam(apiFetch, teamId, gw) {
  const res = await apiFetch(`/api/live/${teamId}?gw=${gw}`);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.error || detail.detail || `live team lookup failed (${res.status})`);
  }
  const data = await res.json();
  return ("picks" in data || "history" in data) ? composeLiveSquad(data, gw) : normalizeComposed(data);
}

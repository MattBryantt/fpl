/* A manager's live team, read from FPL's own public API -- no login, so no
 * per-player selling price stated outright. It is rebuilt instead from what
 * the public endpoints do say: the transfer log carries what every in-season
 * purchase cost, and a player's own gameweek history carries the price he was
 * listed at in gameweek 1, which is what the opening squad paid (see
 * fpl_api.purchase_prices for the Python original of this).
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
// Half of any profit goes back to the game on a sale (config.SELL_ON_FEE).
const SELL_ON_FEE = 0.5;
// Chips under which the week's transfers are free and unlimited, and neither
// spend nor earn a free transfer (fpl_api.TRANSFER_CHIPS).
const TRANSFER_CHIPS = ["freehit", "wildcard"];

/** JS port of transfers.py's next_free_transfers() -- kept in lockstep with
 *  the LP it matches by scripts/verify-transfer-rules.py on the Python side;
 *  there is no MILP on this side to check it against directly, so this is
 *  intentionally a direct, literal translation rather than a reimplementation
 *  from the rule description. */
export function nextFreeTransfers(ft, spent, playedChip = false) {
  const earned = FREE_TRANSFERS_PER_GW - (playedChip ? 1 : 0);
  const raw = ft - spent + earned;
  return Math.max(1, Math.min(MAX_FREE_TRANSFERS, raw));
}

/** Port of transfers.py's sell_price(): purchase price plus half of any rise,
 *  rounded down to £0.1m; a fall is taken in full. */
export function sellPrice(bought, now) {
  const profit = Math.round((now - bought) * 10);
  if (profit <= 0) return Math.round(now * 10) / 10;
  return Math.round(bought * 10 + Math.floor(profit * (1 - SELL_ON_FEE))) / 10;
}

/** Port of fpl_api.purchase_prices(): the latest non-free-hit purchase of each
 *  owned player from the transfer log, else his gameweek-1 price from his own
 *  history (`summaries` is {id: history rows}). Missing means unknown. */
export function purchasePrices(squadIds, transferLog, freehitGws, summaries) {
  const bought = {};
  const log = [...(transferLog || [])].sort((a, b) => a.event - b.event || String(a.time).localeCompare(String(b.time)));
  for (const move of log) {
    if (freehitGws.has(move.event)) continue;
    bought[move.element_in] = move.element_in_cost / 10;
  }
  const out = {};
  for (const id of squadIds) {
    if (bought[id] != null) { out[id] = bought[id]; continue; }
    const opening = (summaries?.[id] || []).find((r) => r.round === 1);
    if (opening) out[id] = opening.value / 10;
  }
  return out;
}

/** Turns FPL's raw {picks, history} bundle into the canonical shape --
 *  mirrors fpl_api.live_squad() field for field, differing only in
 *  camelCase vs snake_case. */
export function composeLiveSquad({ picks, history, transfers, summaries }, gw, priceNow) {
  const rows = picks?.picks || [];
  const squadIds = rows.map((p) => p.element);
  const captainId = rows.find((p) => p.is_captain)?.element ?? null;

  const eh = picks?.entry_history || {};
  const bank = (eh.bank ?? 0) / 10;
  // FPL's own team value, bank included -- passed through for reference, not
  // added to the bank again.
  const value = (eh.value ?? 0) / 10;

  const chips = history?.chips || [];
  const chipsUsed = chips.map((c) => c.name);
  const chipAt = new Map(chips.map((c) => [c.event, c.name]));

  let ft = 1; // the first gameweek after preseason always opens on exactly one
  const current = [...(history?.current || [])].sort((a, b) => a.event - b.event);
  for (const row of current) {
    if (row.event < 2 || row.event > gw) continue;
    const chip = TRANSFER_CHIPS.includes(chipAt.get(row.event));
    ft = nextFreeTransfers(ft, chip ? 0 : row.event_transfers, chip);
  }

  const freehitGws = new Set(chips.filter((c) => c.name === "freehit").map((c) => c.event));
  const purchase = purchasePrices(squadIds, transfers, freehitGws, summaries);
  const sell = {};
  for (const id of squadIds) {
    const now = priceNow(id);
    sell[id] = sellPrice(purchase[id] ?? now, now);
  }
  const worth = Object.values(sell).reduce((a, b) => a + b, 0);

  return {
    gw, squadIds, captainId, bank, value,
    purchasePrices: purchase, sellPrices: sell,
    budgetTotal: Math.round((bank + worth) * 10) / 10,
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
    purchasePrices: data.purchase_prices || {}, sellPrices: data.sell_prices || {},
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
export async function fetchLiveTeam(apiFetch, teamId, gw, priceNow) {
  const res = await apiFetch(`/api/live/${teamId}?gw=${gw}`);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.error || detail.detail || `live team lookup failed (${res.status})`);
  }
  const data = await res.json();
  return ("picks" in data || "history" in data) ? composeLiveSquad(data, gw, priceNow) : normalizeComposed(data);
}

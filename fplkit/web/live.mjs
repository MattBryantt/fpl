
const FREE_TRANSFERS_PER_GW = 1;
const MAX_FREE_TRANSFERS = 5;
const SELL_ON_FEE = 0.5;
const TRANSFER_CHIPS = ["freehit", "wildcard"];

export function nextFreeTransfers(ft, spent, playedChip = false) {
  const earned = FREE_TRANSFERS_PER_GW - (playedChip ? 1 : 0);
  const raw = ft - spent + earned;
  return Math.max(1, Math.min(MAX_FREE_TRANSFERS, raw));
}

export function sellPrice(bought, now) {
  const profit = Math.round((now - bought) * 10);
  if (profit <= 0) return Math.round(now * 10) / 10;
  return Math.round(bought * 10 + Math.floor(profit * (1 - SELL_ON_FEE))) / 10;
}

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

export function composeLiveSquad({ picks, history, transfers, summaries }, gw, priceNow) {
  const rows = picks?.picks || [];
  const squadIds = rows.map((p) => p.element);
  const captainId = rows.find((p) => p.is_captain)?.element ?? null;

  const eh = picks?.entry_history || {};
  const bank = (eh.bank ?? 0) / 10;
  const value = (eh.value ?? 0) / 10;

  const chips = history?.chips || [];
  const chipsUsed = chips.map((c) => c.name);
  const chipAt = new Map(chips.map((c) => [c.event, c.name]));

  let ft = 1;
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

function normalizeComposed(data) {
  return {
    gw: data.gw, squadIds: data.squad_ids, captainId: data.captain_id,
    bank: data.bank, value: data.value, budgetTotal: data.budget_total,
    purchasePrices: data.purchase_prices || {}, sellPrices: data.sell_prices || {},
    chipsUsed: data.chips_used, activeChip: data.active_chip,
    freeTransfers: data.free_transfers,
  };
}

export async function fetchLiveTeam(apiFetch, teamId, gw, priceNow) {
  const res = await apiFetch(`/api/live/${teamId}?gw=${gw}`);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.error || detail.detail || `live team lookup failed (${res.status})`);
  }
  const data = await res.json();
  return ("picks" in data || "history" in data) ? composeLiveSquad(data, gw, priceNow) : normalizeComposed(data);
}

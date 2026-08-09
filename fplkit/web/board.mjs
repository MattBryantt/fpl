/* The data layer, with no server behind it.
 *
 * This is what `/api/pool` used to be. Given the snapshot and the settings on
 * screen it produces exactly the row shape the board already renders, so the
 * rest of the page did not have to learn that its data stopped coming over the
 * wire.
 *
 * The work is split by cost. An unedited player's per-gameweek points were
 * computed by the Python pipeline and travel in the snapshot, so changing the
 * horizon or the half-life is a sum over a slice and a pair of closed-form
 * curves — 573 players in well under a millisecond. Only an *edited* player is
 * rescored from his inputs, and there are never many of those.
 */

import { reprojectPlayer, planWeight, applyOverrides } from "./points.mjs";

/** A snapshot restricted to the first `horizon` gameweeks.
 *  Shortening the horizon is a truncation of what is already known, never a
 *  reason to go back to the laptop. */
export function truncate(snap, horizon) {
  const count = Math.max(1, Math.min(horizon, snap.gameweeks.length));
  if (count === snap.gameweeks.length) return snap;
  const gameweeks = snap.gameweeks.slice(0, count);
  const keep = new Set(gameweeks);
  return { ...snap, gameweeks, fixtures: snap.fixtures.filter((f) => keep.has(f.gw)) };
}

const sum = (xs, upTo) => {
  let total = 0;
  for (let i = 0; i < Math.min(upTo, xs.length); i++) total += xs[i];
  return total;
};

/* Which edits move a player's minutes, and so oblige his club to rebalance.
   A `_mult` on either counts: the question is whether the number changed, not
   how it was expressed. */
const touchesMinutes = (edit) =>
  !!edit && ["p_start", "exp_minutes", "p_start_mult", "exp_minutes_mult"]
    .some((f) => edit[f] !== undefined && edit[f] !== null);

/** Put each club back to eleven starters after an override moved somebody.
 *
 *  The port of model.renormalise_minutes, and the reason it exists: minutes are
 *  a fixed pool. Saying a player starts can only mean somebody else does not,
 *  and without this the board would happily show a club fielding twelve men and
 *  out-scoring the odds. Overridden players are pinned; everyone else is scaled
 *  by one bounded multiplier per club, so the pecking order survives.
 *
 *  Returns fpl_id -> p_start for the players the rebalance *moved*, which the
 *  caller layers underneath the user's own edits.
 */
export function renormaliseMinutes(snap, edits) {
  const rules = snap.rules;
  const maxStart = rules.MAX_P_START ?? 0.95;
  const maxScale = rules.MAX_MINUTES_SCALE ?? 2.5;
  const outfield = rules.XI_OUTFIELD ?? 10;

  const touched = new Set();
  for (const [id, edit] of Object.entries(edits || {})) {
    if (touchesMinutes(edit)) touched.add(+id);
  }
  if (!touched.size) return new Map();

  const clubs = new Set();
  for (const p of snap.players) if (touched.has(p.id)) clubs.add(p.team);

  const moved = new Map();
  for (const club of clubs) {
    for (const keeper of [true, false]) {
      const target = keeper ? 1 : outfield;
      const group = snap.players.filter(
        (p) => p.team === club && (p.pos === "GKP") === keeper);
      const free = group.filter((p) => !touched.has(p.id));
      if (!free.length) continue;

      let spokenFor = 0;
      for (const p of group) {
        if (!touched.has(p.id)) continue;
        spokenFor += applyOverrides(p, edits[p.id], rules).p_start;
      }
      const remaining = Math.max(target - spokenFor, 0);

      const values = free.map((p) => p.p_start);
      if (values.reduce((a, b) => a + b, 0) <= 0) continue;
      const fielded = (lam) =>
        values.reduce((a, v) => a + Math.min(maxStart, v * lam), 0);

      let lam;
      if (fielded(maxScale) <= remaining) {
        lam = maxScale;
      } else {
        let lo = 0, hi = maxScale;
        for (let i = 0; i < 60; i++) {
          const mid = (lo + hi) / 2;
          if (fielded(mid) < remaining) lo = mid; else hi = mid;
        }
        lam = (lo + hi) / 2;
      }
      // Only players the rebalance actually moved. A group with nothing pinned
      // in it -- the keepers, when the override was an outfielder -- solves to
      // a multiplier of one, and recording those would label a goalkeeper
      // "adjusted" for standing still, as well as paying to rescore a whole
      // club that did not change.
      free.forEach((p, i) => {
        const next = Math.min(maxStart, values[i] * lam);
        if (Math.abs(next - p.p_start) > 1e-9) moved.set(p.id, next);
      });
    }
  }
  return moved;
}

/** The edits actually used for scoring: the user's, plus the rebalance an
 *  overridden club owes its other players. The user's own numbers win. */
export function effectiveEdits(snap, edits) {
  const moved = renormaliseMinutes(snap, edits);
  if (!moved.size) return edits || {};
  const out = { ...(edits || {}) };
  for (const [id, pStart] of moved) {
    if (!touchesMinutes(out[id])) out[id] = { ...(out[id] || {}), p_start: pStart };
  }
  return out;
}

/** The player pool as the board renders it, for one set of settings.
 *  `edits` maps fpl_id -> {field: value}. */
export function derivePool(snap, edits, { horizon, halfLife }) {
  const view = truncate(snap, horizon);
  const count = view.gameweeks.length;
  const overridable = Object.keys(snap.rules.OVERRIDABLE);

  // Matches per club over the horizon, so xPts can be turned into a per-game
  // number. Not the same as the gameweek count: a double gameweek is two games
  // and a blank is none, which is exactly the distinction that makes xPts alone
  // misleading when you compare two players on different fixture runs.
  const games = {};
  for (const f of view.fixtures) {
    games[f.home_team] = (games[f.home_team] || 0) + 1;
    games[f.away_team] = (games[f.away_team] || 0) + 1;
  }

  const rows = [];

  // The user's edits, plus whatever rebalancing his clubs owe. A player moved
  // by the rebalance is scored like any other edited player, but the board has
  // to be able to tell the two apart -- one is an opinion, the other is its
  // consequence, and showing them the same way would claim the user said
  // something he did not.
  const userEdits = edits || {};
  const applied = effectiveEdits(snap, userEdits);

  for (const raw of snap.players) {
    const edit = applied[raw.id];
    const edited = !!(edit && Object.keys(edit).length);
    const byUser = !!(userEdits[raw.id] && Object.keys(userEdits[raw.id]).length);
    let gw, cs, price, pStart, pPlay, expMinutes;

    if (edited) {
      const out = reprojectPlayer(view, raw, edit);
      gw = out.gw;
      cs = out.breakdown.exp_clean_sheets || 0;
      price = out.inputs.price;
      pStart = out.inputs.p_start;
      expMinutes = out.derived.exp_minutes ?? raw.exp_minutes;
      // p_play decides who the optimiser is allowed to look at, so it has to
      // follow an override of p_start rather than stay at the model's value --
      // the same coupling that made an edited player invisible to the solver
      // when the server patched columns one at a time. It is derived, not
      // overridable, which is why it comes from `derived` and not `inputs`.
      pPlay = out.derived.p_play ?? raw.p_play;
    } else {
      gw = raw.gw.slice(0, count);
      cs = sum(raw.cs, count);
      price = raw.price;
      pStart = raw.p_start;
      pPlay = raw.p_play;
      expMinutes = raw.exp_minutes;
    }

    // The model's own numbers, kept apart from the edited ones so the stat
    // editor can always say what an override changed.
    const inputs = {};
    for (const field of overridable) if (field in raw) inputs[field] = raw[field];

    const played = games[raw.team] || 0;
    const xptsRaw = sum(gw, count);

    rows.push({
      id: raw.id, name: raw.name, full_name: raw.full_name,
      pos: raw.pos, team: raw.team, team_short: raw.team_short,
      price, p_start: pStart,
      xpts_plan: planWeight(gw, raw.hazard, halfLife),
      xpts_raw: xptsRaw,
      model_xpts_plan: planWeight(raw.gw.slice(0, count), raw.hazard, halfLife),
      // Undecayed and per match on purpose. xpts_plan discounts distant
      // gameweeks, which is right for ranking a squad and wrong for a rate you
      // want to read against last season's PPG.
      games: played,
      xppg: played ? xptsRaw / played : 0,
      ppg: raw.ppg,
      minutes_last: raw.minutes_last,
      // Last season on the same footing as xPPG: total points over all 38
      // fixtures, including the ones he missed. PPG divides by appearances and
      // so silently ignores them, which is why the two columns look further
      // apart than the model and last season actually are.
      ppg_fixture: (raw.pts_last || 0) / 38,
      apps_last: raw.ppg ? Math.round((raw.pts_last || 0) / raw.ppg) : 0,
      cs,
      owned: raw.owned, price_change: raw.price_change,
      confidence: raw.confidence, recency: raw.recency,
      moved: raw.moved, previous_club: raw.previous_club,
      status: raw.status, news: raw.news,
      gw, opp: raw.opp.slice(0, count),
      p_play: pPlay,
      exp_minutes: expMinutes,
      inputs,
      edited: byUser,
      // Moved to keep his club at eleven starters, not by an opinion of yours.
      adjusted: edited && !byUser,
    });
  }

  return {
    players: rows,
    gameweeks: view.gameweeks,
    meta: {
      ...snap.meta,
      horizon: count,
      half_life: halfLife,
      generated_at: snap.generated_at,
      budget: snap.rules.DEFAULT_BUDGET,
      max_per_club: snap.rules.MAX_PER_CLUB,
      squad_by_pos: snap.rules.SQUAD_BY_POS,
      xi_min: snap.rules.XI_MIN_BY_POS,
      xi_max: snap.rules.XI_MAX_BY_POS,
      bench_slot_weights: snap.rules.DEFAULT_BENCH_SLOT_WEIGHTS,
      priced_gws: (snap.meta.priced_gws || []).filter((gw) => view.gameweeks.includes(gw)),
      snapshot_horizon: snap.gameweeks.length,
    },
  };
}

/* The scoring components, in the order a person reads them: what he is paid for
   just by playing, then what he is paid for doing something, then what is taken
   back off him. `sign` is what the term can be, not what it is — the concession
   and card terms are the only ones that arrive negative, and knowing that in
   advance is what lets the bars be laid out before the numbers are in. */
export const POINT_SOURCES = [
  { key: "xpts_appearance", label: "Appearance", note: "1 for playing, 1 more for 60 minutes" },
  { key: "xpts_goals", label: "Goals", note: "open play plus penalties, if he takes them" },
  { key: "xpts_assists", label: "Assists" },
  { key: "xpts_clean_sheet", label: "Clean sheets", note: "only counts if he is on the pitch" },
  { key: "xpts_defcon", label: "Defensive contribution", note: "2 points when he clears the threshold" },
  { key: "xpts_bonus", label: "Bonus" },
  { key: "xpts_saves", label: "Saves", note: "1 per 3, keepers only" },
  { key: "xpts_conceded", label: "Goals conceded", note: "−1 per 2 conceded while on the pitch", sign: -1 },
  { key: "xpts_cards", label: "Cards and penalty misses", sign: -1 },
];

/** Everything behind one player's projection, for the panel that explains it.
 *
 *  The numbers were always there -- `playerFixturePoints` returns a full
 *  decomposition and has since the beginning -- but only `exp_clean_sheets` was
 *  ever read out of it, so the board could show a total and nothing that added
 *  up to it. This returns the whole thing, for an edited player and an unedited
 *  one alike, because "why is he worth this?" is a question about every player
 *  and not only the ones somebody has already argued with.
 *
 *  Two totals come back, and the gap between them is the thing most worth
 *  understanding: `raw_total` is what the fixtures are worth, `plan_total` is
 *  what they are worth *to a decision made today*, once distant gameweeks are
 *  discounted and the chance he is still available is priced in. The board ranks
 *  on the second and people read the first, which is most of the confusion.
 */
export function explainPlayer(snap, raw, edit, { horizon, halfLife }) {
  const view = truncate(snap, horizon);
  const applied = edit && Object.keys(edit).length ? edit : null;
  const out = reprojectPlayer(view, raw, applied);

  const rows = view.gameweeks.map((gw, i) => {
    const decay = Math.pow(0.5, i / halfLife);
    const survival = Math.pow(1 - (raw.hazard || 0), i);
    const points = out.gw[i] || 0;
    return {
      gw, points, decay, survival,
      weight: decay * survival,
      weighted: points * decay * survival,
      opp: (raw.opp || [])[i] || "",
    };
  });

  const games = view.fixtures.filter(
    (f) => f.home_team === raw.team || f.away_team === raw.team).length;
  const total = (pick) => rows.reduce((a, r) => a + pick(r), 0);

  return {
    breakdown: out.breakdown,
    inputs: out.inputs,
    derived: out.derived,
    gw: rows,
    games,
    hazard: raw.hazard || 0,
    half_life: halfLife,
    raw_total: total((r) => r.points),
    plan_total: total((r) => r.weighted),
    edited: !!applied,
  };
}

/** One club's expected lineup: who starts, for how long, and what moved.
 *
 *  Minutes are the model's least reliable input and its most powerful one, so
 *  this is the view worth having when you are feeding it real team news. It
 *  reads the pool rather than recomputing, so it always agrees with what the
 *  rest of the board is showing, and it reports the club's `starters` total --
 *  which should sit at 11.00, and says so when an override has pushed it past
 *  what a team can actually field.
 */
export function clubLineup(players, team) {
  const squad = players.filter((p) => p.team === team)
    .sort((a, b) => b.exp_minutes - a.exp_minutes || b.xpts_raw - a.xpts_raw);

  const starters = squad.reduce((a, p) => a + p.p_start, 0);
  const keeper = squad.filter((p) => p.pos === "GKP")
    .reduce((a, p) => a + p.p_start, 0);

  return {
    team,
    players: squad,
    starters,
    keepers: keeper,
    outfield: starters - keeper,
    // A club fields eleven. Anything else means the overrides in play do not
    // describe a legal team, which is worth saying out loud rather than
    // leaving to be inferred from a column of numbers.
    balanced: Math.abs(starters - 11) < 0.01,
    edited: squad.some((p) => p.edited),
    minutes: squad.reduce((a, p) => a + p.exp_minutes, 0),
  };
}

/** Recompute one player under a set of overrides — what `/api/edit` did. */
export function editPlayer(snap, playerId, overrides, { horizon, halfLife }) {
  const view = truncate(snap, horizon);
  const raw = snap.players.find((p) => p.id === playerId);
  if (!raw) throw new Error(`no player with id ${playerId}`);
  const out = reprojectPlayer(view, raw, overrides);
  out.xpts_plan = planWeight(out.gw, raw.hazard, halfLife);
  return out;
}

/** Reject field names the model does not know, rather than ignoring them —
 *  the same check the server does, for the same reason: a silently dropped
 *  override is indistinguishable from one that had no effect. */
export function checkOverridable(fields, snap) {
  const known = new Set(Object.keys(snap.rules.OVERRIDABLE));
  const ok = (f) => known.has(f) || (f.endsWith("_mult") && known.has(f.slice(0, -5)));
  // `gw` is the one reserved key: per-match overrides, checked a level down.
  const unknown = Object.keys(fields || {}).filter((f) => f !== "gw" && !ok(f));
  if (unknown.length) throw new Error(`not overridable: ${unknown.sort().join(", ")}`);
  for (const [gameweek, perMatch] of Object.entries(fields?.gw || {})) {
    const bad = Object.keys(perMatch).filter((f) => !ok(f));
    if (bad.length) throw new Error(`not overridable in GW${gameweek}: ${bad.sort().join(", ")}`);
  }
}

/** How stale the snapshot is, in words. */
export function age(generatedAt) {
  const then = new Date(generatedAt);
  if (isNaN(then)) return "unknown age";
  const minutes = Math.round((Date.now() - then) / 60000);
  if (minutes < 2) return "just now";
  if (minutes < 90) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

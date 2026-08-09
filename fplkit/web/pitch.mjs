/* The squad, drawn as a team rather than listed as a table.
 *
 * This is a pure renderer: it takes a squad, the pool rows behind it and a few
 * decisions already made elsewhere (which eleven start, who is captain, how the
 * bench is ordered) and returns HTML. It computes none of them itself, because
 * the board's own `bestXI`, `benchSlots` and the solver already agree on those
 * answers and a second opinion here would be a second source of truth.
 *
 * Two things it does own, because they are presentation:
 *
 *   * the *shape* of the pitch — one row per position, the XI laid out the way
 *     a formation is written, with the bench beneath it rather than mixed in;
 *   * what the number under each name is. A shirt has room for exactly one
 *     figure, so which one is a setting rather than a decision the page can make
 *     for you: xPts to rank on, xPPG to compare on, price to budget on, and the
 *     next fixture when the question is who is playing whom.
 *
 * Shirts come from /shirts/<club code>.png, which the server mirrors off the
 * FPL CDN and caches on disk. They are decorative: `wireShirts` swaps in a
 * lettered tile the moment one fails, so a missing image costs a picture and
 * never a player.
 */

const POS_ORDER = ["GKP", "DEF", "MID", "FWD"];

const fmt = (v, d = 1) =>
  (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);

const escape = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** The shirt file for one player: the club's code, and the keeper variant for a
 *  goalkeeper — every club's keeper plays in a different kit from his outfield
 *  team-mates, which on a pitch view is how you spot him without reading. */
export function shirtUrl(teams, player) {
  const code = teams?.[player.team]?.code;
  if (!code) return "";
  return `/shirts/${code}${player.pos === "GKP" ? "_1" : ""}.png`;
}

/* An opponent label is written for a table -- "Nott'm Forest (H)" -- and a shirt
   has room for about six characters. The clubs are already in the snapshot with
   their three-letter names, so shorten against that rather than truncating. */
function shortOpponent(label, teams) {
  if (!label) return "—";
  return label.split(" + ").map((leg) => {
    const match = /^(.*?)\s*\((H|A)\)$/.exec(leg.trim());
    if (!match) return leg.trim();
    const [, name, where] = match;
    const short = teams?.[name]?.short || name.slice(0, 3).toUpperCase();
    return where === "H" ? short : short.toLowerCase();
  }).join("+");
}

/** What the number under each name means. `home` is the header the segmented
 *  control shows; `title` is what a hover says, because a bare figure on a shirt
 *  is only readable if you can find out what it is. */
export const METRICS = {
  xpts: {
    label: "xPts", head: "Plan-weighted points over the horizon",
    get: (p) => fmt(p.xpts_plan, 1),
    title: (p) => `${fmt(p.xpts_plan, 1)} plan-weighted points over the horizon`,
  },
  xppg: {
    label: "xPPG", head: "Projected points per match, undecayed",
    get: (p) => fmt(p.xppg, 2),
    title: (p) => `${fmt(p.xppg, 2)} projected points per match over ${p.games} `
                + `fixture${p.games === 1 ? "" : "s"}`,
  },
  price: {
    label: "£", head: "Price",
    get: (p) => "£" + fmt(p.price, 1),
    title: (p) => `£${fmt(p.price, 1)}m`,
  },
  ppg: {
    label: "PPG", head: "Last season's points per appearance",
    get: (p) => (p.ppg ? fmt(p.ppg, 1) : "—"),
    title: (p) => (p.ppg ? `${fmt(p.ppg, 1)} points per appearance last season`
                         : "no appearances last season"),
  },
  fix: {
    label: "Next", head: "Next fixture — capitals are at home",
    get: (p, teams) => shortOpponent((p.opp || [])[0], teams),
    title: (p) => (p.opp || [])[0] || "no fixture in this gameweek",
  },
  own: {
    label: "Own%", head: "Share of managers who own him",
    get: (p) => fmt(p.owned, 1) + "%",
    title: (p) => `owned by ${fmt(p.owned, 1)}% of managers`,
  },
};

export const METRIC_KEYS = Object.keys(METRICS);

/** Where every player in a squad goes on the pitch.
 *
 *  A complete squad has an eleven and a bench, and is laid out as one. An
 *  incomplete one has neither, and pretending otherwise would demote players to
 *  a bench that does not exist yet — so it is drawn as fifteen slots by
 *  position, which is what you are actually filling in.
 */
export function squadLayout({ ids, lookup, need, xiIds, benchOrder }) {
  const owned = ids.map((id) => lookup.get(id)).filter(Boolean);
  const xi = new Set(xiIds || []);
  const byPoints = (a, b) => (b.xpts_plan || 0) - (a.xpts_plan || 0);
  const complete = xi.size === 11;

  const rows = POS_ORDER.map((pos) => {
    const members = owned.filter((p) => p.pos === pos && (!complete || xi.has(p.id)))
      .sort(byPoints);
    const slots = members.map((p) => ({ player: p }));
    if (!complete) {
      for (let i = members.length; i < (need?.[pos] || 0); i++) slots.push({ pos });
    }
    return { pos, slots };
  });

  const shape = complete
    ? rows.filter((r) => r.pos !== "GKP").map((r) => r.slots.length).join("-")
    : "";

  let bench = [];
  if (complete) {
    const rest = owned.filter((p) => !xi.has(p.id));
    const keeper = rest.filter((p) => p.pos === "GKP");
    const outfield = rest.filter((p) => p.pos !== "GKP")
      .sort((a, b) => {
        const rank = (p) => Number(benchOrder?.[p.id] ?? 9);
        return rank(a) - rank(b) || byPoints(a, b);
      });
    bench = [
      { label: "GK", player: keeper[0], pos: "GKP" },
      ...[0, 1, 2].map((i) => ({ label: ["1st", "2nd", "3rd"][i], player: outfield[i] })),
    ];
  }

  return { rows, bench, shape, complete };
}

/* Status is worth a mark on the shirt rather than a column somewhere else: an
   injury doubt is the single thing most likely to make a squad wrong, and on a
   pitch there is room for it exactly where the eye already is. */
const STATUS_MARK = {
  d: { cls: "doubt", text: "?" },
  i: { cls: "out", text: "!" },
  s: { cls: "out", text: "!" },
  u: { cls: "out", text: "!" },
  n: { cls: "out", text: "!" },
};

function card(player, opts) {
  const { teams, metric, captain, vice, tag, remove, swap, badge } = opts;
  const spec = METRICS[metric] || METRICS.xpts;
  const url = shirtUrl(teams, player);
  const mark = player.status && player.status !== "a" ? STATUS_MARK[player.status] : null;
  const role = player.id === captain ? "C" : player.id === vice ? "V" : "";

  return `
  <div class="pcard${tag ? " is" + tag.kind : ""}" data-id="${player.id}">
    <button class="shirt" data-edit="${player.id}"
            aria-label="${escape(player.full_name)} — ${escape(spec.title(player, teams))}"
            title="${escape(player.full_name)} · ${escape(spec.title(player, teams))}">
      ${url ? `<img class="kit" src="${url}" alt="" width="52" height="66" loading="lazy">` : ""}
      <span class="kitfallback">${escape(player.team_short)}</span>
      ${role ? `<span class="armband${role === "V" ? " vice" : ""}">${role}</span>` : ""}
      ${mark ? `<span class="statusmark ${mark.cls}"
                      title="${escape(player.news || "not fully available")}">${mark.text}</span>` : ""}
      ${player.edited ? `<span class="editmark" title="You have edited this player's inputs">✎</span>` : ""}
    </button>
    ${remove ? `<button class="pcardrm" data-rm="${player.id}"
                        aria-label="Remove ${escape(player.name)}" title="Remove">×</button>` : ""}
    ${swap ? `<button class="pcardswap" data-swap="${player.id}"
                      aria-label="Swap ${escape(player.name)} into your draft"
                      title="Bring ${escape(player.name)} into your draft">→</button>` : ""}
    <div class="pname">${escape(player.name)}</div>
    <div class="pval">${escape(spec.get(player, teams))}</div>
    ${badge ? `<div class="pslot">${escape(badge)}</div>` : ""}
    ${tag ? `<div class="ptag ${tag.kind}">${escape(tag.text)}</div>` : ""}
  </div>`;
}

const emptyCard = (pos, badge) => `
  <button class="pcard empty" data-add-pos="${pos || ""}"
          aria-label="Add a ${pos || "player"}" title="Add a ${pos || "player"}">
    <span class="shirt ghost"><span class="plus">+</span></span>
    <div class="pname">${pos || "Empty"}</div>
    <div class="pval">add</div>
    ${badge ? `<div class="pslot">${escape(badge)}</div>` : ""}
  </button>`;

/** The whole pitch: the eleven in rows, the bench under it.
 *
 *  `tags` maps a player id to {kind, text} — the in/out marking the optimal
 *  squad is read through. Colour never carries it alone: a tagged shirt gets the
 *  word as well, for the same reason the list it replaces did.
 */
export function pitchHTML({ layout, lookup, teams, metric = "xpts", captain = null,
                            vice = null, tags = null, remove = false, swap = false,
                            benchLabels = true }) {
  const opts = (player, badge) => ({
    teams, metric, captain, vice, badge,
    tag: tags?.[player.id] || null,
    remove,
    swap: swap && tags?.[player.id]?.kind === "in",
  });

  const rows = layout.rows.map((row) => `
    <div class="pitchrow" data-pos="${row.pos}">
      ${row.slots.map((slot) => (slot.player
        ? card(slot.player, opts(slot.player))
        : emptyCard(slot.pos))).join("")}
    </div>`).join("");

  const bench = layout.bench.length ? `
    <div class="benchband">
      <div class="benchlabel">Bench<span class="benchnote">auto-subs run down this order</span></div>
      <div class="benchrow">
        ${layout.bench.map((slot) => (slot.player
          ? card(slot.player, opts(slot.player, benchLabels ? slot.label : ""))
          : emptyCard(slot.pos || "", benchLabels ? slot.label : ""))).join("")}
      </div>
    </div>` : "";

  return `<div class="pitch">${rows}</div>${bench}`;
}

/** A loose group of shirts with no pitch under them — the players the solver
 *  dropped, which belong beside its squad without being part of it. */
export function cardsHTML(players, { teams, metric = "xpts", tags = null,
                                     swap = false, badge = null } = {}) {
  return `<div class="benchrow loose">${players.map((player) => card(player, {
    teams, metric, captain: null, vice: null,
    badge: badge ? badge(player) : "",
    tag: tags?.[player.id] || null,
    remove: false,
    swap: swap && tags?.[player.id]?.kind === "in",
  })).join("")}</div>`;
}

/** Turn a failed shirt into a lettered tile.
 *
 *  Called after the HTML lands rather than wired inline, so the module stays a
 *  string builder and the page keeps one place where handlers are attached. A
 *  broken-image icon on eleven shirts is worse than no shirts at all, and the
 *  case is real: a club promoted between a snapshot and a sync has no mirrored
 *  image until the laptop next fetches one.
 */
export function wireShirts(root) {
  for (const img of root.querySelectorAll("img.kit")) {
    if (img.complete && img.naturalWidth === 0) {
      img.closest(".shirt")?.classList.add("nokit");
      continue;
    }
    img.addEventListener("error", () => img.closest(".shirt")?.classList.add("nokit"),
                         { once: true });
  }
}

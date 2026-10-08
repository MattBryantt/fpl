
const POS_ORDER = ["GKP", "DEF", "MID", "FWD"];

const fmt = (v, d = 1) =>
  (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);

const escape = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function shirtUrl(teams, player) {
  const code = teams?.[player.team]?.code;
  if (!code) return "";
  return `/shirts/${code}${player.pos === "GKP" ? "_1" : ""}.png`;
}

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
  start: {
    label: "Start", head: "Chance he starts the next fixture",
    get: (p) => fmt(p.p_start, 2),
    title: (p) => `${Math.round((p.p_start || 0) * 100)}% chance of starting`,
  },
  mins: {
    label: "Mins", head: "Expected minutes per fixture",
    get: (p) => fmt(p.exp_minutes, 0),
    title: (p) => `${fmt(p.exp_minutes, 0)} expected minutes per fixture`,
  },
};

export const METRIC_KEYS = Object.keys(METRICS);

export const MAX_METRICS = 3;

export function cleanMetrics(keys) {
  const wanted = (Array.isArray(keys) ? keys : [keys])
    .filter((k) => METRIC_KEYS.includes(k));
  const ordered = METRIC_KEYS.filter((k) => wanted.includes(k));
  return ordered.length ? ordered.slice(0, MAX_METRICS) : ["xpts"];
}

export function squadLayout({ ids, lookup, need, xiIds, benchOrder, rowOrder }) {
  const owned = ids.map((id) => lookup.get(id)).filter(Boolean);
  const xi = new Set(xiIds || []);
  const byPoints = (a, b) => (b.xpts_plan || 0) - (a.xpts_plan || 0);
  const complete = xi.size === 11;

  const rows = POS_ORDER.map((pos) => {
    const inRow = owned.filter((p) => p.pos === pos && (!complete || xi.has(p.id)));
    const members = rowOrder ? rowOrder(pos, inRow) : inRow.slice().sort(byPoints);
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

const STATUS_MARK = {
  d: { cls: "doubt", text: "?" },
  i: { cls: "out", text: "!" },
  s: { cls: "out", text: "!" },
  u: { cls: "out", text: "!" },
  n: { cls: "out", text: "!" },
};

function card(player, opts) {
  const { teams, metrics, captain, vice, tag, remove, swap, badge, versus, nudge } = opts;
  const keys = metrics.length ? metrics : ["xpts"];
  const url = shirtUrl(teams, player);
  const mark = player.status && player.status !== "a" ? STATUS_MARK[player.status] : null;
  const role = player.id === captain ? "C" : player.id === vice ? "V" : "";
  const summary = keys.map((k) => `${METRICS[k].label} ${METRICS[k].get(player, teams)}`)
    .join(" · ");

  const [lead, ...rest] = keys;
  const cell = (k) => `<span class="pcell" title="${escape(METRICS[k].head)}"
    >${escape(METRICS[k].get(player, teams))}</span>`;

  return `
  <div class="pcard${tag ? " is" + tag.kind : ""}" data-id="${player.id}">
    <div class="shirtwrap">
      <button class="shirt" data-edit="${player.id}"
              aria-label="${escape(player.full_name)} — ${escape(summary)}"
              title="${escape(player.full_name)} · ${escape(summary)}">
        ${url ? `<img class="kit" src="${url}" alt="" width="52" height="66" loading="lazy">` : ""}
        <span class="kitfallback">${escape(player.team_short)}</span>
        ${role ? `<span class="armband${role === "V" ? " vice" : ""}">${role}</span>` : ""}
        ${mark ? `<span class="statusmark ${mark.cls}"
                        title="${escape(player.news || "not fully available")}">${mark.text}</span>` : ""}
      </button>
      ${remove ? `<button class="pcardrm" data-rm="${player.id}"
                          aria-label="Remove ${escape(player.name)}" title="Remove">×</button>` : ""}
      ${swap ? `<button class="pcardswap" data-swap="${player.id}"
                        aria-label="Swap ${escape(player.name)} into your draft"
                        title="Bring ${escape(player.name)} into your draft">→</button>` : ""}
      ${versus ? `<button class="pcardvs" data-vs="${player.id}"
                          aria-label="Compare ${escape(player.name)} with another player"
                          title="Compare ${escape(player.name)} head to head">⇄</button>` : ""}
    </div>
    <div class="pname">${player.edited
      ? `<span class="editmark" title="You have edited this player's inputs">●</span>` : ""
      }${escape(player.name)} <i>${escape(player.team_short)}</i></div>
    <div class="pval" title="${escape(METRICS[lead].head)}">${escape(METRICS[lead].get(player, teams))}</div>
    ${rest.length ? `<div class="psub">${rest.map(cell).join("")}</div>` : ""}
    ${badge ? `<div class="pslot">${escape(badge)}</div>` : ""}
    ${tag ? `<div class="ptag ${tag.kind}">${escape(tag.text)}</div>` : ""}
    ${nudge ? `<div class="pnudge">
      <button class="pnudgebtn" data-nudge="${player.id}" data-dir="-1"
              aria-label="Move ${escape(player.name)} left" title="Move left">‹</button>
      <button class="pnudgebtn" data-nudge="${player.id}" data-dir="1"
              aria-label="Move ${escape(player.name)} right" title="Move right">›</button>
    </div>` : ""}
  </div>`;
}

const emptyCard = (pos, badge) => `
  <button class="pcard empty" data-add-pos="${pos || ""}"
          aria-label="Add a ${pos || "player"}" title="Add a ${pos || "player"}">
    <span class="shirtwrap"><span class="shirt ghost"><span class="plus">+</span></span></span>
    <div class="pname">${pos || "Empty"}</div>
    <div class="pval">add</div>
    ${badge ? `<div class="pslot">${escape(badge)}</div>` : ""}
  </button>`;

export function pitchHTML({ layout, teams, metrics = ["xpts"], captain = null,
                            vice = null, tags = null, remove = false, swap = false,
                            versus = false, benchLabels = true, reorder = false }) {
  const opts = (player, badge, nudge = false) => ({
    teams, metrics, captain, vice, badge, versus, nudge,
    tag: tags?.[player.id] || null,
    remove,
    swap: swap && tags?.[player.id]?.kind === "in",
  });

  const rows = layout.rows.map((row) => `
    <div class="pitchrow" data-pos="${row.pos}">
      ${row.slots.map((slot) => (slot.player
        ? card(slot.player, opts(slot.player, "", reorder && row.pos !== "GKP" && row.slots.length > 1))
        : emptyCard(slot.pos))).join("")}
    </div>`).join("");

  const bench = layout.bench.length ? `
    <div class="benchband">
      <div class="benchlabel">Bench<span class="benchnote">auto-subs run down this order</span></div>
      <div class="subsrow">
        ${layout.bench.map((slot) => (slot.player
          ? card(slot.player, opts(slot.player, benchLabels ? slot.label : ""))
          : emptyCard(slot.pos || "", benchLabels ? slot.label : ""))).join("")}
      </div>
    </div>` : "";

  return `<div class="pitch">${rows}</div>${bench}`;
}

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

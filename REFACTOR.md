# Splitting up `fplkit/web/index.html`

## Context

This board grew as one file on purpose — no build step, so no bundler, so no
reason to split anything until the file itself became the problem. It now is:
8,200+ lines carrying CSS, markup, and nearly all client-side logic in one
`<script type="module">`. This document is a plan for breaking that up. It is
meant to be handed to an agent (or a future you) to execute as its own body
of work, separate from and after the feature round that produced this
document — team-ID live connection, wildcard framing, and CLI chip-pinning
(see git history around this commit for that work, and the project's
`README.md` for the app's actual purpose and model design, which this
document does not repeat).

**Do this refactor with no build step still, unless you decide to add one as
part of it.** The project's zero-infrastructure, zero-cost stance is load
bearing (see `.github/workflows/deploy.yml`'s own comments on cost), and a
bundler is a bigger decision than "split some files" — flag it separately if
you think it's warranted rather than sneaking it in here.

## What NOT to do

- **Do not deduplicate the Python/JS logic pairs.** `fplkit/transfers.py` ↔
  `fplkit/web/transfers.js`, `fplkit/optimise.py` ↔ `fplkit/web/solver.js`,
  `fplkit/model.py`/`fplkit/poisson.py` ↔ `fplkit/web/points.mjs`/`poisson.mjs`
  are **intentional** duplication: one Python implementation (the source of
  truth, used server-side and by the CLI) and one hand-ported JS
  implementation (used by the static/offline board), kept in agreement by the
  `scripts/verify-*-port.mjs`/`.py` scripts. This is not drift to clean up —
  merging them would mean either putting Python in the browser or JS on the
  server, both bigger changes than this refactor is for. Leave the pattern
  alone; if a file in this list gets touched, rerun its verify script.
- **Do not touch the Python side.** `fplkit/server.py`, `fplkit/transfers.py`,
  `fplkit/optimise.py`, `fplkit/model.py`, etc. are comparatively clean —
  each is one file with one job, already under 2,200 lines at the largest
  (`model.py`). This document is about `index.html` only.
- **Do not change behavior while moving code.** This is a structural refactor.
  If you find an actual bug while reading (there is at least one documented
  instance of settings drifting between two copies before being unified —
  see `README.md` around "Chips and transfers in the board"), note it
  separately rather than fixing it inline; a refactor PR that also changes
  behavior is much harder to review and to revert if something breaks.

## The shape of the file today

```
lines 1–28      boilerplate head
lines 29–1050   CSS (~1,000 lines)
lines 1052–1685 HTML markup (squad pitches, tabs, settings panel, drawers)
lines 1687–8204 one <script type="module"> (~6,500 lines)
```

The script already has ~30 informal section-comment boundaries (grep
`^/\* -\{10,\}` to relist them — they drift as the file changes, so regenerate
this rather than trusting a stale list). As of this document, the major ones,
in file order:

- auth token / `api()` helper
- persistence (`localStorage`, `STORE`, `saveSquad`/`squadCodes`/`resolveSquadCodes`)
- settings (read/write the Settings panel)
- fixture decay / chrome (tab switching, `renderChrome`)
- squad maths, tooltip, render (the main `renderAll`/`renderSquad` family)
- bench slot weights, chip economics
- gw line chart, exposure bars, timeline heatmap, fixtures card (Analysis tab)
- nearly-in (near-miss sweep) — **UI presentation only**; the sweep itself is
  `fplkit/optimise.py`'s `near_misses`, ported nowhere — this section just
  renders what the server/worker returns
- transfers & chips (**two** sections, ~3429 and ~6860 — the first is
  presentation/state for the Chips tab UI, the second is
  `buildTransferPayload`/job-building that talks to `transfer-worker.js`;
  do not conflate them when splitting)
- "Your squad vs Optimal" (the compare pitch)
- constraints (include/exclude toggles)
- drafts (save/load/compare named squads)
- stat editor, explain, lineups, lineup pitch shape
- "ask" (the AI chat panel — server-only feature, see its own section)
- overrides.csv, cross-device sync (`pushSyncState`, the `/api/sync` pull)
- data (`loadSnapshot`, `loadPool`, `sync()` — talks to the local server)
- optimal squad, the near-miss sweep, "the same question, one gameweek"
  (per-week ideal-squad comparison in the Chips tab)
- the other squad (own-squad-anchored transfer solve)
- head to head (compare view)
- events (the big delegated `addEventListener` blocks at the bottom)

## Proposed seams

Split along the boundaries above, not by guessing new ones — they already
represent how the author thinks about the code, which is worth preserving.
Suggested module grouping (adjust as you actually read the code; this is a
starting hypothesis, not a spec):

1. **`state.mjs`** — the `S` object, `STORE`, `loadLocal`/`saveLocal`,
   `saveSquad`/`squadCodes`/`resolveSquadCodes`, settings read/write. The
   thing every other module needs; extract first.
2. **`sync.mjs`** — `api()`, the auth token, `pushSyncState`, the `/api/sync`
   pull, `loadOverrides`, `loadMyTeam` (uses `live.mjs`, already extracted).
3. **`squad-view.mjs`** — squad maths, the pitch render family, bench slot
   weights, constraints, drafts.
4. **`analysis-view.mjs`** — gw line chart, exposure bars, timeline heatmap,
   fixtures card, near-miss presentation.
5. **`transfer-view.mjs`** — both "transfers & chips" sections,
   `buildTransferPayload`, the optimal-squad and own-squad solve flows,
   `copyOptimal`, the per-week ideal-squad comparison.
6. **`explain-view.mjs`** — stat editor, explain panel, lineups, the ask chat.
7. **`compare-view.mjs`** — "Your squad vs Optimal", head to head.
8. **`index.html`** left with markup, CSS, and a thin bootstrap: imports from
   the above, wires the bottom `addEventListener` blocks, calls `loadPool()`.

Each of these already exists as a `<script type="module">` importing from
`board.mjs`/`pitch.mjs`/`chips.mjs`/`live.mjs` etc. — the split is about
where the code that *calls* those modules lives, not about the modules
already factored out (`board.mjs`, `points.mjs`, `poisson.mjs`, `pitch.mjs`,
`chips.mjs`, `position-tags.mjs`, `live.mjs`), which are already correctly
separated and out of scope here.

**Whichever seams you actually use**, every new `.mjs` file must be added in
three places or the build silently breaks or ships something uncached:
`fplkit/server.py`'s `ASSETS` dict (media type, served locally and reused by
the static build via `from .server import ASSETS`), `fplkit/web/sw.js`'s
`SHELL` array (offline caching), and the `<script type="module">` imports in
`index.html`. `fplkit/site.py::_check_shell_covers_assets()` already fails
the build if `ASSETS` and `SHELL` disagree — keep relying on that check
rather than trying to remember the list by hand.

## Dead code / stale settings

Not inventoried exhaustively here — that is exactly the kind of thing worth
doing *during* the split, since moving a function is the moment you find out
nothing calls it. Two starting pointers:

- README.md (search "Chips and transfers in the board") documents one past
  instance of settings duplicating between the tab payload builder and the
  squad optimiser before being unified. Check whether anything similar has
  crept back in — the `budget`/`maxclub`/`freetransfers` control values are
  read from the DOM directly in several places (`+$("#budget").value` appears
  at multiple call sites); consider whether those should route through one
  `currentSettings()`-style accessor as part of the `state.mjs` extraction.
- Grep for any `S.*` field or top-level function with exactly one reference
  (its own definition) after the split — a natural byproduct of moving code
  into modules with explicit exports is that an unused export becomes visible
  in a way it never was as a same-file function.

## Verification

- `scripts/verify-js-port.mjs`, `verify-solver-port.mjs`,
  `verify-transfer-port.mjs` — rerun after touching anything that reaches
  `points.mjs`, `solver.js`, or `transfers.js`, even indirectly through a
  moved caller.
- `python fpl.py build --out /tmp/check && python -m http.server -d /tmp/check`
  — build the static site and click through Squad / Analysis / Chips / Drafts
  by hand. There is no automated UI test suite; this is the real check.
- `python fpl.py serve` — the local dev path exercises different code
  (`/api/pool`, `/api/live`, `/api/optimise`) than the static build does
  (everything client-side against `snapshot.json`) — check both, not just one.

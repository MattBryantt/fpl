# FPL expected-points toolkit

Projects Fantasy Premier League points by combining three sources, then answers
the question that actually decides a squad: **is the expensive player worth the
extra money?**

- **Bookmaker odds** (The Odds API) — 1X2 and over/under prices from ~20 books,
  de-vigged and inverted into expected goals for each side of each fixture.
- **Understat** — non-penalty xG, xA and xGChain per 90, giving each player's
  share of his team's attacking output.
- **The FPL API** — prices, positions, minutes, starts, penalty order, saves,
  bonus, and the defensive-contribution stats the 2025/26 rules added.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then paste your Odds API key into .env
```

The Odds API key is free (500 requests/month) from
[the-odds-api.com](https://the-odds-api.com/). It is optional — without it the
model falls back to xG-derived team ratings for every fixture — but the odds are
the single biggest accuracy win, so it is worth the two minutes.

Everything is cached on disk (`.cache/`), so repeated runs cost no quota: FPL
data for 3 hours, odds for 6, Understat for 24. `--refresh` bypasses it.

## Commands

```bash
python fpl.py serve                                       # interactive squad board in a browser
python fpl.py snapshot                                    # freeze the projection so the board runs offline
python fpl.py build --out dist                            # the whole board as static files
python fpl.py plan                                        # start here: squad + path + risk
python fpl.py plan --recency 10                           # weight recent form above early season
python fpl.py transfers --squad out/squad.csv             # transfer and chip strategy, in-season
python fpl.py horizon                                     # how much does the horizon matter?
python fpl.py rank --pos MID --max-price 9.0 --limit 20   # ranked by projected points
python fpl.py value --pos DEF                             # best points per £m
python fpl.py compare Haaland Thiago --squad-test         # is the premium worth it?
python fpl.py upgrade Saka --budget-extra 2.0             # what else could I buy?
python fpl.py squad --budget 100                          # optimal legal 15, fixed horizon
python fpl.py fixtures --horizon 3                        # expected goals per fixture
python fpl.py overrides Senesi                             # edit stats you think are wrong
python fpl.py movers                                      # players whose numbers are from another club
python fpl.py blindspots                                  # players the model can't see
```

Common flags: `--horizon N` (gameweeks), `--start-gw N`, `--full` (show the full
points breakdown), `--csv name.csv` (write to `out/`), `--overrides file.csv`,
`--refresh`. `plan` and `horizon` also take `--half-life N`; `transfers` takes
`--squad`, `--free-transfers`, `--bank`, `--chips-used` and `--chip-value`.

## The squad board

```bash
python fpl.py serve          # → http://127.0.0.1:8000
```

A local page for building squads by hand and watching every number move as you
do. The first thing on it is the squad, drawn as a team: the eleven the
projection would start, in the shape it would start them, with the four
substitutes ranked beneath in the order FPL's auto-subs actually work down. Each
shirt is the player's club kit, with the captain's armband on the highest
plan-weighted score in the XI and a **V** on the next one — which is where the
double lands when the captain does not play.

### Two squads, side by side

A pitch needs about 600px and the page has 1600, so the other half holds a
second squad: the solver's answer to the settings in force, or **any draft you
have saved**, chosen from the picker above it. Both pitches carry the diff —
players the other squad has and yours does not are ringed green and marked
**in**, yours that it dropped are dimmed and marked **out** — and a → on any
incoming shirt takes that one player, dropping the weakest player you hold in
the same position that the other squad did not want either.

Under the two pitches the trade is priced. Outs and ins are **paired by
position**, because that is the swap you would actually make, and each row
carries the two numbers it turns on:

```text
Out of yours          In from Optimal squad     xPts          Δ xPts   Price            Δ £m
out Semenyo    MCI    in  B.Fernandes   MUN     15.0 → 21.5    +6.5    £8.5 → £12.0     +3.5
out Szoboszlai LIV    in  Saka          ARS     14.7 → 20.1    +5.5    £7.0 → £9.5      +2.5
All 2 changes                                                 +12.0                     +6.0
```

Set the picker to *Nothing* and the board drops to one pitch, which is what a
narrow screen does for you anyway.

### What each shirt carries

Up to **three** figures, chosen rather than fixed: the first gets a bar of its
own and the other two share a row beneath it, in the order the selector shows
them. **xPts** to rank on, **xPPG** to compare on, **£** to budget with, **PPG**
for last season, **Next** for the fixture (capitals are at home), **Own%**,
**Start** and **Mins**. Clicking a fourth drops the oldest, which is what
clicking a fourth means.

The four figures above the pitches — projected XI points with the captain
doubled, what the squad costs and what is left in the bank, the XI's per-match
rate, and how much of the template you hold — follow whatever is on them.

### Head to head

The ⇄ on any shirt, or in any pool row, puts a player in the comparison; a
second one opens it. Every number the board holds on both, side by side, with
the gap signed so the better one reads as the better one — which for price and
ownership is the *smaller* figure — and then the horizon gameweek by gameweek,
because two players with the same total can have it arranged very differently.
It is `fpl.py compare` without the terminal.

### The pool, and the tabs

Tap an empty shirt and the player pool opens as a drawer, filtered to that
position. It is the same table it always was: every column, both constraint
buttons, search and sort. Tap a filled shirt and the stat editor opens on him.
The × drops him.

| Tab | What is on it |
| --- | --- |
| **Squad** | both pitches, the swap table, the legality checks. The badge counts how far you are from the solver |
| **Analysis** | the weekly profile, the rank-risk list, the fixture timeline, and the club lineups — the expected XI drawn as a pitch too, ranked on start probability, with start / minutes / xPPG on each shirt |
| **Drafts** | saved squads, and the overlaid weekly chart |

**Settings** (top right) holds everything that scopes the numbers: horizon,
fixture decay, budget, template tilt, minimum start probability, and behind
*More options* the rest. They are one button away rather than permanently on
screen, because a board whose subject is a squad should open on the squad.

**Fixture decay** is the half-life knob, dragged on a scale you can actually
aim with. It reads as what one gameweek of distance costs — 0.79× the fixture
before it, which is a half-life of about three — and runs from 0.50× (only the
next fixture really counts) up to **1.00×, where decay is off entirely** and
every fixture in the horizon is worth its full value. The half-life it works
out to is printed next to the multiplier, and the editor's Weight column shows
where each gameweek landed.

The kits are mirrored, not hot-linked: the server fetches each club's shirt from
the FPL CDN once, caches it under `.cache/shirts/` and serves it from
`/shirts/<code>.png`, and the service worker caches those alongside the shell.
So the pitch draws itself with the laptop off, which is the whole premise of the
rest of the board. A club whose shirt never arrived falls back to a lettered
tile — a missing image costs a picture, never a player.

### Three numbers per player, and why

| | What it is | Read it for |
| --- | --- | --- |
| **PPG** | last season's points **per appearance**, as the FPL API reports it | the number you already carry in your head. Greyed under 900 minutes, where a rate over four cameos reads like form |
| **xPPG** | projected points **per fixture** over the horizon, undecayed | comparing two players on different fixture runs. A double gameweek is two matches and a blank is none, so this is the honest rate |
| **xPts** | plan-weighted total over the horizon | ranking a squad. Discounts distant gameweeks and multiplies by a survival curve, which is what the optimiser maximises |

They answer different questions and the board shows all three side by side
rather than making you hold one in memory while reading another. xPts is the one
to rank on; xPPG is the one to compare on; PPG is the sanity check.

### Where a player's points actually come from

Click the ✎ beside anyone and the panel opens with **Where these points come
from** — the projection taken apart into the things the scoring rules pay for,
and then the arithmetic that turns that into the number in the table:

```text
Appearance                                  9.13
Goals            open play plus penalties   7.32
Assists                                     6.26
Bonus                                       2.61
Clean sheets     only while on the pitch    1.49
Defensive contribution                      1.40
Cards and penalty misses                   −1.02
────────────────────────────────────────────────
Over 5 fixtures                            27.19    ← 5.44 per fixture, the xPPG column

Why the table shows 17.5
Fixture         Points   Weight   Counts as
GW1  BRE (H)      5.51     1.00        5.51
GW2  new (A)      4.81     0.76        3.66
GW3  cov (H)      5.95     0.58        3.45
GW4  cry (A)      4.85     0.45        2.16
GW5  ars (A)      3.98     0.34        1.34
────────────────────────────────────────────────
Plan-weighted total — what the board ranks on   16.12
```

Those components come from the same `playerFixturePoints` the projection runs
on, so they always add up to the total rather than approximating it — and they
move as you drag the sliders, which is the quickest way to see that a player is
80% appearance points and will not repay a captaincy.

The second table is the part that confuses people, and it was previously
invisible: the board **ranks on plan-weighted points** while the eye reads raw
ones. `Weight` is the geometric decay on distance times the survival curve —
half-life 3 gameweeks by default, multiplied by his chance of still being
available. Nothing on screen used to say those were different quantities.

**Watch the denominators.** PPG divides by the games a player *appeared in*;
xPPG divides by his club's *fixtures*. Haaland's 6.8 becomes 6.3 once the three
games he missed are counted, which is most of why the model looks pessimistic
beside it. Hover any PPG value and the tooltip gives both, plus the appearance
count — so the comparison next to it is like for like.

The split of work is deliberate, and it moved. The server used to run the
projection *and* the MILP; it now runs only the projection, and freezes the
result into `out/snapshot.json`. Everything else — the optimiser, the stat
editor, the charts, the legality checks — runs in the browser, against that
file. Changing the horizon or the half-life is arithmetic over numbers the page
already holds, so it is instant and needs nothing behind it.

That is what lets the board work on a phone with the laptop shut. It also means
there is one code path instead of an online one and an offline one, which is the
part that matters for correctness: [scripts/verify-js-port.mjs](scripts/verify-js-port.mjs)
and [scripts/verify-solver-port.mjs](scripts/verify-solver-port.mjs) check the
browser's arithmetic and its MILP against the Python, on real data, every time.

The price is that the data is only as fresh as the last **Sync**, and the header
says how stale it is rather than letting you assume it is live.

**More options**, inside Settings, opens two more rows. The first has everything else the CLI
exposes: first gameweek, max per club, recent-form half-life, a forced
formation, a button to bypass the cache and refetch every source, and
**Restore defaults**, which puts every knob on the board back to its default and
leaves your squad, drafts and edits alone. The ⊕ and
⊘ buttons in the pool require or bar a player — those are *optimiser
constraints*, not squad edits, so they leave whatever you're currently drafting
alone and only change the optimal squad. Active constraints show as removable
chips.

The second row is **bench weights**: one slider per bench slot, each showing the
weight the objective actually uses for that slot rather than a relative share of
some other number. The `Overall` slider rewrites all four from the default
shape, and reads back the scale the four currently imply, so the two can never
contradict each other. Set all four to zero and the solver buys the four
cheapest legal bodies; push `1st sub` up and it starts spending real money on a
player who only ever comes on when a starter blanks. Every move re-solves the
optimal squad, which is the point — the sliders are only useful if you can see
what they buy you.

**Where the data comes from** (top right, or `/data`) is a provenance page — see
below.

### Team lineups, and what an override costs the rest of the club

**Minutes are a fixed pool.** Eleven players start, and 990 minutes get played,
whatever anyone asserts. So saying a player starts can only mean somebody else
does not — and until now nothing enforced that. Overrides were applied at the
very end of the pipeline, long after `minutes_model` had balanced each squad,
and nothing rebalanced it afterwards. Pushing one fringe player's `p_start` to
0.9 left Manchester City fielding **11.6 players** and scoring 8% more than the
odds said they would, with the extra goals conjured rather than taken from a
team-mate. Override a whole XI, which is what entering real team news means, and
the club drifts a long way from anything its fixtures support.

Now the club is put back together around the assertion:

```text
Team lineups                                    Man City   [11.00 starters]
Player                        Start   Mins   xPPG
Savinho          MID  yours    0.90     71   3.21
Haaland          FWD  adjusted 0.89     72   5.42
Semenyo          MID  adjusted 0.89     72   3.08
Donnarumma       GKP           0.95     72   3.44
```

`yours` is what you asserted and is never touched. `adjusted` is the consequence
— everyone else scaled by one bounded multiplier, so the pecking order the data
supports survives. The **Team lineups** card browses any club; the stat editor
shows the same table live while you drag, with a `Δ` column, so you can see who
pays for the minutes before you commit to handing them out.

Two things it will not do. It will not scale your own numbers back to fit: pin
thirteen players at 0.95 and the club fields 13.35, the header turns red and
says so, and the goals follow. And the same logic protects asserted *rates* —
`conserve_team_output` treats an override as fully evidenced, so balancing a
club's books never rescales a number you typed.

The browser does this too, in `board.mjs`, because on a phone there is nothing
else to do it — and
[scripts/verify-lineup-port.mjs](scripts/verify-lineup-port.mjs) checks the two
agree on every player of every rebalanced club, including the awkward shapes: a
promotion, a demotion, a whole XI pinned at once, a keeper swap, and an
over-pinned club the model is supposed to leave over-pinned.

### Asking why, in words

Below the breakdown is **Ask about this player** — a chat box that answers from
the numbers the model actually produced. It is off until you point it at an
endpoint, and there are three free ways to do that:

| | How | What it costs |
| --- | --- | --- |
| **Ollama, on your Mac** | `brew install ollama && ollama pull llama3.1:8b` | nothing, ever — no key, no account, no quota, and no data leaves the machine. This is the default |
| **Google AI Studio** | a key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | free tier, no card. Better answers than an 8B local model |
| **Groq** | a key from [console.groq.com/keys](https://console.groq.com/keys) | free tier, and very fast |

Set `FPL_AI_BASE_URL`, `FPL_AI_MODEL` and `FPL_AI_KEY` in `.env` — the exact
values for each are in [.env.example](.env.example). Any OpenAI-compatible
`/chat/completions` endpoint works, so this is not tied to a vendor. The key is
read by the server process and never reaches the browser.

**It is grounded, deliberately.** The server does not hand a chat endpoint your
question and hope. For every question it rebuilds a dossier for that player —
the full points breakdown, the per-fixture lambdas and whether they came from
bookmakers or from ratings, his inputs, where he ranks in his position, and the
model's own caveats about thin evidence or a summer transfer — out of the same
`reproject_player` the board scores with. Then it tells the model to use only
those numbers, to quote the ones it reasons from, and to say so when the dossier
cannot answer. An answer therefore cannot be built on figures the projection
never produced, which is the failure mode that makes a chat box over a model
worse than no chat box.

What it is good at: "why is he rated this highly", "what is he actually being
paid for", "how much of this rests on him starting", "should I trust this".
What it cannot do is tell you anything the model does not know — it has no
knowledge of team news, and it will say so rather than invent some.

Two honest caveats. It needs the laptop, like Sync does: there is no model on
the phone, and the box says so rather than failing quietly. And a hosted free
tier means the player's numbers go to that provider — public football
statistics, but an external service nonetheless, which is why the local option
is the default and the box tells you which one is answering.

### Putting it somewhere permanent

The board runs the projection maths and the MILP **in the browser**, off
`snapshot.json`. Once that was true, the laptop stopped being a server and
became a build step that nobody had noticed was a build step. So make it one:

```bash
python fpl.py build --out dist     # the whole board, as files
python -m http.server -d dist 8001 # check it, if you like
```

`dist/` is about 4.8 MB and needs nothing but a static host: pages, assets, the
3.3 MB WASM solver, the club shirts and the frozen projection.
[.github/workflows/deploy.yml](.github/workflows/deploy.yml) then runs that
build on a six-hourly cron and publishes it to **Cloudflare Workers** (config in
[wrangler.jsonc](wrangler.jsonc)), which means one permanent HTTPS URL, no Mac
in the loop, no Tailscale, no launchd, no port, no token, and no URL that
changes.

Workers rather than Cloudflare Pages, though both are free and both would work:
since Workers gained native static-asset serving, Cloudflare's guidance is to
start new projects there, and two differences matter here. `run_worker_first`
is where `/api/ask` goes when the ask box is ported — on a static host the
browser already computes the projection, so a server only has to hold the model
key and proxy the call. And Cron Triggers live there too, which is an escape
route if GitHub Actions ever stops being the right place to build the snapshot.

Neither, rather than GitHub Pages: Pages on a **private** repo needs a paid
GitHub plan, and this repo does not want to be public. Static asset requests on
Cloudflare are not billed.

The cron is six-hourly rather than hourly because of the tightest external
limit, not taste — The Odds API's free key allows 500 credits a month, one run
spends two, and four runs a day is about 240. There is a **Run workflow** button
for the hour before a deadline, with a `refresh` box that bypasses every cache.

The build refuses to publish over a good site with an empty one: it checks the
snapshot has 300+ players and that the shell files exist before deploying.
Stale numbers degrade gracefully; no numbers do not.

**What a static host does not have** is the write half of `server.py` —
`/api/drafts`, `/api/overrides`, `/api/snapshot`. Those were always mirrors of
state the browser already owns in `localStorage`, so what is lost is the mirror
and not the data. The page probes for the endpoints on load and hides the three
controls that need them (`Sync`, `Refresh sources`, `Load overrides.csv`), on
the grounds that a control which cannot work is worse than one that is not
there. **Not yet ported:** the `Ask why` box, which needs a server to hold the
API key — it already hides itself when `/api/ai` is absent, and the same
`/api/*` route that carries sync (below) is the obvious home for it.

### Syncing a squad between your phone and your laptop

`localStorage` is per-device by definition, so with nothing else in the
picture a squad picked on the phone stays on the phone. `src/worker.js` is the
one small piece of server this project keeps: it runs on the same Worker as
the static files, behind one route (`/api/sync`), backed by a free Cloudflare
KV store holding exactly three things — drafts, edits, the in-progress squad.
Not settings, not the theme: those stay per-device on purpose, the same as
"the board opens where you left it" already does.

It is push-based, not a live connection. Every change bumps a local clock and
schedules a debounced push; the *only* time a device pulls someone else's state
is once, on load — a background pull that silently replaced a squad you were
mid-edit on would be worse than no sync at all. Two devices editing different
things while both offline will converge on whichever pushed most recently once
they are both back online, which is the honest limitation of last-write-wins:
fine for one person on two devices, not a general multi-user sync.

**Setup, once per device.** The Worker checks a shared secret
(`X-FPL-Token`, the same header and `api()` helper the old `--lan` server
already used), stored as a Wrangler secret so it never sits in `dist/` or the
repo:

```bash
npx wrangler secret put FPL_TOKEN    # paste a random string; only you need it
```

Then open `https://<your-worker>.workers.dev/?t=<that-string>` once on each
device — the token moves out of the URL into `localStorage` on first load, the
same way the old `--lan` link worked, except this one persists across closing
and reopening the app rather than clearing at the end of a browser session.
After that, nothing further: the board syncs by itself.

Without a token, sync is simply off — no prompt, no nag, the board behaves
exactly as it did before this existed.

### Using it from a phone, with the laptop off

The board installs to a phone's home screen and works with nothing behind it —
no signal, no server, laptop shut. Picking a squad, editing a player's inputs
and re-solving the optimiser all happen on the device.

Open the Cloudflare URL once, **Share → Add to Home Screen**, and let it finish
loading. That one load is the install. It caches the app shell, the 3.3 MB WASM
solver and the current projection; after it, the link is only needed when you
want fresher numbers.

This used to mean a Mac kept awake and reachable over Tailscale — `scripts/
setup-phone-access.sh` installed a launchd agent for exactly that. Once the
board became a static build (above), that machinery had nothing left to do:
the permanent Cloudflare URL and the `/api/sync` route are the phone's only
dependency now, so the launchd agent, its plist and the setup script were
retired.

**What works offline** — the whole board, except the two things that are the
projection rather than a view of it:

| | Offline | Needs the laptop |
| --- | --- | --- |
| Browse, sort, filter the pool | ✅ | |
| Build a draft, legality, budget, charts | ✅ | |
| Re-solve the optimal squad, all settings | ✅ | |
| Override a player's inputs, season-wide or per match | ✅ | |
| Change horizon or half-life | ✅ | |
| Save and compare drafts | ✅ | |
| See where a player's points come from | ✅ | |
| Fresh odds, prices, injuries | | Sync |
| First gameweek, recent-form half-life | | Sync |
| Ask why a player is rated highly | | the AI endpoint |

Horizon and half-life only *reweight* points that are already known, so the
browser can do them. First gameweek and recency change which fixtures exist and
what the underlying rates are — those mean re-projecting, and re-projecting
means pandas. Both controls are labelled `sync` and wired to the Sync button
rather than left looking like knobs that quietly do nothing.

Drafts and overrides live in `localStorage`, because saving a squad has to work
on a train. They are pushed back to `out/drafts.json` and merged by name
whenever the laptop is reachable, so the CLI keeps seeing the same squads.

Settings live there too, under `fpl.settings`, written as you touch them: every
knob in the settings rows, the bench weights, the theme, the tab you were on,
the number the shirts carry, the pool filter and
sort, the chart/table toggles, and any ⊕/⊘ constraints. The board opens where
you left it, which matters most on the phone, where the tab gets evicted the
moment you switch apps. Two things are deliberately not saved, because the
snapshot owns them rather than you: the first gameweek, and the bench weights
until you actually move a slider — and a saved horizon longer than the snapshot
can answer is clamped down to one it can. **Restore defaults**, in More options,
puts every knob back; it never touches your squad, drafts or edits.

#### How big it is, and how fast

| | |
| --- | --- |
| Snapshot, 12 gameweeks, 573 players | 489 KB raw, 78 KB gzipped |
| HiGHS WASM solver | 3.3 MB, cached once |
| Full re-solve on the phone | 0.1–2 s, in a worker so the page stays live |
| Horizon or half-life change | a few ms, no network |

### Drafts

Name a squad and save it, then tick two or more to compare. You get a metrics
table (points, gap to the best, cost, field coverage, legality), a per-draft
diff of exactly which players differ from what you're currently building, and
all of them overlaid on the weekly chart.

Only the player ids are saved — never the metrics. A comparison is meaningless
unless every draft is scored under the same assumptions, so they're always
re-scored against the current horizon, half-life and stat edits rather than
frozen at whatever was on screen when you hit save. A draft saved under
different settings is marked `re-scored` so you know why its number moved.

Drafts live in `out/drafts.json`, so they survive restarts.

Two details worth knowing. Comparison is capped at **three saved drafts** (four
series with the live one) — past four, the palette's adjacent pairs stop clearing
the colourblind gate on a line chart, and adding a fifth hue would break the
check rather than pass it. And **colour follows the squad, not the row**: two
drafts holding the same fifteen players draw one line, so they share one colour
in the list, the table and the legend, and the legend names both (`Current draft
= Solver optimal`). Unticking a draft never repaints the others.

Charts follow the project's data-viz rules: validated categorical hues
(four slots, worst adjacent CVD ΔE 9.1 light / 8.4 dark against a target of
8; two slots when only comparing against optimal, 24.7 / 26.8), a diverging
blue↔red scale for the timeline because the question there is polarity — is this
gameweek above or below what this player normally does — and a table-view toggle
on every chart so no value is reachable only through colour. Light and dark are
separately stepped, not flipped.

## Recent form: weighting the end of the season above the start

```bash
python fpl.py plan --recency 10        # half-life in gameweeks; 0 (default) = off
```

Neither live source can do this on its own, which is worth knowing before you
trust it. The FPL API wipes `element-summary` history at a season rollover and
keeps only season totals; Understat returns whole-season aggregates and
**silently ignores date parameters** — three spellings all returned
byte-identical payloads. So match-by-match detail for a finished season is
simply not available from either.

Recency weighting therefore reads the
[vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League)
archive, a community mirror that snapshots the FPL API every gameweek. It is
**third-party**, so the feature is off by default, a fetch failure costs only the
tilt, and it is listed separately on the provenance page.

Two design choices worth stating:

- **It is a multiplier, not a replacement.** The archive carries FPL's
  `expected_goals`, which *includes penalties*, while the model's attacking rate
  is Understat's non-penalty xG. Swapping one for the other would quietly
  reintroduce the penalty contamination that's modelled separately. Instead the
  ratio of recent-weighted to season-long rate is applied on top, carrying the
  trend without touching the basis. Multipliers are clipped to 0.6–1.6.
- **It is applied before shrinkage**, so a big multiplier off a short hot streak
  still gets pulled back toward the positional prior rather than sailing through.

The knob behaves as it should — shorter half-life, larger effect:

| Half-life | Mean absolute change in projected points |
| --- | --- |
| 5 gw | 0.263 |
| 10 gw | 0.176 |
| 19 gw | 0.101 |
| 38 gw | 0.051 |

Season ids are reassigned annually, so rows join to current players through
`code`, the stable per-player identifier — 100% of archive rows map, 457 of them
to players still in the league.

## Predicted clean sheets

`xCS` — expected clean sheets over the horizon — now appears in `rank`, `value`
and the other tables, and in the squad board's pool.

It's counted for **every** position, including forwards who earn nothing for it.
The number answers "will this be kept out while he's playing", which is the
thing you're actually buying a defender for, and it shouldn't disappear because
the player in front of you doesn't get paid for it.

It is measured **on the pitch**, not over the full match, because that is what
the rule pays: a clean sheet is awarded for conceding nothing while you were on
and playing at least 60 minutes, so a defender substituted on 78 minutes keeps
his four points if the goal arrives on 85. The model used to require the club to
keep a full ninety-minute clean sheet while separately charging the concession
penalty against on-pitch minutes only — the same event counted two different
ways. Correcting it is worth about 20% on the clean-sheet component for a
regular starter, and it lands almost entirely on defenders and keepers.

## The bench is not four equal slots

FPL auto-subs work down the bench in order, so the slots are worth very
different amounts: the first outfield substitute comes on whenever a starter
blanks, while the third is close to decoration and the reserve keeper only plays
if your first choice doesn't.

The optimiser now assigns bench *positions* rather than treating the four as an
unordered set, with relative weights in `BENCH_SLOT_PROFILE`:

| Slot | Relative weight | At the default `bench_weight` of 0.12 |
| --- | --- | --- |
| 1st sub | 2.00 | 0.24 |
| 2nd sub | 0.85 | 0.10 |
| 3rd sub | 0.35 | 0.04 |
| Reserve GK | 0.25 | 0.03 |

`bench_weight` scales the whole profile, so the CLI knob still means "how much do
I care about the bench" while the slots stay weighted relative to each other.
The effect is that the solver spends real money on the first substitute and
takes cheap bodies for the last two, instead of paying the same for all four.
The chosen order is shown in the UI (`1st sub`, `2nd sub`, …) and returned by the
optimiser.

If the *shape* of that profile is what you disagree with, not just its size,
pass `bench_slot_weights` instead — a weight per slot, keyed `"GKP"`, `1`, `2`,
`3`, used as-is in place of `bench_weight × BENCH_SLOT_PROFILE`. That is what the
board's four sliders send. Missing slots fall back to the scaled default.

## pStart: a season rate is the wrong answer to a weekly question

`p_start` is the biggest lever in the model — it scales minutes, and minutes
scale everything. It used to be a season-long rate: starts divided by matches
played, blended with a price-based prior. That answers "what share of matches
did he start". The question actually being asked is **"does he start the next
one"**, and the two come apart for anyone whose situation changed inside a
season — the January signing, the man back from three months out, the youngster
who took a place in March. Most players are one of those at least once.

So the model now carries two numbers and blends them by *how far ahead it is
looking*: the long-run rate, and a recency-weighted one (half-life four
gameweeks) from the per-gameweek archive.

**How much the blend should count was measured, not chosen.** Scored out of
sample on 2025-26 — 724 outfield players, ~21,900 predictions, each one built
only from the gameweeks before the one it is guessing — by searching at each
lead for the weight `w` in `p = long_run + w × (recent − long_run)` that
minimised Brier score:

| Lead | best `w` | Brier (blend) | Brier (flat) | improvement |
| --- | --- | --- | --- | --- |
| 1 | 1.09 | 0.10184 | 0.11622 | 12.4% |
| 2 | 0.82 | 0.11452 | 0.12254 | 6.5% |
| 3 | 0.63 | 0.12227 | 0.12704 | 3.8% |
| 4 | 0.51 | 0.12796 | 0.13101 | 2.3% |
| 6 | 0.36 | 0.13666 | 0.13811 | 1.1% |
| 8 | 0.29 | 0.14309 | 0.14398 | 0.6% |

Two things in that table are the whole feature. The blend beats the flat rate at
**every** lead, so this is not a trade of near accuracy for far accuracy. And
`w` decays geometrically — 1.09 down to 0.29, a ratio of 0.828 per gameweek —
which is the measured form of the intuition that a nailed starter is more
obviously nailed next week than he is in two months. `start_form_weight()`
averages that curve over the horizon, so asking for one gameweek fills the board
with who is starting *now*, and asking for twelve hands it back to the
season-long rate.

Reproduce or re-fit it against a new season with:

```bash
python scripts/calibrate-start-form.py --season 2025-26
```

**What it fixed, visibly.** The old shape leaked start probability off the real
starters and onto players who never play — every squad member below the first
team sat at a flat floor set by the price prior, and because each club is
normalised to eleven starters, that floor was paid for by the eleven. Against
last season's actual within-club distribution:

| Within-club rank | Real | Model, before | Model, now (1 GW) |
| --- | --- | --- | --- |
| 1st | 0.95 | 0.95 | 0.95 |
| 6th | 0.72 | 0.70 | 0.72 |
| 11th | 0.48 | 0.34 | 0.49 |
| 15th | 0.29 | 0.16 | 0.27 |
| 20th | 0.09 | 0.16 | 0.07 |
| 24th | 0.01 | 0.16 | 0.00 |

League-wide, players above 0.7 went from 118 to 131 against a real figure of
about 130, and the tail below 0.05 grew from 90 to 122. **So yes — a lot of
players' pStart went up, and it was paid for by the players who were never
starting anyway.**

The editor shows both halves whenever they disagree by more than 0.05
(`season-long 0.41, lately 0.83`), because that is the most useful thing to
know before deciding whether to override him.

**One limitation, stated plainly.** The blend is applied when the projection is
built, so `--horizon` on the CLI changes it and the horizon slider on the board
does not — the board's snapshot is frozen at 12 gameweeks, and the slider
reweights points that are already known rather than re-deriving minutes. Press
Sync, or re-run `snapshot --horizon N`, to see pStart itself move. Making it
per-gameweek inside the fixture loop would remove the caveat entirely and is the
natural next step.

### Was there an API for this instead?

Looked, and the answer is no — not one that is both free and useful:

- **[API-Football](https://www.api-football.com/)** has a genuinely free tier
  (100 requests/day, all endpoints). But its lineups arrive **20–40 minutes
  before kickoff**, which is after the FPL deadline. Useless for picking a team.
- **[Sportmonks Expected Lineups](https://www.sportmonks.com/football-api/expected-lineups-api/)**
  is the real thing — human-curated predicted XIs updated as team news lands —
  and costs €159/month as an add-on to a paid plan.
- **Fantasy Football Scout**, **[Fantasy Football Pundit](https://www.fantasyfootballpundit.com/fantasy-premier-league-team-news/)**,
  **[RotoWire](https://www.rotowire.com/soccer/lineups.php)** and the rest publish
  predicted lineups as **web pages, not APIs**. Scraping them puts a
  third-party's editorial judgement, and their uptime, inside the model.

Which leaves the FPL API's own `status` and `chance_of_playing_next_round`
(already used, as `availability`) and the per-gameweek archive (now used, as
above). The measured 12% Brier improvement came from data already on disk.

## Where the data comes from

`python fpl.py serve` then **/data** — a full provenance page: every field traced
to its source, how the three sources are joined, what is assumed rather than
measured, and what none of it can tell you.

It leads with **live source status** read from the machine at request time, not
from prose: how many players each source returned, how many Understat rows
actually matched, what fraction of fixtures the bookmakers have priced, the age
of each cache entry, and whether the odds key is even set. A provenance page that
describes intentions rather than state is worth very little, so this one reports
what is actually there.

The short version:

| Source | Gives | Auth |
| --- | --- | --- |
| [FPL API](https://fantasy.premierleague.com/api/bootstrap-static/) | prices, positions, minutes, starts, fixtures, chip windows, and the Opta-derived xG/xA/xGC and defensive-contribution counts | none |
| [Understat](https://understat.com/) | **non-penalty** xG, xA, xGChain per 90 | none |
| [The Odds API](https://the-odds-api.com/) | 1×2 and over/under from ~20 books | free key |
| [FPL history archive](https://github.com/vaastav/Fantasy-Premier-League) *(third-party, opt-in)* | per-gameweek rows, for recency weighting | none |

The one thing worth knowing up front: **"Opta data" is already in the FPL API** —
`expected_goals`, `expected_assists`, `expected_goals_conceded` and the
defensive-contribution counts all ship with `bootstrap-static`. There is no
separate Opta feed to buy. Understat earns its place by splitting *non-penalty*
xG out, which the FPL API does not, and which is what lets penalties be modelled
separately and assigned to the designated taker.

## Overriding the model when you think it's wrong

Every rate the model uses is an estimate from last season, and there are things
you know that last season cannot contain — a new penalty taker, a changed role, a
player whose xG came from a system he has left. Any of these can be replaced with
your own number:

| Field | What it is |
| --- | --- |
| `p_start` | probability he starts — the biggest lever in the model |
| `mins_if_start` | how long his shift is on the weeks he starts (default 78) |
| `exp_minutes` | the two multiplied out, if you'd rather state it that way |
| `npxg_per90` | non-penalty xG per 90 (penalties are modelled separately) |
| `xa_per90` | expected assists per 90 |
| `dc_per90` | defensive contribution per 90 (threshold 10 for DEF, 12 otherwise) |
| `bonus_per90` | bonus points per 90 |
| `saves_per90` | saves per 90 (keepers) |
| `yellow_per90` | yellow cards per 90 |
| `penalties_order` | 1 means he takes them |
| `price` | for hypotheticals; it doesn't change what FPL charges you |

Add `_mult` to any of them to *scale* the model's value rather than replace it —
often the more natural way to put it (`npxg_per90_mult=1.2` for "about 20% better
than his old club suggests").

### Starting and staying on are two different claims

`p_start` and `mins_if_start` used to be one field wearing two hats, and the hat
that lost was the one you needed for a striker who starts every week and comes
off on the hour. Stating `exp_minutes` was the only way to say it, and stating
`exp_minutes` back-solved `p_start` — so "always plays, hooked at 60" went into
the model as "starts about 70% of the time, plays 78 minutes when he does". Those
are not the same player. The second one loses the appearance point he definitely
earns, and he keeps the full clean sheet the first one does not.

They are separate now. `p_start` is how often he is in the side; `mins_if_start`
is how long he stays on when he is, defaulting to the league-average 78. Both the
60-minute appearance point and the clean-sheet gate follow the second one, along
a logistic pinned at two points rather than fitted — an hour-long shift reaches
the hour half the time by definition, and a 78-minute shift reaches it 87% of the
time, which is the number already calibrated. Nothing moves for a player you have
not touched: at 78 minutes the curve returns exactly the old constant.

`exp_minutes` still exists and is still the two multiplied out. Stating it now
spends the *shift* first and only reaches for `p_start` when ninety minutes still
is not enough — because lengthening one man's shift says nothing about anybody
else, while raising his start probability takes a shirt off a team-mate and drags
his whole club through the rebalance below. A player the model has never heard of,
asserted into the side at 60 minutes a week, still gets there the old way.

**In the browser:** click the ✎ beside any player. The drawer shows what the
model currently believes for each input, a live recomputed projection as you
drag, and the delta against the unedited value. `Apply` commits the edit;
closing the drawer any other way throws it away, so the board and the optimiser
are never looking at different numbers for the same player. An applied override
flows straight through to the optimal squad, which re-solves against it.

### Per match, not just per player

Some things you know are not facts about a player at all — they are facts about
one fixture. He is rested before a European tie. He is suspended for one game.
He is back in three weeks. He moves up front while the striker is out. Flattening
those across a horizon is not an approximation of the right answer, it is a
different and wrong one.

So the drawer has a **Per match** section below the season-level sliders: one row
per gameweek with its opponent, what he is currently projected for that week, and
a start-probability slider. `⋯` opens the full field list for that gameweek alone.

The layering rule is the obvious one. A season value applies to every fixture; a
match value replaces it for that week. Each per-match control shows the *season*
value as its baseline, not the model's, so setting a week back to what you already
said about the player clears the override rather than recording a redundant one.

Everything else follows: an edited week moves the fixture timeline, the weekly
chart, xPPG and the optimiser's view of him — including his eligibility, because
overriding `p_start` re-derives `p_play`, which is what the solver filters on.

**From the CLI**, it is one extra column. A row with a `gw` is about that match;
a row without one is about the player. An existing `overrides.csv` still loads
unchanged.

```csv
fpl_id,web_name,gw,p_start,npxg_per90
12,Saka,,0.8,            # every fixture
12,Saka,3,0.0,           # ...except gameweek 3, when he is rested
12,Saka,7,,0.9           # ...and gameweek 7, when he moves inside
```

`out/overrides.csv` is written in exactly this shape, so an edit made while
drafting carries over to `plan`, `squad` and the rest.

**Nothing here has a save button, on purpose.** There used to be two — `Apply`
in the editor and `Save to overrides.csv` in the banner — and both were the
wrong shape for what they did. Edits go to `localStorage` the moment you make
them, which is the copy that is always there, including on a phone on a train;
the file is only ever a mirror of that for the CLI to read. So a button marked
save was a button that felt load-bearing and wasn't, and forgetting the second
one meant the CLI quietly planned off numbers you had already corrected.

Now the drawer commits as you drag, debounced, and the CSV mirrors itself a
beat later. When the laptop is not reachable the mirror is skipped silently and
retried when the connection comes back — `localStorage` already has the edits,
so there is nothing to warn about. The banner says where things stand rather
than asking you to do anything about it.

The escape hatch that makes live commit safe is `Undo changes`, which puts the
player back to how he was when you opened the drawer. That is a different thing
from `Reset player`, which throws away every edit you have ever made to him,
including ones from last week.

**From the CLI:**

```bash
python fpl.py overrides                      # list every field and its range
python fpl.py overrides Senesi Rashford      # write a template with current values
python fpl.py plan --overrides out/overrides.csv
```

The CSV identifies players by `fpl_id` or `web_name`; every other column is
optional.

One deliberate behaviour: **an override bypasses shrinkage**. Raw rates are
pulled toward the positional average because a small sample shouldn't speak too
loudly — but an assertion isn't a sample, so if you state a number the model uses
that number exactly.

Edits are applied at the very end of the pipeline, after every rate is derived
and shrunk, and the browser's fast single-player recompute walks the same scoring
code as a full run. That equivalence is checked rather than asserted:
`scripts/verify-js-port.mjs` rescores every player in the pool and 700-odd
override combinations through both paths and reports the worst disagreement,
which sits at the rounding floor of the reference data (5e-7 raw, 5e-5 through
`reproject_player`, which rounds to 4dp on its way out of Python).

## The horizon problem, and `plan`

`squad --horizon N` is sensitive to N in a way that should bother you. Solve it
at every horizon and the answer never stops moving:

```text
$ python fpl.py horizon

  horizon   last_gw   churn   ∩ plan
        1         1       -       10
        2         2       6       13
        3         3       2       13
        5         5       5       11
        8         8       5       13
       10        10       4       14

Raw squads never settle — they still churn 4 players at the longest horizon tested.
Picking a single horizon means picking one of these arbitrarily.
```

`churn` is how many of the fifteen change when you look one gameweek further
ahead. There is no N at which this converges, so "which horizon?" has no answer
on its own terms — a short one chases fixtures, a long one builds for games you
will never field this squad in, because you will have made six transfers by then.

`plan` replaces the cliff with a decay. Three things all fall off with distance:

1. **Optionality.** One free transfer a week. A bad gameweek-five fixture is not
   a cost you are locked into; this week's is.
2. **Availability.** Players get injured, suspended and dropped. Today's nailed
   starter is meaningfully less likely to be one in gameweek eight.
3. **Model confidence.** The bookmakers have priced about the next fortnight.
   Past that the fixtures come from ratings alone.

So future gameweeks are discounted geometrically (`--half-life`, default 3
gameweeks) and multiplied by a per-player survival curve. The half-life of 3 is
not arbitrary: at one transfer a week you can turn over a third of the squad in
three gameweeks, which is roughly where "who I own" stops determining "who I
field".

The point of this is stability. Varying the half-life from 1.5 to 8 changes the
plan squad by 0, 0, 3, 1 and 0 players at successive steps, against the 2–6 per
step the raw horizons churn — and the plan squad shares 12.2/15 with the raw
squads on average, so it sits in the middle of that range rather than at an
extreme. The knob exists, but the answer barely depends on it.

`plan` also reports:

- **`core`** — the players it picks whether you plan one gameweek ahead or six.
  These are the decisions the horizon does not affect, so they are the ones to
  be confident about. The rest are fixture-dependent and worth less conviction.
- **A fixture timeline** — see below.
- **Rank risk** — the high-ownership players you do not own.
- **A transfer path** — which gameweeks to move in, which to bank for, what it
  costs, and where the chips go. Solved under the real free-transfer rules; see
  below.
- **Chip timing**, and usually a hold — see below.
- **Expected value change** from the price forecast.

## The transfer schedule, and why the old one was wrong

There used to be one, and it was deleted. It produced this:

```text
gw 4   transfer   Guéhi   -> Lacroix   +0.60
gw 5   transfer   Lacroix -> Guéhi     +0.40
gw 6   transfer   Guéhi   -> Lacroix   +0.59
```

Two free transfers burned to end up where you started. The conclusion drawn at
the time was that you cannot know your gameweek-six transfer in gameweek one, so
the schedule had to go — and that half is still true, and still says so below.
But it was the wrong reading of *these three lines*. They are not over-confidence
about the future. They are a model that does not know a transfer is a resource:
each week it re-asked "what is the best squad for this week?" and paid whatever
it cost to get there, because nothing in the objective charged it for spending.

`transfers` puts the price of spending into the objective. Three things, all of
them mechanical rather than predictive:

1. **Transfers are a stock, not a flow.** One a gameweek, banked to a maximum of
   five, and everything past the allowance costs −4. That is an inventory
   problem with a hard cap, and it is why "hold this week, do two next week" is
   a move rather than a delay. The balance follows the real rule, including the
   2024/25 change that a wildcard or free hit costs you that week's new transfer
   but no longer burns the ones you banked.
2. **A banked transfer is worth points you have not scored yet.** Holding is
   free to a solver that only counts points on the pitch, so it always spends.
   The bank is priced at 1.5 points with diminishing returns (the second one you
   bank is worth most, the fifth least — it can only ever be spent in a week you
   spend the other four too).
3. **Acting costs something.** A flat 0.2 per move. Between two players a tenth
   of a point apart the model's own error is an order of magnitude larger than
   the gap, and this buys nothing except a refusal to trade on noise.

With those in, the swap-and-swap-back disappears without being banned — which is
the test that the fix is the right one rather than a patch over the symptom.
`scripts/verify-transfer-rules.py` checks it both ways round: the same synthetic
projection, solved with the pricing switched off, brings the oscillation
straight back. If it did not, the scenario would not be testing anything.

```text
$ python fpl.py transfers --squad out/squad.csv --free-transfers 2 --bank 1.5

gw  chip  TRs  FT  hits  bank  XI xPts  weight
 1           1   2     0   0.5     55.8    1.00
 2           0   2     0   0.5     52.3    0.90
 3           0   3     0   0.5     48.3    0.81
 4           0   4     0   0.5     46.8    0.73
 5           1   5     0   1.0     45.1    0.65
 6           1   5     0   0.5     44.6    0.59

gw  Pos  out             £ out  in       £ in  to   gain
 1  MID  Tavernier         6.0  Gakpo     7.0  LIV  1.57
 5  DEF  Virgil            6.5  Guéhi     6.0  MCI  0.46
 6  FWD  Calvert-Lewin     6.0  Mateta    6.5  CRY  0.76

This gameweek: Tavernier → Gakpo — worth +0.06 against rolling the transfer instead.
```

That last number is the only one to act on, and it is the reason the command
solves twice. **+0.06** is the whole plan's value with this gameweek free, minus
the whole plan's value with this gameweek's transfers banned. Because both sides
are scored on the same objective, it already nets off the four points a hit
would cost, the friction, and the banked transfer the move spends. Here it says
the move is worth making and barely: a sixteenth of a point, which is a way of
saying *this is a coin flip, and rolling is fine.*

Everything after gameweek one is a shape, not an instruction. It gets re-solved
next week against team news, price moves and a fixture list this run cannot see,
and it will come out differently. What it is good for is the question the
timeline below was invented to answer — is there a run coming that I should bank
a transfer for? — except now the answer is priced rather than eyeballed.

The horizon is six gameweeks by default and the discount is gentler than
`plan`'s: a 6.5-gameweek half-life, or 0.90 a gameweek, against `plan`'s 3.
That is deliberate and it is not a disagreement. Three of the reasons `plan`
discounts so hard *are* optionality — a bad fixture in five gameweeks is not one
you are locked into, because you will have made transfers by then. This model
makes the transfers explicit, so discounting at 3 again would charge for the
same thing twice. 0.90 is where the two most used solvers in the FPL
optimisation community sit (0.84–0.90) once optionality is modelled rather than
assumed.

`plan` runs this automatically and prints the path underneath the squad;
`--no-transfer-plan` skips it if you only want the projection.

### What still cannot be planned

The fixture timeline is still printed, and still the more honest artefact for
anything past the next gameweek or two:

```text
Player      gw1    gw2    gw3    gw4    gw5    gw6   swing   worst v
Guéhi      =4.0   =3.4   +4.3   -3.0   +4.4   -3.1    1.37   man utd
Shaw       +3.8   =3.4   =2.8   -2.6   =2.9   =3.3    1.25   MAN CITY
João Pedro =3.8   =3.9   -3.1   =4.3   =3.7   =4.1    1.18   arsenal
```

Each cell is that gameweek's projected points, marked against **that player's
own** average — `+` good week for him, `=` par, `-` bad. Sorted by swing, so the
most fixture-dependent players are at the top; opponent in CAPS means at home.
Below it, runs of two or more bad gameweeks are listed separately, because a
single poor fixture is rarely worth a transfer but a run is worth banking one
for.

## Rank risk: what happens if Haaland hauls and you don't own him

FPL is scored on rank, not on points, so the question is never "how many points
will I score" but "how many relative to everyone else". A player owned by 75% of
the field who returns big costs you most of his haul in relative terms even
though your own total is untouched. That is why not owning Haaland can hurt more
than owning a mediocre midfielder.

`plan` quantifies it:

```text
Rank risk — best players you do not own
Player       Pos  Team    £   own%   xPts(plan)   exposure
Haaland      FWD  MCI  15.5   75.2        19.31      14.52
Szoboszlai   MID  LIV   7.0   48.2        12.21       5.89
Rogers       MID  CHE   7.5   32.1        11.73       3.77
```

`exposure` is ownership × projected points: roughly what the average rival banks
from him and you do not. It also reports what share of the field's total
expected points your squad covers.

This is a risk list, not a shopping list — covering everything is how you
guarantee finishing exactly average. `--ownership-weight` is the dial: 0
maximises expected points and ignores the field, higher values buy template
cover at a measurable cost. On an eight-gameweek plan, going from 0 to 0.4 moves
field coverage from 31.6% to 38.9% and costs 2.9 expected XI points.

Because the wildcard cannot be played in gameweek one (`start_event: 2` in the
API), the initial squad only has to be right for the first few gameweeks. That
is a real argument for a shorter half-life than instinct suggests.

## Risk, price and chips

**Injury/rotation hazard.** Each player carries a per-gameweek probability of
dropping out, compounding into the survival curve. Age is the one durable
observable risk factor available (3% per gameweek baseline, rising past 29);
players already flagged doubtful carry double. This is what stops the plan from
loading up on 33-year-olds for an eight-gameweek window.

**Price changes** are forecast from net transfers when the season is running —
that is the actual mechanism, since FPL raises a price once net transfers clear
an ownership-scaled threshold. Before the season starts there are *no transfer
counts at all* (they are literally zero in the API), so direction falls back to
a value proxy and is labelled low confidence.

Ownership does not push a price in a direction; it **amplifies whichever
direction the player is already going, asymmetrically**. A fall needs sellers,
and only owners can sell, so a heavily owned player going badly drops fast.
A rise needs buyers against a threshold that itself scales with ownership, so a
heavily owned player rises more slowly — most of the people who would buy him
already have him. At identical value pressure, forecast rises damp from +0.13 to
+0.09 as ownership goes 2% → 60%, while falls amplify from −0.07 to −0.13.

One honest caveat: preseason, direction comes from value alone, and value
correlates with ownership — so the model will not predict a *fall* for a
popular player until real transfer data exists, because it has no form signal
to tell it he is struggling. The asymmetry is verified against synthetic input
rather than live data for that reason.

Treat the whole thing as a tie-breaker between similar players, never a reason
to pick a worse one. The command always prints which basis it used.

### Chips: the question is not "when", it is "instead of what"

Chip timing used to be four independent heuristics — biggest single-player week
for the triple captain, best bench week for the bench boost, and so on — each of
which answered *when in this window?* and none of which could answer *is any
week in this window good enough to spend it on?* On a flat fixture list the
first question has no honest answer, so the old code hard-coded a refusal and
printed "hold".

The refusal was right and the reason was wrong. Chips are now decided inside the
transfer model, for two reasons that are the same reason:

- **A chip is a squad decision.** A bench boost is worth playing only if the
  bench is worth fielding, and the bench is a transfer decision taken weeks
  earlier. A wildcard is a gameweek with fifteen free transfers, so it competes
  directly against the transfers either side of it. Solved separately, both come
  out wrong.
- **A chip has a reservation price.** This is the one that actually matters.
  Left to itself over a six-gameweek window, a solver plays all four chips in
  the first four gameweeks — not because those are good gameweeks, but because a
  chip unplayed at the end of the window is worth exactly zero, and the window is
  six gameweeks while the chip's window is nineteen. So each chip carries what it
  is worth *held*: 14 points for a bench boost, 10 for a triple captain, 12 for
  a free hit, 15 for a wildcard. Those are the published aggregates for a chip
  played on a double or a blank — the gameweeks that are not on the calendar
  until cup rounds are drawn and games postponed, which is the entire reason
  holding is worth anything.

The result preseason is the same "hold" the heuristic printed, now as an
arithmetic comparison rather than a hard-coded refusal — which means it also
knows when to stop refusing:

```text
chip             gw   worth   edge  read
Free Hit          -       -      -  no blank or double to hit
Wildcard          -       -      -  hold — beaten by keeping it
Bench Boost       -       -      -  hold — beaten by keeping it
Triple Captain    -       -      -  hold — beaten by keeping it
```

`--ignore-chip-hold` sets every reservation price to zero and asks the narrower
question the heuristic was asking. It answers bench boost in GW2 worth 12.5, and
triple captain in GW1 worth 6.1 — both with an `edge` of about 1.1 over the
median gameweek in the window, which is the number that says *this is noise*.
An edge of 1.1 points is not a reason to spend a chip you can hold for a double.

Two chips have no payout of their own, because they pay through the squad they
let you buy rather than through points on the day. `--chip-value` prices those
by re-solving without them: the difference between the best plan that has the
chip and the best plan that does not.

Free hit gets skipped entirely when there is no blank or double in the window,
which is both honest and a real saving — it is a second fifteen-man squad's
worth of binaries to discover it is worth nothing.

## Form and players who changed club

The rates come from last season, and two things make that less reliable than the
minute counts suggest.

**Cross-season regression.** A complete campaign should not be taken at face
value, because a rate is partly the player and partly what happened to him. How
much of it carries is the single most consequential number in the model, and it
used to be set from a remembered figure — "year-over-year correlation runs
0.6–0.7" — applied to every rate at 1,200 prior minutes.

It is now measured, over three seasons of Understat, by
[scripts/calibrate-shrinkage.py](scripts/calibrate-shrinkage.py). The prior is
not a diagnostic of the shrinkage; it *is* the shrinkage, so it is fitted the
way it is used — by minimising the error of `w·own + (1−w)·prior` against what
each player actually did next season, over 468 player-seasons:

| rate | fitted prior | 95% CI | weight on a 3,000-minute season |
| --- | --- | --- | --- |
| npxG/90 | 342 | [186, 538] | 0.90 |
| xA/90 | 891 | [601, 1151] | 0.77 |

Against the 1,200 that had been applied to both, which gives 0.71. **The model
was shrinking its best-evidenced attackers about three times harder than a
season of evidence justifies** — the top fifth of last season's scorers came out
at 0.77 of their rate, and roughly half of that was this constant rather than
any real regression.

The values in use are 550 and 1,000: the cautious end of each interval, not the
point estimate. The players who can be measured this way were established in two
consecutive seasons, and a number fitted on those is read back onto a population
that includes players who lost their place — so erring toward the prior errs in
the direction the sample is weakest. Some compression between players is still
correct. Three times too much of it was not.

Two things that look like improvements and are not, both checked by the same
script rather than argued about:

- **Splitting npxG/90 into shot volume × conversion.** Volume persists well
  (k=316) and conversion barely does (k=2045), which looks like a clear case for
  shrinking them separately. But the product already predicts next season as
  well as anything built out of its parts — r=0.860 for the rate alone against
  0.797 for volume alone and 0.860 for a half-and-half blend. Splitting buys
  nothing, so the model does not.
- **Scaling a player's xG by fixture difficulty per position.** The model
  applies one `attack_scale` to everyone at a club. Measured against ex-ante
  fixture quality over 6,400 player-matches, the elasticity is 0.74 for
  forwards, 0.89 for midfielders and 1.13 for defenders — but bootstrapped over
  *players*, every one of those intervals contains 1.0. There are only 30
  forwards in the sample. Acting on it would be fitting noise, and it would cost
  the exact conservation that makes squads sum to their fixture's lambdas.

**Changing club is worse than that.** Senesi's numbers were produced at
Bournemouth, with Bournemouth's team-mates, system and role. `movers` lists
everyone affected:

```text
Player      Team  from                £    minutes   ctx   npxG90   conf
Rogers      CHE   Aston Villa       7.5      3280   1.03    0.183   moderate
Anderson    MCI   Nottingham Forest 6.5      3332   1.22    0.116   moderate
Senesi      TOT   Bournemouth       6.0      3288   0.88    0.053   moderate
```

Two adjustments apply. `ctx` scales his attacking rates by how his new club's
attack compares to the one his numbers came from — above 1 means he joined a
better attack — damped by a square root, since output is part player and part
team. Only attacking rates are touched: defensive contribution is a function of
role far more than team quality, and the clean-sheet and concession terms
already use the new club's defence. On top of that he carries a prior 1.8×
heavier, so his rates sit closer to the positional average than his raw history
implies. The `conf` column surfaces this everywhere.

What you should do about it: treat `moderate` and below as provisional, and lean
on the `--overrides` file once you have seen a couple of gameweeks. The model
cannot tell you how Senesi fits at Spurs, and neither can anyone else yet.

**Chips.** Chip value comes overwhelmingly from double gameweeks and covering
blanks, and neither is on the calendar until cup rounds are drawn and games get
postponed. When the fixture list is flat, the "best" gameweek for a chip is
whichever one noise favours, so the tool says `hold` rather than dressing that
up as a recommendation:

```text
chip             gw   detail                                        confidence
Triple Captain   -    hold — no doubles or blanks scheduled in      n/a
Bench Boost      -    this window; the first-half set does not      n/a
Wildcard         -    expire until GW19                             n/a
```

Once doubles and blanks appear, the same code picks real gameweeks and raises
the confidence. Double and blank gameweeks are detected from fixture counts per
team per gameweek, so it needs no extra data source. Chip windows come from the
API's own `chips` block, per half of the season — the two sets are kept separate
so the GW19 expiry on the first set is not silently erased.

## Reading the optimiser's numbers

The solver maximises `XI points + captain + bench_weight × bench points`, and
that combined figure is reported as **`objective`**. It is the only number
comparable between two solves.

`xi_points` is a *component* of it. A constrained solve can therefore post a
higher `xi_points` than an unconstrained one by trading bench quality away — a
trade the solver would never make freely, but the component can still rise. If
you force a player in and see XI points go up, that is what happened; the
objective will always have gone down. `compare --squad-test` and
`marginal_value` both rank on `objective` for this reason.

## Answering "is he worth the extra money?"

There are three increasingly honest ways to ask, and the tool gives all three.

**Points per million** (`value`) is the crude version. It systematically
flatters cheap players: a £4.5m defender projecting 3.0 points a game looks
twice as "efficient" as a £15.5m striker projecting 6.0, but you cannot field
fifteen of him, and the two spare millions have to go somewhere.

**Points per extra million** (`compare`, `upgrade`) is better. It asks what the
*upgrade* buys — the points gap divided by the price gap — and compares that to
what a million typically buys elsewhere in the market.

**Marginal squad value** (`compare --squad-test`) is the real answer. It builds
the best possible legal squad with the player forced in, then the best possible
squad with him barred entirely, and reports the difference. That number already
charges him for the money he ties up, because the squad without him got to spend
those millions on somebody else:

```text
$ python fpl.py compare Haaland Thiago --squad-test

Haaland costs £7.5m more than Thiago and is projected +7.20 xPts better over GW1–5.
  That is +0.96 xPts per extra £m. Median xPts/£m in the pool is 2.53.

  Player      £   own_xpts   squad_with   squad_without   marginal
  Thiago    8.0     21.87       257.86          255.83       2.03
  Haaland  15.5     29.07       257.86          256.20       1.67
```

Haaland scores far more points, and is still the wrong pick on this horizon:
locking up £15.5m costs the rest of the squad more than his 7.2-point edge
returns. That inversion is the whole point of the exercise, and it is invisible
in any per-player table.

## How the projection works

**1 — Expected goals per fixture.** De-vigged 1X2 probabilities plus the
over/under line are inverted into `(λ_home, λ_away)` under independent Poisson,
fitted by least squares. Where the books have not priced a fixture yet (they
price roughly a fortnight ahead), team attack and defence ratings derived from
xG are used instead, applied on top of a fixed league-average goals rate.

**2 — The player's share.** His non-penalty xG and xA per 90 are scaled by the
ratio of this fixture's expected goals to his team's season average, and by his
expected minutes. Penalties are modelled separately and given to the designated
taker rather than smeared across everyone with a high xG.

**3 — Points.** The scoring rules are applied, with the non-linear terms —
clean sheets, the −1 per two conceded, saves, defensive contribution — evaluated
over their full Poisson distributions and across the start/substitute cases
separately, rather than by plugging in an average. `E[floor(GC/2)]` is not
`E[GC]/2`, and the difference is worth real points at low lambdas.

Every rate is shrunk toward its positional average, weighted by the minutes
behind it. Without that, the optimiser reliably picks whoever had the smallest,
luckiest sample in the league.

## The part you should not trust

**Minutes.** Everything above is a rounding error next to whether a player
starts. Start probabilities come from last season's starts, which cannot see
transfers, new managers, or a changed pecking order — and in preseason that is a
lot of what matters.

`blindspots` lists every priced player with too little Premier League history to
project (summer signings, returning loanees) and writes an overrides template:

```bash
python fpl.py blindspots                                    # writes out/overrides-template.csv
# edit p_start (0-1), mins_if_start (0-90) or exp_minutes (0-90), drop the rest
python fpl.py squad --overrides out/overrides-template.csv
```

Those players score zero until you override them. That is a gap in the data, not
a prediction — treat it as such.

Other known limitations, roughly in order of how much they cost you:

- **The price forecast is weak preseason** and clearly labelled as such.
- **What a chip is worth held is an estimate**, and it is the number that
  decides whether a chip is played at all. `CHIP_HOLD_VALUE` comes from
  published aggregates for chips played on doubles and blanks, not from anything
  this model measures, and it is deliberately set at the low end of the case for
  waiting. Preseason it is doing almost all the work, because nothing else in a
  flat six-gameweek window can tell one gameweek from another. It is a knob;
  `--ignore-chip-hold` sets it to zero.
- **Future prices are held fixed in the transfer plan.** A move planned for
  gameweek five is costed at today's price, and the price forecast that sits
  next to it is not fed into the budget. That understates the cost of waiting on
  a riser and overstates it on a faller.
- **The transfer plan cannot see the second half of the season.** The horizon is
  six gameweeks and the chips expire at 19, so anything the plan says about
  chips is about whether to spend one *now*, never about a sequence across the
  half.
- **Form within a season is not modelled at all.** Every rate is a season
  average; a player in the middle of a hot streak looks identical to one who
  front-loaded his returns. This matters most in the first few gameweeks of a
  new season, when last year's average is all there is.
- **BPS internals changed for 2026/27** (clearances/blocks/interceptions now
  score 1 BPS per three rather than per two, and the tackled-player deduction is
  gone). Bonus is extrapolated from last season's bonus per 90, so centre-backs
  are now slightly overrated on that component. Core scoring, defensive
  contributions and the chips are unchanged.
- **Independent Poisson understates draws** by about two points of probability
  (worst 1X2 reconstruction error 0.022 across a live gameweek). Dixon-Coles
  would fix it; the effect on clean-sheet probabilities is small.
- **Goal coverage.** The model only attributes a club's expected goals to
  players it has history for, so a promoted club or one that rebuilt over the
  summer has goals it knows the team will score but cannot assign to anybody.
  Non-promoted clubs sit around 0.94; Coventry, Hull and Ipswich are near zero.
  This does not distort the players you *can* see — each is projected from his
  own rate — but their team-mates are invisible. `blindspots` lists the worst
  clubs, and `--overrides` is the fix.
- **Rotation and cup congestion** are invisible beyond the flat hazard rate.
- **Promoted clubs** get flat assumed ratings (80% attack, 125% defence) until
  they have Premier League xG.
- **Set-piece duties** are not modelled beyond penalties, so a new corner taker
  is undervalued.
- **Only the first penalty taker is modelled.** He is credited for penalties in
  proportion to the minutes he is on the pitch, and the rest go to nobody — the
  API gives a `penalties_order`, so backing up to the second taker is a real
  option, but guessing that a substitute inherits the duty is not obviously
  better than declining to guess. It costs about 1% of league goals.
- **Early in a new season the model is thin, and says so.** Rates from Understat
  carry last season's full weight, but everything the FPL API supplies — minutes,
  starts, defensive contribution, saves, bonus — restarts at zero and takes a
  couple of months to say much. The projection degrades toward priors rather
  than toward nonsense, but the first few gameweeks are when `--overrides` earns
  the most.

## Layout

| File | Role |
| --- | --- |
| [fpl.py](fpl.py) | entry point |
| [fplkit/cli.py](fplkit/cli.py) | commands, tables, filters |
| [fplkit/server.py](fplkit/server.py) | squad-board API, static assets, club shirts, sync |
| [fplkit/snapshot.py](fplkit/snapshot.py) | freezes a projection into the file the browser runs on |
| [fplkit/site.py](fplkit/site.py) | writes the whole board to a directory a static host can serve |
| [src/worker.js](src/worker.js) | the Worker route behind `/api/sync` — the one thing a static host cannot do alone |
| [wrangler.jsonc](wrangler.jsonc) | Cloudflare Workers config: static assets, the sync route, the KV binding |
| [fplkit/web/index.html](fplkit/web/index.html) | squad-board UI |
| [fplkit/web/pitch.mjs](fplkit/web/pitch.mjs) | draws a squad as a pitch: shirts, formation, bench order, the in/out diff |
| [fplkit/web/board.mjs](fplkit/web/board.mjs) | derives the player pool from the snapshot — what `/api/pool` was |
| [fplkit/web/points.mjs](fplkit/web/points.mjs) | browser port of the scoring layer, for offline overrides |
| [fplkit/web/poisson.mjs](fplkit/web/poisson.mjs) | browser port of the Poisson helpers |
| [fplkit/web/solver.js](fplkit/web/solver.js) | browser port of the MILP, as CPLEX LP for HiGHS |
| [fplkit/web/solver-worker.js](fplkit/web/solver-worker.js) | runs the solve off the main thread |
| [fplkit/web/chips.mjs](fplkit/web/chips.mjs) | candidate pool, chip windows and the chip report — prep and presentation for the transfer-and-chip planner |
| [fplkit/web/transfers.js](fplkit/web/transfers.js) | browser port of `plan_transfers`, as CPLEX LP for HiGHS |
| [fplkit/web/transfer-worker.js](fplkit/web/transfer-worker.js) | runs the transfer-and-chip solve off the main thread |
| [fplkit/web/sw.js](fplkit/web/sw.js) | service worker: caches the shell and the last snapshot |
| [fplkit/web/data.html](fplkit/web/data.html) | data provenance page |
| [fplkit/model.py](fplkit/model.py) | the three-layer projection |
| [fplkit/planning.py](fplkit/planning.py) | decay, survival, fixture timeline, rank risk |
| [fplkit/transfers.py](fplkit/transfers.py) | multi-period MILP: transfers, free-transfer accounting, hits, chips |
| [fplkit/poisson.py](fplkit/poisson.py) | odds → expected goals → points distributions |
| [fplkit/optimise.py](fplkit/optimise.py) | MILP squad selection and marginal value |
| [fplkit/matching.py](fplkit/matching.py) | fuzzy joins between the three sources |
| [fplkit/config.py](fplkit/config.py) | scoring rules, squad rules, model constants |
| [fplkit/sources/](fplkit/sources/) | FPL API, Understat, The Odds API, history archive |
| [scripts/verify-season-rollover.py](scripts/verify-season-rollover.py) | simulates a season rollover, which a live run cannot |
| [scripts/verify-transfer-rules.py](scripts/verify-transfer-rules.py) | checks the transfer plan against the transfer rules, on projections built to catch it |
| [scripts/make-transfer-cases.py](scripts/make-transfer-cases.py) | solves synthetic transfer-and-chip scenarios with CBC, for `transfers.js` to match |
| [scripts/verify-transfer-port.mjs](scripts/verify-transfer-port.mjs) | re-solves those scenarios with the vendored WASM HiGHS and compares |
| [scripts/calibrate-shrinkage.py](scripts/calibrate-shrinkage.py) | fits the shrinkage priors from three seasons; re-run when one ends |
| [scripts/calibrate-start-form.py](scripts/calibrate-start-form.py) | fits the pStart recency blend and its decay; re-run when a season ends |

Scoring rules and model constants are all in [config.py](fplkit/config.py) — if
FPL changes the rules, that is the only file to edit. That holds for the browser
too: the snapshot carries a `rules` block read straight out of `config.py`, and
`points.mjs` takes every constant from it. Nothing is hand-copied into
JavaScript, so a rule change reaches the phone on the next sync rather than
through somebody remembering there were two copies.

### Checking the ports still agree

```bash
python scripts/make-override-cases.py && node scripts/verify-js-port.mjs
python scripts/make-solver-cases.py   && node scripts/verify-solver-port.mjs
python scripts/make-lineup-cases.py   && node scripts/verify-lineup-port.mjs
python scripts/verify-season-rollover.py
python scripts/verify-transfer-rules.py
python scripts/make-transfer-cases.py && node scripts/verify-transfer-port.mjs
```

The first rescores every player in the pool and 700-odd override combinations —
season-wide, per match, and the awkward ones where a match value contradicts the
season value under it — with `points.mjs`, and compares against the pandas
pipeline and `model.reproject_player`. It also checks that `apply_fields` (one
player, used per fixture) agrees with `apply_overrides` (whole table), since the
per-match path depends on the two staying identical. The second re-solves 54 randomised settings with the
vendored WASM HiGHS and compares against CBC. Both tolerances are set by
rounding in the reference data rather than by slack in the port, so a real
disagreement lands orders of magnitude above them.

The third is a different kind of check: it simulates something the calendar will
only let you observe once a year. See below.

The fourth checks the transfer plan against the transfer *rules*, which is the
one part of this tool whose output cannot be sanity-checked by eye. A squad you
can look at; a six-gameweek path through the free-transfer state machine, with
hits and chips in it, you cannot — and the failure mode is not a crash, it is a
plan that is quietly illegal or quietly oscillating. So it builds synthetic
projections where the right answer is known by construction and the wrong answer
is attractive: a squad with nothing to gain (roll), two alternating premiums the
budget cannot both fit (hold one), an upgrade worth six points a week and one
worth 0.2 (take the hit, refuse the hit), half the league improving at once
(wildcard), a blank gameweek (free hit), a flat calendar (hold everything).
Sixty-four checks over seventeen solves, including the free-transfer recursion
gameweek by gameweek and the fact that a wildcard leaves the banked balance
alone.

One of those runs backwards on purpose. The alternating-premium scenario is
solved a second time with the transfer pricing switched off, and it has to bring
the swap-and-swap-back back — if it does not, the scenario was never testing the
thing it claims to test.

The fifth is the port check for all of the above: `make-transfer-cases.py`
solves eight small synthetic scenarios — a hold, a hit worth taking and one
just too small, a bench boost and a triple captain each worth playing, a free
hit into a blank, a wildcard worth the rebuild, and all four chips available
at once — and dumps the exact pool, points and settings `plan_transfers`
solved, verbatim. `verify-transfer-port.mjs` re-solves each with the vendored
WASM HiGHS and checks the objective agrees (the bar `verify-solver-port.mjs`
uses, for the same reason: two fifteens can tie on value, and which one either
solver returns is not a promise either makes) and, case by case, that the same
chip gets played in the same gameweek.

## Chips and transfers in the board

`fpl.py transfers` solves the whole window and prints a ledger; the board's
Chips tab runs the same model — `transfers.js`, the browser port of
`plan_transfers` — against the fifteen currently on screen, capped to a
six-gameweek horizon (`DEFAULT_TRANSFER_HORIZON`, independent of the board's
own horizon slider) because the candidate pool here is the union of the
top-points and top-value players per position, up to ~150–190 players before
overlap, not the ~50 the squad optimiser ever sees. That pool times horizon is
enough binaries that it is a button ("Plan transfers & chips"), not a
debounced auto-solve — on the synthetic 90-player cases in
`verify-transfer-port.mjs` the slowest single solve was already north of 15
seconds, and a real pool is larger.

Two ownership facts the board didn't otherwise need are new settings: free
transfers available (0–5) and chips already used this half. Bank is not one of
them — it is derived from the existing budget slider minus the squad's cost,
the same number the summary tile already shows. Sell price is always the
current listed price, the same default `plan_transfers` itself uses when none
is given; this tool has never tracked what a player was bought for, so there
is no way to price the 50% sell-on fee exactly, and pretending otherwise would
be a false precision.

Two things `fpl.py transfers` can do that the board deliberately does not:
`value_of_acting` and `chip_values` each re-solve the whole MILP two to five
times over, to isolate one number by taking something away and comparing. One
solve is already the real cost here; a browser button that quietly triggers
several is not something to add without a much stronger reason than "the CLI
has it."

## Making the model add up

Three quantities are decided by something outside a player's own rates, and for
a long time none of them was enforced. Each was found by asking the model to
contradict itself and watching it succeed.

| Invariant | Was | Now |
| --- | --- | --- |
| A club starts **11** players | 8.25 | 11.00 |
| Players score the **fixture's** expected goals | 1.28x | 0.97x |
| A match pays a fixed pool of **bonus** | 74 pts/gw | 56 pts/gw (the rule's value) |
| League yellow cards per gameweek | 29 | 36 (the league pays ~37) |
| League points per gameweek vs what a season actually paid | **0.74** | **0.98** |

**Squads did not field eleven.** `p_start` came from last season's `starts / 38`
with nothing requiring a club to field a team. But a squad is not the set of
players who played for it last season — some retired, some left the league, some
arrived from abroad, and a promoted club's entire squad has no Premier League
record at all. Coventry, Hull and Ipswich projected **zero**. The raw rate is now
treated as evidence about a share rather than as the answer: blended with a
price-based prior by how many minutes stand behind it, then normalised per club
to one keeper and ten outfield — one bounded multiplier per club, solved
exactly, with every player capped at **0.95**. Nobody is certain to start: last
season each club's most-nailed player averaged a 0.966 start rate measured
after the fact, and the first cut of this fix pinned 26 players at 1.00 in a
league where eight managed 38/38. The cap and the single shared multiplier are
what keep the correction from promoting the players the data already knows.

Availability is applied *before* normalising, deliberately — a club whose first
choice is injured still starts eleven, so his share passes to whoever is behind
him rather than evaporating.

**Rates did not sum to team output.** Once every club fielded a full eleven, the
opposite error surfaced: squads collectively expected 37.8 goals in a gameweek
whose own lambdas said 29.7. `conserve_team_output` scales npxg, xa and bonus per
club so the shares add up to what the club actually produces.

**A prior is not free.** Bonus is a fixed pool — three points a match per side —
so a prior for it has to pay out what the rules pay. The positional prior was
the minutes-weighted rate among *established* players, which is the rate of a
regular, and it was then handed to everybody: a player with no minutes at all
came out of the shrinkage on 0.3 bonus per 90. Summed over expected minutes the
league minted **33% more bonus than exists**.

Nothing noticed, because `conserve_team_output` forced each club back to its
2.78 regardless — by scaling the whole squad uniformly, so the invented points
were taken back off whoever had really earned them. Manchester City's squad
projected 4.71 bonus a match against a target of 2.78, and closing that gap cost
Haaland 41% of his rate on top of the 15% shrinkage had already taken. He was
being charged for the assumptions made about his reserve goalkeeper.

Two changes. The bonus prior is now **solved** rather than assumed — the level
that makes the shrunk rates add up to the pool, given the evidence already in
hand, which works out about 25% below the established-player average. And where
conservation still has to move a club, it charges the correction to the rates
that are *prior rather than evidence*, as `lam ** (1 - weight)`: a player whose
number is all record is untouched, one who is all assumption absorbs it, and the
club still lands exactly on its target because the total is monotone in `lam`
and it is solved by bisection.

The effect, against how much bonus a top earner actually keeps year on year
(split-half within last season, players with 700+ minutes in both halves):

| bonus, per fixture | model before | model now | measured |
| --- | --- | --- | --- |
| top 20 earners | 0.55× | **0.63×** | 0.66× |
| next 40 | 0.61× | **0.67×** | 0.66× |

Heavy regression on bonus is *correct* — its split-half correlation is 0.379
against 0.837 for xG per 90, so it barely repeats and a top earner genuinely
keeps only about two-thirds of his rate. The bug was never that bonus regressed.
It was that a biased prior was being recovered from the wrong player.

**A threshold is not a mean.** Defensive contribution is the only step function
in the scoring rules, and shrinkage plus Poisson both flatten exactly the tail it
pays for — 36 points a gameweek against the 61 the league actually paid, measured
over 8,631 player-gameweeks in the archive. The count is now negative binomial
with a dispersion calibrated to that total.

**Three quantities were measured on one basis and applied to another.** Each is
a one-line error and each was worth real points:

- **Assists** were pinned by the league's 786-assists-per-851-goals ratio, but
  applied to a club's *open-play* xG — and a penalty carries no assist, so all
  786 came from the 91% of goals that were not penalties. Numerator and
  denominator counted different things and every assist in the model came out
  about 9% light.
- **Penalties** were credited to the designated taker in proportion to
  `p_start`, as though a man who plays 78 of 90 minutes were on the pitch for
  every penalty his side won. Scaling by minutes like every other rate takes
  about 13% off a premium taker's penalty income. The residue — penalties won
  while he is off — is now simply unattributed, which is why goals reconcile at
  0.97 rather than 0.98; the model does not know who the second taker is.
- **Yellow cards** were the one rate that skipped shrinkage entirely, so a
  player with a single booking in one 90-minute cameo projected a card every
  match. Shrunk with everything else, the league's card total moves from 29 a
  gameweek to 36, against the ~37 it actually pays.

**A season total needs a season to divide by.** The FPL API serves
*season-to-date* totals, which means last season's completed 38 matches right up
until the new season's first whistle — and three matches a fortnight later. The
model divided by a hard-coded 38 either way, which is correct for exactly one of
those. It now reads the number of finished fixtures off the calendar, per club,
and each source is weighed by the evidence behind *it*: Understat's xG keeps the
weight Understat's minutes earned, rather than being discounted because this
season is young.

This is the one correction that cannot be observed in a normal run — for eight
months of the year the constant and the calendar agree, and the day they stop
is the day the model is quietly wrong. So
[scripts/verify-season-rollover.py](scripts/verify-season-rollover.py) simulates
it: real data, scaled to six matches played, with Understat left alone. The
damage was hiding behind the per-club normalisation, which rescales every club
back to eleven starters and so makes the totals look right whatever the
denominator did. What it cannot restore is the blend — understating the raw
start share by 38/6 crushes the evidence term and lets the price-based prior
fill the gap:

| six matches in | correlation of `p_start` with… | hard-coded 38 | reading the calendar |
| --- | --- | --- | --- |
| | who actually started | 0.222 | **0.515** |
| | who is expensive | 0.587 | **0.512** |

Which is the failure worth naming: not a number that looks obviously broken, but
`p_start` quietly ceasing to describe who starts and starting to describe who
cost the most.

What did **not** change is the players you already knew about. Established
regulars sit where they did — the correction went to the previously invisible
players who will actually take those minutes:

| last season, per fixture | model | ratio |
| --- | --- | --- |
| bottom fifth (1.28) | 1.27 | 1.00 |
| middle fifth (2.51) | 2.48 | 0.99 |
| top fifth (4.17) | 3.35 | 0.80 |

Some gradient is regression to the mean and is meant to be there: last season's
leaders are at the top partly because they overperformed. But it used to read
0.77 at the top, and a good part of that was a prior three times heavier than
the evidence supports rather than anything real — see the measured figures
above. What remains is mostly bonus, which genuinely does not repeat.

Goals are now the part you can check directly: Haaland projects at **1.02×** the
non-penalty rate he actually scored at last season, so the goals component is
carrying his record through rather than damping it.

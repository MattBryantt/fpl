# FPL expected-points toolkit

Projects FPL points from bookmaker odds (The Odds API), Understat xG/xA and the
FPL API, then answers whether the expensive player is worth the extra money.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # paste your Odds API key (optional; free at the-odds-api.com)
```

Sources are cached in `.cache/` (FPL 3h, odds 6h, Understat 24h); `--refresh` bypasses it.

## Commands

```bash
python fpl.py serve        # interactive squad board
python fpl.py snapshot     # freeze the projection so the board runs offline
python fpl.py build --out dist
python fpl.py plan | transfers | horizon | rank | value | compare | upgrade
python fpl.py squad | nearmiss | fixtures | overrides | movers | blindspots
```

Common flags: `--horizon N`, `--start-gw N`, `--full`, `--csv name.csv`,
`--overrides file.csv`, `--refresh`. `plan`/`horizon` take `--half-life N`;
`transfers` takes `--squad`, `--free-transfers`, `--bank`, `--chips-used`,
`--force-chip`, `--chip-value`.

## Deploy

The board runs the projection and MILP in the browser from `snapshot.json`, so
`python fpl.py build --out dist` is all a static host needs (~4.8 MB).
`.github/workflows/deploy.yml` builds it every six hours (The Odds API free tier
allows ~240 runs a month at this rate) and publishes to Cloudflare Workers
(`wrangler.jsonc`). The build refuses to publish a snapshot with under 300 players.

A static host has no `/api/drafts`, `/api/overrides` or `/api/snapshot`; the page
hides the controls that need them. `/api/sync` is served by `src/worker.js`.
"Ask why" needs `/api/ai` and hides itself without it.

## Layout

- `fpl.py`, `fplkit/cli.py`: entry point and commands
- `fplkit/server.py`, `site.py`, `snapshot.py`: local server, static build, frozen projection
- `fplkit/model.py`, `poisson.py`, `planning.py`: the projection
- `fplkit/optimise.py`, `transfers.py`: MILP squad selection and transfer/chip planning
- `fplkit/config.py`: scoring rules and model constants (the only file to edit on a rule change; the browser reads them from the snapshot)
- `fplkit/sources/`, `matching.py`: FPL API, Understat, odds, history, fuzzy joins
- `fplkit/web/`: the board. `*-view.mjs` and `state.mjs`/`sync.mjs` are UI; `points.mjs`, `poisson.mjs`, `solver.js`, `transfers.js` are hand-ported JS twins of the Python
- `scripts/`: calibration, case generators and port checks

New `.mjs` files must be added to `ASSETS` in `server.py` and `SHELL` in `web/sw.js`
(the build checks they agree).

## Checking the JS ports agree with Python

```bash
python scripts/make-override-cases.py && node scripts/verify-js-port.mjs
python scripts/make-solver-cases.py   && node scripts/verify-solver-port.mjs
python scripts/make-lineup-cases.py   && node scripts/verify-lineup-port.mjs
python scripts/make-transfer-cases.py && node scripts/verify-transfer-port.mjs
python scripts/verify-season-rollover.py
python scripts/verify-transfer-rules.py
```

Re-run the relevant one after touching a ported file. Re-run
`calibrate-shrinkage.py` and `calibrate-start-form.py` when a season ends.

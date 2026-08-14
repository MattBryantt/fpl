"""Freeze the board into a directory of files that any static host can serve.

The board was already almost this. It runs the projection maths and the MILP in
the browser off `snapshot.json`, which is why it works on a phone with the
laptop shut -- and once that was true, the laptop stopped being a server and
became a *build step* that nobody had noticed was a build step.

This makes it one. `build()` writes every byte the board needs into one
directory, and the only thing left that needs Python is producing the snapshot,
which is a scheduled job rather than a machine that has to be awake when you
open your phone.

What it deliberately does not carry over is the write half of `server.py`:
`/api/drafts`, `/api/overrides`, `/api/snapshot`. Those were always mirrors of
state the browser already owns in `localStorage` -- the board treats
localStorage as the source of truth and pushes to disk opportunistically -- so a
site with no server behind it loses the mirror and not the data. The page checks
for the endpoints and hides the controls that need them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import requests

from . import config
from .server import ASSETS, SHIRT_SOURCE, WEB_DIR
from .sources import fpl_api

# Pages that are their own directory, so the host serves them at a clean URL
# without needing rewrite rules. `/data` has to keep working: it is linked from
# the board and it is in the service worker's shell list.
PAGES = {"index.html": "index.html", "data.html": "data/index.html"}

# Copied to the root rather than under /assets/. The worker's scope cannot rise
# above its own path, so one parked in a subdirectory could not control the page.
ROOT_FILES = ["sw.js", "manifest.webmanifest", "icon.png"]


def _shirt_codes(snapshot: dict) -> list[str]:
    """Every shirt the pitch can ask for: outfield and keeper, per club.

    Taken from the snapshot rather than from a live call, so the set of shirts
    always matches the set of clubs the board is about to draw.
    """
    codes = []
    for team in (snapshot.get("teams") or {}).values():
        code = team.get("code") if isinstance(team, dict) else None
        if code is None:
            continue
        codes += [str(code), f"{code}_1"]
    return codes


def _mirror_shirts(out: Path, codes: list[str]) -> int:
    """Copy each club's shirt in, fetching any the cache has not seen.

    Same reasoning as the server's version: the board has to work with nothing
    behind it, and a service worker can only cache what is same-origin. On a
    static host there is no request-time fallback at all, so anything missing
    here is missing forever -- but a missing shirt costs a picture and not a
    render, because the pitch falls back to a lettered club tile.
    """
    cache = config.CACHE_DIR / "shirts"
    target = out / "shirts"
    target.mkdir(parents=True, exist_ok=True)

    written = 0
    for name in codes:
        source = cache / f"{name}.png"
        if not source.exists():
            try:
                response = requests.get(SHIRT_SOURCE.format(name=name),
                                        headers=fpl_api.HEADERS, timeout=15)
                response.raise_for_status()
            except requests.RequestException:
                continue
            cache.mkdir(parents=True, exist_ok=True)
            source.write_bytes(response.content)
        shutil.copy2(source, target / f"{name}.png")
        written += 1
    return written


def _headers_file(out: Path) -> None:
    """Cloudflare Pages `_headers`, which is how a static host says "don't cache".

    Two rules, and both matter more than they look. The service worker must not
    be cached by the CDN or a shell version bump would take hours to reach a
    phone that is checking for one. And `snapshot.json` must not be cached
    either, because it is the *only* thing on the site that changes without the
    code changing -- caching it is caching last week's prices.

    Everything else is content-addressed by shell version in the worker, so the
    long max-age on assets is safe and is what makes a cold load quick.
    """
    (out / "_headers").write_text(
        "/sw.js\n"
        "  Cache-Control: no-cache\n"
        "\n"
        "/snapshot.json\n"
        "  Cache-Control: no-cache\n"
        "\n"
        "/assets/*\n"
        "  Cache-Control: public, max-age=31536000\n"
        "\n"
        "/shirts/*\n"
        "  Cache-Control: public, max-age=31536000\n", encoding="utf-8")


def _shell_version() -> str:
    """A short hash of everything the service worker caches cache-first.

    `SHELL_VERSION` used to be a hand-bumped counter in sw.js, and it drifted
    both ways: a shell change shipped with the old cache still live (2b4a7a5),
    and there is no way to tell from the diff alone whether a change *needed*
    a bump. Deriving it from the actual bytes removes the judgement call --
    the version changes exactly when, and only when, a cached file does.
    """
    digest = hashlib.sha256()
    files = [WEB_DIR / "index.html", WEB_DIR / "manifest.webmanifest", WEB_DIR / "icon.png"]
    files += [WEB_DIR / name for name in sorted(ASSETS)]
    for path in files:
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _check_shell_covers_assets() -> None:
    """The service worker must precache every asset the page can import.

    sw.js's SHELL list is hand-written and ASSETS is the server's; the comment
    in sw.js has always said the two mirror each other, and for one asset they
    quietly did not. The failure that produces is invisible in every test and on
    every online device: a file missing from SHELL is still *served*, and
    `cacheFirst` still caches it on demand, so the board works. It only breaks
    when a deploy bumps SHELL_VERSION -- `activate` deletes the old cache, the
    on-demand copy goes with it, the new shell never precaches it, and the next
    offline load fails the import and blanks the page.

    So the mirror is checked here, where a mismatch stops a deploy, rather than
    left to a comment.
    """
    text = (WEB_DIR / "sw.js").read_text(encoding="utf-8")
    block = re.search(r"const SHELL = \[(.*?)\];", text, re.S)
    if not block:
        raise RuntimeError("sw.js: could not find the SHELL list to check")
    listed = {entry.removeprefix("/assets/")
              for entry in re.findall(r'"([^"]+)"', block.group(1))
              if entry.startswith("/assets/")}
    missing = sorted(set(ASSETS) - listed)
    unknown = sorted(listed - set(ASSETS))
    if missing or unknown:
        raise RuntimeError(
            "sw.js SHELL and server.ASSETS disagree — an offline board would "
            "fail to load. "
            + (f"Missing from SHELL: {', '.join(missing)}. " if missing else "")
            + (f"In SHELL but not an asset: {', '.join(unknown)}." if unknown else ""))


def _write_service_worker(out: Path) -> str:
    """Stamp the computed shell version into sw.js on the way to `out`.

    The source file keeps a literal placeholder -- `fpl.py serve` reads it
    unstamped, which is fine, since local dev never needs the cache-busting a
    hash provides. Only the built copy, the one an installed phone actually
    runs, carries the real version.
    """
    _check_shell_covers_assets()
    text = (WEB_DIR / "sw.js").read_text(encoding="utf-8")
    version = f"fpl-shell-{_shell_version()}"
    text, count = re.subn(r'const SHELL_VERSION = "[^"]*";',
                           f'const SHELL_VERSION = "{version}";', text, count=1)
    if count != 1:
        raise RuntimeError("sw.js: could not find SHELL_VERSION to stamp")
    (out / "sw.js").write_text(text, encoding="utf-8")
    return version


def build(out_dir: Path, snapshot_path: Path) -> dict[str, int | str]:
    """Write the whole board to `out_dir`. Returns a count of what was written."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for source, destination in PAGES.items():
        target = out / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WEB_DIR / source, target)

    for name in ROOT_FILES:
        if name == "sw.js":
            continue
        shutil.copy2(WEB_DIR / name, out / name)
    shell_version = _write_service_worker(out)

    assets = out / "assets"
    for name in ASSETS:
        target = assets / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WEB_DIR / name, target)

    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    shutil.copy2(snapshot_path, out / "snapshot.json")
    shirts = _mirror_shirts(out, _shirt_codes(snapshot))

    _headers_file(out)
    return {
        "shell_version": shell_version,
        "pages": len(PAGES),
        "assets": len(ASSETS),
        "shirts": shirts,
        "players": len(snapshot.get("players", [])),
        "gameweeks": len(snapshot.get("gameweeks", [])),
    }

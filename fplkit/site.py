
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

LAST_SEASON_STEPS = (0, 25, 50, 75, 100)

PAGES = {"index.html": "index.html", "data.html": "data/index.html"}

ROOT_FILES = ["sw.js", "manifest.webmanifest", "icon.png"]


def _shirt_codes(snapshot: dict) -> list[str]:
    codes = []
    for team in (snapshot.get("teams") or {}).values():
        code = team.get("code") if isinstance(team, dict) else None
        if code is None:
            continue
        codes += [str(code), f"{code}_1"]
    return codes


def _mirror_shirts(out: Path, codes: list[str]) -> int:
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
    (out / "_headers").write_text(
        "/sw.js\n"
        "  Cache-Control: no-cache\n"
        "\n"
        "/snapshot.json\n"
        "  Cache-Control: no-cache\n"
        "\n"
        "/snapshots/*\n"
        "  Cache-Control: no-cache\n"
        "\n"
        "/assets/*\n"
        "  Cache-Control: no-cache\n"
        "\n"
        "/shirts/*\n"
        "  Cache-Control: public, max-age=31536000\n", encoding="utf-8")


def _shell_version() -> str:
    digest = hashlib.sha256()
    files = [WEB_DIR / "index.html", WEB_DIR / "manifest.webmanifest", WEB_DIR / "icon.png"]
    files += [WEB_DIR / name for name in sorted(ASSETS)]
    for path in files:
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _check_shell_covers_assets() -> None:
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
    _check_shell_covers_assets()
    text = (WEB_DIR / "sw.js").read_text(encoding="utf-8")
    version = f"fpl-shell-{_shell_version()}"
    text, count = re.subn(r'const SHELL_VERSION = "[^"]*";',
                           f'const SHELL_VERSION = "{version}";', text, count=1)
    if count != 1:
        raise RuntimeError("sw.js: could not find SHELL_VERSION to stamp")
    (out / "sw.js").write_text(text, encoding="utf-8")
    return version


def build(out_dir: Path, snapshot_path: Path,
          variants: dict[int, Path] | None = None) -> dict[str, int | str]:
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
    for pct, path in (variants or {}).items():
        target = out / "snapshots" / f"prev-{pct}.json"
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(path, target)
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

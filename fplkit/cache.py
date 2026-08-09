"""Disk cache for HTTP responses, so repeated runs do not re-hit the APIs.

The Odds API free tier is 500 requests a month, so caching is not a nicety.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from .config import CACHE_DIR


def _key_path(namespace: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return CACHE_DIR / namespace / f"{digest}.json"


def cached_json(
    namespace: str,
    key: str,
    fetch: Callable[[], Any],
    ttl_seconds: float,
    force_refresh: bool = False,
) -> Any:
    """Return `fetch()`'s result, memoised on disk under (namespace, key)."""
    path = _key_path(namespace, key)

    if not force_refresh and path.exists():
        try:
            payload = json.loads(path.read_text())
            if time.time() - payload["fetched_at"] < ttl_seconds:
                return payload["data"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass  # corrupt or partial cache entry; just refetch

    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": time.time(), "key": key, "data": data}))
    return data


def inspect(namespace: str) -> list[dict]:
    """What is currently cached for a source, and how old it is.

    Used by the provenance page so it can report the state of the actual cache
    rather than describe what it is supposed to contain.
    """
    target = CACHE_DIR / namespace
    if not target.exists():
        return []
    entries = []
    for path in sorted(target.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        fetched = payload.get("fetched_at", 0)
        entries.append({
            "key": payload.get("key", path.stem),
            "fetched_at": fetched,
            "age_seconds": max(0.0, time.time() - fetched),
            "bytes": path.stat().st_size,
        })
    return entries


def clear(namespace: str | None = None) -> int:
    """Delete cached entries; returns the number of files removed."""
    target = CACHE_DIR / namespace if namespace else CACHE_DIR
    if not target.exists():
        return 0
    removed = 0
    for path in target.rglob("*.json"):
        path.unlink()
        removed += 1
    return removed

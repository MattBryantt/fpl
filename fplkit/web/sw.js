/* Service worker: what makes the board work with the laptop off.
 *
 * Two caching strategies, because the two kinds of thing here fail differently.
 *
 *   The shell — page, scripts, the 3.3MB solver — is cache-first. It only
 *   changes when the code changes, and the code changes when SHELL_VERSION does,
 *   so serving it from cache is both correct and the difference between opening
 *   instantly and waiting on a network that may not be there.
 *
 *   The snapshot is network-first with a cache fallback. Fresh data is always
 *   preferable, but a stale projection is worth infinitely more than an error
 *   page, and "the laptop is off" is the normal case rather than the exception.
 *
 * Nothing here ever caches an /api/ response. Those are writes and syncs; a
 * replayed one would be a lie.
 */

// Stamped by `python fpl.py build` (site.py: _write_service_worker) with a
// hash of the shell files, so a built copy always carries the right version
// without anyone having to remember to bump it. This literal only shows up
// when serving straight from source (`fpl.py serve`), where a stale shell
// cache was never the failure mode a hash needed to guard against.
const SHELL_VERSION = "fpl-shell-dev";
const DATA_CACHE = "fpl-data-v1";
// Club shirts, kept apart from the shell: they are fetched as the pitch meets
// each club rather than listed up front, and they outlive a shell version --
// a code change is no reason to re-download twenty kits.
const SHIRT_CACHE = "fpl-shirts-v1";

// Everything the board needs to start with no network. Explicit, and mirrored
// by ASSETS in server.py — `site._check_shell_covers_assets` fails the build if
// the two disagree, which they silently did once: position-tags.mjs was added
// to ASSETS and imported by index.html but never listed here, so it was only
// ever cached on demand. That survived until a deploy bumped SHELL_VERSION,
// whose activate step deletes the old cache — taking the on-demand copy with
// it and leaving an offline phone unable to import it, which fails the whole
// module and shows a blank board. An asset the page imports has to be in here.
const SHELL = [
  "/",
  "/icon.png",
  "/manifest.webmanifest",
  "/assets/board.mjs",
  "/assets/chips.mjs",
  "/assets/pitch.mjs",
  "/assets/poisson.mjs",
  "/assets/points.mjs",
  "/assets/position-tags.mjs",
  "/assets/solver.js",
  "/assets/solver-worker.js",
  "/assets/transfers.js",
  "/assets/transfer-worker.js",
  "/assets/vendor/highs.js",
  "/assets/vendor/highs.wasm",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_VERSION);
    await cache.addAll(SHELL);
    // Take over straight away. The alternative is a first visit that installs a
    // worker which only starts controlling things on the *second* visit -- and
    // on a phone the second visit is often the one on the train with no signal.
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL_VERSION, DATA_CACHE, SHIRT_CACHE]);
    for (const name of await caches.keys()) {
      if (!keep.has(name)) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;   // never cache a write or a sync

  if (url.pathname === "/snapshot.json") {
    event.respondWith(networkFirst(request));
    return;
  }
  // Shirts are cache-first and cached as they are met: twenty clubs is twenty
  // images, they never change, and the pitch has to draw itself with the laptop
  // off. A club whose shirt was never fetched falls back to a lettered tile in
  // the page, so a miss here costs a picture rather than a render.
  if (url.pathname.startsWith("/shirts/")) {
    event.respondWith(cacheFirst(request, SHIRT_CACHE));
    return;
  }
  if (url.pathname === "/" || url.pathname === "/data"
      || url.pathname === "/icon.png" || url.pathname === "/manifest.webmanifest"
      || url.pathname.startsWith("/assets/")) {
    event.respondWith(cacheFirst(request));
  }
});

async function cacheFirst(request, cacheName = SHELL_VERSION) {
  const cached = await caches.match(request, { ignoreSearch: true });
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) (await caches.open(cacheName)).put(request, response.clone());
  return response;
}

async function networkFirst(request) {
  // navigator.onLine only knows whether there is an interface, so it is no use
  // as proof of connectivity — but a definite "no" is worth acting on: it skips
  // a fetch that can only fail, and the failed fetch it skips is a red line in
  // the console every time the board is opened away from the laptop.
  if (!self.navigator.onLine) {
    const cached = await caches.match(request, { ignoreSearch: true });
    if (cached) return cached;
  }
  try {
    const response = await fetch(request);
    if (response.ok) {
      (await caches.open(DATA_CACHE)).put(request, response.clone());
    }
    return response;
  } catch (_) {
    const cached = await caches.match(request, { ignoreSearch: true });
    if (cached) return cached;
    // No network and nothing cached: this is a genuinely fresh install that has
    // never reached the laptop. Say so in a shape the page can read.
    return new Response(
      JSON.stringify({ error: "no snapshot cached yet — open this once while "
                             + "the laptop is reachable" }),
      { status: 503, headers: { "Content-Type": "application/json" } });
  }
}

/* Note: the *page* writes DATA_CACHE, not this worker — see loadSnapshot() in
   index.html. A worker cannot attach the auth token, and on a first visit it is
   not yet controlling the page when the snapshot is first fetched. This worker
   only ever reads that cache, as the fallback below. */

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

// Bump whenever the shell changes. The shell is served cache-first, so an
// installed phone keeps whatever it already has until this string moves --
// which is the point on a train and a silent way to ship nothing otherwise.
const SHELL_VERSION = "fpl-shell-v5";
const DATA_CACHE = "fpl-data-v1";

// Everything the board needs to start with no network. Explicit, and mirrored
// by ASSETS in server.py — if the two disagree the install fails loudly here
// rather than the page half-working on a train.
const SHELL = [
  "/",
  "/icon.png",
  "/manifest.webmanifest",
  "/assets/board.mjs",
  "/assets/poisson.mjs",
  "/assets/points.mjs",
  "/assets/solver.js",
  "/assets/solver-worker.js",
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
    const keep = new Set([SHELL_VERSION, DATA_CACHE]);
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
  if (url.pathname === "/" || url.pathname === "/data"
      || url.pathname === "/icon.png" || url.pathname === "/manifest.webmanifest"
      || url.pathname.startsWith("/assets/")) {
    event.respondWith(cacheFirst(request));
  }
});

async function cacheFirst(request) {
  const cached = await caches.match(request, { ignoreSearch: true });
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) (await caches.open(SHELL_VERSION)).put(request, response.clone());
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

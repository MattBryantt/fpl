
const SHELL_VERSION = "fpl-shell-dev";
const DATA_CACHE = "fpl-data-v1";
const SHIRT_CACHE = "fpl-shirts-v1";

const SHELL = [
  "/",
  "/icon.png",
  "/manifest.webmanifest",
  "/assets/analysis-view.mjs",
  "/assets/board.mjs",
  "/assets/chips.mjs",
  "/assets/compare-view.mjs",
  "/assets/explain-view.mjs",
  "/assets/live.mjs",
  "/assets/pitch.mjs",
  "/assets/poisson.mjs",
  "/assets/points.mjs",
  "/assets/position-tags.mjs",
  "/assets/solver.js",
  "/assets/solver-worker.js",
  "/assets/squad-view.mjs",
  "/assets/state.mjs",
  "/assets/sync.mjs",
  "/assets/transfer-view.mjs",
  "/assets/transfers.js",
  "/assets/transfer-worker.js",
  "/assets/vendor/highs.js",
  "/assets/vendor/highs.wasm",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_VERSION);
    await cache.addAll(SHELL.map((url) => new Request(url, { cache: "reload" })));
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
  if (url.pathname.startsWith("/api/")) return;

  if (url.pathname === "/snapshot.json" || url.pathname.startsWith("/snapshots/")) {
    event.respondWith(networkFirst(request));
    return;
  }
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
    return new Response(
      JSON.stringify({ error: "no snapshot cached yet — open this once while "
                             + "the laptop is reachable" }),
      { status: 503, headers: { "Content-Type": "application/json" } });
  }
}


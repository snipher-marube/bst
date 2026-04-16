/**
 * AnalyticsMeta Service Worker
 * Caches the app shell so the dashboard loads instantly and is usable
 * offline (read-only: cached widget data is served; writes queue locally).
 */

const CACHE_VERSION = 'v1';
const SHELL_CACHE   = `analyticsmeta-shell-${CACHE_VERSION}`;
const DATA_CACHE    = `analyticsmeta-data-${CACHE_VERSION}`;

// Static assets that form the "app shell" — cache on install.
const SHELL_ASSETS = [
  '/offline/',
  'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css',
  'https://cdn.tailwindcss.com',
  'https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js',
];

// ── Install: cache the app shell ─────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => {
      // Cache what we can; non-critical failures are tolerated.
      return Promise.allSettled(
        SHELL_ASSETS.map((url) => cache.add(url).catch(() => null))
      );
    })
  );
  self.skipWaiting();
});

// ── Activate: remove stale caches ────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k.startsWith('analyticsmeta-') && k !== SHELL_CACHE && k !== DATA_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch: network-first for API + navigation; cache-first for static assets ─
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle same-origin requests and a few whitelisted CDNs.
  const isSameOrigin = url.origin === self.location.origin;
  const isCDN = url.hostname.includes('cdnjs.cloudflare.com') ||
                url.hostname.includes('fonts.googleapis.com') ||
                url.hostname.includes('fonts.gstatic.com');

  if (!isSameOrigin && !isCDN) return;   // Let the browser handle cross-origin freely.
  if (request.method !== 'GET') return;  // Never cache mutations.

  // API requests: network-first, fall back to cache (stale data).
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(request, DATA_CACHE));
    return;
  }

  // Static assets: cache-first (versioned URLs never change content).
  if (
    url.pathname.startsWith('/static/') ||
    isCDN
  ) {
    event.respondWith(cacheFirst(request, SHELL_CACHE));
    return;
  }

  // Navigation requests (HTML pages): network-first, offline fallback.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/offline/') || new Response('<h1>You are offline</h1>', { headers: { 'Content-Type': 'text/html' } }))
    );
    return;
  }
});

// ── Strategies ────────────────────────────────────────────────────────────────

async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return new Response('', { status: 503 });
  }
}

async function networkFirst(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    return cached || new Response(JSON.stringify({ error: 'offline' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

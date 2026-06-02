/*
  Service Worker — LLM Remote PWA
  Strategy: Network-first for API calls, cache-first for static assets.
  Provides offline shell so the app installs on all platforms.
*/

const CACHE_NAME = 'llm-remote-v3.6.6';

const SHELL_ASSETS = [
  '/',
  '/index.html',
  '/login.html',
  '/css/style.css',
  '/js/api.js',
  '/js/state.js',
  '/js/ui.js',
  '/js/pcm16-streamer.js',
  '/js/voice.js',
  '/js/app.js',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

// Install: pre-cache the app shell
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

// Activate: clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: network-first for API/session, cache-first for static assets
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache API or session calls — always network
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/session/')) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(JSON.stringify({ error: 'offline' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        })
      )
    );
    return;
  }

  // Static assets: network-first so updates are immediate, cache fallback for offline
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
      }
      return response;
    }).catch(() => caches.match(event.request))
  );
});

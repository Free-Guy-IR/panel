const CACHE_NAME = 'marzban-pwa-cache-v1';
const urlsToCache = [
  '/',
  '/index.html',
  '/src/index.css',
  '/src/entry.client.tsx', // Assuming this is the main entry point for the app
  '/statics/favicon/favicon.ico',
  '/statics/favicon/android-chrome-192x192.png',
  '/statics/favicon/android-chrome-512x512.png',
  '/statics/favicon/apple-touch-icon.png',
  // Add other critical assets like JS bundles, CSS files, fonts, etc.
  // You might need to inspect the network requests in your browser's developer tools
  // to get a comprehensive list of assets to cache.
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        console.log('Opened cache');
        return cache.addAll(urlsToCache);
      })
  );
});

self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request)
      .then((response) => {
        // Cache hit - return response
        if (response) {
          return response;
        }
        return fetch(event.request);
      })
  );
});

self.addEventListener('activate', (event) => {
  const cacheWhitelist = [CACHE_NAME];
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheWhitelist.indexOf(cacheName) === -1) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
});
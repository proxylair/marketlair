/*
 * firebase-messaging-sw.js
 * -------------------------
 * Firebase Cloud Messaging service worker. Must live at the SITE ROOT
 * (not under /articles/, not under templates/ once built) because a
 * service worker's scope is limited to the directory it's served from
 * and everything below it -- root is what lets it cover the whole site.
 *
 * This is the "compat" SDK loaded via importScripts because service
 * workers on a no-build static site can't easily use ES module imports
 * from a CDN. Matches the compat scripts loaded in base.html.
 *
 * Config note: a service worker runs in its own execution context, so it
 * can't read `window.firebaseConfig` off the page. Instead subscribe.js
 * registers this worker with the config passed as a URL query string
 * (?firebaseConfig=<url-encoded JSON>), and we parse it back out below.
 * This keeps the Firebase config in exactly ONE place in the source
 * (base.html) instead of duplicated across two files.
 */
importScripts("https://www.gstatic.com/firebasejs/10.13.2/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.13.2/firebase-messaging-compat.js");

var params = new URL(self.location.href).searchParams;
var configParam = params.get("firebaseConfig");

if (configParam) {
  var firebaseConfig = JSON.parse(decodeURIComponent(configParam));
  firebase.initializeApp(firebaseConfig);

  var messaging = firebase.messaging();

  // Background handler -- fires when a push arrives while no MarketLair tab
  // is focused. Foreground pushes (tab open) are handled in subscribe.js
  // instead, since this handler never fires for those.
  messaging.onBackgroundMessage(function (payload) {
    var title = (payload.notification && payload.notification.title) || "MarketLair";
    var body = (payload.notification && payload.notification.body) || "A card you follow just moved.";
    var options = {
      body: body,
      // Relative (no leading slash) so it resolves against this service
      // worker's own URL -- which, now that it's registered at the site's
      // actual root (see subscribe.js), correctly means
      // .../marketlair/icon-192.png rather than the domain root.
      icon: (payload.notification && payload.notification.icon) || "icon-192.png",
      badge: "icon-192.png",
      // send_alerts.py always sets payload.data.url to the site's real
      // full URL, so this fallback only matters for a message that
      // somehow arrives without one -- self.registration.scope (not "/")
      // so it still lands on the site's actual root on a GitHub Pages
      // project subpath instead of the domain root.
      data: { url: (payload.data && payload.data.url) || self.registration.scope }
    };
    self.registration.showNotification(title, options);
  });
}

// Clicking a notification focuses an existing MarketLair tab if one is
// open, otherwise opens the target URL in a new one.
self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var targetUrl = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (windowClients) {
      for (var i = 0; i < windowClients.length; i++) {
        var client = windowClients[i];
        if (client.url.indexOf(self.location.origin) === 0 && "focus" in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});

/*
 * ---------------------------------------------------------------------
 * Offline caching (install / activate / fetch)
 * ---------------------------------------------------------------------
 * Everything below is independent of the push-notification code above --
 * it runs regardless of whether ?firebaseConfig= was present, so a
 * visitor who never touches push notifications still gets basic offline
 * support once this worker registers (see subscribe.js's
 * ensureServiceWorkerRegistered(), called on every page load). Kept
 * deliberately simple and defensive: this SW previously broke push
 * notifications in production via a bad registration PATH (see git
 * history / subscribe.js's comments) -- nothing here should be able to
 * touch that code path, but the same lesson applies: same-origin only,
 * GET only, and every branch has an explicit fallback rather than an
 * uncaught rejection, since an uncaught fetch-handler failure surfaces to
 * the visitor as a broken page load instead of a console warning.
 */
var CACHE_VERSION = "marketlair-v1";
var OFFLINE_URL = "offline.html";
// Resolved relative to this file's own URL (self.location), which is
// already correct on a GitHub Pages project subpath -- same reasoning as
// subscribe.js using window.MARKETLAIR_ROOT instead of a leading "/".
var PRECACHE_URLS = [
  "./",
  "index.html",
  "style.css",
  "personalize.js",
  "subscribe.js",
  "engagement.js",
  "manifest.webmanifest",
  "icon-192.png",
  "favicon.ico",
  OFFLINE_URL
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(function (cache) {
      // Cache each asset independently -- cache.addAll() is all-or-nothing,
      // and one missing/renamed asset shouldn't take the whole install
      // down (that would leave push notifications working fine but offline
      // caching silently broken until the next deploy happens to fix it).
      return Promise.all(
        PRECACHE_URLS.map(function (url) {
          return cache.add(url).catch(function (err) {
            console.warn("[MarketLair SW] precache failed for", url, err);
          });
        })
      );
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names
          .filter(function (name) { return name !== CACHE_VERSION; })
          .map(function (name) { return caches.delete(name); })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

function isSameOrigin(url) {
  try {
    return new URL(url).origin === self.location.origin;
  } catch (e) {
    return false;
  }
}

function isDataRequest(url) {
  // card-index.json / snapshot-summary.json -- anything JSON gets the
  // network-first treatment below rather than stale-while-revalidate, so
  // an ONLINE visitor never sees yesterday's prices just because a cached
  // copy existed. Cache is a fallback for when the network request itself
  // fails, not a substitute for it.
  try {
    return new URL(url).pathname.slice(-5) === ".json";
  } catch (e) {
    return false;
  }
}

function putInCache(request, response) {
  // Only cache real, successful, same-origin responses -- never an
  // opaque/error/redirect response, which would poison future offline
  // loads with something unusable.
  if (!response || !response.ok) return;
  var copy = response.clone();
  caches.open(CACHE_VERSION).then(function (cache) { cache.put(request, copy); }).catch(function () {});
}

// Try the network; fall back to whatever's cached; if there's nothing
// cached either, optionally fall back to the generic offline page (used
// for navigations, not for data requests -- a failed JSON fetch should
// reject like normal so engagement.js's own .catch() handlers run,
// rather than silently resolving to an HTML page).
function networkFirst(request, useOfflinePageFallback) {
  return fetch(request)
    .then(function (response) {
      putInCache(request, response);
      return response;
    })
    .catch(function () {
      return caches.match(request).then(function (cached) {
        if (cached) return cached;
        if (useOfflinePageFallback) return caches.match(OFFLINE_URL);
        return Response.error();
      });
    });
}

function staleWhileRevalidate(request) {
  return caches.match(request).then(function (cached) {
    var network = fetch(request)
      .then(function (response) {
        putInCache(request, response);
        return response;
      })
      .catch(function () {
        return cached || Response.error();
      });
    return cached || network;
  });
}

self.addEventListener("fetch", function (event) {
  var request = event.request;

  // Only ever handle GET -- anything else (Firestore's own transport,
  // etc) passes straight through untouched.
  if (request.method !== "GET") return;

  // Cross-origin requests (Firebase SDK, Google Fonts, TCGplayer product
  // images) are left completely alone -- no caching, no interception,
  // exactly today's behavior before this feature existed.
  if (!isSameOrigin(request.url)) return;

  var acceptHeader = request.headers.get("accept") || "";
  var isNavigation = request.mode === "navigate" || acceptHeader.indexOf("text/html") !== -1;

  if (isNavigation) {
    event.respondWith(networkFirst(request, true));
    return;
  }

  if (isDataRequest(request.url)) {
    event.respondWith(networkFirst(request, false));
    return;
  }

  // Static app-shell assets (CSS/JS/icons/manifest): safe to serve from
  // cache instantly while refreshing in the background.
  event.respondWith(staleWhileRevalidate(request));
});

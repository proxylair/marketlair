/*
 * subscribe.js
 * -------------
 * Push notification opt-in/manage/opt-out for MarketLair -- "get pinged
 * when a card you follow moves." Uses Firebase Cloud Messaging
 * (client-side only, no custom backend) + Firestore to store subscriber
 * tokens. Vanilla JS, no build step, matches personalize.js in spirit.
 *
 * Requires window.firebaseConfig and window.FCM_VAPID_KEY to be set in
 * base.html (inline <script>, before this file loads) and the Firebase
 * compat SDK scripts (app, messaging, firestore) to be loaded first.
 *
 * Ownership model: there's no login on this site, so the Firestore
 * security rules (see firestore.rules) treat possession of the exact FCM
 * token as proof of ownership of that subscriber document. "Change
 * games" and "turn off alerts" are both implemented here as delete the
 * doc, then (if re-subscribing) recreate it -- never an update, because
 * the rules don't allow updates at all.
 *
 * What actually sends notifications later (e.g. "Charizard just moved
 * 12%") is a separate piece -- scripts/send_alerts.py, using the Firebase
 * Admin SDK. This file only handles the opt-in/manage/opt-out side.
 */
(function () {
  "use strict";

  var TOKEN_KEY = "ml_push_token"; // the active FCM token, or absent if not subscribed
  var GAMES_KEY = "ml_push_games"; // last-known followedGames written to Firestore (avoids a client read)
  // Must match engagement.js's WATCHLIST_KEY exactly -- that file owns the
  // "Keep an eye on this" watchlist (reads/writes it, dispatches the
  // marketlair:watchlist-changed event below), this file only reads it to
  // sync into the subscriber's Firestore doc. Not shared via a JS module
  // (no build step, no bundler) -- the constant string IS the interface
  // between the two files, same as every other localStorage key here.
  var WATCHLIST_KEY = "ml_watchlist";
  var WATCHLIST_SYNC_DEBOUNCE_MS = 1500;
  var watchlistSyncTimer = null;

  var GAMES = [
    { slug: "game-mtg", icon: "🔮", name: "Magic: The Gathering" },
    { slug: "game-pokemon", icon: "⚡", name: "Pokemon TCG" },
    { slug: "game-onepiece", icon: "🏴‍☠️", name: "One Piece Card Game" },
    { slug: "game-lorcana", icon: "✨", name: "Disney Lorcana" },
    { slug: "game-riftbound", icon: "⚔️", name: "Riftbound TCG" },
    { slug: "game-community", icon: "📦", name: "Collecting & Community" }
  ];

  function supported() {
    return (
      typeof window.firebase !== "undefined" &&
      window.firebaseConfig &&
      window.firebaseConfig.apiKey &&
      window.FCM_VAPID_KEY &&
      "serviceWorker" in navigator &&
      "Notification" in window
    );
  }

  var appCheckActivated = false;

  // App Check must be activated exactly once, right after initializeApp()
  // and before any Firestore/Messaging call -- the compat SDK attaches an
  // App Check token to every subsequent request automatically once
  // activated, but does nothing retroactively for calls already made.
  // Gated on window.APPCHECK_SITE_KEY being non-empty (see base.html) so
  // this is a true no-op -- same behavior as today -- until that key is
  // filled in with a real reCAPTCHA v3 site key and App Check enforcement
  // is turned on for this project in the Firebase console.
  function activateAppCheckIfConfigured(app) {
    if (appCheckActivated) return;
    appCheckActivated = true; // set before the call too -- never retry-loop on a bad key
    if (!window.APPCHECK_SITE_KEY) return;
    if (typeof firebase.appCheck !== "function") return; // SDK script failed to load/blocked -- degrade silently
    try {
      firebase.appCheck(app).activate(window.APPCHECK_SITE_KEY, true);
    } catch (e) {
      console.warn("[MarketLair] App Check activation failed -- continuing without it", e);
    }
  }

  function getApp() {
    var app = firebase.apps.length ? firebase.apps[0] : firebase.initializeApp(window.firebaseConfig);
    activateAppCheckIfConfigured(app);
    return app;
  }

  function lsGet(key) {
    try {
      return localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }
  function lsSet(key, val) {
    try {
      localStorage.setItem(key, val);
    } catch (e) {
      /* private browsing etc -- fail silently */
    }
  }
  function lsRemove(key) {
    try {
      localStorage.removeItem(key);
    } catch (e) {
      /* no-op */
    }
  }

  function getToken_() {
    return lsGet(TOKEN_KEY);
  }
  function getGamesList() {
    try {
      var raw = lsGet(GAMES_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function getFollowedGamesFromPersonalize() {
    // Seed the alert picker from the homepage "follow a game" selection
    // (personalize.js) the first time someone subscribes -- reasonable
    // default, edited independently afterward via Manage.
    try {
      var raw = localStorage.getItem("ml_followed_games");
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function getWatchlist() {
    try {
      var raw = localStorage.getItem(WATCHLIST_KEY);
      var list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (e) {
      return [];
    }
  }

  function showToast(title, body) {
    var toast = document.createElement("div");
    toast.className = "ml-toast";
    toast.innerHTML = "<strong>" + title + "</strong><span>" + body + "</span>";
    document.body.appendChild(toast);
    setTimeout(function () {
      toast.classList.add("ml-toast-out");
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 300);
    }, 5000);
  }

  function registerServiceWorkerWithConfig() {
    var configParam = encodeURIComponent(JSON.stringify(window.firebaseConfig));
    // Must resolve to the site's actual root (e.g.
    // proxylair.github.io/marketlair/firebase-messaging-sw.js), not the
    // domain root -- a leading "/" would 404 on a GitHub Pages project
    // subpath. window.MARKETLAIR_ROOT is the same "" / "../" prefix base.html
    // already uses for every other on-page link, so this resolves
    // correctly relative to whichever page (index vs. an article) the
    // visitor subscribed from.
    var root = window.MARKETLAIR_ROOT || "";
    return navigator.serviceWorker.register(root + "firebase-messaging-sw.js?firebaseConfig=" + configParam);
  }

  function ensureServiceWorkerRegistered() {
    // Registers the FCM service worker proactively on every page load --
    // NOT gated behind Notification.requestPermission(), unlike the
    // subscribe() flow below. Two independent reasons to do this:
    //   1. An active service worker is one of the signals browsers use to
    //      decide a site is "installable" as an app (Add to Home Screen /
    //      the install icon in the address bar). Without this, that only
    //      became true for a visitor after they'd already subscribed to
    //      alerts, which is backwards -- most people will want to install
    //      the app before ever touching push notifications.
    //   2. It means the worker is already warm if someone does click
    //      "Get price alerts" later, instead of registering for the first
    //      time in the middle of that flow.
    // Registering a service worker does NOT request notification
    // permission or send anything anywhere -- it's inert until a page
    // actually calls messaging.getToken(), which only happens from the
    // subscribe button's click handler.
    if (!("serviceWorker" in navigator) || !window.firebaseConfig || !window.firebaseConfig.apiKey) {
      return;
    }
    registerServiceWorkerWithConfig().catch(function (err) {
      console.error("[MarketLair] service worker registration failed:", err);
    });
  }

  function attachForegroundListener(app) {
    // Foreground messages -- the service worker's background handler only
    // fires when no MarketLair tab has focus. Re-attached on every page
    // load for an already-subscribed visitor, not just right after a
    // fresh subscribe, so returning visitors still get in-tab toasts.
    var messaging = firebase.messaging(app);
    messaging.onMessage(function (payload) {
      var title = (payload.notification && payload.notification.title) || "MarketLair";
      var body = (payload.notification && payload.notification.body) || "A card you follow just moved.";
      showToast(title, body);
    });
    return messaging;
  }

  function writeSubscriberDoc(db, token, games, watchedCards) {
    var doc = {
      token: token,
      followedGames: games,
      userAgent: navigator.userAgent,
      createdAt: firebase.firestore.FieldValue.serverTimestamp()
    };
    // Omit the field entirely for an empty watchlist rather than writing
    // [] -- matches firestore.rules treating watchedCards as fully
    // optional. Capped at 200 client-side too so a huge watchlist can
    // never get the whole write rejected by the rules' size check.
    if (watchedCards && watchedCards.length) {
      doc.watchedCards = watchedCards.slice(-200);
    }
    return db.collection("push_subscribers").doc(token).set(doc);
  }

  function subscribe(button) {
    button.disabled = true;
    button.textContent = "Requesting permission...";
    var app = getApp();

    Notification.requestPermission()
      .then(function (permission) {
        if (permission !== "granted") {
          render();
          return null;
        }
        return registerServiceWorkerWithConfig();
      })
      .then(function (registration) {
        if (!registration) return null;
        var messaging = attachForegroundListener(app);
        return messaging.getToken({
          vapidKey: window.FCM_VAPID_KEY,
          serviceWorkerRegistration: registration
        });
      })
      .then(function (token) {
        if (!token) return;
        var games = getFollowedGamesFromPersonalize();
        // A visitor may have already built up a watchlist before ever
        // clicking "Get price alerts" -- include it from the start rather
        // than requiring a separate trip through Manage to pick it up.
        var watchlist = getWatchlist();
        var db = firebase.firestore(app);
        return writeSubscriberDoc(db, token, games, watchlist).then(function () {
          lsSet(TOKEN_KEY, token);
          lsSet(GAMES_KEY, JSON.stringify(games));
          render();
        });
      })
      .catch(function (err) {
        console.error("[MarketLair] push subscribe failed:", err);
        button.disabled = false;
        button.textContent = "🔔 Get price alerts";
      });
  }

  function unsubscribe() {
    var token = getToken_();
    if (!token) return Promise.resolve();
    var app = getApp();
    var db = firebase.firestore(app);
    return db.collection("push_subscribers").doc(token).delete()
      .then(function () {
        // Best-effort -- also unregister the token with FCM itself so it
        // stops being a live (if unused) registration. Not fatal if this
        // fails; deleting the Firestore doc is what actually stops sends.
        try {
          return firebase.messaging(app).deleteToken();
        } catch (e) {
          return null;
        }
      })
      .catch(function (e) {
        console.error("[MarketLair] unsubscribe cleanup failed:", e);
      })
      .then(function () {
        lsRemove(TOKEN_KEY);
        lsRemove(GAMES_KEY);
        render();
      });
  }

  function saveGames(newGames) {
    var oldToken = getToken_();
    if (!oldToken) return Promise.resolve();
    var app = getApp();
    var db = firebase.firestore(app);
    // No update permission in the rules on purpose -- delete, then
    // recreate under the same token with the new game list. Carries the
    // current watchlist along too, so opening Manage and saving always
    // leaves both halves of targeting (games + watched cards) in sync.
    return db.collection("push_subscribers").doc(oldToken).delete()
      .then(function () { return writeSubscriberDoc(db, oldToken, newGames, getWatchlist()); })
      .then(function () {
        lsSet(GAMES_KEY, JSON.stringify(newGames));
        render();
      })
      .catch(function (e) {
        console.error("[MarketLair] saving alert preferences failed:", e);
      });
  }

  function syncWatchedCards(watchlist) {
    // Fires (debounced) whenever engagement.js's watchlist changes --
    // keeps a subscriber's Firestore doc current with their watchlist
    // automatically, so "alert me about cards I'm watching" doesn't
    // silently go stale until they happen to reopen Manage. A no-op if
    // they're not subscribed to push at all -- nothing to sync to.
    var token = getToken_();
    if (!token || !supported()) return;
    var app = getApp();
    var db = firebase.firestore(app);
    db.collection("push_subscribers").doc(token).delete()
      .then(function () { return writeSubscriberDoc(db, token, getGamesList(), watchlist); })
      .catch(function (e) {
        console.error("[MarketLair] syncing watchlist to alerts failed:", e);
      });
  }

  document.addEventListener("marketlair:watchlist-changed", function (evt) {
    var watchlist = (evt.detail && evt.detail.watchlist) || getWatchlist();
    if (watchlistSyncTimer) clearTimeout(watchlistSyncTimer);
    // Debounced so a quick burst of heart-clicks becomes one Firestore
    // write instead of one delete+recreate per click.
    watchlistSyncTimer = setTimeout(function () {
      syncWatchedCards(watchlist);
    }, WATCHLIST_SYNC_DEBOUNCE_MS);
  });

  function openManage() {
    var overlay = document.createElement("div");
    overlay.className = "follow-picker-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    var panel = document.createElement("div");
    panel.className = "follow-picker";

    var h3 = document.createElement("h3");
    h3.textContent = "Manage price alerts";
    panel.appendChild(h3);

    var p = document.createElement("p");
    p.textContent = "Pick which games trigger an alert. You'll get at most one digest a week, only when something notable moves.";
    panel.appendChild(p);

    var grid = document.createElement("div");
    grid.className = "picker-grid";
    var selected = getGamesList().slice();

    GAMES.forEach(function (g) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "picker-chip" + (selected.indexOf(g.slug) !== -1 ? " selected" : "");
      chip.textContent = g.icon + " " + g.name;
      chip.addEventListener("click", function () {
        var idx = selected.indexOf(g.slug);
        if (idx === -1) {
          selected.push(g.slug);
          chip.classList.add("selected");
        } else {
          selected.splice(idx, 1);
          chip.classList.remove("selected");
        }
      });
      grid.appendChild(chip);
    });
    panel.appendChild(grid);

    var actions = document.createElement("div");
    actions.className = "picker-actions";

    var save = document.createElement("button");
    save.type = "button";
    save.className = "picker-save";
    save.textContent = "Save changes";
    save.addEventListener("click", function () {
      save.disabled = true;
      save.textContent = "Saving...";
      saveGames(selected).then(function () {
        if (overlay.parentNode) document.body.removeChild(overlay);
      });
    });
    actions.appendChild(save);

    var off = document.createElement("button");
    off.type = "button";
    off.className = "picker-skip picker-danger";
    off.textContent = "Turn off price alerts";
    off.addEventListener("click", function () {
      off.disabled = true;
      off.textContent = "Turning off...";
      unsubscribe().then(function () {
        if (overlay.parentNode) document.body.removeChild(overlay);
      });
    });
    actions.appendChild(off);

    panel.appendChild(actions);
    overlay.appendChild(panel);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) document.body.removeChild(overlay);
    });
    document.body.appendChild(overlay);
  }

  function render() {
    var mount = document.getElementById("ml-notify-bar");
    if (!mount) return;
    mount.innerHTML = "";

    if (!supported()) return; // no config wired up, or unsupported browser -- render nothing

    if (getToken_()) {
      var wrap = document.createElement("span");
      wrap.className = "notify-status";

      var chip = document.createElement("span");
      chip.className = "follow-chip notify-chip";
      chip.textContent = "🔔 Alerts on";
      wrap.appendChild(chip);

      var manage = document.createElement("button");
      manage.type = "button";
      manage.className = "follow-edit notify-manage";
      manage.textContent = "Manage";
      manage.addEventListener("click", openManage);
      wrap.appendChild(manage);

      mount.appendChild(wrap);

      // Keep the foreground toast listener live for a returning
      // already-subscribed visitor, not just right after a fresh subscribe.
      attachForegroundListener(getApp());
      return;
    }

    if (Notification.permission === "denied") {
      return; // user said no at the browser level -- don't nag
    }

    var button = document.createElement("button");
    button.type = "button";
    button.className = "follow-edit follow-edit-empty notify-btn";
    button.textContent = "🔔 Get price alerts";
    button.title = "One digest a week, only when something notable moves -- not a running feed.";
    button.addEventListener("click", function () {
      subscribe(button);
    });
    mount.appendChild(button);
  }

  document.addEventListener("DOMContentLoaded", function () {
    render();
    ensureServiceWorkerRegistered();
  });
})();

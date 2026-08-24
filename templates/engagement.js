/*
 * engagement.js
 * -------------
 * Lightweight "quality of life" layer on top of the static site -- no
 * backend, no accounts, nothing but localStorage plus two small JSON files
 * build_site.py already writes at the site root (card-index.json and
 * snapshot-summary.json). Everything here degrades to a silent no-op if
 * localStorage or fetch is unavailable -- none of this is load-bearing for
 * the rest of the site.
 *
 * Five independent features:
 *   1. "Since Your Last Visit" banner        (every page, via #ml-visit-banner)
 *   2. Watchlist heart buttons                (homepage mover/quick-hit cards)
 *   3. "Cards You're Watching" strip           (homepage only, #watchlist-section)
 *   4. Shareable card images                  (homepage mover/quick-hit/watchlist cards)
 *   5. "Check Your List" paste-in checker      (homepage only, #list-checker-section)
 *
 * Root-relative fetches use window.MARKETLAIR_ROOT (set in base.html) --
 * NOT a leading "/" -- because this site is served from a GitHub Pages
 * project subpath (proxylair.github.io/marketlair/), not the domain root.
 * A hardcoded "/card-index.json" would 404 there. (This exact class of bug
 * bit the push-notification service worker once already -- see subscribe.js.)
 */
(function () {
  "use strict";

  var ROOT = window.MARKETLAIR_ROOT || "";
  var LAST_VISIT_KEY = "ml_last_visit";
  var WATCHLIST_KEY = "ml_watchlist";

  // ---------- tiny localStorage helpers (all fail silently) ----------

  function readWatchlist() {
    try {
      var raw = localStorage.getItem(WATCHLIST_KEY);
      var list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (e) {
      return [];
    }
  }

  function writeWatchlist(list) {
    try {
      localStorage.setItem(WATCHLIST_KEY, JSON.stringify(list));
    } catch (e) {
      /* private browsing / storage disabled -- feature just won't persist */
    }
  }

  function isWatched(key) {
    return readWatchlist().indexOf(key) !== -1;
  }

  // Returns true if the card is now watched (false if it was just removed).
  function toggleWatch(key) {
    var list = readWatchlist();
    var idx = list.indexOf(key);
    if (idx === -1) {
      list.push(key);
    } else {
      list.splice(idx, 1);
    }
    writeWatchlist(list);
    // subscribe.js listens for this to keep a subscribed visitor's
    // Firestore watchedCards in sync with their watchlist automatically
    // (debounced there) -- if push isn't wired up or nobody's listening,
    // this is just an inert DOM event, no error either way.
    try {
      document.dispatchEvent(new CustomEvent("marketlair:watchlist-changed", {
        detail: { watchlist: list.slice() }
      }));
    } catch (e) {
      /* CustomEvent unsupported in some ancient browser -- non-fatal */
    }
    return idx === -1;
  }

  function fetchJson(path) {
    return fetch(ROOT + path, { cache: "no-store" }).then(function (res) {
      if (!res.ok) throw new Error("fetch failed: " + path);
      return res.json();
    });
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---------- 1. Since Your Last Visit ----------

  function initVisitBanner() {
    var container = document.getElementById("ml-visit-banner");
    if (!container) return;

    var lastVisit;
    try {
      lastVisit = localStorage.getItem(LAST_VISIT_KEY);
    } catch (e) {
      return; // no localStorage -- nothing to compare against, skip entirely
    }

    fetchJson("snapshot-summary.json").then(function (summary) {
      var today = summary.last_updated;
      if (today && lastVisit && lastVisit !== today && summary.mover_count > 0) {
        var topBits = (summary.top_movers || []).slice(0, 3).map(function (t) {
          var pct = typeof t.pct === "number" ? t.pct : 0;
          return escapeHtml(t.name) + " (" + (pct > 0 ? "+" : "") + pct.toFixed(0) + "%)";
        }).join(", ");
        var plural = summary.mover_count === 1 ? "" : "s";
        container.innerHTML =
          '<div class="visit-banner">' +
          "<span>👋 Welcome back — since your last visit on " + escapeHtml(lastVisit) + ", " +
          summary.mover_count + " card" + plural + " moved" + (topBits ? ": " + topBits : "") + ".</span>" +
          '<a href="' + ROOT + 'index.html#movers">See what moved</a>' +
          '<button type="button" class="visit-banner-close" aria-label="Dismiss">×</button>' +
          "</div>";
        var closeBtn = container.querySelector(".visit-banner-close");
        if (closeBtn) {
          closeBtn.addEventListener("click", function () {
            container.innerHTML = "";
          });
        }
      }
      try {
        localStorage.setItem(LAST_VISIT_KEY, today || lastVisit || "");
      } catch (e) { /* ignore */ }
    }).catch(function () {
      /* summary.json missing/unreachable -- nice-to-have, fail quiet */
    });
  }

  // ---------- 2. Watchlist heart buttons ----------

  function paintButton(btn, watching) {
    btn.setAttribute("aria-pressed", watching ? "true" : "false");
    btn.classList.toggle("watching", watching);
    var icon = btn.querySelector(".watch-icon");
    if (icon) icon.textContent = watching ? "♥" : "♡";
    var name = btn.getAttribute("data-card-name") || "this card";
    btn.setAttribute(
      "aria-label",
      watching ? "Remove " + name + " from your watchlist" : "Add " + name + " to your watchlist"
    );
  }

  function wireWatchButton(btn) {
    var key = btn.getAttribute("data-card-key");
    if (!key || btn.__cpWired) return;
    btn.__cpWired = true;
    paintButton(btn, isWatched(key));
    btn.addEventListener("click", function (evt) {
      evt.preventDefault();
      evt.stopPropagation();
      var nowWatching = toggleWatch(key);
      // Sync every button for this card on the page (a mover-grid card and
      // its quick-hit twin can both reference the same key).
      document.querySelectorAll('.watch-btn[data-card-key="' + key + '"]').forEach(function (b) {
        paintButton(b, nowWatching);
      });
      refreshWatchlistSection();
    });
  }

  function initWatchButtons() {
    document.querySelectorAll(".watch-btn[data-card-key]").forEach(wireWatchButton);
  }

  // ---------- 3. "Cards You're Watching" strip ----------

  function renderWatchCard(key, card) {
    var hasPct = typeof card.pct === "number";
    var direction = hasPct ? (card.pct >= 0 ? "up" : "down") : "";
    var arrow = direction === "up" ? "▲" : direction === "down" ? "▼" : "";
    var pctHtml = hasPct
      ? '<span class="quick-hit-pct ' + direction + '">' + arrow + " " +
        (card.pct > 0 ? "+" : "") + card.pct.toFixed(0) + "%</span>"
      : "";
    var img = card.image
      ? '<img src="' + escapeHtml(card.image) + '" alt="' + escapeHtml(card.name) + '" loading="lazy">'
      : '<div class="quick-hit-noimg">🃏</div>';
    var linkOpen = card.url
      ? '<a href="' + escapeHtml(card.url) + '" target="_blank" rel="noopener nofollow">'
      : "<div>";
    var linkClose = card.url ? "</a>" : "</div>";
    var priceText = typeof card.price === "number" ? "$" + card.price.toFixed(2) : "--";
    var priceAttr = typeof card.price === "number" ? card.price.toFixed(2) : "";
    return (
      '<div class="quick-hit" data-card-key="' + escapeHtml(key) + '">' +
      '<div class="card-actions">' +
      '<button type="button" class="watch-btn watching" data-card-key="' + escapeHtml(key) +
      '" data-card-name="' + escapeHtml(card.name) + '" aria-pressed="true" ' +
      'aria-label="Remove ' + escapeHtml(card.name) + ' from your watchlist">' +
      '<span class="watch-icon">♥</span></button>' +
      '<button type="button" class="share-btn" data-card-name="' + escapeHtml(card.name) +
      '" data-set="' + escapeHtml(card.set || "") + '" data-game-label="' + escapeHtml(card.game_label || "") +
      '" data-new-price="' + priceAttr + '" aria-label="Share ' + escapeHtml(card.name) + ' as an image">' +
      '<span class="share-icon">↗</span></button>' +
      "</div>" +
      linkOpen + img +
      '<div class="quick-hit-body">' + pctHtml +
      "<strong>" + escapeHtml(card.name) + "</strong>" +
      '<span class="quick-hit-price">' + priceText + "</span>" +
      "</div>" + linkClose +
      "</div>"
    );
  }

  function refreshWatchlistSection() {
    var section = document.getElementById("watchlist-section");
    var strip = document.getElementById("watchlist-strip");
    if (!section || !strip) return; // not the homepage -- nothing to do

    var keys = readWatchlist();
    if (!keys.length) {
      section.hidden = true;
      strip.innerHTML = "";
      return;
    }

    fetchJson("card-index.json").then(function (index) {
      var html = keys
        .map(function (key) {
          var card = index[key];
          return card ? renderWatchCard(key, card) : "";
        })
        .join("");
      if (!html) {
        section.hidden = true;
        strip.innerHTML = "";
        return;
      }
      strip.innerHTML = html;
      section.hidden = false;
      strip.querySelectorAll(".watch-btn[data-card-key]").forEach(wireWatchButton);
      strip.querySelectorAll(".share-btn").forEach(wireShareButton);
    }).catch(function () {
      // card-index.json missing/unreachable -- hide rather than show a
      // broken/empty section.
      section.hidden = true;
    });
  }

  // ---------- 4. Shareable card images ----------
  //
  // Renders a branded PNG entirely client-side onto a <canvas> from the
  // share button's own data-* attributes (see render_share_button in
  // build_site.py) -- no server, no external image dependency. Deliberately
  // does NOT draw the TCGplayer product photo: that's a cross-origin image,
  // and drawing a cross-origin image onto a canvas "taints" it, which makes
  // canvas.toBlob()/toDataURL() throw a SecurityError instead of producing
  // a file. Our own same-origin icon-192.png is safe to draw.

  var SHARE_WIDTH = 1200;
  var SHARE_HEIGHT = 630;
  var SHARE_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
  var GAME_SHARE_COLORS = {
    "Magic: The Gathering": "#eb6834",
    "Pokemon TCG": "#d99000",
    "One Piece Card Game": "#1baf7a",
    "Disney Lorcana": "#2a78d6",
    "Riftbound TCG": "#d4548a",
    "Collecting & Community": "#158215"
  };

  function loadImage(src) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () { resolve(img); };
      img.onerror = function () { reject(new Error("image failed to load: " + src)); };
      img.src = src;
    });
  }

  function wrapCanvasText(ctx, text, maxWidth) {
    var words = String(text || "").split(" ");
    var lines = [];
    var current = "";
    words.forEach(function (w) {
      var test = current ? current + " " + w : w;
      if (current && ctx.measureText(test).width > maxWidth) {
        lines.push(current);
        current = w;
      } else {
        current = test;
      }
    });
    if (current) lines.push(current);
    return lines;
  }

  function roundRectPath(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // data: { name, set, gameLabel, newPrice, oldPrice?, pct? }
  // oldPrice/pct are omitted for a plain watchlist share (no move to show).
  function buildShareCanvas(data) {
    return loadImage(ROOT + "icon-192.png").catch(function () {
      return null; // logo is decorative -- still produce an image without it
    }).then(function (logo) {
      var canvas = document.createElement("canvas");
      canvas.width = SHARE_WIDTH;
      canvas.height = SHARE_HEIGHT;
      var ctx = canvas.getContext("2d");

      var grad = ctx.createLinearGradient(0, 0, SHARE_WIDTH, SHARE_HEIGHT);
      grad.addColorStop(0, "#2a78d6");
      grad.addColorStop(0.45, "#4a3aa7");
      grad.addColorStop(1, "#eb6834");
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, SHARE_WIDTH, SHARE_HEIGHT);

      // Logo + wordmark.
      ctx.textBaseline = "middle";
      if (logo) ctx.drawImage(logo, 60, 56, 64, 64);
      ctx.fillStyle = "#ffffff";
      ctx.font = "700 34px " + SHARE_FONT;
      ctx.fillText("MarketLair", logo ? 138 : 60, 88);

      // Game tag chip.
      var chipText = data.gameLabel || "";
      if (chipText) {
        ctx.font = "700 24px " + SHARE_FONT;
        var chipPad = 20;
        var chipWidth = ctx.measureText(chipText).width + chipPad * 2;
        var chipY = 168;
        ctx.fillStyle = "rgba(0, 0, 0, 0.24)";
        roundRectPath(ctx, 60, chipY, chipWidth, 46, 23);
        ctx.fill();
        ctx.fillStyle = "#ffffff";
        ctx.fillText(chipText, 60 + chipPad, chipY + 23);
      }

      // Card name, large, wrapped to at most 2 lines. Everything below this
      // point is laid out relative to how many lines the name actually
      // took (a 1-line name and a 2-line name need different amounts of
      // room before the price block) rather than fixed y-coordinates --
      // a fixed price-line y left the set name and price crowded together
      // whenever a long card name wrapped to 2 lines.
      ctx.textBaseline = "alphabetic";
      ctx.font = "800 56px " + SHARE_FONT;
      ctx.fillStyle = "#ffffff";
      var lines = wrapCanvasText(ctx, data.name || "", SHARE_WIDTH - 120).slice(0, 2);
      var nameY = 280;
      lines.forEach(function (line, i) { ctx.fillText(line, 60, nameY + i * 64); });
      var cursorY = nameY + lines.length * 64;

      if (data.set) {
        cursorY += 40;
        ctx.font = "500 26px " + SHARE_FONT;
        ctx.fillStyle = "rgba(255, 255, 255, 0.82)";
        ctx.fillText(data.set, 60, cursorY);
        cursorY += 16;
      }

      // Price line. Clamped so a worst-case 2-line name + long set name
      // still leaves the footer clear rather than running into it -- the
      // pct line sits 62px below priceY, so this clamp has to leave room
      // for THAT too, not just the price line itself.
      var hasMove = typeof data.pct === "number" && typeof data.oldPrice === "number";
      var priceY = Math.min(cursorY + 56, 480);
      ctx.font = "800 46px " + SHARE_FONT;
      ctx.fillStyle = "#ffffff";
      if (hasMove) {
        ctx.fillText("$" + data.oldPrice.toFixed(2) + " → $" + data.newPrice.toFixed(2), 60, priceY);
        var up = data.pct >= 0;
        ctx.fillStyle = up ? "#8CF28C" : "#FFA3A3";
        ctx.fillText(
          (up ? "▲ +" : "▼ ") + data.pct.toFixed(1) + "%",
          60,
          priceY + 62
        );
      } else if (typeof data.newPrice === "number") {
        ctx.fillText("$" + data.newPrice.toFixed(2), 60, priceY);
        ctx.font = "600 26px " + SHARE_FONT;
        ctx.fillStyle = "rgba(255, 255, 255, 0.85)";
        ctx.fillText("On my MarketLair watchlist", 60, priceY + 42);
      }

      // Footer.
      ctx.font = "600 24px " + SHARE_FONT;
      ctx.fillStyle = "rgba(255, 255, 255, 0.75)";
      ctx.fillText("proxylair.github.io/marketlair", 60, SHARE_HEIGHT - 48);

      return canvas;
    });
  }

  function slugifyForFilename(s) {
    var out = String(s || "card").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    return (out || "card").slice(0, 60);
  }

  function downloadBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  function shareText(data) {
    if (typeof data.pct === "number") {
      return data.name + " just moved " + (data.pct > 0 ? "+" : "") + data.pct.toFixed(1) + "% on MarketLair.";
    }
    return data.name + " -- tracked on MarketLair.";
  }

  function shareCard(data, btn) {
    var original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "<span class=\"share-icon\">…</span>";

    buildShareCanvas(data).then(function (canvas) {
      return new Promise(function (resolve, reject) {
        canvas.toBlob(function (blob) {
          if (blob) resolve(blob); else reject(new Error("canvas.toBlob returned null"));
        }, "image/png");
      });
    }).then(function (blob) {
      var filename = "marketlair-" + slugifyForFilename(data.name) + ".png";
      var file = typeof File !== "undefined" ? new File([blob], filename, { type: "image/png" }) : null;

      if (navigator.share && navigator.canShare && file && navigator.canShare({ files: [file] })) {
        return navigator.share({ files: [file], title: "MarketLair: " + data.name, text: shareText(data) })
          .catch(function (err) {
            // AbortError = the visitor closed the native share sheet -- not a failure.
            if (!err || err.name !== "AbortError") downloadBlob(blob, filename);
          });
      }
      downloadBlob(blob, filename);
    }).catch(function (err) {
      console.error("[MarketLair] share image generation failed:", err);
    }).then(function () {
      btn.disabled = false;
      btn.innerHTML = original;
    });
  }

  function wireShareButton(btn) {
    if (btn.__cpWired) return;
    btn.__cpWired = true;
    btn.addEventListener("click", function (evt) {
      evt.preventDefault();
      evt.stopPropagation();
      var oldPriceAttr = btn.getAttribute("data-old-price");
      var pctAttr = btn.getAttribute("data-pct");
      var data = {
        name: btn.getAttribute("data-card-name") || "",
        set: btn.getAttribute("data-set") || "",
        gameLabel: btn.getAttribute("data-game-label") || "",
        newPrice: parseFloat(btn.getAttribute("data-new-price")),
        oldPrice: oldPriceAttr ? parseFloat(oldPriceAttr) : undefined,
        pct: pctAttr ? parseFloat(pctAttr) : undefined
      };
      shareCard(data, btn);
    });
  }

  function initShareButtons() {
    document.querySelectorAll(".share-btn").forEach(wireShareButton);
  }

  // ---------- 5. Check Your List (paste-in bulk price checker) ----------
  //
  // Entirely client-side: card-index.json is fetched once (lazily, only
  // when the button is first used -- most visitors will never touch this
  // feature, so there's no reason to pay for the fetch on every page
  // load) and matched against pasted lines by normalized card NAME. Set
  // isn't parsed out of a typical TCGplayer mass-entry paste, so a single
  // pasted name can resolve to several printings -- all of them are shown
  // rather than guessing which one the visitor meant.

  var MAX_LIST_LINES = 250;
  var nameIndexCache = null;
  var GAME_META_JS = {
    "Magic: The Gathering": { slug: "game-mtg", icon: "🔮" },
    "Pokemon TCG": { slug: "game-pokemon", icon: "⚡" },
    "One Piece Card Game": { slug: "game-onepiece", icon: "🏴‍☠️" },
    "Disney Lorcana": { slug: "game-lorcana", icon: "✨" },
    "Riftbound TCG": { slug: "game-riftbound", icon: "⚔️" },
    "Collecting & Community": { slug: "game-community", icon: "📦" }
  };

  function slugifyName(s) {
    return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }

  function withKey(card, key) {
    var out = { key: key };
    for (var k in card) {
      if (Object.prototype.hasOwnProperty.call(card, k)) out[k] = card[k];
    }
    return out;
  }

  function buildNameIndex(cardIndex) {
    var idx = {};
    Object.keys(cardIndex).forEach(function (key) {
      var card = cardIndex[key];
      var norm = slugifyName(card.name);
      if (!norm) return;
      if (!idx[norm]) idx[norm] = [];
      idx[norm].push(withKey(card, key));
    });
    return idx;
  }

  function parseListLine(line) {
    var trimmed = line.trim();
    if (!trimmed || trimmed.charAt(0) === "#") return null;
    // Strip a leading quantity: "4 Card", "4x Card", "x4 Card". TCGplayer's
    // own mass-entry format is "<qty> <name>", so this covers the common case.
    var m = trimmed.match(/^(?:x\s*)?(\d+)\s*x?\s+(.+)$/i);
    return (m ? m[2] : trimmed).trim();
  }

  function findMatches(nameIndex, rawName) {
    var norm = slugifyName(rawName);
    if (!norm) return [];
    if (nameIndex[norm]) return nameIndex[norm];
    // Forgiving fallback for a near-miss (e.g. a missing parenthetical
    // suffix) -- only used when there's no exact name match, and capped so
    // one vague line can't flood the results.
    var out = [];
    var keys = Object.keys(nameIndex);
    for (var i = 0; i < keys.length && out.length < 6; i++) {
      if (keys[i].indexOf(norm) === 0 || norm.indexOf(keys[i]) === 0) {
        out = out.concat(nameIndex[keys[i]]);
      }
    }
    return out.slice(0, 6);
  }

  function renderMatchChip(card) {
    var meta = GAME_META_JS[card.game_label] || { slug: "game-default", icon: "🃏" };
    var priceText = typeof card.price === "number" ? "$" + card.price.toFixed(2) : "--";
    var momentumHtml = card.momentum
      ? '<span class="momentum-chip" title="' + escapeHtml(card.momentum.detail) + '">' + card.momentum.emoji + "</span>"
      : "";
    var inner =
      '<span class="tag ' + meta.slug + '">' + meta.icon + " " + escapeHtml(card.game_label) + "</span>" +
      '<span class="set-name">' + escapeHtml(card.set || "") + "</span>" +
      '<span class="price">' + priceText + "</span>" + momentumHtml;
    return card.url
      ? '<a class="list-check-chip" href="' + escapeHtml(card.url) + '" target="_blank" rel="noopener nofollow">' + inner + "</a>"
      : '<span class="list-check-chip">' + inner + "</span>";
  }

  function renderListCheckRow(rawLine, matches) {
    var body = matches.length
      ? '<div class="list-check-matches">' + matches.map(renderMatchChip).join("") + "</div>"
      : '<span class="list-check-none">Not currently tracked</span>';
    return (
      '<div class="list-check-row">' +
      '<div class="list-check-query">' + escapeHtml(rawLine) + "</div>" +
      body +
      "</div>"
    );
  }

  function runListChecker() {
    var input = document.getElementById("list-checker-input");
    var status = document.getElementById("list-checker-status");
    var results = document.getElementById("list-checker-results");
    var button = document.getElementById("list-checker-run");
    if (!input || !results) return;

    var lines = input.value.split("\n").map(parseListLine).filter(Boolean).slice(0, MAX_LIST_LINES);
    if (!lines.length) {
      if (status) status.textContent = "Paste at least one card name first.";
      return;
    }

    button.disabled = true;
    if (status) status.textContent = "Checking " + lines.length + " card" + (lines.length === 1 ? "" : "s") + "…";
    results.innerHTML = "";

    var indexPromise = nameIndexCache
      ? Promise.resolve(nameIndexCache)
      : fetchJson("card-index.json").then(function (cardIndex) {
          nameIndexCache = buildNameIndex(cardIndex);
          return nameIndexCache;
        });

    indexPromise.then(function (nameIndex) {
      var foundCount = 0;
      var html = lines.map(function (line) {
        var matches = findMatches(nameIndex, line);
        if (matches.length) foundCount++;
        return renderListCheckRow(line, matches);
      }).join("");
      results.innerHTML = html;
      if (status) {
        status.textContent = "Found " + foundCount + " of " + lines.length + " card" +
          (lines.length === 1 ? "" : "s") + " in what we're currently tracking.";
      }
    }).catch(function () {
      if (status) status.textContent = "Couldn't load card data -- try again in a moment.";
    }).then(function () {
      button.disabled = false;
    });
  }

  function initListChecker() {
    var button = document.getElementById("list-checker-run");
    if (!button || button.__cpWired) return;
    button.__cpWired = true;
    button.addEventListener("click", runListChecker);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initVisitBanner();
    initWatchButtons();
    initShareButtons();
    initListChecker();
    refreshWatchlistSection();
  });
})();

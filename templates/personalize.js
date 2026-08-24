/*
 * personalize.js
 * ---------------
 * Client-side only "follow a game" personalization for MarketLair -- no
 * backend, no account, just localStorage. Reorders the movers/quick-hits/
 * article grids so a visitor's followed games surface first, and renders a
 * small persistent bar + a one-time picker prompt.
 *
 * Deliberately vanilla JS, no build step, no framework -- matches the rest
 * of this site.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "ml_followed_games";

  var GAMES = [
    { slug: "game-mtg", icon: "🔮", name: "Magic: The Gathering" },
    { slug: "game-pokemon", icon: "⚡", name: "Pokemon TCG" },
    { slug: "game-onepiece", icon: "🏴‍☠️", name: "One Piece Card Game" },
    { slug: "game-lorcana", icon: "✨", name: "Disney Lorcana" },
    { slug: "game-riftbound", icon: "⚔️", name: "Riftbound TCG" },
    { slug: "game-community", icon: "📦", name: "Collecting & Community" }
  ];

  function getFollowed() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null; // null = "never chosen" vs [] = "chose nothing"
    } catch (e) {
      return null;
    }
  }

  function setFollowed(list) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
    } catch (e) {
      /* localStorage unavailable (private browsing etc) -- fail silently, site still works */
    }
  }

  function gameBySlug(slug) {
    for (var i = 0; i < GAMES.length; i++) {
      if (GAMES[i].slug === slug) return GAMES[i];
    }
    return null;
  }

  function reorder(selector, followed) {
    if (!followed || !followed.length) return;
    var grids = document.querySelectorAll(selector);
    grids.forEach(function (grid) {
      var items = Array.prototype.slice.call(grid.children);
      var followedItems = [];
      var restItems = [];
      items.forEach(function (el) {
        var isFollowed = followed.some(function (slug) {
          return el.classList.contains(slug);
        });
        (isFollowed ? followedItems : restItems).push(el);
      });
      followedItems.concat(restItems).forEach(function (el) {
        grid.appendChild(el);
      });
    });
  }

  function applyPersonalization(followed) {
    reorder(".article-grid", followed);
    reorder(".mover-grid", followed);
    reorder(".quick-hits-strip", followed);
  }

  function renderBar(followed) {
    var mount = document.getElementById("ml-follow-bar");
    if (!mount) return;
    mount.innerHTML = "";

    if (!followed || !followed.length) {
      var prompt = document.createElement("button");
      prompt.type = "button";
      prompt.className = "follow-edit follow-edit-empty";
      prompt.textContent = "⭐ Follow your games for a personalized feed";
      prompt.addEventListener("click", function () {
        openPicker(followed || []);
      });
      mount.appendChild(prompt);
      return;
    }

    var bar = document.createElement("div");
    bar.className = "follow-bar";

    var label = document.createElement("span");
    label.className = "follow-label";
    label.textContent = "Following:";
    bar.appendChild(label);

    followed.forEach(function (slug) {
      var g = gameBySlug(slug);
      if (!g) return;
      var chip = document.createElement("span");
      chip.className = "follow-chip";
      chip.textContent = g.icon + " " + g.name;
      bar.appendChild(chip);
    });

    var edit = document.createElement("button");
    edit.type = "button";
    edit.className = "follow-edit";
    edit.textContent = "Edit";
    edit.addEventListener("click", function () {
      openPicker(followed);
    });
    bar.appendChild(edit);

    mount.appendChild(bar);
  }

  function openPicker(existing) {
    var overlay = document.createElement("div");
    overlay.className = "follow-picker-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    var panel = document.createElement("div");
    panel.className = "follow-picker";

    var h3 = document.createElement("h3");
    h3.textContent = "Which games do you follow?";
    panel.appendChild(h3);

    var p = document.createElement("p");
    p.textContent = "We'll bring those movers and articles to the top of your feed. Nothing gets hidden -- just reordered.";
    panel.appendChild(p);

    var grid = document.createElement("div");
    grid.className = "picker-grid";

    var selected = existing.slice();

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
    save.textContent = "Save my feed";
    save.addEventListener("click", function () {
      setFollowed(selected);
      document.body.removeChild(overlay);
      renderBar(selected);
      applyPersonalization(selected);
    });
    actions.appendChild(save);

    var skip = document.createElement("button");
    skip.type = "button";
    skip.className = "picker-skip";
    skip.textContent = "Skip, show me everything";
    skip.addEventListener("click", function () {
      setFollowed([]);
      document.body.removeChild(overlay);
      renderBar([]);
    });
    actions.appendChild(skip);

    panel.appendChild(actions);
    overlay.appendChild(panel);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) document.body.removeChild(overlay);
    });
    document.body.appendChild(overlay);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var followed = getFollowed(); // null on first-ever visit
    renderBar(followed);
    if (followed && followed.length) applyPersonalization(followed);
  });
})();

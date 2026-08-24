#!/usr/bin/env python3
"""
build_site.py
--------------
Tiny static site generator. Reads Markdown articles (with a small YAML-ish
front-matter block) from content/articles/, renders them through
templates/base.html, and writes finished HTML + an index + sitemap.xml
into site/.

No framework, no build step beyond "run this script" -- deploy the site/
folder as-is to GitHub Pages, Cloudflare Pages, or Netlify (all free).

Usage: python3 scripts/build_site.py
"""
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import markdown as md
from jinja2 import Template

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content" / "articles"
TEMPLATE_PATH = ROOT / "templates" / "base.html"
SNAP_DIR = ROOT / "data" / "snapshots"
SITE_DIR = ROOT / "docs"  # GitHub Pages can serve straight from a /docs folder, no extra config
SITE_URL = "https://proxylair.github.io/marketlair"  # update if/when a custom domain is bought

# Cards below this price move around on pennies alone -- a $0.04 -> $0.08
# card is "+100%" but meaningless. Keep the homepage movers list honest.
MOVERS_MIN_PRICE = 1.00
MOVERS_MIN_PCT = 8.0
MOVERS_LIMIT = 3
QUICK_HITS_LIMIT = 12
# A move bigger than this is almost never a real market event -- it's
# nearly always a data/mapping error (wrong product matched between
# snapshots, a listing glitch on TCGplayer's end, etc). Filter it out of
# "movers" entirely rather than publishing "CARD CRASHES 90%" and having
# to walk it back next week.
MOVERS_MAX_PCT = 500.0
# If the latest snapshot has fewer than this fraction of the cards the
# prior one had, treat the whole pull as suspect (partial/broken fetch)
# rather than a real, sitewide price event.
MIN_SNAPSHOT_COVERAGE_RATIO = 0.5

# Per-game color-coding + a little emoji personality for tags/cards.
GAME_META = {
    "Magic: The Gathering": {"slug": "game-mtg", "icon": "\U0001F52E"},
    "Pokemon TCG": {"slug": "game-pokemon", "icon": "⚡"},
    "One Piece Card Game": {"slug": "game-onepiece", "icon": "\U0001F3F4‍☠️"},
    "Disney Lorcana": {"slug": "game-lorcana", "icon": "✨"},
    "Riftbound TCG": {"slug": "game-riftbound", "icon": "⚔️"},
    "Collecting & Community": {"slug": "game-community", "icon": "\U0001F4E6"},
}


def game_slug(game: str) -> str:
    return GAME_META.get(game, {}).get("slug", "game-default")


def game_icon(game: str) -> str:
    return GAME_META.get(game, {}).get("icon", "\U0001F0CF")


def card_key(name: str, set_name: str) -> str:
    """Stable, URL/localStorage-safe identity for a card across snapshots.
    tcgcsv product IDs aren't in our snapshot schema, so (name, set) is the
    join key everywhere -- this just slugifies that pair once, consistently,
    for use as a data-attribute and as the key in card-index.json."""
    raw = f"{name}|{set_name}".lower()
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:120]


# ---------- Momentum engine ----------
# Uses every available snapshot (not just the latest two) to say more than
# "this card moved X% this week" -- e.g. "third straight week up" or "this
# is a sudden move after being flat for weeks." Needs real history to say
# anything meaningful, so most labels simply won't appear until there are
# a handful of weekly snapshots on file -- that's intentional (see
# MIN_HISTORY_FOR_LABEL below) rather than guessing from thin data.
MIN_HISTORY_FOR_STREAK = 2   # need 2 week-over-week changes (3 snapshots) to call anything a streak
MIN_HISTORY_FOR_LABEL = 3    # need 3 changes (4 snapshots) for the richer labels below
BREAKOUT_MULTIPLE = 3.0      # latest move must be >= 3x the prior average to call it a "breakout"
VOLATILE_MIN_SIGN_CHANGES = 2


def _pct_series(prices):
    """prices: list of (snapshot_date, price) oldest->newest, one card.
    Returns week-over-week % changes, oldest->newest, skipping any gap
    where the card wasn't priced in a given snapshot."""
    series = []
    prev = None
    for _date, price in prices:
        if price is not None and prev is not None and prev > 0:
            series.append((price - prev) / prev * 100)
        prev = price if price is not None else prev
    return series


def _streak_len(pct_series):
    """How many trailing entries share the same sign as the most recent one."""
    if not pct_series:
        return 0
    sign = pct_series[-1] > 0
    n = 0
    for pct in reversed(pct_series):
        if (pct > 0) != sign:
            break
        n += 1
    return n


def classify_momentum(pct_series):
    """pct_series: week-over-week % changes for one card, oldest->newest,
    ending with the most recent (current) week. Returns None if there's
    not enough history to say anything responsible, else a dict with
    emoji/label/detail for a small badge."""
    if len(pct_series) < MIN_HISTORY_FOR_STREAK:
        return None

    latest = pct_series[-1]
    streak = _streak_len(pct_series)
    prior = pct_series[:-1]

    if len(pct_series) >= MIN_HISTORY_FOR_LABEL:
        # Breakout: was relatively quiet, then suddenly wasn't.
        prior_avg_abs = sum(abs(p) for p in prior) / len(prior) if prior else 0
        if prior_avg_abs > 0 and prior_avg_abs < MOVERS_MIN_PCT and abs(latest) >= prior_avg_abs * BREAKOUT_MULTIPLE:
            return {"emoji": "\U0001F680", "label": "Breakout", "detail": "sudden move after being relatively flat"}

        # Reversing: the streak just before this week was itself a real
        # streak (>=2) in the OPPOSITE direction of this week.
        pre_streak = _streak_len(prior)
        if pre_streak >= 2 and (prior[-1] > 0) != (latest > 0):
            return {"emoji": "↩️", "label": "Reversing", "detail": f"broke a {pre_streak}-week trend"}

        # Cooling off: still positive, but decelerating hard after a real up-streak.
        if latest > 0 and pre_streak >= 2 and prior[-1] > 0 and abs(latest) < abs(prior[-1]) * 0.5:
            return {"emoji": "❄️", "label": "Cooling Off", "detail": "slowing after a run-up"}

        # Steady climber: consistently positive, no single dominating spike.
        if all(p > 0 for p in pct_series):
            sorted_p = sorted(pct_series)
            median = sorted_p[len(sorted_p) // 2]
            if median > 0 and max(pct_series) < median * 2.5:
                return {"emoji": "\U0001F4C8", "label": "Steady Climber", "detail": f"{len(pct_series)} straight weeks up, no single spike"}

        # Volatile: direction keeps flipping.
        sign_changes = sum(1 for a, b in zip(pct_series, pct_series[1:]) if (a > 0) != (b > 0))
        if sign_changes >= VOLATILE_MIN_SIGN_CHANGES:
            return {"emoji": "⚡", "label": "Volatile", "detail": "swinging both directions recently"}

    # Fall back to a plain streak badge once we're confident enough (2+ same-direction weeks).
    if streak >= MIN_HISTORY_FOR_STREAK:
        if latest > 0:
            return {"emoji": "\U0001F525", "label": "Heating Up", "detail": f"{streak} straight weeks up"}
        return {"emoji": "\U0001F4C9", "label": "Sliding", "detail": f"{streak} straight weeks down"}

    return None


def load_all_snapshots(max_snapshots=8):
    """Most recent `max_snapshots` snapshot files, oldest first, as
    (date_stamp, parsed_json) pairs. Capped so momentum calc stays cheap
    even once months of history pile up -- ~2 months of weekly pulls is
    plenty of context for any of the labels above."""
    files = sorted(SNAP_DIR.glob("*.json"))[-max_snapshots:]
    return [(f.stem, json.loads(f.read_text(encoding="utf-8"))) for f in files]


def build_price_history(snapshots):
    """snapshots: output of load_all_snapshots(). Returns {card_key:
    [(date, price_or_None), ...]} oldest->newest, built once and shared by
    both the momentum engine and the sparkline renderer so they work off
    identical underlying data instead of two separate scans."""
    price_history = {}
    for date, snap in snapshots:
        for game in snap.get("games", {}).values():
            for c in game.get("cards", []):
                key = card_key(c["name"], c["set"])
                price_history.setdefault(key, []).append((date, c.get("market_price")))
    return price_history


def build_momentum_map(snapshots, price_history=None):
    """snapshots: output of load_all_snapshots(). Returns {card_key:
    momentum-dict-or-None} for every card present in the latest snapshot."""
    if len(snapshots) < 2:
        return {}
    latest_date, latest = snapshots[-1]
    if price_history is None:
        price_history = build_price_history(snapshots)

    momentum = {}
    for game in latest.get("games", {}).values():
        for c in game.get("cards", []):
            key = card_key(c["name"], c["set"])
            series = _pct_series(price_history.get(key, []))
            momentum[key] = classify_momentum(series)
    return momentum


# ---------- Sparklines ----------
# Tiny inline SVG trend lines embedded directly in each mover/quick-hit
# card at build time -- no client-side fetch, no charting library. Reuses
# the same price_history the momentum engine already builds.
SPARKLINE_WIDTH = 84
SPARKLINE_HEIGHT = 26
SPARKLINE_MIN_POINTS = 3  # 1-2 priced points is a dot/line segment, not a trend


def render_sparkline(prices, direction):
    """prices: [(date, price_or_None), ...] oldest->newest for one card.
    Returns a small inline SVG polyline, or "" if there isn't enough
    priced history yet to draw anything meaningful. Color follows the
    same up/down direction as the card's pct badge rather than deriving
    its own (a card can be net-up over its history but net-down this
    week -- the badge's direction is the one the visitor is looking at)."""
    points = [p for _, p in prices if p is not None]
    if len(points) < SPARKLINE_MIN_POINTS:
        return ""
    lo, hi = min(points), max(points)
    span = hi - lo
    n = len(points)
    coords = []
    for i, p in enumerate(points):
        x = (i / (n - 1)) * SPARKLINE_WIDTH
        y = SPARKLINE_HEIGHT - ((p - lo) / span * SPARKLINE_HEIGHT if span > 0 else SPARKLINE_HEIGHT / 2)
        coords.append(f"{x:.1f},{y:.1f}")
    color = "#0ca30c" if direction == "up" else "#d03b3b"
    return (
        f'<svg class="sparkline" viewBox="0 0 {SPARKLINE_WIDTH} {SPARKLINE_HEIGHT}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


def style_buy_cta(html_body: str) -> str:
    """Give the '**Where to buy:**' paragraph a class so CSS can turn its
    links into big, clickable CTA buttons instead of plain text links."""
    html_body = html_body.replace(
        "<p><strong>Where to buy:</strong>",
        "<p class=\"buy-cta\"><strong>Where to buy:</strong>",
    )
    # The buttons already have their own spacing -- drop the plain-text
    # middle-dot separator that made sense between plain links, not buttons.
    html_body = html_body.replace("</a> · <a", "</a><a")
    # Affiliate disclosure directly under the buy links, not just buried on
    # the About page -- best practice (and most affiliate-network terms)
    # want the disclosure right next to the commercial links themselves.
    # style_buy_cta only ever runs on article bodies, which are always
    # rendered at docs/articles/*.html (root "../"), so the relative link
    # below is safe to hardcode.
    html_body = re.sub(
        r'(<p class="buy-cta">.*?</p>)',
        r'\1<em class="disclosure-note">MarketLair may earn a commission on '
        r'purchases through the links above. It doesn’t change the price '
        r'you pay -- see our <a href="../about.html">disclosure</a>.</em>',
        html_body,
        flags=re.DOTALL,
    )
    return html_body


def build_game_pulse(movers, latest_games):
    """Per-game rollup for the homepage 'Market Pulse' strip -- one tile per
    tracked game (Magic, Pokemon, etc) summarizing how many of its cards
    cleared the movers filter this week, the average swing, and the single
    biggest mover. Lets a visitor who only collects one game skip straight
    to it instead of scanning the combined movers grid. latest_games is the
    latest snapshot's "games" dict (game_key -> {"label":..., "cards": [...]})
    -- iterating it (not GAME_META) means this naturally skips
    "Collecting & Community", which is an article-only category with no
    actual price data behind it."""
    by_game = {}
    for m in movers:
        by_game.setdefault(m["game_label"], []).append(m)

    pulses = []
    for game_key, game in latest_games.items():
        label = game.get("label", game_key)
        game_movers = by_game.get(label, [])
        if game_movers:
            avg_pct = sum(m["pct"] for m in game_movers) / len(game_movers)
            top = max(game_movers, key=lambda m: abs(m["pct"]))
        else:
            avg_pct = None
            top = None
        pulses.append({
            "label": label,
            "cards_tracked": len(game.get("cards", [])),
            "mover_count": len(game_movers),
            "avg_pct": avg_pct,
            "top": top,
        })

    # Most active games first (most qualifying movers), ties broken by the
    # single biggest move -- puts whatever's actually happening up top
    # rather than in a fixed game order.
    pulses.sort(
        key=lambda p: (p["mover_count"], abs(p["top"]["pct"]) if p["top"] else 0),
        reverse=True,
    )
    return pulses


def compute_market_snapshot():
    """Reads the two most recent price snapshots and returns real movers +
    coverage stats for the homepage. Returns None if fewer than 2 snapshots
    exist yet -- the homepage just skips that section rather than faking data."""
    files = sorted(SNAP_DIR.glob("*.json"))
    if not files:
        return None

    latest = json.loads(files[-1].read_text(encoding="utf-8"))
    total_cards = sum(len(g.get("cards", [])) for g in latest.get("games", {}).values())
    stats = {
        "cards_tracked": total_cards,
        "games_tracked": len(latest.get("games", {})),
        "last_updated": latest.get("fetched_at", "")[:10],
        "data_health_warning": None,
    }
    # Momentum needs more than the latest 2 snapshots (see build_momentum_map)
    # -- {} until there's enough weekly history on file, at which point
    # labels start appearing automatically with no code change needed.
    # price_history is shared with the sparkline renderer below so both
    # features read the exact same underlying series.
    all_snapshots = load_all_snapshots()
    price_history = build_price_history(all_snapshots)
    momentum_map = build_momentum_map(all_snapshots, price_history)

    movers = []
    if len(files) >= 2:
        old = json.loads(files[-2].read_text(encoding="utf-8"))

        # A big drop in total tracked cards vs. the prior snapshot usually
        # means a partial/broken fetch (a game's groups/products call
        # failed, tcgcsv had an outage, etc), not a real sitewide event.
        # Flag it rather than silently publishing whatever movers that
        # partial data happens to produce.
        old_total = sum(len(g.get("cards", [])) for g in old.get("games", {}).values())
        if old_total > 0 and total_cards < old_total * MIN_SNAPSHOT_COVERAGE_RATIO:
            stats["data_health_warning"] = (
                f"Latest snapshot has {total_cards} priced cards vs {old_total} in the "
                f"prior one -- a >{(1 - MIN_SNAPSHOT_COVERAGE_RATIO) * 100:.0f}% drop, which "
                "usually means a partial/broken data pull, not a real market event. "
                "Treat this week's movers with suspicion before publishing or alerting."
            )
            print(f"!! {stats['data_health_warning']}", file=sys.stderr)

        for game_key, game in latest.get("games", {}).items():
            old_cards = {
                (c["name"], c["set"]): c["market_price"]
                for c in old.get("games", {}).get(game_key, {}).get("cards", [])
                if c.get("market_price") is not None
            }
            label = game.get("label", game_key)
            for c in game.get("cards", []):
                new_price = c.get("market_price")
                if new_price is None or new_price < MOVERS_MIN_PRICE:
                    continue
                old_price = old_cards.get((c["name"], c["set"]))
                if not old_price:
                    continue
                pct = (new_price - old_price) / old_price * 100
                if abs(pct) < MOVERS_MIN_PCT:
                    continue
                if abs(pct) > MOVERS_MAX_PCT:
                    print(
                        f"  !! suspicious move filtered out: {c['name']} ({label}) "
                        f"${old_price:.2f} -> ${new_price:.2f} ({pct:+.1f}%) -- likely a "
                        "data error, not a real move",
                        file=sys.stderr,
                    )
                    continue
                key = card_key(c["name"], c["set"])
                movers.append({
                    "name": c["name"],
                    "key": key,
                    "set": c["set"],
                    "game_label": label,
                    "old_price": old_price,
                    "new_price": new_price,
                    "pct": pct,
                    "image": c.get("image"),
                    "url": c.get("url"),
                    "momentum": momentum_map.get(key),
                    "sparkline": render_sparkline(price_history.get(key, []), "up" if pct > 0 else "down"),
                })
        movers.sort(key=lambda m: m["pct"], reverse=True)

    # Total qualifying movers before gainers/losers get capped to MOVERS_LIMIT
    # each -- used by the "Since Your Last Visit" banner so it can say "12
    # cards moved" even though the homepage grid only shows the top 3+3.
    stats["total_movers"] = len(movers)

    gainers = [m for m in movers if m["pct"] > 0][:MOVERS_LIMIT]
    losers = sorted([m for m in movers if m["pct"] < 0], key=lambda m: m["pct"])[:MOVERS_LIMIT]
    # Quick Hits draws from a wider slice (still real, still filtered -- just
    # not capped at 3+3) so the scroll strip has enough to be worth scrolling.
    quick_hits = sorted(movers, key=lambda m: abs(m["pct"]), reverse=True)[:QUICK_HITS_LIMIT]
    stats["prior_snapshot"] = json.loads(files[-2].read_text(encoding="utf-8")).get("fetched_at", "")[:10] if len(files) >= 2 else None
    game_pulse = build_game_pulse(movers, latest.get("games", {}))
    return {"stats": stats, "gainers": gainers, "losers": losers, "quick_hits": quick_hits, "game_pulse": game_pulse}


def render_momentum_badge(m, css_class="momentum-badge"):
    """Small emoji+label badge from the momentum engine (classify_momentum).
    None whenever there isn't enough snapshot history yet -- see
    MIN_HISTORY_FOR_STREAK -- so this renders nothing until the site has a
    few weeks of real data on file."""
    mo = m.get("momentum")
    if not mo:
        return ""
    return f'<span class="{css_class}" title="{mo["detail"]}">{mo["emoji"]} {mo["label"]}</span>'


def html_attr_escape(s):
    """Minimal escaping for interpolating a value into an HTML attribute.
    Card names occasionally contain literal quote marks (a handful of real
    Magic/Pokemon card names do) which would otherwise break out of a
    "..."-delimited attribute and corrupt the markup -- cheap enough to
    apply everywhere a card name/set lands in an attribute rather than
    assuming it never happens."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_watch_button(key, name):
    """'Keep an eye on this' watchlist toggle. Purely client-side --
    engagement.js reads/writes localStorage; the button just needs a stable
    data-card-key (see card_key()) to key off of. Rendered unwatched by
    default; engagement.js repaints it on page load from localStorage."""
    name = html_attr_escape(name)
    return (
        f'<button type="button" class="watch-btn" data-card-key="{key}" '
        f'data-card-name="{name}" aria-pressed="false" aria-label="Add {name} to your watchlist">'
        f'<span class="watch-icon">♡</span></button>'
    )


def render_share_button(m):
    """Triggers engagement.js's client-side canvas image generator, which
    renders a branded shareable PNG from the data-* attributes here. No
    server-side image generation, and deliberately no TCGplayer product
    photo in the rendered image -- that photo is cross-origin, and drawing
    a cross-origin image onto a <canvas> "taints" it, which makes
    toBlob()/toDataURL() throw instead of producing a shareable file. Reused
    as-is for quick-hits, since snapshot["quick_hits"] entries are the same
    mover dicts as the Biggest Movers grid (see compute_market_snapshot)."""
    name = html_attr_escape(m["name"])
    game_label = html_attr_escape(m["game_label"])
    set_name = html_attr_escape(m.get("set", ""))
    return (
        f'<button type="button" class="share-btn" data-card-name="{name}" '
        f'data-set="{set_name}" data-game-label="{game_label}" '
        f'data-old-price="{m["old_price"]:.2f}" data-new-price="{m["new_price"]:.2f}" '
        f'data-pct="{m["pct"]:.1f}" aria-label="Share {name}\'s price move as an image">'
        f'<span class="share-icon">↗</span></button>'
    )


def render_mover_card(m):
    direction = "up" if m["pct"] > 0 else "down"
    gslug = game_slug(m["game_label"])
    arrow = "▲" if direction == "up" else "▼"
    return (
        f'<div class="mover-card {direction} {gslug}" data-card-key="{m["key"]}">'
        f'<div class="card-actions">{render_watch_button(m["key"], m["name"])}{render_share_button(m)}</div>'
        f'<span class="tag {gslug}">{game_icon(m["game_label"])} {m["game_label"]}</span>'
        f'<strong>{m["name"]}</strong>'
        f'{render_momentum_badge(m)}'
        f'<span class="mover-price">${m["old_price"]:.2f} → ${m["new_price"]:.2f}</span>'
        f'<span class="mover-pct {direction}">{arrow} {m["pct"]:+.1f}%</span>'
        f'{m["sparkline"]}'
        f'</div>'
    )


def render_quick_hit(m):
    direction = "up" if m["pct"] > 0 else "down"
    gslug = game_slug(m["game_label"])
    arrow = "▲" if direction == "up" else "▼"
    img = (
        f'<img src="{m["image"]}" alt="{m["name"]}" loading="lazy">'
        if m.get("image") else
        f'<div class="quick-hit-noimg">{game_icon(m["game_label"])}</div>'
    )
    link_open = f'<a href="{m["url"]}" target="_blank" rel="noopener nofollow">' if m.get("url") else "<div>"
    link_close = "</a>" if m.get("url") else "</div>"
    momentum_chip = render_momentum_badge(m, css_class="momentum-chip")
    return (
        f'<div class="quick-hit {gslug}" data-card-key="{m["key"]}">'
        f'<div class="card-actions">{render_watch_button(m["key"], m["name"])}{render_share_button(m)}</div>'
        f'{link_open}{img}'
        f'<div class="quick-hit-body">'
        f'<span class="quick-hit-pct {direction}">{arrow} {m["pct"]:+.0f}%</span>{momentum_chip}'
        f'<strong>{m["name"]}</strong>'
        f'<span class="quick-hit-price">${m["new_price"]:.2f}</span>'
        f'{m["sparkline"]}'
        f'</div>{link_close}'
        f'</div>'
    )


def render_quick_hits(snapshot):
    if snapshot is None or not snapshot.get("quick_hits"):
        return ""
    cards = "".join(render_quick_hit(m) for m in snapshot["quick_hits"])
    return (
        "<section class='quick-hits-section'>"
        "<h2 class='section-heading'>⚡ Quick Hits</h2>"
        f"<div class='quick-hits-strip'>{cards}</div>"
        "</section>"
    )


def render_game_pulse_tile(p):
    gslug = game_slug(p["label"])
    icon = game_icon(p["label"])
    if p["mover_count"] == 0:
        body = "<span class='pulse-quiet'>No significant moves this week</span>"
    else:
        avg_dir = "up" if p["avg_pct"] > 0 else "down"
        avg_arrow = "▲" if avg_dir == "up" else "▼"
        top = p["top"]
        top_dir = "up" if top["pct"] > 0 else "down"
        top_arrow = "▲" if top_dir == "up" else "▼"
        plural = "" if p["mover_count"] == 1 else "s"
        body = (
            f"<span class='pulse-stat'>{p['mover_count']} card{plural} moved "
            f"<span class='pulse-avg {avg_dir}'>{avg_arrow} {p['avg_pct']:+.1f}% avg</span></span>"
            f"<span class='pulse-top'>Top: {top['name']} "
            f"<span class='pulse-top-pct {top_dir}'>{top_arrow} {top['pct']:+.1f}%</span></span>"
        )
    return (
        f"<div class='pulse-tile {gslug}'>"
        f"<span class='tag {gslug}'>{icon} {p['label']}</span>"
        f"{body}"
        f"</div>"
    )


def render_game_pulse(snapshot):
    if snapshot is None or not snapshot.get("game_pulse"):
        return ""
    tiles = "".join(render_game_pulse_tile(p) for p in snapshot["game_pulse"])
    return (
        "<section class='game-pulse-section'>"
        "<h2 class='section-heading'>\U0001F3AF Market Pulse by Game</h2>"
        f"<div class='game-pulse-grid'>{tiles}</div>"
        "</section>"
    )


def render_market_section(snapshot):
    if snapshot is None:
        return ""
    stats = snapshot["stats"]
    stats_html = (
        "<div class='stats-bar'>"
        f"<div class='stat-tile'><strong>{stats['cards_tracked']:,}</strong><span>cards tracked</span></div>"
        f"<div class='stat-tile'><strong>{stats['games_tracked']}</strong><span>games covered</span></div>"
        f"<div class='stat-tile'><strong>{stats['last_updated']}</strong><span>last updated</span></div>"
        "</div>"
    )
    if not snapshot["gainers"] and not snapshot["losers"]:
        note = (
            "<p class='meta'>No cards cleared the noise filter (min $1.00, "
            f"min {MOVERS_MIN_PCT:.0f}% move) since the last snapshot -- "
            "check back after the next price pull.</p>"
        ) if stats.get("prior_snapshot") else (
            "<p class='meta'>Movers need at least two snapshots to compare -- "
            "check back after the next scheduled price pull.</p>"
        )
        return f"<div id='movers'>{stats_html}{note}</div>"

    cards = "".join(render_mover_card(m) for m in snapshot["gainers"] + snapshot["losers"])
    since = f" <span class='meta'>since {stats['prior_snapshot']}</span>" if stats.get("prior_snapshot") else ""
    return (
        f"<div id='movers'>"
        f"{stats_html}"
        f"<h2 class='section-heading'>\U0001F525 Biggest Movers{since}</h2>"
        f"<div class='mover-grid'>{cards}</div>"
        f"</div>"
    )


def render_watchlist_section():
    """Static shell for the 'Cards You're Watching' strip -- hidden by
    default (and by CSS as a no-JS fallback), populated + unhidden
    client-side by engagement.js from localStorage + card-index.json. Lives
    on the homepage only, same as Quick Hits."""
    return (
        "<section id='watchlist-section' class='quick-hits-section' hidden>"
        "<h2 class='section-heading'>\U0001F49B Cards You're Watching</h2>"
        "<div class='quick-hits-strip' id='watchlist-strip'></div>"
        "</section>"
    )


def render_list_checker_section():
    """Static shell for the 'Check Your List' paste-in tool -- a textarea
    where a visitor pastes a TCGplayer-style mass-entry list (quantities
    optional, one card per line) and engagement.js matches each line
    against card-index.json entirely client-side (no upload, nothing
    leaves the browser). Results are injected into #list-checker-results;
    this function only emits the static form shell."""
    return (
        "<section id='list-checker-section' class='list-checker-section'>"
        "<h2 class='section-heading'>\U0001F4CB Check Your List</h2>"
        "<p class='meta'>Paste a list of cards (quantities optional, one per line) and see "
        "which ones we're tracking right now -- nothing you paste leaves your browser.</p>"
        "<textarea id='list-checker-input' class='list-checker-textarea' rows='6' "
        "placeholder='4 Lightning Bolt&#10;1 Charizard ex&#10;Roronoa Zoro (ST32-005)'></textarea>"
        "<div class='list-checker-actions'>"
        "<button type='button' id='list-checker-run' class='list-checker-run'>Check my list</button>"
        "<span id='list-checker-status' class='list-checker-status'></span>"
        "</div>"
        "<div id='list-checker-results' class='list-checker-results'></div>"
        "</section>"
    )


def build_engagement_data(snapshot):
    """Writes the two small JSON files engagement.js relies on:
      - card-index.json: every priced card in the latest snapshot, keyed by
        card_key(), so a watched card can be resolved back into a name/
        price/image/link even in a week where it isn't one of the "movers"
        cards actually rendered with a heart button. Also the lookup table
        behind the "Check Your List" paste-in tool (client-side name
        matching -- see engagement.js). Momentum is recomputed here rather
        than threaded in from compute_market_snapshot() so this function
        stays self-contained; re-running it over ~8 snapshots x a few
        thousand cards is cheap enough that it isn't worth the extra
        parameter plumbing.
      - snapshot-summary.json: a tiny digest (last_updated + how many cards
        moved + the top few) that the "Since Your Last Visit" banner polls
        to decide whether it has anything worth telling a returning visitor.
    Both are written to the site root and fetched root-relative via
    window.MARKETLAIR_ROOT, same as every other cross-page asset here.
    """
    files = sorted(SNAP_DIR.glob("*.json"))
    card_index = {}
    summary = {"last_updated": None, "mover_count": 0, "top_movers": []}
    if not files:
        return card_index, summary

    latest = json.loads(files[-1].read_text(encoding="utf-8"))
    summary["last_updated"] = latest.get("fetched_at", "")[:10]

    all_snapshots = load_all_snapshots()
    momentum_map = build_momentum_map(all_snapshots)

    for game_key, game in latest.get("games", {}).items():
        label = game.get("label", game_key)
        for c in game.get("cards", []):
            price = c.get("market_price")
            if price is None:
                continue
            key = card_key(c["name"], c["set"])
            card_index[key] = {
                "name": c["name"],
                "set": c["set"],
                "game_label": label,
                "price": price,
                "image": c.get("image"),
                "url": c.get("url"),
                "momentum": momentum_map.get(key),
            }

    if snapshot is not None:
        summary["mover_count"] = snapshot["stats"].get("total_movers", 0)
        top = sorted(snapshot["quick_hits"], key=lambda m: abs(m["pct"]), reverse=True)[:3]
        summary["top_movers"] = [{"name": m["name"], "pct": round(m["pct"], 1)} for m in top]

    return card_index, summary


def render_related_articles(current_slug, current_game, all_articles):
    others = [a for a in all_articles if a["slug"] != current_slug]
    if not others:
        return ""
    same_game = [a for a in others if a["game"] == current_game]
    rest = [a for a in others if a["game"] != current_game]
    picks = (same_game + rest)[:3]
    items = "".join(
        f'<li class="article-card {game_slug(a["game"])}">'
        f'<span class="meta"><span class="tag {game_slug(a["game"])}">{game_icon(a["game"])} {a["game"]}</span> '
        f'<span>{a["date"]}</span></span>'
        f'<a class="card-title" href="{a["slug"]}.html">{a["title"]}</a>'
        f'<p>{a["description"]}</p>'
        f'<a class="card-cta" href="{a["slug"]}.html">Read the breakdown</a>'
        f'</li>'
        for a in picks
    )
    return (
        "<section class='related'>"
        "<h2 class='section-heading'>More from MarketLair</h2>"
        f"<ul class='article-grid'>{items}</ul>"
        "</section>"
    )


def parse_front_matter(text: str):
    """Very small front-matter parser: expects a leading --- block of key: value lines."""
    fm = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip()
            body = text[end + 4:].lstrip("\n")
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    return fm, body


def build():
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "articles").mkdir(exist_ok=True)

    # copy static assets
    shutil.copyfile(ROOT / "templates" / "style.css", SITE_DIR / "style.css")
    shutil.copyfile(ROOT / "templates" / "personalize.js", SITE_DIR / "personalize.js")
    shutil.copyfile(ROOT / "templates" / "subscribe.js", SITE_DIR / "subscribe.js")
    # QoL layer: since-your-last-visit banner, watchlist heart buttons, and
    # the "Cards You're Watching" strip -- all localStorage-only, no backend,
    # no accounts. See engagement.js's own header comment for the split.
    shutil.copyfile(ROOT / "templates" / "engagement.js", SITE_DIR / "engagement.js")
    # firebase-messaging-sw.js MUST land at the site root (not under
    # articles/) -- a service worker's scope is the directory it's served
    # from and everything below it, so root is what lets it cover the
    # whole site. subscribe.js registers it via window.MARKETLAIR_ROOT
    # (the same "" / "../" prefix every other on-page link uses), which is
    # what actually makes this correct on a GitHub Pages project subpath
    # like proxylair.github.io/marketlair/ -- a literal "/firebase-messaging-sw.js"
    # would 404 there (this bit us once already; see git history).
    shutil.copyfile(
        ROOT / "templates" / "firebase-messaging-sw.js",
        SITE_DIR / "firebase-messaging-sw.js",
    )
    # Offline fallback page the service worker serves for a failed
    # navigation with nothing cached yet -- must also land at the site
    # root so it resolves correctly under caches.match(OFFLINE_URL)
    # (relative to the SW's own scope) on a GitHub Pages project subpath.
    shutil.copyfile(ROOT / "templates" / "offline.html", SITE_DIR / "offline.html")
    # Icons -- favicon, apple-touch-icon, and the sizes used by both the
    # web app manifest (installed-icon) and push notifications
    # (firebase-messaging-sw.js's icon/badge).
    for icon_file in (
        "favicon.ico", "favicon-16.png", "favicon-32.png",
        "apple-touch-icon.png", "icon-64.png", "icon-192.png", "icon-512.png",
        "icon-maskable-512.png",
    ):
        shutil.copyfile(ROOT / "templates" / icon_file, SITE_DIR / icon_file)
    (SITE_DIR / "manifest.webmanifest").write_text(json.dumps({
        "id": ".",
        "name": "MarketLair",
        "short_name": "MarketLair",
        "description": "Real trading-card market data, tracked regularly, explained simply.",
        "start_url": ".",
        "scope": ".",
        "display": "standalone",
        "background_color": "#fdfbf6",
        "theme_color": "#2a78d6",
        # "any" icons are used as-is (our own rounded-square shape); the
        # "maskable" one is a separate, more-padded render so Android's
        # adaptive-icon crop (circle/squircle/whatever the launcher uses)
        # doesn't clip the pulse line -- see icon-maskable-source.svg.
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, indent=2), encoding="utf-8")

    template = Template(TEMPLATE_PATH.read_text(encoding="utf-8"))
    articles = []

    # First pass: parse every article's front matter + body so related-article
    # links can be built with full knowledge of the catalog before any file
    # is written.
    for path in sorted(CONTENT_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        fm, body = parse_front_matter(raw)
        html_body = md.markdown(body, extensions=["extra", "sane_lists"])
        html_body = style_buy_cta(html_body)
        articles.append({
            "title": fm.get("title", path.stem),
            "description": fm.get("description", ""),
            "date": fm.get("date", datetime.now().strftime("%Y-%m-%d")),
            "game": fm.get("game", ""),
            "slug": path.stem,
            "html_body": html_body,
        })

    articles.sort(key=lambda a: a["date"], reverse=True)

    # Second pass: render + write each article page, now with a "More from
    # MarketLair" block linking to other articles.
    for a in articles:
        tag_html = (
            f"<span class='tag {game_slug(a['game'])}'>{game_icon(a['game'])} {a['game']}</span> "
            if a["game"] else ""
        )
        related_html = render_related_articles(a["slug"], a["game"], articles)
        page_html = template.render(
            title=a["title"],
            description=a["description"],
            content=(
                f"<article><h1>{a['title']}</h1><p class='meta'>{tag_html}{a['date']}</p>"
                f"{a['html_body']}</article>{related_html}"
            ),
            root="../",
            year=datetime.now().year,
        )
        out_path = SITE_DIR / "articles" / f"{a['slug']}.html"
        out_path.write_text(page_html, encoding="utf-8")

    # index page
    def index_item(a):
        gslug = game_slug(a["game"])
        tag = (
            f"<span class='tag {gslug}'>{game_icon(a['game'])} {a['game']}</span>"
            if a["game"] else ""
        )
        return (
            f'<li class="article-card {gslug}">'
            f'<span class="meta">{tag} <span>{a["date"]}</span></span>'
            f'<a class="card-title" href="articles/{a["slug"]}.html">{a["title"]}</a>'
            f'<p>{a["description"]}</p>'
            f'<a class="card-cta" href="articles/{a["slug"]}.html">Read the breakdown</a>'
            f'</li>'
        )

    list_items = "\n".join(index_item(a) for a in articles)
    game_pills = "".join(
        f"<span>{meta['icon']} {name}</span>" for name, meta in GAME_META.items()
    )
    snapshot = compute_market_snapshot()
    market_html = render_market_section(snapshot)
    game_pulse_html = render_game_pulse(snapshot)
    quick_hits_html = render_quick_hits(snapshot)
    watchlist_html = render_watchlist_section()
    list_checker_html = render_list_checker_section()
    card_index, engagement_summary = build_engagement_data(snapshot)
    (SITE_DIR / "card-index.json").write_text(json.dumps(card_index), encoding="utf-8")
    (SITE_DIR / "snapshot-summary.json").write_text(json.dumps(engagement_summary), encoding="utf-8")
    index_html = template.render(
        title="MarketLair -- Trading Card Market Data & Analysis",
        description="Plain-English trading card market breakdowns, backed by real price data.",
        content=(
            "<div class='hero'>"
            "<h1>\U0001F525 MarketLair</h1>"
            "<p>Real trading-card market data, tracked regularly, explained simply -- "
            "no fluff, just what's moving and why it matters.</p>"
            f"<div class='game-strip'>{game_pills}</div>"
            "</div>"
            f"{market_html}"
            f"{game_pulse_html}"
            f"{quick_hits_html}"
            f"{watchlist_html}"
            f"{list_checker_html}"
            "<h2 class='section-heading'>Latest Analysis</h2>"
            f"<ul class='article-grid'>{list_items}</ul>"
        ),
        root="",
        year=datetime.now().year,
    )
    (SITE_DIR / "index.html").write_text(index_html, encoding="utf-8")

    # about / disclosure page
    about_html = template.render(
        title="About & Affiliate Disclosure",
        description="About MarketLair and how it makes money.",
        content=(
            "<h1>About MarketLair</h1>"
            "<p>MarketLair tracks real trading-card market prices and publishes plain-English "
            "breakdowns of what's moving and why. Data comes from public marketplace APIs.</p>"
            "<h2>Affiliate Disclosure</h2>"
            "<p>Some links on this site are affiliate links (TCGplayer, eBay Partner Network). "
            "If you click through and make a purchase, this site may earn a small commission "
            "at no extra cost to you. We only link to products we'd genuinely point you to.</p>"
            "<h2>Ownership Disclosure</h2>"
            "<p>MarketLair is run by the same person behind "
            "<a href=\"https://proxylair.com\" target=\"_blank\" rel=\"noopener\">ProxyLair</a>, "
            "a custom TCG proxy card design and production studio. We're upfront about that "
            "connection anywhere the two come up -- if an article mentions ProxyLair, treat it "
            "the same way you'd treat any other disclosed relationship on this site.</p>"
            "<h2>Push Notifications &amp; Data</h2>"
            "<p>If you click \"Get price alerts,\" your browser stores a notification "
            "token (a random ID tied to your browser install, not to you personally), "
            "along with which games you chose to follow and your browser's user-agent "
            "string. If you've added cards to your watchlist (the heart button on mover "
            "cards), we also store which cards those are, so we can alert you when one of "
            "them moves even outside your followed games -- that list stays in sync with "
            "your watchlist automatically. We use all of this only to send you the weekly "
            "price-mover digest you signed up for -- we don't sell it, share it, or use it "
            "for anything else. Click \"Manage\" next to the alerts indicator any time to "
            "change which games you follow or turn alerts off entirely, which deletes that "
            "data (your watchlist itself lives only in your browser, separately -- turning "
            "off alerts doesn't clear it). We don't use tracking cookies or run analytics "
            "on this site beyond basic, aggregate hosting logs.</p>"
        ),
        root="",
        year=datetime.now().year,
    )
    (SITE_DIR / "about.html").write_text(about_html, encoding="utf-8")

    # methodology page -- the credibility backbone: where do these numbers
    # actually come from, and what are their limits.
    methodology_html = template.render(
        title="Methodology",
        description="Where MarketLair's price data comes from, what \"market price\" means, and where the numbers get shaky.",
        content=(
            "<h1>Methodology</h1>"
            "<p>Once you publish a number like $2,873.33 for a single card, the fair "
            "next question is: where exactly did that come from? Here's the honest "
            "answer.</p>"
            "<h2>Where the data comes from</h2>"
            "<p>Prices come from <a href=\"https://tcgcsv.com\" target=\"_blank\" rel=\"noopener\">TCGCSV</a>, "
            "a free, no-API-key mirror of TCGplayer's own product and pricing feeds. "
            "We don't run our own marketplace or set any prices ourselves -- we're "
            "reading the same catalog TCGplayer exposes and explaining what changed.</p>"
            "<h2>What \"market price\" means</h2>"
            "<p>The price shown in articles is TCGplayer's <strong>market price</strong> for "
            "that product: a rolling estimate based on recent sales activity, not a "
            "single listing and not a guaranteed sale price. We also pull low and high "
            "prices where relevant, which reflect the current range of active listings.</p>"
            "<h2>How \"movers\" are calculated</h2>"
            "<p>We keep a dated snapshot of the full catalog each time we pull data, and "
            "compare the two most recent snapshots to find the biggest gainers and "
            "losers. To keep that list honest, we filter out anything under $1.00 "
            f"(a card going from $0.04 to $0.08 is technically \"+100%\" and means "
            f"nothing) and anything moving less than {MOVERS_MIN_PCT:.0f}% -- normal "
            "day-to-day noise, not a real move.</p>"
            "<h2>Low-volume and thin-market cards</h2>"
            "<p>Rare, newly-released, or low-print cards can have a \"market price\" set "
            "by only a handful of actual sales -- sometimes even by active listings "
            "rather than completed ones. We call this out directly in articles that "
            "cover chase-tier cards, because a price built on 2-3 sales can move 20-30% "
            "on a single new listing. Treat those numbers as the current pecking order, "
            "not a guaranteed valuation.</p>"
            "<h2>Sealed product vs. singles</h2>"
            "<p>When an article covers sealed product (starter decks, booster boxes), "
            "that price is for the sealed unit itself, not the sum of the cards inside "
            "it -- we call out the difference explicitly when it's relevant to the "
            "takeaway.</p>"
            "<h2>What's not included</h2>"
            "<p>Prices shown do not include shipping, marketplace fees, or sales tax. "
            "They're a snapshot as of the article's date and will drift -- always check "
            "current listings before buying or selling based on a number you read here.</p>"
            "<h2>Analysis vs. fact</h2>"
            "<p>When an article suggests <em>why</em> a price might be moving -- "
            "competitive play, collector demand, print scarcity -- that's our read of "
            "the data, not a confirmed fact. We try to flag that distinction with "
            "language like \"may indicate\" or \"the data suggests\" rather than stating "
            "a cause as settled.</p>"
        ),
        root="",
        year=datetime.now().year,
    )
    (SITE_DIR / "methodology.html").write_text(methodology_html, encoding="utf-8")

    # sitemap.xml
    urls = ["", "about.html", "methodology.html"] + [f"articles/{a['slug']}.html" for a in articles]
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        sitemap.append(f"  <url><loc>{SITE_URL}/{u}</loc></url>")
    sitemap.append("</urlset>")
    (SITE_DIR / "sitemap.xml").write_text("\n".join(sitemap), encoding="utf-8")

    # robots.txt
    (SITE_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8"
    )

    print(f"Built {len(articles)} article(s) into {SITE_DIR}/")


if __name__ == "__main__":
    build()

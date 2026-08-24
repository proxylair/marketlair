#!/usr/bin/env python3
"""
fetch_snapshot.py
------------------
Pulls current market prices for five trading card games -- Magic: The
Gathering, Pokemon TCG, One Piece Card Game, Disney Lorcana, and Riftbound --
and saves one timestamped JSON snapshot per run to data/snapshots/.

Data source: tcgcsv.com, a free, no-API-key mirror of TCGplayer's own
product/price feeds. Same data source across all five games, which keeps
this script simple and keeps the price numbers directly comparable to what
buyers/sellers actually see on TCGplayer.
  Docs: https://tcgcsv.com

Run this weekly. Once you have 2+ snapshots, `find_movers.py` can diff them
to find real price movers -- the highest-value, hardest-to-fake content
angle for this site, since nobody else is tracking YOUR exact snapshot
history.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://tcgcsv.com/tcgplayer"
HEADERS = {"User-Agent": "MarketLairResearch/1.0 (contact: proxylair@gmail.com)"}

# categoryId per game, from https://tcgcsv.com/tcgplayer/categories
GAMES = {
    "mtg": {"category_id": 1, "label": "Magic: The Gathering"},
    "pokemon": {"category_id": 3, "label": "Pokemon TCG"},
    "onepiece": {"category_id": 68, "label": "One Piece Card Game"},
    "lorcana": {"category_id": 71, "label": "Disney Lorcana"},
    "riftbound": {"category_id": 89, "label": "Riftbound TCG"},
}

# How many of each game's most-recently-released sets to track. Recent sets
# are where price movement and budget-deck content actually happens; going
# back further mostly adds noise (and a much bigger/slower fetch).
#
# This is a floor and a target, not a fixed count -- some games (observed on
# One Piece) ship many small single-product "sets" in TCGplayer's catalog
# (individual starter decks, ~11-13 cards each) interleaved with real
# booster sets. A fixed "5 most recent groups" for a game like that grabs
# six starter decks and zero real sets, undercounting that game by an
# order of magnitude versus MTG/Pokemon/Lorcana. Instead: always include at
# least GROUPS_MIN groups, but keep pulling further back until either
# CARDS_TARGET priced products have been collected or GROUPS_MAX groups
# have been examined -- so a game with big sets stops early (same behavior
# as before), and a game with lots of small releases keeps going until it
# reaches an actual full-size set.
GROUPS_MIN = 5
GROUPS_MAX = 20
CARDS_TARGET = 300

# Below this many priced cards for a single game, something's probably
# wrong upstream (a failed groups/products call, an empty category, a
# tcgcsv outage mid-pull) rather than that game genuinely having almost
# nothing priced. Flagged loudly rather than silently saved as normal --
# this feeds the same "don't publish/alert on broken data" principle as
# build_site.py's snapshot-coverage check, just catching it earlier.
MIN_CARDS_PER_GAME = 20

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "snapshots"


def get_json(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("results", data)


def recent_groups(category_id: int, limit: int):
    groups = get_json(f"{BASE}/{category_id}/groups")
    now = datetime.now(timezone.utc).isoformat()
    released = [g for g in groups if g.get("publishedOn") and g["publishedOn"] < now]
    # groupId is assigned roughly sequentially by TCGplayer as sets are added
    # to their catalog. It's a more reliable "recency" signal than
    # publishedOn -- some categories (observed on Pokemon) have publishedOn
    # values that reflect a bulk metadata refresh date rather than the
    # card set's actual release date, which makes sorting by publishedOn
    # alone surface decades-old promo pools instead of the current sets.
    released.sort(key=lambda g: g.get("groupId", 0), reverse=True)
    return released[:limit]


def fetch_game(category_id: int, groups_min=GROUPS_MIN, groups_max=GROUPS_MAX, cards_target=CARDS_TARGET):
    cards = []
    candidates = recent_groups(category_id, groups_max)
    for i, group in enumerate(candidates):
        gid = group["groupId"]
        try:
            products = get_json(f"{BASE}/{category_id}/{gid}/products")
            prices = get_json(f"{BASE}/{category_id}/{gid}/prices")
        except requests.RequestException as e:
            print(f"    !! group {gid} failed: {e}", file=sys.stderr)
            continue
        price_by_id = {p["productId"]: p for p in prices}
        for prod in products:
            price = price_by_id.get(prod["productId"])
            if not price:
                continue
            market_price = price.get("marketPrice")
            # Defend against garbage upstream data (null, zero, negative,
            # or a non-numeric value) rather than letting it flow through
            # to movers math later, where a bad old_price/new_price of 0
            # would produce a division-by-zero or a nonsense "+inf%" move.
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                continue
            cards.append({
                "name": prod.get("name"),
                "set": group.get("name"),
                "set_published": group.get("publishedOn"),
                "market_price": market_price,
                "low_price": price.get("lowPrice"),
                "high_price": price.get("highPrice"),
                "url": prod.get("url"),
                "image": prod.get("imageUrl"),
            })
        time.sleep(0.15)  # be polite to a free service
        if i + 1 >= groups_min and len(cards) >= cards_target:
            break
    return cards


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {"fetched_at": datetime.now(timezone.utc).isoformat(), "games": {}}
    warnings = []

    for key, meta in GAMES.items():
        print(f"Fetching {meta['label']} ...", file=sys.stderr)
        try:
            cards = fetch_game(meta["category_id"])
        except requests.RequestException as e:
            # A whole-game failure (e.g. the /groups call itself, which
            # isn't wrapped inside fetch_game) shouldn't lose the other
            # four games' data -- save this one as empty and keep going,
            # loudly, rather than crashing the entire weekly pull.
            print(f"  !! {meta['label']} failed entirely: {e}", file=sys.stderr)
            cards = []
        snapshot["games"][key] = {"label": meta["label"], "cards": cards}
        print(f"  -> {len(cards)} priced cards/products", file=sys.stderr)
        if len(cards) < MIN_CARDS_PER_GAME:
            warnings.append(
                f"{meta['label']}: only {len(cards)} priced card(s) (expected at least "
                f"{MIN_CARDS_PER_GAME}) -- likely a partial/broken fetch, not real data."
            )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = OUT_DIR / f"{stamp}.json"
    out_path.write_text(json.dumps(snapshot, indent=2))
    print(f"Saved snapshot to {out_path}")

    if warnings:
        print("\n!! DATA HEALTH WARNINGS -- review before running find_movers.py/build_site.py:",
              file=sys.stderr)
        for w in warnings:
            print(f"   - {w}", file=sys.stderr)


if __name__ == "__main__":
    main()

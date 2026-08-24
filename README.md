# MarketLair

A trading-card market data site covering five games -- Magic: The Gathering,
Pokemon TCG, One Piece Card Game, Disney Lorcana, and Riftbound -- built from
real price snapshots, turned into plain-English articles, monetized with
affiliate links (TCGplayer, eBay Partner Network) and later display ads.
Built to run on $0 -- no paid tools, no API keys required for the current
pipeline.

**Read this whole file before you do anything else.** It tells you exactly
what's real, what's not built yet, and what you have to do by hand (signing
up for affiliate accounts, buying a domain, publishing).

## What's actually in this repo

- `scripts/fetch_snapshot.py` -- pulls current prices for all five games
  from [tcgcsv.com](https://tcgcsv.com) (free, no key -- it's a mirror of
  TCGplayer's own product/price feeds) and saves one dated JSON file to
  `data/snapshots/`, covering the most recently released sets in each
  game. Run this weekly. One real snapshot (today's) is already in there.
- `scripts/find_movers.py` -- once you have 2+ snapshots, this diffs them
  per game and prints the biggest price gainers/losers. **This is the
  actual differentiator** -- nobody else is tracking your specific
  snapshot history, so "movers" articles get more genuinely original the
  longer you run this. Usage: `python3 scripts/find_movers.py 8 pokemon`
  (min % change, optional game filter -- omit the game to see all five).
- `scripts/build_site.py` -- turns Markdown files in `content/articles/`
  into the static site in `docs/`. Five real articles are already written
  (one per game), using live data pulled today.
- `scripts/send_alerts.py` -- sends a push-notification digest to
  everyone who clicked "Get price alerts" on the site, using the exact
  same movers data the homepage shows. See "Push notifications" below.
- `content/articles/` -- article source files (Markdown + a small front
  matter block for title/date/description/game).
- `docs/` -- the finished, deployable static site. This is what you upload.

### Why TCGCSV instead of a game-specific API

Pokemon, One Piece, and Lorcana all have their own dedicated free APIs
(pokemontcg.io, optcgapi.com, lorcana-api.com), but none of them cover
Riftbound, and juggling four different data shapes was more code than it's
worth. TCGCSV mirrors TCGplayer's actual product/price catalog for every
game TCGplayer sells -- including all five here -- with one consistent
JSON shape, so one script covers all of it. Trade-off: it gives aggregate
market/low/high price per product, not per-condition pricing (e.g. it
won't separate "near mint" from "lightly played"). Fine for trend/movers
content; not precise enough for pricing your own individual listings.

## Run it yourself

```bash
pip install -r requirements.txt
python3 scripts/fetch_snapshot.py     # pulls fresh price data
python3 scripts/build_site.py         # rebuilds docs/ from content/articles/
```

Open `docs/index.html` in a browser to preview locally before publishing.

## Deploy for free (pick one, both are genuinely $0)

**GitHub Pages** (simplest if you already have a GitHub account):
1. Create a new repo, push this whole folder to it (exact commands below).
2. Repo Settings -> Pages -> Source: "Deploy from a branch" -> Branch:
   `main`, Folder: `/docs`. Save.
3. You get a free `yourname.github.io/marketlair` URL within a minute or two.
   A real domain (~$10-12/year) is optional and can wait until the site has
   traction -- don't spend money on it yet.

**Cloudflare Pages** (also free, slightly more polished dashboard):
1. Sign up at pages.cloudflare.com, connect the GitHub repo, set the build
   output directory to `docs/`.
2. Same free subdomain-first, custom-domain-later approach.

## The weekly workflow (this is the part that has to stay human for now)

1. Run `fetch_snapshot.py` (or let the scheduled Claude task below do it).
2. Once you have 2+ snapshots, run `find_movers.py` to see real price
   movement -- that's your next article topic, already half-written by the
   data itself.
3. Draft the article. Either write it yourself, paste the mover data into
   any AI chat tool you already have access to, or use the scheduled task
   below.
4. **Read it before publishing.** This is the step that can't be skipped --
   both Google (for search ranking) and any future ad network penalize
   thin, templated, unedited AI content. One genuinely good article a week
   beats seven auto-generated ones.
5. `python3 scripts/build_site.py`, then push/redeploy.
6. Once the redeploy is live, `python3 scripts/send_alerts.py` to notify
   subscribers -- do this last so the notification links to content
   that's actually up.

Realistically this is 30-60 minutes/week once the pipeline is warmed up:
~5 min to run the scripts, ~20-40 min to read/edit/tighten a draft,
~5 min to redeploy.

## Push notifications

Visitors can click "🔔 Get price alerts" on the site to subscribe (Firebase
Cloud Messaging + Firestore, wired up in `templates/subscribe.js` /
`templates/firebase-messaging-sw.js`). `scripts/send_alerts.py` is what
actually sends the digest -- it's a separate step from the site itself
since a static site can't run server-side code.

One-time setup:
1. Firebase console -> gear icon -> Project settings -> Service accounts
   tab -> "Generate new private key". Save the downloaded JSON as
   `scripts/serviceAccountKey.json` (already gitignored -- this file
   grants full admin access to the Firebase project, so it must never be
   committed or shared).
2. `pip install -r requirements.txt` (pulls in `firebase-admin`).

Then, as the last step of the weekly workflow above:
```bash
python3 scripts/send_alerts.py --dry-run   # preview what would be sent, no credentials required
python3 scripts/send_alerts.py             # sends for real
```
It tracks the last snapshot it alerted on (`data/last_alerted_snapshot.txt`)
so re-running it by accident won't double-notify anyone.

## Monetization -- what to sign up for, and what's NOT done yet

None of these are wired into the site yet because they all require you
personally to create an account (identity/tax info) -- I can't do this step
for you.

- **TCGplayer Affiliate Program** -- apply via their Impact.com portal:
  https://docs.tcgplayer.com/docs/tcgplayer-affiliate-program . No minimum
  traffic requirement listed. ~48-hour click attribution window. Once
  approved, replace the `#` placeholder links in
  `content/articles/*.md` with your real affiliate links.
- **eBay Partner Network** -- sign up at partnernetwork.ebay.com. Trading
  cards category commission is currently listed around 3%. Also free to
  join.
- **Google AdSense** -- apply once the site has a handful of real articles
  and some organic traffic; approval typically wants original content and
  basic pages (About/disclosure, which this site already has).

Until affiliate accounts are approved, the `#` links in the article are
inert placeholders -- swap them for real tracking links as your first
"publish day" task.

## Realistic expectations (read this part)

This is a content/SEO asset. It will not produce meaningful money this
month. Realistic shape of this, based on how this model generally plays
out: small, inconsistent affiliate clicks for the first couple of months
while the site has no search authority and no backlinks; if a handful of
articles start ranking, low hundreds of dollars a month is a reasonable
6-9 month target; $2k+/month would require either a much bigger content
library, some articles ranking well for real search volume, or a second
traffic source (e.g. cross-posting the "movers" data on Reddit/X/TikTok,
where card-collecting communities are active). None of that is
automated -- it's audience-building, and it takes the time it takes.

## Suggested next articles (once you have time)

- Anything `find_movers.py` surfaces once you have a second snapshot, for
  any of the five games -- this gets more valuable every week you keep
  running it, since it's the one thing generic AI content sites can't
  fake without their own price history.
- "Grading ROI" pieces: is it worth submitting a specific card to PSA/BGS
  at current submission prices vs. its raw value -- genuinely useful,
  genuinely hard for a generic AI channel to fake without real market
  knowledge, which is exactly the edge you have from working the shop.
- Cross-game comparisons once you have a few weeks of data: e.g. "which of
  the five games is seeing the most price volatility this month" -- a
  genuinely unique angle since almost no one tracks all five in one place.

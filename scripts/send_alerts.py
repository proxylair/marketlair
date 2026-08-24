#!/usr/bin/env python3
"""
send_alerts.py
----------------
Sends a push-notification digest to MarketLair's Firebase Cloud Messaging
subscribers, using the exact same "biggest movers" data the homepage
shows (reuses build_site.compute_market_snapshot() so the numbers can
never drift between what the site displays and what gets pushed).

Run this AFTER build_site.py + redeploy, as the last step of the weekly
workflow in README.md -- that way the notification points at content
that's already live.

Requires:
  - firebase-admin (pip install -r requirements.txt)
  - a Firebase service account key JSON. Generate one at: Firebase
    console -> gear icon -> Project settings -> Service accounts tab ->
    "Generate new private key". By default this script looks for it at
    scripts/serviceAccountKey.json (gitignored); point it elsewhere (e.g.
    if you'd rather keep it outside the repo entirely) with:
        MARKETLAIR_SERVICE_ACCOUNT=/path/to/key.json python3 scripts/send_alerts.py ...
    NEVER commit this file -- it grants full admin access to the Firebase
    project, including reading every subscriber's data.

Three modes, on purpose -- this sends real push notifications to real
people, so there's no bare "just run it and see":
  python3 scripts/send_alerts.py --dry-run        # preview only, sends nothing
  python3 scripts/send_alerts.py --test <token>   # sends ONLY to one token (e.g. your own)
  python3 scripts/send_alerts.py --send           # sends to everyone, asks for typed confirmation first
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import build_site  # noqa: E402 -- reuses the exact movers logic the homepage uses

SERVICE_ACCOUNT_PATH = Path(
    os.environ.get("MARKETLAIR_SERVICE_ACCOUNT", str(SCRIPTS_DIR / "serviceAccountKey.json"))
)
LAST_ALERT_MARKER = ROOT / "data" / "last_alerted_snapshot.txt"
SITE_URL = build_site.SITE_URL  # same TODO-a-real-domain placeholder as the rest of the site

# Reverse of build_site.GAME_META: slug -> label, so a subscriber's
# followed-game slugs (copied from personalize.js's localStorage into
# Firestore by subscribe.js) can be matched back to a mover's game_label.
SLUG_TO_LABEL = {meta["slug"]: label for label, meta in build_site.GAME_META.items()}


def latest_snapshot_stamp():
    files = sorted(build_site.SNAP_DIR.glob("*.json"))
    return files[-1].stem if files else None


def already_alerted(stamp):
    return LAST_ALERT_MARKER.exists() and LAST_ALERT_MARKER.read_text().strip() == stamp


def mark_alerted(stamp):
    LAST_ALERT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    LAST_ALERT_MARKER.write_text(stamp)


def movers_for_subscriber(all_movers, followed_slugs, watched_keys=None):
    """Empty/missing followed_slugs means the subscriber hit "Skip, show
    me everything" (or subscribed before any game selection existed) --
    that's every mover, not none. watched_keys are card_key()s from the
    subscriber's client-side "Keep an eye on this" watchlist (synced into
    Firestore by subscribe.js) -- a watched card that clears this week's
    movers filter is included REGARDLESS of its game, on top of whatever
    the subscriber's followed-games selection already covers. A card only
    ever reaches watched_keys by actually clearing the movers filter in
    the first place (see compute_market_snapshot) -- this never alerts on
    every price tick, only on the same "significant move" bar as everyone
    else's digest."""
    watched_keys = watched_keys or set()
    if not followed_slugs:
        game_matches = all_movers
    else:
        labels = {SLUG_TO_LABEL.get(s) for s in followed_slugs}
        game_matches = [m for m in all_movers if m["game_label"] in labels]

    if not watched_keys:
        return game_matches

    watch_matches = [m for m in all_movers if m.get("key") in watched_keys]
    seen = set()
    combined = []
    for m in game_matches + watch_matches:
        if m["key"] in seen:
            continue
        seen.add(m["key"])
        combined.append(m)
    return combined


def build_message(movers):
    top = movers[0]
    direction = "up" if top["pct"] > 0 else "down"
    arrow = "\U0001F4C8" if direction == "up" else "\U0001F4C9"
    if len(movers) == 1:
        body = f'{top["name"]} moved {top["pct"]:+.1f}% (${top["old_price"]:.2f} -> ${top["new_price"]:.2f})'
    else:
        body = f'{top["name"]} {top["pct"]:+.1f}% and {len(movers) - 1} more card(s) you follow just moved'
    title = f"{arrow} MarketLair: weekly price alert"
    return title, body


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="preview only, sends nothing")
    mode.add_argument("--test", metavar="TOKEN", help="send ONLY to this one FCM token")
    mode.add_argument("--send", action="store_true", help="send to every real subscriber")
    p.add_argument("--force", action="store_true",
                    help="send anyway even if this snapshot failed the data-health check")
    return p.parse_args()


def main():
    args = parse_args()
    dry_run = args.dry_run

    stamp = latest_snapshot_stamp()
    if not stamp:
        print("No snapshots yet -- run fetch_snapshot.py first.")
        return
    if args.send and already_alerted(stamp):
        print(f"Already sent alerts for snapshot {stamp} -- nothing to do. "
              "(Delete data/last_alerted_snapshot.txt to force a resend.)")
        return

    snapshot = build_site.compute_market_snapshot()
    if snapshot is None:
        print("No snapshots yet -- run fetch_snapshot.py first.")
        return

    # Check data health BEFORE the "no movers" shortcut -- a broken/partial
    # pull can just as easily produce zero movers (which would otherwise
    # look like a quiet, uneventful week and sail through silently) as it
    # can produce spurious ones. The coverage-ratio problem is orthogonal
    # to whether anything happened to clear the mover threshold.
    health_warning = snapshot["stats"].get("data_health_warning")
    if health_warning and not args.force:
        print(f"REFUSING TO SEND -- data health check failed:\n  {health_warning}\n"
              "Investigate the snapshot before publishing or alerting. If this really is "
              "fine, re-run with --force.")
        return

    if not snapshot["gainers"] and not snapshot["losers"]:
        print("No movers cleared the noise filter this week -- no alerts to send.")
        if args.send:
            mark_alerted(stamp)
        return

    all_movers = snapshot["gainers"] + snapshot["losers"]

    print(f"Snapshot: {stamp}  |  {len(all_movers)} mover(s) past the filter")
    for m in sorted(all_movers, key=lambda m: abs(m["pct"]), reverse=True)[:5]:
        print(f"  {m['name']} ({m['game_label']}): {m['pct']:+.1f}%")

    if not SERVICE_ACCOUNT_PATH.exists():
        if dry_run:
            print(f"\n[dry-run] No service account key found at {SERVICE_ACCOUNT_PATH} -- can't "
                  "read real subscribers, but the movers data above is exactly what would be "
                  "evaluated once credentials are in place.")
            return
        print(f"\nMissing {SERVICE_ACCOUNT_PATH}. Generate a service account key in the Firebase "
              "console (Project settings -> Service accounts -> Generate new private key), save "
              "it there (or point MARKETLAIR_SERVICE_ACCOUNT at it), and re-run.")
        return

    import firebase_admin
    from firebase_admin import credentials, firestore, messaging

    cred = credentials.Certificate(str(SERVICE_ACCOUNT_PATH))
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    subscribers = list(db.collection("push_subscribers").stream())
    print(f"{len(subscribers)} subscriber(s) on file.")

    # Build the (token, relevant movers) list once, shared by every mode --
    # dry-run/test/send all evaluate targeting identically, only the actual
    # network send differs.
    targets = []
    skipped = 0
    per_game_counts = {}
    watchlist_assisted = 0
    for doc in subscribers:
        data = doc.to_dict() or {}
        token = data.get("token", doc.id)
        followed = data.get("followedGames") or []
        watched = set(data.get("watchedCards") or [])
        relevant = movers_for_subscriber(all_movers, followed, watched)
        if not relevant:
            skipped += 1
            continue
        # Did the watchlist pull in anything this subscriber's followed
        # games wouldn't have surfaced on their own? Reporting-only, not
        # part of targeting -- lets --dry-run/--send show whether the
        # feature is actually doing anything for real subscribers.
        game_only = movers_for_subscriber(all_movers, followed)
        if watched and len(relevant) > len(game_only):
            watchlist_assisted += 1
        relevant.sort(key=lambda m: abs(m["pct"]), reverse=True)
        targets.append((doc.id, token, relevant))
        for slug in (followed or ["(unfiltered)"]):
            per_game_counts[slug] = per_game_counts.get(slug, 0) + 1

    if args.test:
        targets = [t for t in targets if t[1] == args.test]
        if not targets:
            print(f"No matching subscriber for --test token (not subscribed, or no movers "
                  "in their followed games this week).")
            return

    if dry_run:
        for doc_id, token, relevant in targets:
            title, body = build_message(relevant)
            print(f"[dry-run] would notify {token[:12]}...: {title} -- {body}")
        print(f"\n[dry-run] Would notify {len(targets)} subscriber(s), skip {skipped} "
              "(no movers in their followed games or watchlist). "
              f"{watchlist_assisted} of those got at least one extra mover from their watchlist. "
              "Nothing was sent.")
        return

    if args.send:
        print(f"\nAbout to send real notifications:")
        print(f"  Recipients: {len(targets)}  |  Skipped (no relevant movers): {skipped}")
        print(f"  Watchlist-assisted: {watchlist_assisted} (got a mover their followed games alone wouldn't have)")
        for slug, count in sorted(per_game_counts.items(), key=lambda kv: -kv[1]):
            print(f"    {slug}: {count}")
        confirm = input("\nType SEND to confirm: ")
        if confirm.strip() != "SEND":
            print("Not confirmed -- aborting, nothing sent.")
            return

    sent, stale_tokens = 0, []
    for doc_id, token, relevant in targets:
        title, body = build_message(relevant)
        message = messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body),
            data={"url": f"{SITE_URL}/"},
            webpush=messaging.WebpushConfig(
                fcm_options=messaging.WebpushFCMOptions(link=f"{SITE_URL}/")
            ),
        )
        try:
            messaging.send(message)
            sent += 1
        except messaging.UnregisteredError:
            stale_tokens.append(doc_id)
        except Exception as e:  # noqa: BLE001 -- one bad token shouldn't kill the run
            print(f"  !! failed for {token[:12]}...: {e}", file=sys.stderr)

    for token_id in stale_tokens:
        db.collection("push_subscribers").document(token_id).delete()
    if stale_tokens:
        print(f"Cleaned up {len(stale_tokens)} stale/unregistered token(s).")

    if args.send:
        mark_alerted(stamp)

    print(f"Done. Notified {sent} subscriber(s).")


if __name__ == "__main__":
    main()

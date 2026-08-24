# Turning on Firebase App Check (optional hardening)

The code side of this is already done -- `base.html` loads the App Check
compat SDK and `subscribe.js` activates it automatically as soon as
`window.APPCHECK_SITE_KEY` (in `base.html`) is non-empty. Nothing changes
for visitors until you finish the steps below; until then it's a silent
no-op, same as before this was added.

## What App Check actually buys you

Right now the Firestore security rules trust "possession of a real FCM
token" as proof a write is legitimate. That's reasonable, but the Firebase
config in `base.html` is public (it has to be -- it ships to every
browser), so in principle someone could hand-craft Firestore writes
without ever loading the real site. App Check adds a second, harder-to-fake
signal: every request also has to carry a fresh attestation token proving
it came from an actual page load of `proxylair.github.io/marketlair`,
generated via reCAPTCHA v3 running invisibly in the background (no
checkbox, no visitor-facing friction).

## Steps (Firebase console + Google Cloud console)

1. **Register a reCAPTCHA v3 site key.**
   Go to https://www.google.com/recaptcha/admin/create, pick "reCAPTCHA
   v3," and add the domain `proxylair.github.io`. This gives you a **site
   key** (public, safe to ship in `base.html`) and a **secret key** (do
   NOT put this anywhere in the repo -- Firebase's own backend holds it
   once you register the provider in step 2).

2. **Enable App Check in the Firebase console.**
   Project settings -> App Check -> find the MarketLair web app -> Register
   -> provider: reCAPTCHA v3 -> paste the site key from step 1.

3. **Paste the site key into the code.**
   In `templates/base.html`, replace:
   ```js
   window.APPCHECK_SITE_KEY = "";
   ```
   with:
   ```js
   window.APPCHECK_SITE_KEY = "your-recaptcha-v3-site-key-here";
   ```
   Rebuild (`python3 scripts/build_site.py`) and deploy as usual.

4. **Watch metrics BEFORE enforcing.**
   Firebase console -> App Check -> Firestore (and Cloud Messaging, if you
   want it covered too) has a "Metrics" tab showing verified vs.
   unverified request counts. Once real visitors have loaded the site with
   the new build for a few days and verified requests dominate, you're
   ready for step 5. Enforcing too early -- before the new build has
   actually reached most visitors -- would reject legitimate writes from
   anyone still on a cached old page load.

5. **Turn on enforcement.**
   Firebase console -> App Check -> Firestore -> "Enforce". Repeat for
   Cloud Messaging if desired. This is the step that actually makes App
   Check block unverified requests -- everything before it is
   observe-only.

## If something goes wrong

Enforcement is reversible with one click (App Check -> Firestore ->
"Unenforce") if legitimate traffic starts getting rejected -- e.g. if a
mistyped site key shipped, or a browser/extension blocks the reCAPTCHA
script. `subscribe.js` also degrades silently if the App Check SDK fails
to load or `activate()` throws (see `activateAppCheckIfConfigured()`), so
a bad key fails safe into "App Check effectively off" rather than breaking
subscribe/watchlist sync outright -- but enforcement in the console
overrides that client-side safety net, so double-check the site key before
flipping it on.

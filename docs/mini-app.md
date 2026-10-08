# Telegram Mini App

The optional Mini App provides a mobile daily dashboard, reviewed-food search,
measured-gram meal form, favorites, meal history, versioned edits/delete/undo,
seven-day recorded-energy bars and recent measured weights. It reads current
ledger revisions and shows missing nutrient data as unknown. Partial nutrient
sums remain labeled; no missing-day intake or weight is invented.

It runs in the existing worker process and is disabled by default. Enable it only
with a public HTTPS Mini App URL and an operator-configured reverse proxy. The
HTTP listener defaults to loopback. Publish only that listener through the existing
HTTPS proxy; do not expose SQLite, a development server or an unauthenticated API.
The bot's dashboard button must use the configured HTTPS URL. No secrets belong
in that URL or in Nginx access logs. The package's static assets contain no personal
records, authentication bypass, sample nutrition database or third-party analytics.

Authentication follows Telegram's official [Mini App launch-data HMAC
validation](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
The owner must launch it from their private Telegram chat. `initData` is checked
with constant-time HMAC comparison and a 15-minute age limit. Reopen the app when
the launch expires. Credentials are sent only in `X-Telegram-Init-Data`; they are
never stored in browser storage, placed in query strings, or written to HTTP logs.
The API rejects duplicated launch fields, future timestamps, wrong users, bots
and group contexts. API data and static responses disable caching and apply a
restrictive content-security policy. The browser renders user text as text.

Submitting a form creates a deterministic, namespaced negative synthetic update
in the ordinary authorized durable inbox. It does not advance the Telegram poll
cursor. The regular worker applies the command and sends the same durable receipt
to the private chat. The browser polls that receipt and displays it. No web code
writes a parallel meal ledger. A client UUID retries the same command idempotently;
reusing it for changed input is rejected. Only an opaque pending request identifier
is stored in session storage so a browser refresh can recover a queued receipt.
If delivery is slow or a connection drops, **Check receipt** and **Refresh** resume
that same request without creating another meal. An expired launch cannot discard
an already accepted pending request; reopen the app to authenticate again and
check its result. Saving is refused when the browser cannot preserve a retry
identifier. Closing or escaping a portion form discards its unsaved edit context.

The measured form asks the user to confirm they measured every amount. Edits carry
the displayed `M<number>r<revision>`; a stale form cannot change a newer revision.
Favorites and repeats also carry displayed versions. Estimated portions, including
repeats of earlier estimates, still require a fresh exact-revision approval on the
Telegram receipt. The Mini App explicitly hands that review back to chat. Goal
approvals, recovery check-ins and training details remain in the guided chat UI.

In chat, Repeat is a one-shot action for that delivered meal receipt. A second tap
on the same receipt cannot duplicate a meal or its estimate draft. To log the next
occurrence, use Repeat on the newest saved receipt or reopen the source meal.
Every repeated estimate still needs its own reviewed draft approval.

Offline verification is in `tests/test_miniapp_auth.py` and
`tests/test_miniapp_server.py`; it uses only synthetic launch signatures and data.
Opening this application in a normal browser without Telegram displays a launch
instruction and cannot reveal the diary. Production activation and a real Telegram
webview check require the HTTPS URL and reverse-proxy configuration.

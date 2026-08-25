# Phase 3.3 — Production Deployment Procedure

Production serves live orders from a hand-copied tree inside a venv's
`site-packages`, with no git checkout:

```
/var/www/flask-optiwar-ow-release-090525/venv/lib/python3.11/site-packages/flaskr/
```

Nothing has ever reconciled that tree against `main`, so the two have drifted
in **both** directions. Every deployment from here runs through
`deploy/deploy.py`, which makes the drift visible and refuses to destroy it.

## What the drift actually is

Measured 2026-08-11, all 34 `*.py` files hashed:

| | |
|---|---|
| identical to `main` | 22 |
| behind `main` (safe to deploy) | `acr.py`, `ai_client.py`, `chat_gateway.py` |
| **ahead of `main`** | `__init__.py`, `crm.py`, `mail.py`, `models.py`, `pricing.py`, `delhivery_union.py`, `missing_order_search.py`, `dashboard_admin_streamlit.py` |
| only in production | `country_iso.py` |

The "ahead" set is the important one. Production is running code from pull
requests that were opened and never merged — GA4 (#6), Google Customer Reviews
(#7), the admin ticket-notification retry (#3), the GMC legacy-category flag
(#5). **A whole-tree deploy from `main` would silently switch off live
features and delete a module.** That is why deployment is file-scoped.

Reconciling those files back into `main` is a separate task and should happen
soon; until it does, the guards below are what stand between a deploy and a
regression.

## Guards

**Provenance.** Before replacing a file, its running content is hashed and
looked up in this repository's history for that path. A match means production
is at some known commit and the deploy is a fast-forward. No match means the
box carries an edit that was never committed, and overwriting it would destroy
the only copy — the deployment refuses and asks for it to be committed first.

**Scope.** Only `DEPLOY_SET` in `deploy/deploy.py` is touched. Adding a file is
a deliberate edit that must then survive the provenance guard. Entries are
paths relative to the application root, so a template ships the same way a
module does — `templates/success.html` is in the set because it, not just
`models.py`, decides what the confirmation page tells a customer about their
money. Only the `*.py` members are byte-compiled.

**Build from git.** A dirty working tree or a `HEAD` behind `origin/main` is a
hard block. `py_compile` plus the full unit suite run before anything is
copied, and `py_compile` runs again on the box after the copy.

**Secrets stay out.** Configuration lives in the systemd unit and
`/etc/optiwar/optiwar-secrets.env`; the deploy never reads or writes either.

**Environment.** `REQUIRED_ENV` names the variables the deployed code reads.
Their presence is checked on the box by name — never by value — and a missing
one is a hard block. Without this, deploying code that reads
`os.environ.get(NAME, "")` onto a box that does not set `NAME` replaces a
working feature with an empty string and raises nothing.

## Reviewed drift

`REVIEWED_DRIFT` records running content that matches no commit but has been
read in full and is deliberately not being kept, keyed by md5. It is the only
way past the provenance guard, and adding an entry means writing down what the
production-only content was and why losing it is correct.

Its one entry today is `models.py`: production hardcodes a Google Maps browser
key where `main` reads `GOOGLE_MAPS_API_KEY` from the environment. That key is
now set in `/etc/optiwar/optiwar-secrets.env` and listed in `REQUIRED_ENV`, so
the deploy cannot proceed on a box that would lose it.

## The paid-order pipeline release

`models.py`, `payments.py` and the two new modules `paid_orders.py` and
`razorpay_events.py` carry one behavioural change: a paid order is applied by a
single function, `order_status` is append-only, and inventory moves once, on
payment. Notes specific to it:

- **No schema change.** Every column and table the pipeline writes —
  `order_status.source/manual_flag/note`, `sales_log`, `product_status_history`,
  `products.sold_out_at/status_changed_at/status_reason`,
  `payment_collector.uq_payment_ref` — is already present in production.
- **New modules.** Files absent from production must be listed in
  `NEW_IN_RELEASE`; absence is otherwise a block, so that a path typo cannot be
  mistaken for a new module. A rollback restores only what it replaced, leaving
  these behind — harmless, because the `models.py` that imports them is
  reverted with them.
- **Webhook.** `POST /razorpay/webhook` is verified with
  `RAZORPAY_WEBHOOK_SECRET` and rejects an unsigned or tampered delivery before
  parsing it. A smoke test asserts the route answers 405 to a `GET`: a
  `models.py` that failed to import `paid_orders` would 404 there while every
  page still answered 200, and a payment nobody records is money taken for an
  order that never ships. Registering the endpoint in the Razorpay dashboard is
  a manual step outside this tool.
- **The origin guard has to know about it.** Production runs `csrf_guard.py`
  with `CSRF_ENFORCE=true`, and it answers 403 to any POST without an
  Origin/Referer — which is every server-to-server delivery Razorpay makes. The
  endpoint is therefore listed in `CSRF_EXEMPT_ENDPOINTS` (its authentication is
  the HMAC over the raw body, not a cookie), `csrf_guard.py` is in the deploy
  set, and a second smoke test POSTs the route with no Origin and requires 400 —
  the signature check refusing an unsigned body, which only happens if the
  request reached the view. The first release of the pipeline shipped without
  this and the route was reachable by `GET` and 403 to Razorpay.

## Procedure

```bash
python3 deploy/deploy.py plan                 # writes nothing
python3 deploy/deploy.py migrate --confirm    # additive schema, on its own
python3 deploy/deploy.py apply --confirm      # backup, replace, restart, smoke
python3 deploy/deploy.py canary               # staff conversation + event proof
python3 deploy/deploy.py release --confirm    # all three, unattended
python3 deploy/deploy.py rollback --confirm   # previous release, one command
```

`plan` prints the running release, the old→new hash manifest, the files it is
*refusing* to touch, any schema DDL still pending, and a pre-deploy smoke
baseline to compare against.

`apply` copies each replaced file to `/root/deploy_releases/<timestamp>/` with
a manifest, and points `/root/deploy_releases/previous` at it, before writing
anything.

### Schema first, separately

`ensure_schema()` runs at app boot and will add five nullable columns and three
indexes. Both tables are tiny (6 and 2 rows), so the DDL is instant — but
`migrate` runs it deliberately *before* the restart anyway, so that a code swap
and a schema change are never the same event, and then re-reads
`information_schema` to prove the restart will find nothing left to do.

The list is not restated here or in `deploy.py`: it is read out of `acr.py`'s
`_AI_EVENTS_EXTRA_COLS` / `_AI_EVENTS_EXTRA_IDX` / `_AI_ACTIONS_EXTRA_IDX`,
which `ensure_schema()` itself applies. A second copy is a copy that can
disagree, and a disagreement means `plan` reporting "nothing pending" while the
restart runs DDL — the one thing this step exists to rule out.
`tests/test_deploy_migration.py` holds that wiring in place.

Every item is additive and nullable, so it is compatible in both directions:
pre-Part-B code neither reads nor writes the new columns, which is why a
rollback leaves them alone.

### Unattended release

`release` runs migrate → apply → canary as one operation and **acts on the
verdict at every step**. Neither `apply` nor `canary` used to: both printed the
rollback command and stopped, which is fine with an operator watching and
useless at 02:00 with nobody there — the new code is already live and serving by
then. `apply` now restores the previous release itself when the service fails to
come up or any smoke test fails.

| canary | `release` does |
|---|---|
| 0 | leaves it live; the observation window starts |
| 1 — the release is at fault | rolls back automatically, code only |
| 2 — no evidence | leaves the live, smoke-clean release alone and reports |

Exit 2 must not roll back: reverting a healthy release because the model
provider was briefly busy during the deploy window is both a likely and a bad
outcome.

`release` itself exits `0` proven, `1` failed and recovered, `2` live but
unproven, and **`3` the storefront is not serving and the rollback did not fix
it** — the one case that needs a person on the box. `rollback` reports the same
way: it re-runs the smoke suite after restoring and returns non-zero if the
service did not come back, so "restored" is verified rather than assumed.

### Restart window

Orders over the last 90 days, by hour (IST): none at all between 23:00 and
07:59; the block runs 08:00–17:00 and peaks 12:00–16:00. Nginx hit rate is flat
and bot-dominated, so it is not a useful signal. **Restart between 02:00 and
05:00 IST.**

### Smoke tests

Run from the production host, so they traverse nginx and gunicorn as a customer
would: `.com` and `.in` home, product listing, login page, checkout redirect
for an anonymous visitor, `/support/status`, `/ops` rejecting anonymous access
with 401, plus `systemctl is-active` and a scan of the journal for tracebacks
and failed worker boots.

### Canary

`canary` enrols a staff browser via the signed `ow_acr_canary` cookie
(`ACR_CANARY_ONLY=true`, so shoppers are unaffected), drives a conversation
through start → message → action-result, then reads `ai_events` back and
reports which of the canonical events appeared:

```
SESSION_STARTED  MODEL_CALL  RECOMMENDATION_GENERATED
NAVIGATION_OFFERED  ACTION_CONFIRMED  ACTION_EXECUTED
```

It also reports any legacy `AI_*` names still being written, which is what
starts the canonical-vs-legacy reconciliation window.

Run against production *before* deployment, it returns 1 and gives the baseline
the deployment has to change:

```
SESSION=chat_c818bf3356ad48e3   ACTION=f07fc06dbb3c4e54926cf4ecbc24ace1
  SESSION_STARTED  MODEL_CALL  RECOMMENDATION_GENERATED
  NAVIGATION_OFFERED  ACTION_CONFIRMED  ACTION_EXECUTED     all MISSING
  legacy in last 7 days:  AI_ACTION_PROPOSED 4   AI_ACTION_COMPLETED 3
```

The action path ran — the canary's own turn is what moves those legacy counters,
which stood at 2/2 before the first run — and produced no canonical events. That
is the deployment gap stated as evidence rather than inference. After deployment
the same command must return 0 with all six populated.

Exit codes distinguish the two things an operator must not confuse:

| code | meaning |
|---|---|
| 0 | all six canonical events written — the deployment is proven |
| 1 | the canary ran and the release is bad — events missing, a chat endpoint non-200, or a malformed/absent `session_id` in a 200 |
| 2 | the canary could not run (enrolment 401, no cookie, `ai_events` query failed, transport failure, or a retryable `AI_TEMPORARILY_UNAVAILABLE` 503) — no evidence either way |

`1` is grounds to roll back; `2` means fix the canary and re-run. The split has
to cut in both directions. A failed staff enrolment silently disables the ACR
action path, which reads identically to a broken release — hence `2`. But a
non-200 from `/api/chat/start`, `/api/chat/message` or `/api/chat/action-result`
is the deployed code failing, and it is the *only* check that exercises it: the
smoke suite makes no chat request, and the deploy set is exactly those three
chat files. That must never be filed as inconclusive.

One exception, because the app says so itself: a 503 carrying
`AI_TEMPORARILY_UNAVAILABLE` is `ai_client.unavailable_contract()` shedding
load, which the widget soft-retries. The canary retries it twice before giving
up and then reports `2` — a busy model provider is not a bad release.

## Rollback

```bash
python3 deploy/deploy.py rollback --confirm
```

Restores the previous release's files and restarts. **Code only, deliberately:**
schema additions are additive and forward-compatible, and security hardening
must never be undone by a rollback.

## Out of scope for this deployment

Razorpay credential rotation and the move out of the world-readable systemd
unit are a separate controlled change. Combining a credential rotation with the
first standardized deployment would make any failure ambiguous.

`LIVE_HANDOVER_ENABLED` stays `false` at both Optiwar and KET. Step 5 stays
dry-run only, with no cron.

## Standing up a new node

The tool holds no host-specific logic. `OPTIWAR_DEPLOY_HOST`, `OPTIWAR_APP_DIR`,
`OPTIWAR_SERVICE`, `OPTIWAR_DB` and `OPTIWAR_RELEASES` are the whole surface, so
a second node needs only: a checkout at the release commit, Python
dependencies, `/etc/optiwar/optiwar-secrets.env`, `migrate --confirm`, the
service unit, shared data services, and `release --confirm`.

One real obstacle remains, and it is not in this tool. Four files hold live
credentials inline in `/var/www` that exist nowhere in git — `pricing.py`,
`delhivery_union.py`, `missing_order_search.py`, `dashboard_admin_streamlit.py`
— while `main` reads them from environment variables that are **not set** on the
box. A node built from the repository today would come up with an empty
Delhivery token, pricing secret and admin database password. Until those are
externalised for real, production cannot be reconstructed from git alone.

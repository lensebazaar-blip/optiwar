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
a deliberate edit that must then survive the provenance guard.

**Build from git.** A dirty working tree or a `HEAD` behind `origin/main` is a
hard block. `py_compile` plus the full unit suite run before anything is
copied, and `py_compile` runs again on the box after the copy.

**Secrets stay out.** Configuration lives in the systemd unit and
`/etc/optiwar/optiwar-secrets.env`; the deploy never reads or writes either.

## Procedure

```bash
python3 deploy/deploy.py plan                 # writes nothing
python3 deploy/deploy.py apply --confirm      # backup, replace, restart, smoke
python3 deploy/deploy.py canary               # staff conversation + event proof
python3 deploy/deploy.py rollback --confirm   # previous release, one command
```

`plan` prints the running release, the old→new hash manifest, the files it is
*refusing* to touch, any schema DDL still pending, and a pre-deploy smoke
baseline to compare against.

`apply` copies each replaced file to `/root/deploy_releases/<timestamp>/` with
a manifest, and points `/root/deploy_releases/previous` at it, before writing
anything.

### Schema first, separately

`ensure_schema()` runs at app boot and will add five nullable columns to
`ai_events` plus an index on `ai_actions`. Both tables are tiny (6 and 2 rows),
so the DDL is instant — but run it deliberately *before* the restart anyway, so
that a code swap and a schema change are never the same event. `plan` lists
exactly what is outstanding.

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

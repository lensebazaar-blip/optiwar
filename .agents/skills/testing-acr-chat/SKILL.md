---
name: testing-acr-chat
description: Run the Optiwar AI chat widget end-to-end locally (real chat_gateway backend + real chat-widget.js) against a local MariaDB with a scripted LLM. Use when verifying AI chat / ACR action-integrity, fallback buttons, action-result recording, the read-only Ops Console, the QC conversation-review layer, action lifecycle/expiry sweeps, or Part-B canonical ai_events emission.
---

# Testing the Optiwar AI chat widget (ACR) locally

The full Flask app needs the production DB/catalog/services, so testing the widget
end-to-end is done with a **thin harness** that imports only `chat_gateway` (+ `acr`)
and stubs the heavy bits. This gives a real browser flow while only the LLM is faked.

## One-time environment setup
- `pip install pymysql openai` (widget imports `openai` at module load; MariaDB via PyMySQL shim — no build tools needed).
- `sudo apt-get install -y mariadb-server && sudo service mariadb start`
- Create DB + minimal tables the widget queries (the ACR `ai_actions`/`ai_events` tables are auto-created by `acr.ensure_schema`):
  - `optiwar2` DB, user `oslb6`.
  - `chat_sessions(session_id PK, customer_id, contact_email, contact_name, status, current_page_url, created_at, last_activity, resolved_at)`
  - `chat_messages(id AUTO PK, session_id, source, role, content, status, metadata, client_message_id, created_at, UNIQUE(session_id,source,client_message_id))`
  - `chat_events(id AUTO PK, session_id, event_type, payload, created_at)`

## Harness pattern (`harness_acr.py`, keep out of the repo)
- `pymysql.install_as_MySQLdb()` before importing anything.
- Register a stub `flaskr` package with `__path__=[repo]`, then stub `flaskr.mail`
  (`create_ticket_in_db`) and `flaskr.ops` (`_require_ops_auth`) so you don't pull the
  full app. Import `flaskr.acr` and `flaskr.chat_gateway` via `importlib`.
- Flask app with `template_folder=<repo>/templates`, `static_folder=<repo>/static`,
  `static_url_path=/static`; config `SECRET_KEY`, `MYSQL_*`, dummy `DEEPSEEK_API_KEY`.
- `cg.init_chat_gateway(app)` then **monkeypatch `cg._call_deepseek`** to a scripted
  function: on a non-confirmation message set `flask.g._chat_nav_products=[...2 products...]`
  and return an OFFER ("Would you like me to take you to these frames?") with NO
  `[ACTION:NAVIGATE]` tag; on `acr.is_confirmation(msg)` return plain text, also no tag.
  This exercises the seeded-pending-action + confirmation path exactly like the real bug.
- **To prove recommendation identity** (multi-product → filtered listing, not the generic
  catalogue): in the scripted LLM also set `flask.g._chat_nav_filters={"color":"black","shape":"aviator"}`.
  Then the confirmation `navigate_url` becomes
  `/eyeglasses/all-spectacle-frames.html?color=black&shape=aviator`. Make the landing route
  print `location.pathname+location.search` so the destination identity is visible on screen.
- **Local http gotcha:** the owner cookie is set `secure=True`, so over `http://localhost`
  the browser won't send it. Monkeypatch `cg._set_chat_owner_cookie` to set `secure=False`,
  else `/messages` polling and `/action-result` return `403`.
- Serve `/` (sets `window.__optiwarChat={email,name}` + includes the widget) and a landing
  route for `/eyeglasses/all-spectacle-frames.html`. Add `/harness-admin-login` that sets
  `session['user_email']='admin@optiwar.com'` and redirects to the Ops Console.
- **Run in a dedicated PERSISTENT shell** (`python3 harness_acr.py`, port 5001). Do NOT
  launch it via a `( ... & )` subshell / `nohup &` from a one-shot command — it gets reaped
  and the port goes dead (HTTP 000). Keep the process in a long-lived shell session and poll
  `curl -s -o /dev/null -w '%{http_code}' localhost:5001/` until it returns 200.

## Enabling ACR in the harness (required for the action + Part-B paths)
The ACR action block (pending action, NAVIGATION_OFFERED, ACTION_CONFIRMED, safe-URL gate) is
gated behind `_acr_enabled_for(email)`, which defaults OFF. Set in the harness Flask config:
`ACR_ACTIONS_ENABLED=True, ACR_CANARY_ONLY=False` (post-canary "on for all sessions" mode).
Without this the recommendation turn seeds no pending action and you'll only see
SESSION_STARTED + RECOMMENDATION_GENERATED.

## Critical gotcha — use a FRESH email per browser run
The widget calls `/status` first; if an **active session already exists for that email**
(e.g. created by an earlier `curl /start`), it **resumes without calling `/start`** and
never gets the owner cookie → `/action-result` 403 → Ops Console shows `CONFIRMED` instead
of `EXECUTED`. Always start a browser run with an email that has no prior session so the
widget itself calls `/start`. Check the harness log for `POST /api/chat/action-result 200`.

## Golden-path flow to record
1. Open `/`, open widget (Text Chat), type `show me black frames` → assert 2 frames + offer, **no navigation**.
2. Type `yes` → assert a blue **"▶ Open recommended frames"** button renders (not raw markdown) **and** browser navigates to the frames page. Assert the URL is the **filtered** listing (`?color=…&shape=…`), not the bare `all-spectacle-frames.html`.
3. **Arrival-based EXECUTED (finding #5):** grab the harness log and assert the destination `GET /eyeglasses/all-spectacle-frames.html?...` line **precedes** `POST /api/chat/action-result 200`. Reporting must be at arrival, not dispatch.
4. Open Ops Console (`/harness-admin-login`) → assert the row shows `NAVIGATE … EXECUTED`, `0 failures`, `0 promise-w/o-action`, badge `READ-ONLY (A3)`.
5. **Audit:** `SELECT * FROM ai_events WHERE event_type='OPS_CONSOLE_ACCESS'` should show `actor`/`ip`/`format` for each console load.

## Part-B canonical events (ai_events) — what to assert
After the golden path, `SELECT event_type,action_type,journey_stage,success,provider,model,duration_ms,payload FROM ai_events WHERE session_id=? ORDER BY created_at`.
Expect exactly (no duplicates) for one golden session:
- `SESSION_STARTED` ×1 (stage LANDING) — **create-only**; hitting `/start` again for the same
  email resumes and must NOT add a second row. This is the key no-double-count check.
- `RECOMMENDATION_GENERATED` ×1 — payload has immutable `skus` (product `code`s), `result_count`, `filters`.
- `NAVIGATION_OFFERED` ×1 — payload `target_path` is path-only (query stripped by `sanitize_url_for_event`).
- `ACTION_CONFIRMED` ×1 (single PENDING→CONFIRMED edge).
- `ACTION_EXECUTED` ×1 — success=1, **callback-only** (none before `/action-result`).
- `MODEL_CALL` — see injection note below.
- Typed columns `request_id/provider/model/workload/consent_scope` exist only after
  `ensure_schema` runs `_ensure_ai_events_columns`; starting the harness once adds them. If a
  DB lacks them, `log_event` falls back to the legacy column list (event still inserts).

### MODEL_CALL — inject telemetry (harness stubs the wrapper)
Because the harness monkeypatches `cg._call_deepseek`, the real `ai_client.call_model` wrapper
is bypassed, so `pop_calls()` is empty and no MODEL_* event fires. To exercise the web-layer
emission path end-to-end, inside the scripted LLM call
`flaskr.ai_client._record_call(kind="model_call", provider="deepseek", model="deepseek-chat", actual_model="deepseek-chat", workload="deepseek_chat", success=True, duration_ms=..., input_tokens=..., output_tokens=..., request_id=...)`.
Then `_emit_model_events` drains it → one `MODEL_CALL` per turn with provider/model/tokens.
The wrapper's own timeout/429/failure branches (`MODEL_TIMEOUT`/`ADMISSION_503`/`PROVIDER_FAILURE`)
are NOT reachable from the browser harness — cover them with `test_ai_wrapper`/`test_acr_part_b`.

### Safe-URL gate (UNSAFE_URL_REJECTED / ACTION_BLOCKED)
Make the scripted LLM return an off-site tag (e.g. on a trigger word:
`"... [ACTION:NAVIGATE:https://evil.example.com/phish]"`). Assert: the browser lands on the
**safe** `/eyeglasses/all-spectacle-frames.html` (server substitutes `FRAMES_LISTING_FALLBACK`),
`ai_events` has both `UNSAFE_URL_REJECTED` and `ACTION_BLOCKED` (`failure_code=unsafe_url`), and
a scan for the off-site host across `page_url`/`payload` returns **0 rows**.

### PII scan
`SELECT COUNT(*) FROM ai_events WHERE payload LIKE '%@%' OR payload LIKE '%reply_head%' OR payload LIKE '%Bearer%' OR page_url LIKE '%?%'` should be 0. NOTE: do not match `%token%` — it
false-positives on the legitimate `input_tokens`/`output_tokens` payload keys.

## Auth gate (finding #2 / Option A) — two-part
- The harness **stubs `flaskr.ops`**, so it does NOT exercise the real hardened `_require_ops_auth`.
  Verify the real one **directly**: stub `flaskr.db`+`flaskr.notifications`, `importlib.import_module('flaskr.ops')`,
  and assert against a Flask `test_request_context`: old default `Bearer optiwar-ops-2025` (no token configured)→False,
  any Bearer with no `OPS_API_TOKEN`→False (fail-closed), correct configured Bearer→True, admin session→True.
- `OPS_CONSOLE_AUTH_FAILURE` (Part B): an unauthenticated console hit writes a row with
  `success=0`, `failure_code=unauthorized`, payload `{"ip": ...}` ONLY (no actor, no token).
- **Demoing the 401 in the browser:** the Flask session cookie is signed with the harness `SECRET_KEY`
  (hardcoded, survives restarts), so a prior `/harness-admin-login` visit leaves the browser **already
  authenticated** and the console loads without a fresh login. To show the 401 gate on camera, clear the
  site's cookies mid-recording (Ctrl+Shift+Delete → "Cookies and other site data" → Delete data, or
  address-bar site-info → Cookies), then reload → `{"error":"unauthorized"}`.

## Making the QC layer visible on screen (`/harness-qc`)
QC (`acr_qc`) has no UI of its own, so a browser test of it otherwise reduces to reading a shell.
Add a harness-only route that renders `qc.review_window(db, hours=2)` as a signals table plus the
newest `ai_actions` rows, with severity colour-coded (FAIL red / WARN amber / INFO green). Two
traps, both of which produced misleading screens before they were fixed:
- The gateway hands out a **dict cursor**, so `for c in row` yields *column names*. The table will
  cheerfully render `session_id  action_id  status` in every row and look plausible. Index by
  name (`row[c]`) with a tuple fallback.
- Any `overdue`-style column you compute in the harness must use the **same expression as
  `acr_qc`**, or the table will contradict the verdict printed directly above it.
- The db helper is `cg._get_db()` (underscore-prefixed); there is no `cg.get_db`.

## Testing action-lifecycle states through the UI
- **A genuinely stranded confirmation** (customer says yes, never arrives): send the confirmation,
  then `ctrl+w` the tab immediately. The auto-navigate fires ~1.5s after the reply
  (`chat-widget.js`), so closing the tab beats it and the destination never posts
  `/action-result`. This is a real customer behaviour, not a hand-written DB row — much better
  evidence than inserting a `CONFIRMED` row yourself. **Verify by absence:** grep the harness log
  and assert no `POST /api/chat/action-result` for that `session_id`.
- **Ageing an action** for expiry/staleness tests: which column you move matters, because they
  answer different questions. `expires_at` is the *offer's* deadline and drives
  `expire_due_actions`; `resolved_at` is stamped at confirmation and drives QC's in-flight window
  (`acr_qc.EXECUTION_TTL_SECONDS`). To show "QC now reports it" move `resolved_at`; to show "the
  sweep terminates it" move `expires_at`. Moving only one and expecting both to change is a
  common false conclusion.
- **Assert the sweep does not erase the defect.** After `expire_due_actions(dry_run=False)` the row
  leaves `CONFIRMED`, so a QC layer that counts only live rows would silently go green. Re-load the
  QC view *after* the sweep and assert the signal is still reported, exactly once.
- `ai_actions` has **no `failure_code` column** — the failure code lives on the `ai_events`
  `ACTION_EXPIRED` row, together with `payload {"from_status": ...}`. Selecting it from
  `ai_actions` fails with `Unknown column 'failure_code'`.
- The sweep is global, so it will also expire **unrelated stale rows** left by earlier runs. Note
  which rows were pre-existing before claiming your action caused a count to change.

## Live production staff-only canary (real site, real LLM)
When testing the deployed canary on `optiwar.com`/`optiwar.in` (not the local harness):
- **Gate/enrolment:** ACR runs only for enrolled sessions when `ACR_ACTIONS_ENABLED=true` + `ACR_CANARY_ONLY=true`. Enrol your browser by minting the signed `ow_acr_canary` cookie via the admin endpoint (`/api/chat/admin/acr-canary?on=1`, needs admin session or `OPS_API_TOKEN` Bearer). Ordinary customers stay on the legacy path.
- **The live prompt front-loads NAVIGATE.** Unlike the local harness (offer turn → `yes` → navigate), the production system prompt emits the NAVIGATE target *on the recommendation turn itself*. So the positive flow is effectively single-turn: desktop auto-navigates ~1.5s after the recommendation; a logged-in session may instead render the `▶ Open recommended frames` fallback button you must click. Do NOT expect a two-turn "wait then yes" — test what prod does and cover the bare-`yes` pending path via server-side + unit evidence instead.
- **Customer-facing chat UI requires login.** The floating widget's Text Chat and `/search` redirect/deny unauthenticated guests ("Please log in to continue") even though API `/api/chat/start` works for a guest with the canary cookie. Register a throwaway account through the live signup form (has a CAPTCHA) to drive the UI.
- **Session/domain isolation:** clear cookies + localStorage + sessionStorage and re-enrol the canary cookie for the specific domain before switching `.com`↔`.in`, or the widget resumes the old session and shows stale cross-domain content (EUR on `.in`, etc.). This is a test-harness contamination risk, not necessarily a cross-customer bug — report it as such.
- **Negative / handover / expiry canaries that reliably reproduce on prod:**
  - Negative confirmation: use the *ticket* offer (`my order arrived damaged and I want a refund` → "create a support ticket? Yes/No" → `no`). Nav offers front-load the tag so a `no` on a nav offer isn't reproducible.
  - Handover isolation: `I want to talk to a human agent` → `yes` should create a ticket + connect to supervisor with **NO stale navigation** to any earlier recommendation. Note this creates a REAL ticket (e.g. `OPTIWA-1017`) that may notify staff/KET — flag it, don't assume it's harmless.
  - Expired action: do it server-side against the prod DB on a scratch `session_id` — `acr.create_pending_action(...)`, `UPDATE ai_actions SET expires_at=NOW()-INTERVAL 1 MINUTE`, then assert `acr.get_live_pending_action(...)` returns `None` (control: a fresh one returns live). Clean up the scratch `ai_actions` **and** `ai_events` rows after.
- **Server-side evidence (prod host):** the venv uses real `MySQLdb` (not pymysql). Get DB creds from the running gunicorn process without printing them: `export $(tr '\0' '\n' < /proc/$(pgrep -f gunicorn|head -1)/environ | grep -E '^MYSQL_(HOST|USER|PASSWORD|DB)=' | xargs -d '\n')`, then read `MYSQL_*` from `os.environ`. `ai_actions` columns: `action_id, session_id, action_type, target, status, source_message_id, result_code, duration_ms, created_at, resolved_at, expires_at` (no `updated_at`).
- **Arrival-based EXECUTED on prod:** in nginx `access.log`, assert the destination `GET /eyeglasses/all-spectacle-frames.html?color=...` line **precedes** the `POST /api/chat/action-result` line; `ai_actions.status` should be `EXECUTED`. Post-Part-B, `ai_events` uses the canonical names (`NAVIGATION_OFFERED`/`ACTION_CONFIRMED`/`ACTION_EXECUTED`), while historical rows may still carry legacy `AI_ACTION_*` names — the report keeps a temporary alias. State `EXECUTED = destination loaded and reported the action result`; prove commercial usability separately (expected filters present, ≥1 in-stock tile, correct host currency).
- **Recording survives restarts as raw segments** under `/home/ubuntu/screencasts/<name>/*.mkv`. Concatenate with an ffmpeg concat file using **absolute paths**; if segments differ in fps, re-encode each to a common fps/`yuv420p` first, then `-c copy` concat.

## Devin Secrets Needed
- Local harness: none (LLM stubbed, DB local).
- Live production canary: SSH access to the prod host (`root@172.105.54.11`) for server-side DB/log checks and the `OPS_API_TOKEN` (root-only `/etc/optiwar/optiwar-secrets.env`) to mint the canary cookie / hit `/ops` Bearer endpoints. Never print the token.

---
name: testing-acr-chat
description: Run the Optiwar AI chat widget end-to-end locally (real chat_gateway backend + real chat-widget.js) against a local MariaDB with a scripted LLM. Use when verifying AI chat / ACR action-integrity, fallback buttons, action-result recording, or the read-only Ops Console.
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
- **Local http gotcha:** the owner cookie is set `secure=True`, so over `http://localhost`
  the browser won't send it. Monkeypatch `cg._set_chat_owner_cookie` to set `secure=False`,
  else `/messages` polling and `/action-result` return `403`.
- Serve `/` (sets `window.__optiwarChat={email,name}` + includes the widget) and a landing
  route for `/eyeglasses/all-spectacle-frames.html`. Add `/harness-admin-login` that sets
  `session['user_email']='admin@optiwar.com'` and redirects to the Ops Console.
- Run a foreground process in a dedicated shell (`python3 harness_acr.py`), port 5001.

## Critical gotcha — use a FRESH email per browser run
The widget calls `/status` first; if an **active session already exists for that email**
(e.g. created by an earlier `curl /start`), it **resumes without calling `/start`** and
never gets the owner cookie → `/action-result` 403 → Ops Console shows `CONFIRMED` instead
of `EXECUTED`. Always start a browser run with an email that has no prior session so the
widget itself calls `/start`. Check the harness log for `POST /api/chat/action-result 200`.

## Golden-path flow to record
1. Open `/`, open widget (Text Chat), type `show me black frames` → assert 2 frames + offer, **no navigation**.
2. Type `yes` → assert a blue **"▶ Open recommended frames"** button renders (not raw markdown) **and** browser navigates to the frames page.
3. Open Ops Console (`/harness-admin-login`) → assert the row shows `NAVIGATE … EXECUTED`, `0 failures`, `0 promise-w/o-action`, badge `READ-ONLY (A3)`.

## Devin Secrets Needed
- None. LLM is stubbed; DB is local. (Real live-site testing would need `DEEPSEEK_API_KEY`/`OPENAI_API_KEY` and DB access, but that is not required for this local harness.)

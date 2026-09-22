# Optiwar — Pendency Register

Single source of truth for open work. The daily report's PENDENCY REGISTER
section is rendered from this table (`reports/pendency_register_section.py`);
edit here, nowhere else. Close an item by setting Status to `DONE` (it drops
out of the report) — do not delete rows.

Status: `OPEN` | `BLOCKED` | `WAITING` (third party) | `DONE`.

| ID | Item | Owner | Status | Since | Note |
|----|------|-------|--------|-------|------|
| PR-1 | `face_scan_done` WhatsApp template — Meta review | Meta / MSG91 | WAITING | 2026-09-17 | template_id 1056767667255863. Do not resubmit variants. On approval validate variable order, one notice per scan/group, idempotency, delivery + callback before broad use. |
| PR-2 | MSG91 delivery webhook — provider side | Owner | OPEN | 2026-09-16 | Code complete (idempotent, forward-only, face-scan invites folded). Resume/point the MSG91 webhook at `/support/msg91_delivery_event` with `MSG91_DELIVERY_TOKEN`. |
| PR-3 | ACR Step-5 observability — commerce outcome events | Devin | OPEN | 2026-09-22 | ai_events now has SESSION_RESUMED / SESSION_NOT_FOUND / JOURNEY_STAGE. Still pending: cart/payment/purchase attribution rows (ai_session_commerce, ai_session_outcomes empty), quality score. |
| PR-4 | ACR Step-5 closure job — 2 DDL steps + batch=10 canary | Owner approval | BLOCKED | 2026-08-31 | Runs only on approval; acr_closure_job not installed on production. |
| PR-5 | Contact-lens pilot — 4 lenses on .com, absent from .in | Owner data | BLOCKED | 2026-09-09 | Needs price_eur, ships_within, minimums per lens. merchant_enabled stays 0 until each release is confirmed. |
| PR-6 | Contact-lens photography — 249 cartons in /root/lens_sources/incoming | Devin (on "go") | OPEN | 2026-09-21 | Parked by owner. One sample done (Aspire Pro Multifocal). No import until owner says go. |
| PR-7 | PR3 lens validator — `lens_order.validate_detailed` + Lens Intelligence card | Devin | OPEN | 2026-09-10 | Own tracked item; not to be folded into other PRs. |
| PR-8 | Face Assistant rollout | Owner | BLOCKED | 2026-09-22 | Keep OFF until FACE-D stable, face-action telemetry, remote scan health, lock proven. First for lensebazaar@gmail.com only. |
| PR-9 | Stale open-status chat sessions — retention rule | Owner | OPEN | 2026-09-22 | ~120 `chat_sessions.status='active'` older than 24h (mostly deploy-canary). Nothing purged until a rule is set. |
| PR-10 | AI model cost basis undeclared | Owner | OPEN | 2026-09-22 | ai_model_registry cost_basis = UNDECLARED; report shows n/a until provider invoice rates are entered. |
| PR-11 | Daily report: read-only DB user lacks face tables | Owner | OPEN | 2026-09-22 | GRANT SELECT on face_measurements, face_demand_log to the report user for the frame-only Face Demand section. |
| PR-12 | Reverse-pickup charge rule for returns console | Owner | OPEN | 2026-09-16 | Built as per-case entry; confirm or fix an amount per site. |
| PR-13 | KET integration questions (9) to support.ket.ltd | Owner | OPEN | 2026-09-16 | Not blocking. |
| PR-14 | Secret rotation + EnvironmentFile (P1-1) | Owner | OPEN | 2026-08-05 | Cleartext secrets in gunicorn unit; bulk rotation deferred. |

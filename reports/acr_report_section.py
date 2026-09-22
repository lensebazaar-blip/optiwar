#!/usr/bin/env python3
"""ACR AI Operations — Daily Report section.

Appended to the Optiwar 06:00 Operations Report, mirroring the GMC section
contract: expose ``build()`` returning the section as a string and ``print`` it
on ``__main__`` so ``run_daily_report.sh`` can append the stdout.

Layered for a mixed audience (executive -> operations -> business -> quality ->
engineering):

  Layer 1  Executive Summary          (30-second health read)
  Layer 2  Operations funnel          (where customers leak)
  Layer 3  AI Business Intelligence    (commercial signal)
  Layer 4  AI Quality                  (conversation quality)
  Layer 5  Engineering                 (technical metrics)
  Layer 6  Reconciliation              (every AI-activity count, with its source,
                                        population and window, side by side)

Sourcing: the canonical ACR event stream (``ai_events``) and action ledger
(``ai_actions``) are the authority for every AI figure. The legacy chat tables
(``chat_sessions`` / ``chat_events``) and the AI wrapper's ``ai_metrics.log``
are shown only in the reconciliation layer, labelled as what they are, so two
numbers that describe different populations are never presented as one.

A metric whose canonical event has never been written on this database (the
code that emits it is not deployed yet, or a job that writes it is not
scheduled) renders as ``n/a (...)`` with the reason, never as a fabricated 0.

Data protection: this is a broad-distribution email. It emits only counts,
rates, statuses, non-identifying IDs, SKUs and product titles. It never emits
raw customer messages, transcripts, prescription values, face measurements,
emails/names/phones, secrets, prompts or model reasoning.

Config (all optional; safe defaults): DB connection is read from the
environment (never inlined) — set ``ACR_REPORT_DB_*`` or fall back to the
standard ``MYSQL_*`` vars. Thresholds may be overridden via ``ACR_REPORT_*``
env vars. Intended to run behind an ``ACR_REPORT_ENABLED`` flag in the
orchestrator.
"""
import glob
import gzip
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

# Imported by directory rather than by package: this file is executed as a
# script by the report orchestrator, and resolving a sibling through the cwd is
# what once let a stale copy of this section win over the current one.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from report_db import NA, SqlError, run_sql, scalar, to_int  # noqa: E402

WIDTH = 70
BANNER = "=" * WIDTH
WINDOW_HOURS = int(os.environ.get("ACR_REPORT_WINDOW_HOURS", "24"))

# Status vocabulary
GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
_ORDER = {GREEN: 0, AMBER: 1, RED: 2}

# Instrumentation coverage below this percentage means the health verdict is
# built on too little of the picture to be asserted as GREEN. Reporting a
# confident all-clear while most metrics are n/a is the same false-green
# failure mode as an executive summary that ignores its own subsections.
MIN_COVERAGE_PCT = float(os.environ.get("ACR_REPORT_MIN_COVERAGE_PCT", "60"))

# The canonical event exists in code but has never been written on this
# database: the emitting release is not deployed, or the job that writes it is
# not scheduled. Distinct from NA (never instrumented) and from a true zero.
NA_NOT_EMITTED = "n/a (event not yet emitted on this database)"
NA_LEDGER = "n/a (closure job not scheduled — ledger empty)"
NA_COST = "n/a (cost basis undeclared in ai_model_registry; needs provider invoice)"
NA_NO_SIGNAL = "n/a (no canonical signal exists yet)"
# Any n/a variant starts like this; coverage counts rendered rows by it.
NA_PREFIX = "n/a ("

# Sessions the deployment canary creates on every release. They are real rows
# in every table and are shown, but separately, so a release day does not read
# as a busy day.
CANARY_EMAIL_LIKE = "deploy-canary%"

# The AI wrapper's durable per-round-trip log (debug-only cross-check; the
# canonical MODEL_CALL event is the authority). logrotate renames it at ~03:30,
# so a 06:00 read of the live file alone sees 2.5 hours — the rotated file for
# the window has to be read too.
AI_METRICS_LOG = os.environ.get("ACR_REPORT_AI_METRICS_LOG",
                                "/var/log/optiwar/ai_metrics.log")

# Aliases: the local names these were introduced under, so a call site does not
# change because the implementation moved to the shared reader.
_scalar, _to_int = scalar, to_int


def _host_case(col):
    """Derive .com / .in from a URL column. Values seen in prod include
    optiwar.com, in.optiwar.com and optiwar.in."""
    return ("CASE "
            "WHEN %s LIKE '%%optiwar.in%%' THEN '.in' "
            "WHEN %s LIKE '%%in.optiwar.com%%' THEN '.in' "
            "WHEN %s LIKE '%%optiwar.com%%' THEN '.com' "
            "ELSE 'unknown' END" % (col, col, col))


HOST_CASE = _host_case("current_page_url")
EVENT_HOST_CASE = _host_case("page_url")
SINCE = "NOW() - INTERVAL %d HOUR" % WINDOW_HOURS

# Canonical event names (kept literal here: this section runs outside the app
# package, on the report host).
EV_SESSION_STARTED = "SESSION_STARTED"
EV_SESSION_RESUMED = "SESSION_RESUMED"
EV_SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
EV_JOURNEY_STAGE = "JOURNEY_STAGE"
EV_RECOMMENDATION_GENERATED = "RECOMMENDATION_GENERATED"
EV_NAVIGATION_OFFERED = "NAVIGATION_OFFERED"
EV_FACE_ACTION_OFFERED = "FACE_ACTION_OFFERED"
EV_ACTION_CONFIRMED = "ACTION_CONFIRMED"
EV_ACTION_EXECUTED = "ACTION_EXECUTED"
EV_ACTION_FAILED = "ACTION_FAILED"
EV_ACTION_BLOCKED = "ACTION_BLOCKED"
EV_ACTION_EXPIRED = "ACTION_EXPIRED"
EV_PROMISE_WITHOUT_ACTION = "PROMISE_WITHOUT_ACTION"
EV_UNSAFE_URL_REJECTED = "UNSAFE_URL_REJECTED"
EV_MODEL_CALL = "MODEL_CALL"
EV_MODEL_TIMEOUT = "MODEL_TIMEOUT"
EV_ADMISSION_503 = "ADMISSION_503"
EV_PROVIDER_FAILURE = "PROVIDER_FAILURE"
EV_HANDOVER_ESCALATED = "HANDOVER_ESCALATED"
EV_KET_TICKET_CREATED = "KET_TICKET_CREATED"
EV_SESSION_OUTCOME = "SESSION_OUTCOME"
EV_COMMERCE_OUTCOME = "COMMERCE_OUTCOME"
EV_OPS_CONSOLE_AUTH_FAILURE = "OPS_CONSOLE_AUTH_FAILURE"

# chat_sessions.session_id and ai_events.session_id were created under
# different collations; a join between them must state one.
SESSION_JOIN = "s.session_id = e.session_id COLLATE utf8mb4_general_ci"

FUNNEL_STAGES = ("LISTING", "PRODUCT", "CHECKOUT", "PURCHASE")
FACE_ACTION_TYPES = ("FACE_SHOP_FOR", "FACE_DEFAULT", "FACE_CART_LINE")


def _by_host(counts):
    com = counts.get(".com", 0)
    _in = counts.get(".in", 0)
    unk = counts.get("unknown", 0)
    return com, _in, com + _in + unk


def _q(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def _event_count(event_type, extra=""):
    return _to_int(_scalar(
        "SELECT COUNT(*) FROM ai_events WHERE event_type=%s AND created_at >= %s %s"
        % (_q(event_type), SINCE, extra)))


def _event_sessions(event_type, extra=""):
    return _to_int(_scalar(
        "SELECT COUNT(DISTINCT session_id) FROM ai_events WHERE event_type=%s "
        "AND created_at >= %s %s" % (_q(event_type), SINCE, extra)))


def _ever(event_type):
    """Has this canonical event ever been written here? Distinguishes a true 0
    in the window from an event nobody emits yet."""
    return _scalar("SELECT 1 FROM ai_events WHERE event_type=%s LIMIT 1"
                   % _q(event_type)) is not None


class NotEmitted(object):
    """A metric whose value is known to be absent for a stated reason. Renders
    as that reason; counts as pending (not live) for coverage."""

    def __init__(self, reason):
        self.reason = reason

    def __str__(self):
        return self.reason


def _gated(event_type, fn, reason=NA_NOT_EMITTED):
    """``fn()`` when the event has ever been emitted, else the stated gap."""
    if not _ever(event_type):
        return NotEmitted(reason)
    return fn()


# ─────────────────────────── metric collection ───────────────────────────

def _collect():
    """Return (metrics dict, errors list). Each metric may be an int, a
    (com, in, total) tuple, a float rate, a dict, a NotEmitted, or None for a
    query that failed."""
    m = {}
    errs = []

    def safe(key, fn):
        try:
            m[key] = fn()
        except SqlError as e:
            m[key] = None
            errs.append("%s: %s" % (key, e))

    # ── sessions (canonical) ──
    def sessions_started():
        rows = run_sql(
            "SELECT %s h, COUNT(*) FROM ai_events "
            "WHERE event_type=%s AND created_at >= %s GROUP BY h"
            % (EVENT_HOST_CASE, _q(EV_SESSION_STARTED), SINCE))
        return _by_host({r[0]: _to_int(r[1]) for r in rows})
    safe("sessions_started", sessions_started)

    safe("sessions_started_canary", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM ai_events e JOIN chat_sessions s ON %s "
        "WHERE e.event_type=%s AND e.created_at >= %s AND s.contact_email LIKE %s"
        % (SESSION_JOIN, _q(EV_SESSION_STARTED), SINCE, _q(CANARY_EMAIL_LIKE)))))

    safe("sessions_resumed", lambda: _gated(
        EV_SESSION_RESUMED, lambda: _event_count(EV_SESSION_RESUMED)))
    safe("sessions_not_found", lambda: _gated(
        EV_SESSION_NOT_FOUND, lambda: _event_count(EV_SESSION_NOT_FOUND)))

    # Sessions with any canonical activity in the window (the population every
    # per-session rate below is measured against).
    safe("sessions_active_window", lambda: _to_int(_scalar(
        "SELECT COUNT(DISTINCT session_id) FROM ai_events "
        "WHERE created_at >= %s AND session_id IS NOT NULL AND session_id<>''" % SINCE)))

    # Open-status rows: chat_sessions.status='active' is a current-state flag
    # with no expiry, not a measure of the window. Broken down so the number
    # can be read: how many are stale, how many are the deploy canary's.
    def open_status():
        row = run_sql(
            "SELECT COUNT(*), "
            "SUM(last_activity < %s), "
            "SUM(contact_email LIKE %s), "
            "SUM(customer_id IS NOT NULL), "
            "SUM(last_activity < NOW() - INTERVAL 7 DAY) "
            "FROM chat_sessions WHERE status='active'"
            % (SINCE, _q(CANARY_EMAIL_LIKE)))
        r = row[0] if row else ()
        return dict(total=_to_int(r[0]) if r else 0,
                    stale=_to_int(r[1]) if len(r) > 1 else 0,
                    canary=_to_int(r[2]) if len(r) > 2 else 0,
                    authenticated=_to_int(r[3]) if len(r) > 3 else 0,
                    older_7d=_to_int(r[4]) if len(r) > 4 else 0)
    safe("sessions_open_status", open_status)

    safe("customers_assisted", lambda: _to_int(_scalar(
        "SELECT COUNT(DISTINCT s.customer_id) FROM chat_sessions s "
        "JOIN ai_events e ON %s "
        "WHERE s.customer_id IS NOT NULL AND e.created_at >= %s" % (SESSION_JOIN, SINCE))))

    def guest_auth():
        rows = run_sql(
            "SELECT CASE WHEN s.customer_id IS NULL THEN 'guest' ELSE 'auth' END g, "
            "COUNT(*) FROM ai_events e JOIN chat_sessions s ON %s "
            "WHERE e.event_type=%s AND e.created_at >= %s GROUP BY g"
            % (SESSION_JOIN, _q(EV_SESSION_STARTED), SINCE))
        d = {r[0]: _to_int(r[1]) for r in rows}
        return d.get("guest", 0), d.get("auth", 0)
    safe("guest_auth", guest_auth)

    # ── recommendations / actions (canonical) ──
    safe("recommendations", lambda: _event_count(EV_RECOMMENDATION_GENERATED))
    safe("recommendations_zero_result", lambda: _event_count(
        EV_RECOMMENDATION_GENERATED, "AND success=0"))

    def top_recommended():
        rows = run_sql(
            "SELECT payload FROM ai_events WHERE event_type=%s AND created_at >= %s "
            "AND payload IS NOT NULL ORDER BY created_at DESC LIMIT 500"
            % (_q(EV_RECOMMENDATION_GENERATED), SINCE))
        c = Counter()
        for r in rows:
            try:
                skus = json.loads(r[0]).get("skus") or []
            except (ValueError, AttributeError, IndexError):
                continue
            for s in skus:
                if s:
                    c[str(s)] += 1
        return c.most_common(5)
    safe("top_recommended", top_recommended)

    def action_counts(action_type_sql):
        rows = run_sql(
            "SELECT status, COUNT(*) FROM ai_actions WHERE action_type %s "
            "AND created_at >= %s GROUP BY status" % (action_type_sql, SINCE))
        return {r[0]: _to_int(r[1]) for r in rows}
    safe("nav_actions", lambda: action_counts("='NAVIGATE'"))
    safe("face_actions", lambda: action_counts(
        "IN (%s)" % ",".join(_q(t) for t in FACE_ACTION_TYPES)))

    safe("nav_expired_by_time", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM ai_actions WHERE action_type='NAVIGATE' "
        "AND status='PENDING' AND expires_at IS NOT NULL AND expires_at < NOW() "
        "AND created_at >= %s" % SINCE)))
    safe("nav_confirmed", lambda: _event_count(
        EV_ACTION_CONFIRMED, "AND action_type='NAVIGATE'"))

    safe("promise_without_action", lambda: _event_count(EV_PROMISE_WITHOUT_ACTION))
    safe("unsafe_url_rejected", lambda: _event_count(EV_UNSAFE_URL_REJECTED))

    # ── funnel (canonical JOURNEY_STAGE: distinct sessions per stage) ──
    def funnel():
        rows = run_sql(
            "SELECT journey_stage, COUNT(DISTINCT session_id) FROM ai_events "
            "WHERE event_type=%s AND created_at >= %s GROUP BY journey_stage"
            % (_q(EV_JOURNEY_STAGE), SINCE))
        d = {r[0]: _to_int(r[1]) for r in rows}
        return {s: d.get(s, 0) for s in FUNNEL_STAGES}
    safe("funnel", lambda: _gated(EV_JOURNEY_STAGE, funnel))

    # ── outcomes (closure ledger) ──
    def outcomes():
        rows = run_sql(
            "SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.outcome')), COUNT(*) "
            "FROM ai_events WHERE event_type=%s AND created_at >= %s GROUP BY 1"
            % (_q(EV_SESSION_OUTCOME), SINCE))
        return {r[0]: _to_int(r[1]) for r in rows}
    safe("outcomes", lambda: _gated(EV_SESSION_OUTCOME, outcomes, NA_LEDGER))

    safe("purchases_attributed", lambda: _gated(
        EV_COMMERCE_OUTCOME, lambda: _event_count(EV_COMMERCE_OUTCOME), NA_LEDGER))

    def products_bought():
        rows = run_sql(
            "SELECT p.product_code, COUNT(*) FROM ai_session_commerce c "
            "JOIN orders o ON o.order_id=c.order_id "
            "JOIN products p ON p.product_id=o.product_id "
            "WHERE c.created_at >= %s GROUP BY p.product_code ORDER BY 2 DESC LIMIT 5"
            % SINCE)
        return [(r[0], _to_int(r[1])) for r in rows]
    safe("products_bought", lambda: _gated(EV_COMMERCE_OUTCOME, products_bought, NA_LEDGER))

    # ── escalation ──
    safe("escalations", lambda: _event_count(EV_HANDOVER_ESCALATED))
    safe("ket_tickets", lambda: _event_count(EV_KET_TICKET_CREATED))

    # ── quality: sessions carrying a defect signal in the window ──
    defect_events = (EV_ACTION_FAILED, EV_PROMISE_WITHOUT_ACTION, EV_MODEL_TIMEOUT,
                     EV_PROVIDER_FAILURE, EV_ADMISSION_503, EV_UNSAFE_URL_REJECTED,
                     EV_HANDOVER_ESCALATED)
    safe("sessions_needs_review", lambda: _to_int(_scalar(
        "SELECT COUNT(DISTINCT session_id) FROM ai_events WHERE created_at >= %s "
        "AND session_id IS NOT NULL AND session_id<>'' AND event_type IN (%s)"
        % (SINCE, ",".join(_q(e) for e in defect_events)))))

    def quality_reasons():
        rows = run_sql(
            "SELECT event_type, COUNT(*) FROM ai_events WHERE created_at >= %s "
            "AND event_type IN (%s) GROUP BY event_type"
            % (SINCE, ",".join(_q(e) for e in defect_events)))
        return {r[0]: _to_int(r[1]) for r in rows}
    safe("quality_reasons", quality_reasons)

    # ── AI health (canonical MODEL_* events) ──
    def model_calls():
        rows = run_sql(
            "SELECT COALESCE(provider,'-'), COALESCE(model,'-'), COALESCE(workload,'-'), "
            "success, COUNT(*), "
            "SUM(COALESCE(JSON_EXTRACT(payload,'$.input_tokens'),0)), "
            "SUM(COALESCE(JSON_EXTRACT(payload,'$.output_tokens'),0)) "
            "FROM ai_events WHERE event_type=%s AND created_at >= %s "
            "GROUP BY 1,2,3,4" % (_q(EV_MODEL_CALL), SINCE))
        out = []
        for r in rows:
            out.append(dict(provider=r[0], model=r[1], workload=r[2],
                            success=_to_int(r[3], 0), n=_to_int(r[4]),
                            input_tokens=_to_int(r[5]), output_tokens=_to_int(r[6])))
        return out
    safe("model_calls", model_calls)

    def latencies():
        rows = run_sql(
            "SELECT duration_ms FROM ai_events WHERE event_type=%s AND created_at >= %s "
            "AND success=1 AND duration_ms IS NOT NULL ORDER BY duration_ms"
            % (_q(EV_MODEL_CALL), SINCE))
        return sorted(_to_int(r[0]) for r in rows if r and r[0] not in (None, "NULL"))
    safe("model_latencies", latencies)

    safe("model_timeouts", lambda: _event_count(EV_MODEL_TIMEOUT))
    safe("admission_503", lambda: _event_count(EV_ADMISSION_503))
    safe("provider_failures", lambda: _event_count(EV_PROVIDER_FAILURE))
    safe("ops_auth_failures", lambda: _event_count(EV_OPS_CONSOLE_AUTH_FAILURE))

    # ── legacy / wrapper populations, for the reconciliation layer only ──
    safe("legacy_sessions_created", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_events WHERE event_type='session_created' "
        "AND created_at >= %s" % SINCE)))
    safe("legacy_ai_started", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_events WHERE event_type='ai_started' "
        "AND created_at >= %s" % SINCE)))
    safe("legacy_ai_completed", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_events WHERE event_type='ai_completed' "
        "AND created_at >= %s" % SINCE)))
    safe("avg_conv_len", _avg_conv_len)
    safe("legacy_resolved", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_events WHERE event_type='session_resolved' "
        "AND created_at >= %s" % SINCE)))
    safe("legacy_abandoned", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_sessions WHERE status='abandoned' "
        "AND last_activity >= %s" % SINCE)))

    m["wrapper_calls"] = wrapper_log_calls()

    return m, errs


def _avg_conv_len():
    row = run_sql(
        "SELECT ROUND(AVG(c),1) FROM (SELECT COUNT(*) c FROM chat_messages "
        "WHERE created_at >= %s GROUP BY session_id) t" % SINCE)
    return float(row[0][0]) if row and row[0] and row[0][0] not in (None, "NULL") else 0.0


_WRAPPER_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def wrapper_log_files(path=None, now=None):
    """The live wrapper log plus every rotated copy whose date falls inside
    the window (``ai_metrics.log-YYYYMMDD[.gz]``)."""
    path = path or AI_METRICS_LOG
    now = now or datetime.now()
    files = [path] if os.path.exists(path) else []
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    for f in sorted(glob.glob(path + "-*")):
        stamp = re.search(r"-(\d{8})", os.path.basename(f))
        if not stamp:
            continue
        try:
            day = datetime.strptime(stamp.group(1), "%Y%m%d")
        except ValueError:
            continue
        # A file rotated on day D holds day D-1's lines up to the rotation.
        if day >= cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
            files.append(f)
    return files


def wrapper_log_calls(path=None, now=None):
    """``ai-call`` lines in the window across live + rotated files, by outcome.
    Returns None when there is no log at all (not a zero)."""
    now = now or datetime.now()
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    files = wrapper_log_files(path, now)
    if not files:
        return None
    by_outcome = Counter()
    for f in files:
        opener = gzip.open if f.endswith(".gz") else open
        try:
            with opener(f, "rt", errors="replace") as fh:
                for line in fh:
                    if "ai-call " not in line:
                        continue
                    ts = _WRAPPER_TS.match(line)
                    if ts:
                        try:
                            if datetime.strptime(ts.group(1), "%Y-%m-%d %H:%M:%S") < cutoff:
                                continue
                        except ValueError:
                            pass
                    outcome = "-"
                    for tok in line.split():
                        if tok.startswith("outcome="):
                            outcome = tok[len("outcome="):]
                    by_outcome[outcome] += 1
        except OSError:
            continue
    return dict(by_outcome)


# ─────────────────────────── status logic ───────────────────────────

def _worst(*statuses):
    real = [s for s in statuses if s in _ORDER]
    if not real:
        return GREEN
    return max(real, key=lambda s: _ORDER[s])


TERMINAL_STATUSES = ("EXECUTED", "FAILED", "BLOCKED", "EXPIRED")


def _nav_execution_rate(nav, expired_extra=0):
    """executed / terminal outcomes.

    Terminal = the terminal ai_actions statuses (EXECUTED/FAILED/BLOCKED/EXPIRED)
    PLUS time-expired offers (``expired_extra``): the app never writes an EXPIRED
    status — expiry is derived from PENDING rows whose ``expires_at`` has passed —
    so those must be counted here as non-executed, or the rate is inflated.
    Still-live PENDING and CONFIRMED-but-unresolved actions are in-flight (their
    outcome hasn't arrived) and are deliberately excluded. Returns None when there
    are no terminal outcomes yet (rate is not meaningful)."""
    if not nav:
        return None
    terminal = sum(v for k, v in nav.items() if k in TERMINAL_STATUSES) + (expired_extra or 0)
    if terminal == 0:
        return None
    return round(100.0 * nav.get("EXECUTED", 0) / terminal, 1)


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


_COLLECT_CACHE = []


def _collect_once():
    """``_collect`` memoised for the lifetime of one report run.

    ``build()`` and ``findings()`` both need the same numbers; querying twice
    would double the section's DB load and could report two different states
    for the same run.
    """
    if not _COLLECT_CACHE:
        _COLLECT_CACHE.append(_collect())
    return _COLLECT_CACHE[0]


def _reset_cache():
    """Drop the memoised collection (one report run == one snapshot)."""
    del _COLLECT_CACHE[:]


def _is_live(v):
    return v is not None and not isinstance(v, NotEmitted)


def _coverage(metrics, na_count):
    """Instrumentation coverage as (live, pending, percent).

    ``live`` counts metrics that actually resolved to a value; ``pending``
    counts the rendered rows still showing ``n/a``. Deriving pending from the
    rendered output rather than a hand-maintained list means coverage rises by
    itself as metrics are promoted — the number cannot drift out of step with
    what the report actually shows.
    """
    live = sum(1 for v in metrics.values() if _is_live(v))
    pending = int(na_count or 0)
    total = live + pending
    pct = (100.0 * live / total) if total else 0.0
    return live, pending, pct


def _telemetry_contradiction(metrics):
    """Detect a self-inconsistent read of the chat tables.

    Open-status sessions that are *not* stale, with zero started sessions and
    zero conversations in the same window, is not a quiet day — a session
    active inside the window must have produced activity — so the counters
    disagree and the source is incomplete rather than empty. Stale open-status
    rows (last activity before the window) are excluded: they are a retention
    question, reported on their own line, not a contradiction. Returns a
    message or None.
    """
    open_status = metrics.get("sessions_open_status")
    if isinstance(open_status, dict):
        fresh = open_status.get("total", 0) - open_status.get("stale", 0)
    elif isinstance(open_status, int):
        fresh = open_status
    else:
        return None
    started = metrics.get("sessions_started")
    started_total = started[2] if isinstance(started, tuple) else started
    conversations = metrics.get("legacy_ai_started", metrics.get("conversations"))
    if fresh <= 0:
        return None
    if started_total in (None, 0) and conversations in (None, 0):
        return ("%d open-status session(s) active inside the window but 0 started / "
                "0 conversations in the last %dh — counters disagree, coverage "
                "incomplete" % (fresh, WINDOW_HOURS))
    return None


def _rate_status(rate, green_min, amber_min):
    if rate is None:
        return None
    if rate >= green_min:
        return GREEN
    if rate >= amber_min:
        return AMBER
    return RED


# ─────────────────────────── rendering ───────────────────────────

def _fmt_hosts(v):
    if v is None:
        return NA
    if isinstance(v, tuple) and len(v) == 3:
        return "%-6d  .com %-5d  .in %-5d" % (v[2], v[0], v[1])
    return str(v)


def _val(v):
    if v is None:
        return NA
    return str(v)


def _fmt_dist(d, keys=None):
    if not _is_live(d):
        return _val(d)
    if not d:
        return "none"
    items = [(k, d[k]) for k in keys] if keys else sorted(d.items(), key=lambda kv: -kv[1])
    return " | ".join("%s %s" % (k, v) for k, v in items)


def _fmt_top(items):
    if not _is_live(items):
        return _val(items)
    if not items:
        return "none in window"
    return ", ".join("%s (%d)" % (k, n) for k, n in items)


def _lifecycle_line(acts, confirmed=None, expired_extra=0):
    """offered | [confirmed |] executed | failed | blocked | expired. The
    confirmed column is shown only when a confirmation count is supplied."""
    if isinstance(acts, dict):
        offered = str(sum(acts.values()))
        executed = str(acts.get("EXECUTED", 0))
        failed = str(acts.get("FAILED", 0))
        blocked = str(acts.get("BLOCKED", 0))
        expired = str(acts.get("EXPIRED", 0) + (expired_extra or 0))
    else:
        offered = executed = failed = blocked = expired = NA
    parts = ["offered %s" % offered]
    if confirmed is not None:
        parts.append("confirmed %s" % _val(confirmed))
    parts += ["executed %s" % executed, "failed %s" % failed,
              "blocked %s" % blocked, "expired %s" % expired]
    return " | ".join(parts)


def build():
    L = []
    add = L.append

    try:
        m, errs = _collect_once()
    except Exception as e:  # noqa: BLE001 - never break the daily report
        return "\n".join([BANNER, "  ACR AI OPERATIONS (Last %dh)" % WINDOW_HOURS,
                          BANNER, "  [WARN] section unavailable: %s" % e, BANNER])

    # The action ledger (ai_actions) is the core health source. Distinguish
    # "query degraded / no data" (None) from a genuine zero: when it is None we
    # must NOT collapse to 0 and print a fabricated GREEN.
    nav = m.get("nav_actions")
    nav_available = isinstance(nav, dict)
    if nav_available:
        offered = sum(nav.values())
        failed = nav.get("FAILED", 0)
        success_rate = _nav_execution_rate(nav, m.get("nav_expired_by_time") or 0)
    else:
        offered = failed = None
        success_rate = None

    st_success = _rate_status(success_rate,
                              float(os.environ.get("ACR_REPORT_NAV_GREEN", "95")),
                              float(os.environ.get("ACR_REPORT_NAV_AMBER", "85")))
    st_failed = (None if failed is None
                 else GREEN if failed == 0
                 else AMBER if failed <= 2 else RED)
    st_data = AMBER if not nav_available else None
    data_note = "  (data incomplete)" if not nav_available else ""

    def bar(status):
        fill = {GREEN: "#" * 14, AMBER: "#" * 9 + "." * 5, RED: "#" * 4 + "." * 10}
        return "%s %s" % (fill.get(status, "." * 14), status)

    ss = m.get("sessions_started")
    ss_total = ss[2] if isinstance(ss, tuple) else None
    canary = m.get("sessions_started_canary")
    ga = m.get("guest_auth")
    funnel = m.get("funnel")
    open_status = m.get("sessions_open_status")

    # ═══ LAYER 1 — EXECUTIVE SUMMARY ═══
    add(BANNER)
    add("  ACR AI OPERATIONS (Last %dh)%sSTATUS: %s%s" %
        (WINDOW_HOURS, " " * max(1, 26 - len(str(WINDOW_HOURS))), "{{STATUS}}",
         data_note))
    add(BANNER)
    add("  AI STATUS   {{BAR}}%s" % data_note)
    add("  COVERAGE    {{COVERAGE}}")
    add("")
    add("  Sessions started      %s%s" % (
        _val(ss_total), ("  (incl. %d deploy-canary)" % canary) if canary else ""))
    add("  Customers assisted    %s" % _val(m.get("customers_assisted")))
    add("  Recommendations       %s" % _val(m.get("recommendations")))
    add("  Purchases assisted    %s" % _val(m.get("purchases_attributed")))
    add("  Revenue assisted      %s" % NA)   # needs ledger + order amounts
    add("  Escalations           %s" % _val(m.get("escalations")))
    add("  Failures              %s" % _val(failed))
    add("  Unsafe actions        %s" % _val(m.get("unsafe_url_rejected")))
    add("  Overall               {{STATUS}}")
    add("")

    # ═══ LAYER 2 — OPERATIONS: CUSTOMER JOURNEY FUNNEL ═══
    add("  " + "-" * (WIDTH - 4))
    add("  OPERATIONS — CUSTOMER JOURNEY  (distinct chat sessions reaching each stage)")
    add("  " + "-" * (WIDTH - 4))
    add("    Landing / sessions        %s" % _fmt_hosts(ss))
    add("    Recommendation            %s" % _val(m.get("recommendations")))
    add("    Navigation offered        %s" % _val(offered))
    add("    Navigation confirmed      %s" % _val(m.get("nav_confirmed")))
    fl = funnel if isinstance(funnel, dict) else {}
    add("    Listing viewed            %s" % (fl.get("LISTING") if fl else _val(funnel)))
    add("    Product viewed            %s" % (fl.get("PRODUCT") if fl else _val(funnel)))
    add("    Cart / checkout           %s  (one page: the cart is shown on /checkout)"
        % (fl.get("CHECKOUT") if fl else _val(funnel)))
    add("    Payment                   (inside /checkout; read from the order, see Purchase)")
    add("    Order success page        %s" % (fl.get("PURCHASE") if fl else _val(funnel)))
    add("    Purchase (attributed)     %s" % _val(m.get("purchases_attributed")))
    add("    (guest / authenticated)   %s" %
        ("%d / %d" % ga if ga else NA))
    add("")

    # ═══ LAYER 3 — AI BUSINESS INTELLIGENCE ═══
    add("  " + "-" * (WIDTH - 4))
    add("  AI BUSINESS INTELLIGENCE")
    add("  " + "-" * (WIDTH - 4))
    add("    Top recommended products      %s" % _fmt_top(m.get("top_recommended")))
    add("    Products bought after AI      %s" % _fmt_top(m.get("products_bought")))
    add("    Frequently rejected products  %s" % NA_NO_SIGNAL)
    add("    Zero-result recommendations   %s" % _val(m.get("recommendations_zero_result")))
    add("    Handed to a person            escalated %s | KET tickets %s"
        % (_val(m.get("escalations")), _val(m.get("ket_tickets"))))
    oc = m.get("outcomes")
    add("    Session outcomes              %s" % _fmt_dist(
        oc, ("ANSWERED", "ESCALATED", "ABANDONED", "FAILED") if isinstance(oc, dict) else None))
    add("    (aggregate only; no customer wording or PII)")
    add("")

    # ═══ LAYER 4 — AI QUALITY ═══
    add("  " + "-" * (WIDTH - 4))
    add("  AI QUALITY  (sessions with canonical activity in the window)")
    add("  " + "-" * (WIDTH - 4))
    active_w = m.get("sessions_active_window")
    needs = m.get("sessions_needs_review")
    if isinstance(active_w, int) and isinstance(needs, int):
        add("    Sessions %d | clean %d | needs review %d"
            % (active_w, max(0, active_w - needs), needs))
    else:
        add("    Sessions %s | clean %s | needs review %s" % (_val(active_w), NA, NA))
    add("    Needs-review reasons (event counts):")
    add("      %s" % _fmt_dist(m.get("quality_reasons")))
    add("    Per-conversation QC scoring is the audited QC export, not this email.")
    add("")

    # ═══ LAYER 5 — ENGINEERING ═══
    add("  " + "-" * (WIDTH - 4))
    add("  ENGINEERING")
    add("  " + "-" * (WIDTH - 4))
    add("    Action lifecycle (NAVIGATE):")
    add("      %s" % _lifecycle_line(nav, m.get("nav_confirmed") or NA,
                                  m.get("nav_expired_by_time")))
    add("      nav execution rate (executed/terminal)   %s   [%s]" %
        (("%.1f%%" % success_rate) if success_rate is not None else NA, st_success or "-"))
    add("    Action lifecycle (FACE_*; assistant face abilities are OFF in production):")
    add("      %s" % _lifecycle_line(m.get("face_actions")))
    add("      promise-without-action       %s" % _val(m.get("promise_without_action")))
    add("      unsafe-url rejected          %s" % _val(m.get("unsafe_url_rejected")))
    add("    AI health (canonical MODEL_* events):")
    mc = m.get("model_calls")
    lat = m.get("model_latencies")
    if isinstance(mc, list):
        total = sum(c["n"] for c in mc)
        ok = sum(c["n"] for c in mc if c["success"] == 1)
        in_tok = sum(c["input_tokens"] for c in mc)
        out_tok = sum(c["output_tokens"] for c in mc)
        add("      model calls                  %d  (ok %d, failed %d)" % (total, ok, total - ok))
        dist = Counter()
        for c in mc:
            dist["%s/%s [%s]" % (c["provider"], c["model"], c["workload"])] += c["n"]
        add("      provider/model distribution  %s" % (
            " | ".join("%s %d" % kv for kv in dist.most_common()) if dist else "none"))
        add("      tokens in / out              %d / %d" % (in_tok, out_tok))
    else:
        add("      model calls                  %s" % NA)
        add("      provider/model distribution  %s" % NA)
        add("      tokens in / out              %s" % NA)
    if isinstance(lat, list):
        add("      latency p50 / p95 (ms)       %s / %s" % (
            _val(_percentile(lat, 50)) if lat else "none",
            _val(_percentile(lat, 95)) if lat else "none"))
    else:
        add("      latency p50 / p95 (ms)       %s" % NA)
    add("      model timeouts / adm-503     %s / %s" % (
        _val(m.get("model_timeouts")), _val(m.get("admission_503"))))
    add("      provider failures            %s" % _val(m.get("provider_failures")))
    add("      estimated provider cost      %s" % NA_COST)
    add("    Ops Console auth failures      %s" % _val(m.get("ops_auth_failures")))
    add("    Session resumed / not-found    %s / %s" % (
        _val(m.get("sessions_resumed")), _val(m.get("sessions_not_found"))))
    add("")

    # ═══ LAYER 6 — RECONCILIATION ═══
    add("  " + "-" * (WIDTH - 4))
    add("  AI ACTIVITY — EVERY COUNT WITH ITS SOURCE  (window: last %dh)" % WINDOW_HOURS)
    add("  " + "-" * (WIDTH - 4))
    add("    %-42s %-8s %s" % ("population", "count", "source"))
    add("    %-42s %-8s %s" % ("sessions started (canonical)", _val(ss_total),
                                "ai_events.SESSION_STARTED; incl. canary"))
    add("    %-42s %-8s %s" % ("sessions created (legacy bridge)",
                                _val(m.get("legacy_sessions_created")),
                                "chat_events.session_created"))
    add("    %-42s %-8s %s" % ("sessions with any AI event", _val(active_w),
                                "ai_events distinct session_id"))
    add("    %-42s %-8s %s" % ("conversations = customer turns (legacy)",
                                _val(m.get("legacy_ai_started")),
                                "chat_events.ai_started; one per message"))
    add("    %-42s %-8s %s" % ("model round-trips (canonical)",
                                str(sum(c["n"] for c in mc)) if isinstance(mc, list) else NA,
                                "ai_events.MODEL_CALL; deterministic replies make none"))
    wc = m.get("wrapper_calls")
    add("    %-42s %-8s %s" % ("model round-trips (wrapper log)",
                                str(sum(wc.values())) if isinstance(wc, dict)
                                else "n/a (no ai_metrics.log)",
                                "ai_metrics.log + rotated; debug cross-check"))
    add("    %-42s %-8s %s" % ("recommendations (canonical)", _val(m.get("recommendations")),
                                "ai_events.RECOMMENDATION_GENERATED"))
    add("    %-42s %-8s %s" % ("AI replies (legacy)", _val(m.get("legacy_ai_completed")),
                                "chat_events.ai_completed"))
    add("    avg conversation length %s msgs | resolved %s | abandoned (status) %s  [legacy]"
        % (_val(m.get("avg_conv_len")), _val(m.get("legacy_resolved")),
           _val(m.get("legacy_abandoned"))))
    add("    Rule: a recommendation or navigation offer needs no model call when the")
    add("    reply is deterministic (catalogue rules, face fit), so ACR counts may")
    add("    exceed model round-trips; debug.log string counts are not call counts.")
    add("")
    if isinstance(open_status, dict):
        add("    Open-status sessions (chat_sessions.status='active', ALL TIME, no expiry):")
        add("      total %d | last activity before window %d | older than 7d %d | "
            "deploy-canary %d | signed-in %d"
            % (open_status["total"], open_status["stale"], open_status["older_7d"],
               open_status["canary"], open_status["authenticated"]))
        add("      This is a status flag, not %dh activity. Nothing is purged; retention"
            % WINDOW_HOURS)
        add("      rule for stale open-status rows is a register item.")
    else:
        add("    Open-status sessions (status='active', ALL TIME)  %s" % _val(open_status))

    # ---- coverage verdict, computed once the whole section has rendered ----
    live, pending, pct = _coverage(m, sum(1 for ln in L if NA_PREFIX in ln))
    contradiction = _telemetry_contradiction(m)
    st_coverage = AMBER if (pct < MIN_COVERAGE_PCT or contradiction) else None
    overall = _worst(st_success, st_failed, st_data, st_coverage)

    coverage_txt = "%d/%d metrics live (%.0f%%)" % (live, live + pending, pct)
    if pct < MIN_COVERAGE_PCT:
        coverage_txt += " — AMBER: instrumentation coverage incomplete"

    add("")
    add("  DATA COVERAGE  %s" % coverage_txt)
    if contradiction:
        add("  [coverage] %s" % contradiction)
    for e in errs[:6]:
        add("  [degraded] %s" % e)
    add("  Source: canonical ai_events/ai_actions; legacy chat tables and the wrapper")
    add("  log appear only in the reconciliation layer, labelled.")
    add("  Generated %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add(BANNER)

    out = "\n".join(L)
    return (out.replace("{{STATUS}}", overall)
               .replace("{{BAR}}", bar(overall))
               .replace("{{COVERAGE}}", coverage_txt))


def findings():
    """Severity findings this section contributes to the executive summary.

    Kept separate from :func:`build` so the report-level aggregator can reason
    about ACR health without parsing rendered text. Failing to produce the
    section is itself reported, so an ACR outage cannot read as healthy.
    """
    from reports.report_severity import ACTION, WARNING, Finding

    try:
        m, errs = _collect_once()
    except Exception as e:  # noqa: BLE001 - never break the daily report
        return [Finding(ACTION, "acr", "ACR section unavailable: %s" % e, "acr")]

    out = []
    nav = m.get("nav_actions")
    if not isinstance(nav, dict):
        out.append(Finding(WARNING, "acr",
                           "action ledger unavailable — AI health verdict is "
                           "not trustworthy", "acr"))
    contradiction = _telemetry_contradiction(m)
    if contradiction:
        out.append(Finding(WARNING, "acr", contradiction, "acr"))
    unsafe = m.get("unsafe_url_rejected")
    if isinstance(unsafe, int) and unsafe > 0:
        out.append(Finding(WARNING, "acr",
                           "%d unsafe navigation URL(s) rejected in the window" % unsafe,
                           "acr"))
    for e in errs[:6]:
        out.append(Finding(WARNING, "acr", "degraded metric %s" % e, "acr"))
    return out


def main():
    """Print the section and publish findings for the executive aggregator."""
    text = build()
    try:
        from reports.report_severity import emit
        emit("acr", findings())
    except Exception:  # noqa: BLE001 - the section still stands on its own
        pass
    print(text)


if __name__ == "__main__":
    main()

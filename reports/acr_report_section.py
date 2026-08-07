#!/usr/bin/env python3
"""ACR AI Operations — Daily Report section (Part A).

Appended to the Optiwar 06:00 Operations Report, mirroring the GMC section
contract: expose ``build()`` returning the section as a string and ``print`` it
on ``__main__`` so ``run_daily_report.sh`` can append the stdout.

Layered for a mixed audience (executive -> operations -> business -> quality ->
engineering):

  Layer 1  Executive Summary          (30-second health read)
  Layer 2  Operations funnel          (where customers leak)
  Layer 3  AI Business Intelligence    (commercial signal)
  Layer 4  AI Quality Score            (conversation quality)
  Layer 5  Engineering                 (technical metrics)

Sourcing (per direction): canonical ACR structured state (``ai_events`` /
``ai_actions``) first, bridged by the legacy chat tables where the canonical
stream is not yet emitted. Metrics that need Part-B instrumentation or later
classification/attribution render as ``n/a (pending instrumentation)`` rather
than a fabricated 0, so the report never overstates coverage.

Data protection: this is a broad-distribution email. It emits only counts,
rates, statuses, non-identifying IDs, SKUs and product titles. It never emits
raw customer messages, transcripts, prescription values, face measurements,
emails/names/phones, secrets, prompts or model reasoning. "Intents" are shown
only as business buckets, never verbatim customer wording.

Config (all optional; safe defaults): DB connection is read from the
environment (never inlined) — set ``ACR_REPORT_DB_*`` or fall back to the
standard ``MYSQL_*`` vars. Thresholds may be overridden via ``ACR_REPORT_*``
env vars. Intended to run behind an ``ACR_REPORT_ENABLED`` flag in the
orchestrator.
"""
import os
import subprocess

WIDTH = 70
BANNER = "=" * WIDTH
WINDOW_HOURS = int(os.environ.get("ACR_REPORT_WINDOW_HOURS", "24"))

# Status vocabulary
GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
_ORDER = {GREEN: 0, AMBER: 1, RED: 2}
NA = "n/a (pending instrumentation)"


def _db_conf():
    return dict(
        host=os.environ.get("ACR_REPORT_DB_HOST") or os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("ACR_REPORT_DB_USER") or os.environ.get("MYSQL_USER", ""),
        passwd=os.environ.get("ACR_REPORT_DB_PASS") or os.environ.get("MYSQL_PASSWORD", ""),
        name=(os.environ.get("ACR_REPORT_DB_NAME") or os.environ.get("MYSQL_DB")
              or os.environ.get("MYSQL_DATABASE", "")),
    )


class SqlError(Exception):
    pass


def run_sql(query):
    """Run a read-only query via the mysql client; return rows as list of tuples.

    Credentials come from the environment (never inlined). Raises SqlError so the
    caller can degrade a single metric to n/a without aborting the whole report.
    """
    c = _db_conf()
    if not (c["user"] and c["name"]):
        raise SqlError("db credentials not configured (ACR_REPORT_DB_* / MYSQL_*)")
    cmd = ["mysql", "-h", c["host"], "-u", c["user"], c["name"], "-N", "-e", query]
    env = dict(os.environ, MYSQL_PWD=c["passwd"])  # avoid -p on argv
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    except Exception as e:  # noqa: BLE001 - degrade to n/a
        raise SqlError(str(e))
    if res.returncode != 0:
        raise SqlError((res.stderr or "query failed").strip().split("\n")[-1])
    rows = []
    for line in res.stdout.strip().split("\n"):
        if line.strip():
            rows.append(tuple(line.split("\t")))
    return rows


def _scalar(query, default=None):
    rows = run_sql(query)
    if not rows or not rows[0]:
        return default
    return rows[0][0]


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# Host normalization: derive .com / .in from the session's page url. Values seen
# in prod include optiwar.com, in.optiwar.com and optiwar.in.
HOST_CASE = (
    "CASE "
    "WHEN current_page_url LIKE '%optiwar.in%' THEN '.in' "
    "WHEN current_page_url LIKE '%in.optiwar.com%' THEN '.in' "
    "WHEN current_page_url LIKE '%optiwar.com%' THEN '.com' "
    "ELSE 'unknown' END"
)
SINCE = "NOW() - INTERVAL %d HOUR" % WINDOW_HOURS


def _by_host(counts):
    com = counts.get(".com", 0)
    _in = counts.get(".in", 0)
    unk = counts.get("unknown", 0)
    return com, _in, com + _in + unk


# ─────────────────────────── metric collection ───────────────────────────

def _collect():
    """Return (metrics dict, errors list). Each metric may be an int, a
    (com, in, total) tuple, a float rate, or None for n/a."""
    m = {}
    errs = []

    def safe(key, fn):
        try:
            m[key] = fn()
        except SqlError as e:
            m[key] = None
            errs.append("%s: %s" % (key, e))

    # Sessions started (bridge: chat_events.session_created joined to host)
    def sessions_started():
        rows = run_sql(
            "SELECT %s h, COUNT(*) FROM chat_events e "
            "JOIN chat_sessions s ON s.session_id=e.session_id "
            "WHERE e.event_type='session_created' AND e.created_at >= %s "
            "GROUP BY h" % (HOST_CASE, SINCE))
        return _by_host({r[0]: _to_int(r[1]) for r in rows})
    safe("sessions_started", sessions_started)

    safe("sessions_active", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_sessions WHERE status='active'")))

    safe("conversations", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_events WHERE event_type='ai_started' "
        "AND created_at >= %s" % SINCE)))

    safe("customers_assisted", lambda: _to_int(_scalar(
        "SELECT COUNT(DISTINCT s.customer_id) FROM chat_sessions s "
        "JOIN chat_events e ON e.session_id=s.session_id "
        "WHERE s.customer_id IS NOT NULL AND e.created_at >= %s" % SINCE)))

    # Guest vs authenticated session split
    def guest_auth():
        rows = run_sql(
            "SELECT CASE WHEN s.customer_id IS NULL THEN 'guest' ELSE 'auth' END g, "
            "COUNT(DISTINCT s.session_id) FROM chat_sessions s "
            "JOIN chat_events e ON e.session_id=s.session_id "
            "WHERE e.event_type='session_created' AND e.created_at >= %s "
            "GROUP BY g" % SINCE)
        d = {r[0]: _to_int(r[1]) for r in rows}
        return d.get("guest", 0), d.get("auth", 0)
    safe("guest_auth", guest_auth)

    # Recommendations (approx from ai_completed carrying an action; T1->T2)
    safe("recommendations", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_events WHERE event_type='ai_completed' "
        "AND payload LIKE '%%navigate%%' AND created_at >= %s" % SINCE)))

    # Action lifecycle from the canonical action ledger
    def action_counts():
        rows = run_sql(
            "SELECT status, COUNT(*) FROM ai_actions WHERE action_type='NAVIGATE' "
            "AND created_at >= %s GROUP BY status" % SINCE)
        return {r[0]: _to_int(r[1]) for r in rows}
    safe("nav_actions", action_counts)

    safe("nav_expired_by_time", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM ai_actions WHERE action_type='NAVIGATE' "
        "AND status='PENDING' AND expires_at IS NOT NULL AND expires_at < NOW() "
        "AND created_at >= %s" % SINCE)))

    # Human escalation + tickets
    safe("escalations", lambda: _to_int(_scalar(
        "SELECT COUNT(DISTINCT session_id) FROM chat_events "
        "WHERE event_type='agent_reply' AND created_at >= %s" % SINCE))
        or _to_int(_scalar(
            "SELECT COUNT(*) FROM chat_sessions WHERE status IN "
            "('human_pending','human_open')")))

    safe("ket_tickets", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM tickets WHERE date_created >= %s" % SINCE)))

    # Average conversation length (messages per conversation, 24h)
    safe("avg_conv_len", lambda: _avg_conv_len())

    # Outcomes (partial): resolved from chat_events; abandoned from status
    safe("resolved", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_events WHERE event_type='session_resolved' "
        "AND created_at >= %s" % SINCE)))
    safe("abandoned", lambda: _to_int(_scalar(
        "SELECT COUNT(*) FROM chat_sessions WHERE status='abandoned' "
        "AND last_activity >= %s" % SINCE)))

    return m, errs


def _avg_conv_len():
    row = run_sql(
        "SELECT ROUND(AVG(c),1) FROM (SELECT COUNT(*) c FROM chat_messages "
        "WHERE created_at >= %s GROUP BY session_id) t" % SINCE)
    return float(row[0][0]) if row and row[0] and row[0][0] not in (None, "NULL") else 0.0


# ─────────────────────────── status logic ───────────────────────────

def _worst(*statuses):
    real = [s for s in statuses if s in _ORDER]
    if not real:
        return GREEN
    return max(real, key=lambda s: _ORDER[s])


def _nav_success_rate(nav):
    if not nav:
        return None
    offered = sum(nav.values())
    if offered == 0:
        return None
    executed = nav.get("EXECUTED", 0)
    return round(100.0 * executed / offered, 1)


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
    return NA if v is None else str(v)


def build():
    L = []
    add = L.append
    from datetime import datetime

    try:
        m, errs = _collect()
    except Exception as e:  # noqa: BLE001 - never break the daily report
        return "\n".join([BANNER, "  ACR AI OPERATIONS (Last %dh)" % WINDOW_HOURS,
                          BANNER, "  [WARN] section unavailable: %s" % e, BANNER])

    nav = m.get("nav_actions") or {}
    offered = sum(nav.values()) if nav else 0
    executed = nav.get("EXECUTED", 0)
    failed = nav.get("FAILED", 0)
    blocked = nav.get("BLOCKED", 0)
    expired = nav.get("EXPIRED", 0) + (m.get("nav_expired_by_time") or 0)
    success_rate = _nav_success_rate(nav)

    # ---- statuses (v0 thresholds; calibrate during observation) ----
    st_success = _rate_status(success_rate,
                              float(os.environ.get("ACR_REPORT_NAV_GREEN", "95")),
                              float(os.environ.get("ACR_REPORT_NAV_AMBER", "85")))
    st_failed = GREEN if failed == 0 else (AMBER if failed <= 2 else RED)
    st_escal = GREEN  # informational
    overall = _worst(st_success, st_failed, st_escal)

    def bar(status):
        fill = {GREEN: "#" * 14, AMBER: "#" * 9 + "." * 5, RED: "#" * 4 + "." * 10}
        return "%s %s" % (fill.get(status, "." * 14), status)

    ss = m.get("sessions_started")
    ss_total = ss[2] if isinstance(ss, tuple) else None
    ga = m.get("guest_auth")

    # ═══ LAYER 1 — EXECUTIVE SUMMARY ═══
    add(BANNER)
    add("  ACR AI OPERATIONS (Last %dh)%sSTATUS: %s" %
        (WINDOW_HOURS, " " * max(1, 26 - len(str(WINDOW_HOURS))), overall))
    add(BANNER)
    add("  AI STATUS   %s" % bar(overall))
    add("")
    add("  Sessions              %s" % _val(ss_total))
    add("  Customers assisted    %s" % _val(m.get("customers_assisted")))
    add("  Recommendations       %s" % _val(m.get("recommendations")))
    add("  Purchases assisted    %s" % NA)   # T3: needs GA4/attribution
    add("  Revenue assisted      %s" % NA)   # T3: needs GA4/attribution
    add("  Escalations           %s" % _val(m.get("escalations")))
    add("  Failures              %s" % _val(failed))
    add("  Unsafe actions        %s" % NA)   # T2: needs UNSAFE_URL_REJECTED event
    add("  Overall               %s" % overall)
    add("")

    # ═══ LAYER 2 — OPERATIONS: CUSTOMER JOURNEY FUNNEL ═══
    add("  " + "-" * (WIDTH - 4))
    add("  OPERATIONS — CUSTOMER JOURNEY")
    add("  " + "-" * (WIDTH - 4))
    add("    Landing / sessions        %s" % _fmt_hosts(ss))
    add("    Recommendation            %s" % _val(m.get("recommendations")))
    add("    Navigation offered        %s" % (offered if nav else NA))
    add("    Navigation confirmed      %s" % NA)  # T2: ACTION_CONFIRMED event
    add("    Product viewed            %s" % NA)  # T2: journey event
    add("    Cart                      %s" % NA)  # T2: cart_added event
    add("    Checkout                  %s" % NA)  # T2: checkout_started event
    add("    Payment                   %s" % NA)  # T2: payment_started event
    add("    Purchase                  %s" % NA)  # T3: attribution
    add("    (guest / authenticated)   %s" %
        ("%d / %d" % ga if ga else NA))
    add("")

    # ═══ LAYER 3 — AI BUSINESS INTELLIGENCE ═══
    add("  " + "-" * (WIDTH - 4))
    add("  AI BUSINESS INTELLIGENCE")
    add("  " + "-" * (WIDTH - 4))
    add("    Top recommended products      %s" % NA)  # needs RECOMMENDATION_GENERATED.sku
    add("    Products bought after AI      %s" % NA)  # needs attribution
    add("    Frequently rejected products  %s" % NA)  # needs rejection signal
    add("    Questions AI couldn't answer  %s" % NA)  # needs unanswered classification
    add("    Zero-recommendation categories %s" % NA)
    add("    Most abandoned journeys       %s" % NA)
    add("    (aggregate only; no customer wording or PII)")
    add("")

    # ═══ LAYER 4 — AI QUALITY SCORE ═══
    add("  " + "-" * (WIDTH - 4))
    add("  AI QUALITY SCORE")
    add("  " + "-" * (WIDTH - 4))
    add("    Excellent / Good / Needs-Review   %s" % NA)  # needs QC scoring
    add("    Reasons (nav-failed, incomplete, repeat, handover, timeout, halluc.)")
    add("       %s" % NA)
    add("")

    # ═══ LAYER 5 — ENGINEERING ═══
    add("  " + "-" * (WIDTH - 4))
    add("  ENGINEERING")
    add("  " + "-" * (WIDTH - 4))
    add("    Action lifecycle (NAVIGATE):")
    add("      offered %s | confirmed %s | executed %d | failed %d | blocked %d | expired %d"
        % ((offered if nav else 0), NA, executed, failed, blocked, expired))
    add("      confirmed-nav success rate   %s   [%s]" %
        (("%.1f%%" % success_rate) if success_rate is not None else NA, st_success or "-"))
    add("      promise-without-action       %s" % NA)  # T2: PROMISE_WITHOUT_ACTION
    add("      unsafe-url rejected          %s" % NA)  # T2: UNSAFE_URL_REJECTED
    add("    AI health (wrapper — pending event sourcing):")
    add("      p95 latency                  %s" % NA)  # T2: MODEL_CALL
    add("      model timeouts / adm-503     %s" % NA)  # T2
    add("      provider failures            %s" % NA)  # T2
    add("      provider/model distribution  %s" % NA)  # T2
    add("      estimated provider cost      %s" % NA)  # T2/T3
    add("    Ops Console auth failures      %s" % NA)  # T2
    add("    Session rebind / not-found     %s" % NA)  # T2
    add("")
    add("    Conversations %s | avg length %s msgs | active sessions %s"
        % (_val(m.get("conversations")), _val(m.get("avg_conv_len")),
           _val(m.get("sessions_active"))))
    add("    Escalations %s | KET tickets %s | resolved %s | abandoned %s"
        % (_val(m.get("escalations")), _val(m.get("ket_tickets")),
           _val(m.get("resolved")), _val(m.get("abandoned"))))

    if errs:
        add("")
        add("    [degraded metrics] " + "; ".join(errs[:6]))

    add("")
    add("  Source: canonical ai_events/ai_actions + bridge chat tables. "
        "n/a rows await Part-B event instrumentation.")
    add("  Generated %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add(BANNER)
    return "\n".join(L)


if __name__ == "__main__":
    print(build())

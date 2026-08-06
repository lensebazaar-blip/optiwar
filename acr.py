"""
ACR — AI Commerce Runtime (approved near-term slice: A1 / A2 / A3).

Thin, route-independent service layer for the first ACR workstream. Kept out of
the Flask routes so ``chat_gateway`` stays thin and this logic can move behind a
proper service boundary later (per the v6 architecture baseline).

Scope implemented here is ONLY the approved near-term items:
  A1  Action Integrity   — structured pending actions, cross-turn confirmation
                           resolution, verified action results, mandatory fallback.
  A2  Instrumentation    — action IDs, action outcomes, promise-without-action
                           detection, appended to a canonical ``ai_events`` stream.
  A3  Ops Console (r/o)   — read-only accessors for the admin console.

No existing table is modified; two additive tables are created if absent.
"""
import json
import re
import uuid

# Confirmation phrases that resolve against a live pending action rather than
# being re-inferred from the latest word. This is the direct fix for the "yes"
# turn that carried no product context.
_CONFIRM_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|ya|sure|ok|okay|okey|k|"
    r"please do|do it|go|go ahead|proceed|continue|"
    r"take me( there)?|open( it| them| that| these| those)?|show me|"
    r"lets go|let'?s go|sounds good|absolutely|definitely|of course)\b[\s!.,]*$",
    re.IGNORECASE,
)

# Phrases where the assistant CLAIMS/PROMISES a navigation. If one appears with
# no structured action, that is the silent-failure ("AI lied") case (A2). These
# are assertive claims only — an *offer* ("Would you like me to take you
# there?") is not a promise and is filtered out by _OFFER_RE below.
_PROMISE_RE = re.compile(
    r"taking you there|let me take you|i'?ll take you|i am taking you|i'?m taking you|"
    r"i'?ve opened|i have opened|opening (it|them|that|the|these|those|your)|"
    r"i'?ll open|let me open|redirecting you|navigating you|taking you to",
    re.IGNORECASE,
)

# Offer/question markers: when present the reply is asking permission, not
# claiming completion, so it must not be treated as a promise-without-action.
_OFFER_RE = re.compile(
    r"would you like|shall i|want me to|do you want|should i|may i|"
    r"can i (open|take|show)|let me know",
    re.IGNORECASE,
)

# Navigation-target markers: the reply is offering to *navigate* specifically
# (open/take/show a page or the frames), as opposed to offering a ticket or a
# supervisor handover. Only a genuine navigation offer should seed a pending
# NAVIGATE action — otherwise a later bare "yes" answering a ticket/handover
# yes/no question would be resolved into an unwanted redirect.
_NAV_OFFER_TARGET_RE = re.compile(
    r"take you( there| to)?|open (it|them|that|these|those|this|the|your)|"
    r"show you|go to|these frames|the frames|frames page|browse (these|them)|"
    r"see (them|these)",
    re.IGNORECASE,
)

PENDING_TTL_SECONDS = 1800  # 30 min
FRAMES_LISTING_FALLBACK = "/eyeglasses/all-spectacle-frames.html"


def is_confirmation(text):
    """True when a customer message is a bare affirmative/confirmation."""
    return bool(text and _CONFIRM_RE.match(text.strip()))


def promises_navigation(text):
    """True when an assistant reply *claims* a navigation it may not have
    performed. Offers/questions ("Would you like me to take you there?") are
    not promises and return False."""
    if not text or _OFFER_RE.search(text):
        return False
    return bool(_PROMISE_RE.search(text))


def offers_navigation(text):
    """True when an assistant reply is *offering to navigate* (e.g. "Would you
    like me to take you to these frames?"). Used to gate seeding a pending
    NAVIGATE action so a ticket/handover yes/no offer never seeds one."""
    if not text:
        return False
    return bool(_OFFER_RE.search(text) and _NAV_OFFER_TARGET_RE.search(text))


# ─── Schema (additive) ───

def ensure_schema(get_conn):
    """Create ACR tables if absent. Never alters existing tables."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS ai_actions (
                action_id         VARCHAR(36) PRIMARY KEY,
                session_id        VARCHAR(64) NOT NULL,
                action_type       VARCHAR(32) NOT NULL,
                target            TEXT,
                status            VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                source_message_id BIGINT NULL,
                result_code       VARCHAR(48) NULL,
                duration_ms       INT NULL,
                created_at        DATETIME NOT NULL,
                resolved_at       DATETIME NULL,
                expires_at        DATETIME NULL,
                KEY idx_session_status (session_id, status),
                KEY idx_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS ai_events (
                event_id      VARCHAR(36) PRIMARY KEY,
                event_type    VARCHAR(48) NOT NULL,
                session_id    VARCHAR(64) NULL,
                action_id     VARCHAR(36) NULL,
                journey_stage VARCHAR(32) NULL,
                action_type   VARCHAR(32) NULL,
                page_url      TEXT NULL,
                success       TINYINT(1) NULL,
                failure_code  VARCHAR(48) NULL,
                duration_ms   INT NULL,
                payload       TEXT NULL,
                created_at    DATETIME NOT NULL,
                KEY idx_type_created (event_type, created_at),
                KEY idx_session (session_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        )
    finally:
        conn.close()


# ─── Canonical event stream (A2 / minimum of the ACR-5 event model) ───

def log_event(db, event_type, session_id=None, action_id=None, journey_stage=None,
              action_type=None, page_url=None, success=None, failure_code=None,
              duration_ms=None, payload=None):
    """Append an AI event. Best-effort: never raises into the request path."""
    try:
        cur = db.cursor()
        cur.execute(
            """INSERT INTO ai_events
                  (event_id, event_type, session_id, action_id, journey_stage,
                   action_type, page_url, success, failure_code, duration_ms,
                   payload, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
            (uuid.uuid4().hex, event_type, session_id, action_id, journey_stage,
             action_type, page_url,
             None if success is None else (1 if success else 0),
             failure_code, duration_ms,
             json.dumps(payload) if payload else None),
        )
    except Exception:
        pass


# ─── Structured actions (A1) ───

def create_pending_action(db, session_id, action_type, target, source_message_id=None,
                          ttl_seconds=PENDING_TTL_SECONDS):
    """Persist a pending action, superseding any earlier live one of the same
    type for the session so a later confirmation resolves the latest offer.

    Best-effort: if the additive tables are missing (schema creation is itself
    best-effort at boot) this returns None instead of raising into the chat
    reply path, so action bookkeeping can never break a customer conversation."""
    try:
        cur = db.cursor()
        cur.execute(
            """UPDATE ai_actions SET status='SUPERSEDED', resolved_at=NOW()
               WHERE session_id=%s AND action_type=%s AND status='PENDING'""",
            (session_id, action_type),
        )
        action_id = uuid.uuid4().hex
        cur.execute(
            """INSERT INTO ai_actions
                  (action_id, session_id, action_type, target, status,
                   source_message_id, created_at, expires_at)
               VALUES (%s,%s,%s,%s,'PENDING',%s,NOW(),
                       DATE_ADD(NOW(), INTERVAL %s SECOND))""",
            (action_id, session_id, action_type, target, source_message_id, ttl_seconds),
        )
    except Exception:
        return None
    log_event(db, 'AI_ACTION_PROPOSED', session_id=session_id, action_id=action_id,
              action_type=action_type, payload={'target': target})
    return action_id


def get_live_pending_action(db, session_id, action_type='NAVIGATE'):
    """Return the latest non-expired PENDING action of a type, or None.
    Best-effort: returns None if the table is unavailable."""
    try:
        cur = db.cursor()
        cur.execute(
            """SELECT action_id, target FROM ai_actions
               WHERE session_id=%s AND action_type=%s AND status='PENDING'
                 AND (expires_at IS NULL OR expires_at > NOW())
               ORDER BY created_at DESC LIMIT 1""",
            (session_id, action_type),
        )
        return cur.fetchone()
    except Exception:
        return None


def mark_action(db, action_id, status, result_code=None, duration_ms=None):
    """Best-effort status update; never raises into the request path."""
    if not action_id:
        return False
    try:
        cur = db.cursor()
        cur.execute(
            """UPDATE ai_actions
               SET status=%s, result_code=%s, duration_ms=%s, resolved_at=NOW()
               WHERE action_id=%s""",
            (status, result_code, duration_ms, action_id),
        )
        return True
    except Exception:
        return False


def record_action_result(db, action_id, success, failure_code=None, duration_ms=None):
    """Record the browser-reported outcome of an executed action (A1 verify).
    Best-effort: returns False (not raise) if the table is unavailable."""
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT session_id, action_type FROM ai_actions WHERE action_id=%s",
            (action_id,),
        )
        row = cur.fetchone()
    except Exception:
        return False
    if not row:
        return False
    mark_action(db, action_id, 'EXECUTED' if success else 'FAILED',
                result_code=failure_code, duration_ms=duration_ms)
    log_event(db, 'AI_ACTION_COMPLETED' if success else 'AI_ACTION_FAILED',
              session_id=row['session_id'], action_id=action_id,
              action_type=row['action_type'], success=bool(success),
              failure_code=failure_code, duration_ms=duration_ms)
    return True


# ─── Fallback affordance ───

def nav_link_label(url):
    """A short human label for a navigation fallback button."""
    u = (url or "").lower()
    if "all-spectacle-frames" in u or "frames" in u:
        return "Open recommended frames"
    if "/lenses" in u:
        return "Open lens options"
    if "/tryon" in u:
        return "Open face measurement"
    if "/checkout" in u:
        return "Go to checkout"
    if "pid=" in u:
        return "Open this frame"
    return "Open the page"


def with_fallback_link(reply, url):
    """Append a mandatory, always-clickable fallback link to a reply. Renders as
    an action button in the widget. Idempotent: won't duplicate the same link."""
    if not url:
        return reply
    if ("](%s)" % url) in (reply or ""):
        return reply
    label = nav_link_label(url)
    base = (reply or "").rstrip()
    joiner = "\n\n" if base else ""
    return "%s%s[\u25b6 %s](%s)" % (base, joiner, label, url)


# ─── Ops Console read-only accessors (A3) ───

def ops_console_snapshot(db, limit=50):
    """Live-ish snapshot of recent sessions with their latest message and action."""
    cur = db.cursor()
    cur.execute(
        """SELECT s.session_id, s.contact_name, s.contact_email, s.status,
                  s.current_page_url, s.last_activity,
                  (SELECT m.content FROM chat_messages m
                     WHERE m.session_id = s.session_id
                     ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message,
                  (SELECT m.source FROM chat_messages m
                     WHERE m.session_id = s.session_id
                     ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_source
           FROM chat_sessions s
           ORDER BY s.last_activity DESC
           LIMIT %s""",
        (limit,),
    )
    sessions = cur.fetchall() or []
    for s in sessions:
        cur.execute(
            """SELECT action_id, action_type, target, status, result_code, created_at
               FROM ai_actions WHERE session_id=%s
               ORDER BY created_at DESC LIMIT 1""",
            (s['session_id'],),
        )
        s['last_action'] = cur.fetchone()
    return sessions


def ops_console_stats(db, hours=24):
    """Aggregate counts for the console header (events + action outcomes)."""
    cur = db.cursor()
    stats = {'events': {}, 'actions': {}}
    cur.execute(
        """SELECT event_type, COUNT(*) AS c FROM ai_events
           WHERE created_at > DATE_SUB(NOW(), INTERVAL %s HOUR)
           GROUP BY event_type""",
        (hours,),
    )
    for r in (cur.fetchall() or []):
        stats['events'][r['event_type']] = r['c']
    cur.execute(
        """SELECT status, COUNT(*) AS c FROM ai_actions
           WHERE created_at > DATE_SUB(NOW(), INTERVAL %s HOUR)
           GROUP BY status""",
        (hours,),
    )
    for r in (cur.fetchall() or []):
        stats['actions'][r['status']] = r['c']
    return stats

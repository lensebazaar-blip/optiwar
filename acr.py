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

# ─── Canonical ACR event vocabulary (Part B) ───
# One authoritative name per lifecycle state. These replace the legacy
# AI_ACTION_* strings going forward; the Daily Report keeps a temporary
# read-side alias for historical rows until one clean observation window has
# passed (then the alias is retired). Never dual-write both names.
EV_SESSION_STARTED = "SESSION_STARTED"
EV_SESSION_RESUMED = "SESSION_RESUMED"           # reserved (not emitted yet)
EV_RECOMMENDATION_GENERATED = "RECOMMENDATION_GENERATED"
EV_NAVIGATION_OFFERED = "NAVIGATION_OFFERED"
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
EV_OPS_CONSOLE_ACCESS = "OPS_CONSOLE_ACCESS"
EV_OPS_CONSOLE_AUTH_FAILURE = "OPS_CONSOLE_AUTH_FAILURE"

# Journey stages (coarse, safe to store).
STAGE_LANDING = "LANDING"
STAGE_RECOMMENDATION = "RECOMMENDATION"
STAGE_NAVIGATION = "NAVIGATION"
STAGE_SUPPORT = "SUPPORT"

# Purpose-specific consent scopes (never inferred; the caller passes the
# effective scope for that event). VARCHAR(32) stores a single effective scope;
# a structured multi-scope representation can be layered on later.
CONSENT_FUNCTIONAL = "functional"
CONSENT_ANALYTICS = "analytics"
CONSENT_SENSITIVE_AI = "sensitive_ai"
CONSENT_VOICE = "voice"
CONSENT_ATTACHMENT = "attachment_processing"

# Typed columns added to ai_events by the Part-B idempotent migration.
_AI_EVENTS_EXTRA_COLS = (
    ("request_id", "VARCHAR(64) NULL"),
    ("provider", "VARCHAR(24) NULL"),
    ("model", "VARCHAR(64) NULL"),
    ("workload", "VARCHAR(32) NULL"),
    ("consent_scope", "VARCHAR(32) NULL"),
)


# Hosts a navigation action is allowed to point at. Authoritative server-side
# policy; the browser safeUrl() remains defence-in-depth, not the decision.
_SAFE_NAV_HOSTS = (
    "optiwar.com", "www.optiwar.com", "in.optiwar.com", "optiwar.in",
    "www.optiwar.in",
)


def is_safe_nav_url(url):
    """Deterministic server-side navigation-safety check. Same-origin optiwar
    paths only. Returns True for a site-relative path ('/x') or an absolute URL
    on an approved optiwar host over http(s); False for anything else
    (external host, protocol-relative '//host', javascript:/data: schemes)."""
    if not url:
        return True  # no navigation to gate
    u = str(url).strip()
    if not u:
        return True
    if u.startswith("//"):
        return False
    if u.startswith("/"):
        return True
    try:
        from urllib.parse import urlsplit
        p = urlsplit(u)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    return host in _SAFE_NAV_HOSTS


def sanitize_url_for_event(url):
    """Return a storage-safe URL for events: scheme+host+path only, query and
    fragment stripped (a query string can carry email/token/PII). Returns None
    for a falsy input."""
    if not url:
        return None
    try:
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(str(url))
        return urlunsplit((p.scheme, p.netloc, p.path, "", "")) or str(url).split("?", 1)[0]
    except Exception:
        return str(url).split("?", 1)[0]

# Catalog filters that identify a recommendation, in URL order.
NAV_FILTER_KEYS = ('color', 'shape', 'facefit', 'min_price', 'max_price')


def filtered_listing_url(filters):
    """Frames-listing URL carrying the recommendation's own filters so a
    multi-product recommendation lands on *those* filtered results instead of a
    generic catalogue. Falls back to the generic listing when no filters known."""
    from urllib.parse import urlencode
    ordered = [(k, str(filters[k])) for k in NAV_FILTER_KEYS
               if filters and filters.get(k) not in (None, '')]
    if not ordered:
        return FRAMES_LISTING_FALLBACK
    return "%s?%s" % (FRAMES_LISTING_FALLBACK, urlencode(ordered))


def canary_allows(actions_enabled, canary_only, cookie_ok, contact_email, allow_emails):
    """Pure decision for the ACR customer-facing canary gate (safeguard #3).

    - ``actions_enabled`` false -> off for everyone (legacy stable path).
    - ``canary_only`` true      -> on only for a valid canary cookie or a
                                    ``contact_email`` in the ``allow_emails``
                                    (comma-separated) list.
    - ``canary_only`` false     -> on for all sessions (post-canary rollout).
    """
    if not actions_enabled:
        return False
    if not canary_only:
        return True
    if cookie_ok:
        return True
    allow = {e.strip().lower() for e in (allow_emails or '').split(',') if e.strip()}
    return bool(contact_email and str(contact_email).strip().lower() in allow)


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
        _ensure_ai_events_columns(cur)
    finally:
        conn.close()


def _ensure_ai_events_columns(cur):
    """Idempotently add the Part-B typed columns to ai_events. Additive only;
    each column is guarded by an information_schema check so re-runs and
    read-only replicas never error. Best-effort: a failure here must not block
    boot or the event path (callers already run ensure_schema best-effort)."""
    for name, decl in _AI_EVENTS_EXTRA_COLS:
        try:
            cur.execute(
                """SELECT COUNT(*) FROM information_schema.COLUMNS
                   WHERE table_schema=DATABASE() AND table_name='ai_events'
                     AND column_name=%s""",
                (name,),
            )
            row = cur.fetchone()
            exists = (row[0] if isinstance(row, (list, tuple)) else
                      list(row.values())[0]) if row else 0
            if not exists:
                cur.execute("ALTER TABLE ai_events ADD COLUMN %s %s" % (name, decl))
        except Exception:
            # Column may already exist / insufficient grant / replica — skip.
            pass
    for idx, cols in (("idx_provider_model", "provider, model"),
                      ("idx_request", "request_id")):
        try:
            cur.execute(
                """SELECT COUNT(*) FROM information_schema.STATISTICS
                   WHERE table_schema=DATABASE() AND table_name='ai_events'
                     AND index_name=%s""",
                (idx,),
            )
            row = cur.fetchone()
            has = (row[0] if isinstance(row, (list, tuple)) else
                   list(row.values())[0]) if row else 0
            if not has:
                cur.execute("ALTER TABLE ai_events ADD KEY %s (%s)" % (idx, cols))
        except Exception:
            pass


# ─── Canonical event stream (A2 / minimum of the ACR-5 event model) ───

def log_event(db, event_type, session_id=None, action_id=None, journey_stage=None,
              action_type=None, page_url=None, success=None, failure_code=None,
              duration_ms=None, payload=None, request_id=None, provider=None,
              model=None, workload=None, consent_scope=None):
    """Append an AI event. Best-effort: never raises into the request path.

    The typed Part-B columns (request_id/provider/model/workload/consent_scope)
    are written when present. If the columns are absent (migration not yet
    applied on this DB), the INSERT falls back to the legacy column set so a
    lagging schema never drops the event."""
    page_url = sanitize_url_for_event(page_url)
    success_i = None if success is None else (1 if success else 0)
    payload_s = json.dumps(payload) if payload else None
    try:
        cur = db.cursor()
        cur.execute(
            """INSERT INTO ai_events
                  (event_id, event_type, session_id, action_id, journey_stage,
                   action_type, page_url, success, failure_code, duration_ms,
                   payload, request_id, provider, model, workload, consent_scope,
                   created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
            (uuid.uuid4().hex, event_type, session_id, action_id, journey_stage,
             action_type, page_url, success_i, failure_code, duration_ms,
             payload_s, request_id, provider, model, workload, consent_scope),
        )
        return
    except Exception:
        pass
    # Fallback for a DB where the Part-B columns don't exist yet.
    try:
        cur = db.cursor()
        cur.execute(
            """INSERT INTO ai_events
                  (event_id, event_type, session_id, action_id, journey_stage,
                   action_type, page_url, success, failure_code, duration_ms,
                   payload, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
            (uuid.uuid4().hex, event_type, session_id, action_id, journey_stage,
             action_type, page_url, success_i, failure_code, duration_ms, payload_s),
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
    log_event(db, EV_NAVIGATION_OFFERED, session_id=session_id, action_id=action_id,
              action_type=action_type, journey_stage=STAGE_NAVIGATION,
              payload={'target_path': sanitize_url_for_event(target)})
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
    """Best-effort status update; never raises into the request path.

    For the PENDING->CONFIRMED edge this emits exactly one ACTION_CONFIRMED
    event (guarded by the WHERE status='PENDING' rowcount), so confirmation is
    counted once regardless of which call site confirms the action."""
    if not action_id:
        return False
    try:
        cur = db.cursor()
        if status == 'CONFIRMED':
            cur.execute(
                """UPDATE ai_actions SET status='CONFIRMED', resolved_at=NOW()
                   WHERE action_id=%s AND status='PENDING'""",
                (action_id,),
            )
            if cur.rowcount and cur.rowcount > 0:
                cur.execute(
                    "SELECT session_id, action_type FROM ai_actions WHERE action_id=%s",
                    (action_id,),
                )
                r = cur.fetchone()
                if r:
                    sid = r['session_id'] if isinstance(r, dict) else r[0]
                    at = r['action_type'] if isinstance(r, dict) else r[1]
                    log_event(db, EV_ACTION_CONFIRMED, session_id=sid,
                              action_id=action_id, action_type=at,
                              journey_stage=STAGE_NAVIGATION)
            return True
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
    sid = row['session_id'] if isinstance(row, dict) else row[0]
    at = row['action_type'] if isinstance(row, dict) else row[1]
    # Executed is recorded ONLY here, on the verified browser callback — never
    # optimistically at confirmation time — so it is counted exactly once.
    mark_action(db, action_id, 'EXECUTED' if success else 'FAILED',
                result_code=failure_code, duration_ms=duration_ms)
    log_event(db, EV_ACTION_EXECUTED if success else EV_ACTION_FAILED,
              session_id=sid, action_id=action_id, action_type=at,
              journey_stage=STAGE_NAVIGATION, success=bool(success),
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

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

# How long a confirmed action may take to report arrival before it counts as
# stranded, measured from the confirmation. Lives here rather than in acr_qc
# because both the read side (QC) and the write side (superseding a confirmed
# action) have to answer "is this still in flight?" the same way.
EXECUTION_TTL_SECONDS = 120

FRAMES_LISTING_FALLBACK = "/eyeglasses/all-spectacle-frames.html"

# ─── Step 5: closure/sweeper constants ───
# Inactivity after which a session becomes an *abandonment candidate* — i.e.
# eligible for closure. This is NOT the terminal outcome: a resumable shopper
# can still return and reset last_activity. The immutable SESSION_OUTCOME is
# only written at the real terminal boundary (session archived/closed by the
# existing stale-session lifecycle), never at the 120-minute mark.
ABANDONMENT_CANDIDATE_MINUTES = 120

# The terminal boundary for emitting SESSION_OUTCOME. A session is only given an
# immutable outcome once it reaches this status (set by archive_stale_sessions).
TERMINAL_SESSION_STATUS = "archived"

# Purchase attribution horizon. An order is attributed to a session only if it
# was placed between the session start and this many hours after the session's
# last activity. This bounds *commerce attribution* only — it is recorded as the
# attribution_window alongside the order, and never rewrites the conversation's
# immutable outcome.
PURCHASE_ATTRIBUTION_HOURS = 24

# ─── Tri-state truth ───
# An authoritative probe answers TRUE, FALSE or UNKNOWN. UNKNOWN is what a
# failed, denied, missing or timed-out query returns.
#
# Collapsing UNKNOWN into FALSE is acceptable for a dashboard tile and unsafe
# for an immutable customer outcome: a denied SELECT on `orders` would read as
# "no order", and the shopper who actually bought would be permanently recorded
# as ABANDONED. When a probe that could change the answer is UNKNOWN we write
# nothing and retry on the next sweep.
TRUTH_TRUE = "TRUE"
TRUTH_FALSE = "FALSE"
TRUTH_UNKNOWN = "UNKNOWN"


def truth_or(*values):
    """OR over tri-state truth. TRUE dominates; UNKNOWN beats FALSE.

    One source proving something happened settles it even if another source is
    unreadable, but "nothing found" from a source that failed is not evidence of
    absence.
    """
    if any(v == TRUTH_TRUE for v in values):
        return TRUTH_TRUE
    if any(v == TRUTH_UNKNOWN for v in values):
        return TRUTH_UNKNOWN
    return TRUTH_FALSE


# ─── Conversational session outcomes ───
# The immutable record of how the *conversation* ended. Purchase is deliberately
# absent: a shopper can end a chat, have the session archived, return days later
# and buy. Those are two different facts, and recording the purchase must not
# require rewriting history. Commerce lands in the separate attribution ledger.
#
# Precedence (higher wins). Never decided by an LLM:
# ESCALATED  <- handover/ticket truth
# FAILED     <- terminal *unrecovered* failure
# ABANDONED  <- archived without a normal resolution (matured candidate)
# ANSWERED   <- residual: archived after a normal resolution, none of the above
OUTCOME_ESCALATED = "ESCALATED"
OUTCOME_FAILED = "FAILED"
OUTCOME_ABANDONED = "ABANDONED"
OUTCOME_ANSWERED = "ANSWERED"
_OUTCOME_PRIORITY = (
    OUTCOME_ESCALATED,
    OUTCOME_FAILED,
    OUTCOME_ABANDONED,
    OUTCOME_ANSWERED,
)

# Commerce attribution vocabulary, deliberately kept out of _OUTCOME_PRIORITY.
COMMERCE_PURCHASED = "PURCHASED"

# Why an outcome could not be finalised on this sweep. Recorded, then retried.
DEFER_ESCALATION_TRUTH = "escalation_truth_unavailable"
DEFER_FAILURE_TRUTH = "failure_truth_unavailable"
DEFER_RESOLUTION_TRUTH = "resolution_truth_unavailable"
DEFER_ORDER_TRUTH = "order_truth_unavailable"


def _record_deferrals(db, deferred):
    """Emit OUTCOME_DEFERRED once per session+reason, not once per sweep.

    A deferral persists until the truth source comes back, and the sweep runs
    every 15 minutes, so emitting unconditionally would write ~96 identical
    events per session per day and bury the event stream under the very
    condition it is meant to make visible. The event marks the *onset* of a
    deferral; the current backlog is reported by the job's summary line.
    """
    for d in deferred:
        already = _probe(
            db,
            """SELECT 1 FROM ai_events
               WHERE session_id=%s AND event_type=%s AND failure_code=%s
               LIMIT 1""",
            (d['session_id'], EV_OUTCOME_DEFERRED, d['reason']),
        )
        if already == TRUTH_TRUE:
            continue
        log_event(db, EV_OUTCOME_DEFERRED, session_id=d['session_id'],
                  success=False, failure_code=d['reason'],
                  payload={'reason': d['reason']})


def decide_session_outcome(is_escalated=TRUTH_FALSE, is_failed=TRUTH_FALSE,
                           normally_resolved=TRUTH_FALSE):
    """Decide one conversation's immutable outcome (pure, tri-state).

    Returns ``(outcome, deferred_reason)``. Exactly one is non-None: either the
    outcome is established, or it is deferred because a probe that could still
    change the answer is UNKNOWN.

    Precedence is evaluated highest-first, and a probe is only allowed to block
    when it could actually change the result — if escalation is TRUE the session
    is ESCALATED even when the failure probe is unreadable, because no value of
    the lower-priority probe could alter that. That keeps deferral rare and
    honest rather than making every unreadable table stall the sweep.
    """
    if is_escalated == TRUTH_TRUE:
        return OUTCOME_ESCALATED, None
    if is_escalated == TRUTH_UNKNOWN:
        return None, DEFER_ESCALATION_TRUTH

    if is_failed == TRUTH_TRUE:
        return OUTCOME_FAILED, None
    if is_failed == TRUTH_UNKNOWN:
        return None, DEFER_FAILURE_TRUTH

    if normally_resolved == TRUTH_TRUE:
        return OUTCOME_ANSWERED, None
    if normally_resolved == TRUTH_UNKNOWN:
        # ANSWERED vs ABANDONED are both immutable and both wrong if guessed.
        return None, DEFER_RESOLUTION_TRUTH
    return OUTCOME_ABANDONED, None

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
# Commerce attribution is a separate fact from how the conversation ended, so it
# gets its own event rather than overwriting SESSION_OUTCOME.
EV_COMMERCE_OUTCOME = "COMMERCE_OUTCOME"
# A sweep that could not establish authoritative truth records why, so a
# deferred outcome is visible in the event stream instead of looking like an
# unprocessed session.
EV_OUTCOME_DEFERRED = "OUTCOME_DEFERRED"
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

# Typed columns and indexes added by the Part-B idempotent migration. These are
# the single source of truth for the additive schema: ensure_schema() applies
# them at boot, and deploy/deploy.py reads them to plan and apply the same
# migration deliberately beforehand. Adding one here is enough.
_AI_EVENTS_EXTRA_COLS = (
    ("request_id", "VARCHAR(64) NULL"),
    ("provider", "VARCHAR(24) NULL"),
    ("model", "VARCHAR(64) NULL"),
    ("workload", "VARCHAR(32) NULL"),
    ("consent_scope", "VARCHAR(32) NULL"),
)
_AI_EVENTS_EXTRA_IDX = (
    ("idx_provider_model", "provider, model"),
    ("idx_request", "request_id"),
)
_AI_ACTIONS_EXTRA_IDX = (
    ("idx_status_expires", "status, expires_at"),
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
                KEY idx_created (created_at),
                KEY idx_status_expires (status, expires_at)
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
        _ensure_ai_actions_indexes(cur)
    finally:
        conn.close()


def _ensure_ai_actions_indexes(cur):
    """Idempotently add the index the Step-5 expiry sweep needs on an existing
    ai_actions table. (status, expires_at) turns the sweep's
    ``status='PENDING' AND expires_at < NOW() ORDER BY expires_at`` from a full
    scan + filesort into an index range scan. Additive and best-effort."""
    for idx, cols in _AI_ACTIONS_EXTRA_IDX:
        try:
            cur.execute(
                """SELECT COUNT(*) FROM information_schema.STATISTICS
                   WHERE table_schema=DATABASE() AND table_name='ai_actions'
                     AND index_name=%s""",
                (idx,),
            )
            row = cur.fetchone()
            has = (row[0] if isinstance(row, (list, tuple)) else
                   list(row.values())[0]) if row else 0
            if not has:
                cur.execute(
                    "ALTER TABLE ai_actions ADD KEY %s (%s)" % (idx, cols))
        except Exception:
            pass


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
    for idx, cols in _AI_EVENTS_EXTRA_IDX:
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

    Returns the ``event_id`` actually written, or None if the event could not be
    stored — so a caller that needs to know whether the event landed (e.g. the
    closure sweeps, which must not silently lose a terminal event) can check,
    and one that doesn't can keep ignoring the result.

    The typed Part-B columns (request_id/provider/model/workload/consent_scope)
    are written when present. If the columns are absent (migration not yet
    applied on this DB), the INSERT falls back to the legacy column set so a
    lagging schema never drops the event."""
    page_url = sanitize_url_for_event(page_url)
    success_i = None if success is None else (1 if success else 0)
    payload_s = json.dumps(payload) if payload else None
    event_id = uuid.uuid4().hex
    try:
        cur = db.cursor()
        cur.execute(
            """INSERT INTO ai_events
                  (event_id, event_type, session_id, action_id, journey_stage,
                   action_type, page_url, success, failure_code, duration_ms,
                   payload, request_id, provider, model, workload, consent_scope,
                   created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
            (event_id, event_type, session_id, action_id, journey_stage,
             action_type, page_url, success_i, failure_code, duration_ms,
             payload_s, request_id, provider, model, workload, consent_scope),
        )
        return event_id
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
            (event_id, event_type, session_id, action_id, journey_stage,
             action_type, page_url, success_i, failure_code, duration_ms, payload_s),
        )
        return event_id
    except Exception:
        return None


# ─── Structured actions (A1) ───

def create_pending_action(db, session_id, action_type, target, source_message_id=None,
                          ttl_seconds=PENDING_TTL_SECONDS):
    """Persist a pending action, superseding any earlier live one of the same
    type for the session so a later confirmation resolves the latest offer.

    Best-effort: if the additive tables are missing (schema creation is itself
    best-effort at boot) this returns None instead of raising into the chat
    reply path, so action bookkeeping can never break a customer conversation."""
    stranded = []
    try:
        cur = db.cursor()
        # An action the customer *accepted* that is about to be replaced is a
        # broken journey, and SUPERSEDED does not say so. Read those rows before
        # the update, because afterwards the only difference between "nobody
        # answered the offer" and "the customer said yes and nothing happened"
        # is gone — and the second is the defect worth reporting.
        cur.execute(
            """SELECT action_id FROM ai_actions
               WHERE session_id=%s AND action_type=%s AND status='CONFIRMED'
                 AND resolved_at IS NOT NULL
                 AND resolved_at < DATE_SUB(NOW(), INTERVAL %s SECOND)""",
            (session_id, action_type, EXECUTION_TTL_SECONDS),
        )
        for row in (cur.fetchall() or ()):
            stranded.append(row['action_id'] if isinstance(row, dict) else row[0])
        cur.execute(
            # CONFIRMED counts as live. A confirmed action whose browser result
            # never arrived is not finished, and leaving it out of the supersede
            # means a new offer strands it at CONFIRMED with no terminal state
            # for any sweep to reach.
            """UPDATE ai_actions SET status='SUPERSEDED', resolved_at=NOW()
               WHERE session_id=%s AND action_type=%s
                 AND status IN ('PENDING','CONFIRMED')""",
            (session_id, action_type),
        )
        action_id = uuid.uuid4().hex
        # Recorded before the replacement row exists: if the insert fails the
        # supersede has already happened, and the evidence must not depend on
        # the rest of this function succeeding.
        for old in stranded:
            log_event(db, EV_ACTION_EXPIRED, session_id=session_id, action_id=old,
                      action_type=action_type, journey_stage=STAGE_NAVIGATION,
                      success=False, failure_code='confirmed_never_executed',
                      payload={'from_status': 'CONFIRMED', 'reason': 'superseded',
                               'superseded_by': action_id})
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


# ─── Step 5: closure / sweeper (ACTION_EXPIRED + SESSION_OUTCOME) ───
#
# Two edge-triggered, idempotent, bounded sweeps intended to run under a single
# runner (advisory lock) on a short cron. Both honour a dry-run mode that
# computes and returns what WOULD change without writing anything.
#
#   1. expire_due_actions()  — PENDING ai_actions past expires_at become
#      EXPIRED; exactly one ACTION_EXPIRED event per row this run actually
#      transitions (the atomic UPDATE ... WHERE status='PENDING' rowcount is
#      the edge trigger, so two overlapping runs can't double-emit).
#
#   2. finalize_archived_session_outcomes() — archived sessions with no
#      recorded outcome get exactly one immutable SESSION_OUTCOME. Idempotency
#      is enforced by an atomic INSERT-claim into the ai_session_outcomes ledger
#      (session_id PRIMARY KEY): the run that wins the INSERT is the only one
#      that emits the event, so overlapping runs can't produce duplicate
#      terminal outcomes. The 120-minute mark only makes a session an
#      abandonment *candidate*; the terminal outcome waits for TERMINAL_SESSION_STATUS.

LEDGER_COLLATION = 'utf8mb4_general_ci'
# session_id is VARCHAR(64) PRIMARY KEY in both ledgers, so one declaration
# serves the alignment ALTER for either table.
_LEDGER_TABLES = ('ai_session_outcomes', 'ai_session_commerce')


def _align_ledger_collation(cur, allow_ddl=True):
    """Convert an existing ledger's session_id to the collation the sweeps join
    in. CREATE TABLE IF NOT EXISTS is a no-op on a ledger created before the
    collation was stated, so on those installations the anti-joins would still
    raise 'illegal mix of collations' and nothing would ever be recorded.

    Best-effort and idempotent: reads the current collation first and only
    alters on a genuine mismatch, so a correct table is never rewritten and a
    missing ALTER grant degrades to the previous behaviour.

    With ``allow_ddl`` false the mismatch is only *reported*. That distinction
    matters: this ALTER rebuilds a primary key, and a preview run that claims to
    write nothing must not rewrite a production table to make its own query
    work. Returns the DDL a live run would issue, as descriptions."""
    pending = []
    for table in _LEDGER_TABLES:
        try:
            cur.execute(
                """SELECT COLLATION_NAME FROM information_schema.COLUMNS
                   WHERE table_schema=DATABASE() AND table_name=%s
                     AND column_name='session_id'""",
                (table,),
            )
            row = cur.fetchone()
            if not row:
                # No such column: the table does not exist yet, so the CREATE
                # above either made it correctly or was itself suppressed.
                continue
            current = (row[0] if isinstance(row, (list, tuple))
                       else list(row.values())[0])
            if current and current != LEDGER_COLLATION:
                pending.append("%s.session_id is %s, needs %s"
                               % (table, current, LEDGER_COLLATION))
                if allow_ddl:
                    cur.execute(
                        "ALTER TABLE %s MODIFY session_id VARCHAR(64) "
                        "COLLATE %s NOT NULL" % (table, LEDGER_COLLATION))
        except Exception:
            # Insufficient grant / replica / concurrent DDL — the sweep will
            # report the collation error rather than this being silently wrong.
            pass
    return pending


def ensure_closure_schema(get_conn, allow_ddl=True):
    """Create the additive SESSION_OUTCOME idempotency ledger if absent. The
    session_id PRIMARY KEY is the one-per-session guarantee (atomic claim), not
    a SELECT-then-INSERT. Best-effort.

    The only alteration ever made to an existing table is aligning session_id's
    collation with the one the sweeps join in — a ledger created before that
    collation was stated cannot be joined at all.

    ``allow_ddl=False`` makes this a report: nothing is created or altered and
    the schema work a live run would do is returned instead. A preview run that
    promises to change nothing cannot be the thing that rebuilds a primary key,
    so the caller that suppresses every write suppresses this too.

    event_id references the SESSION_OUTCOME row in ai_events and stays NULL until
    that event has actually landed; a NULL therefore means "claimed but not yet
    proven", which is what makes a failed event write retryable.

    Returns the DDL a live run would issue, as descriptions ([] when the schema
    is already correct)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        if not allow_ddl:
            return _report_ledger_schema(cur)
        cur.execute(
            """CREATE TABLE IF NOT EXISTS ai_session_outcomes (
                session_id VARCHAR(64) COLLATE utf8mb4_general_ci PRIMARY KEY,
                outcome    VARCHAR(16) NOT NULL,
                event_id   VARCHAR(36) NULL,
                created_at DATETIME NOT NULL,
                KEY idx_outcome (outcome),
                KEY idx_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        )
        # Commerce attribution is a separate fact with its own lifetime: an
        # order can arrive long after the conversation was archived and given
        # its immutable outcome. Keeping it in its own table is what lets the
        # purchase be recorded without rewriting that history.
        cur.execute(
            """CREATE TABLE IF NOT EXISTS ai_session_commerce (
                session_id       VARCHAR(64) COLLATE utf8mb4_general_ci PRIMARY KEY,
                order_id         VARCHAR(64) NOT NULL,
                attribution_type VARCHAR(32) NOT NULL,
                attribution_window_hours INT NOT NULL,
                event_id         VARCHAR(36) NULL,
                created_at       DATETIME NOT NULL,
                KEY idx_order (order_id),
                KEY idx_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        )
        return _align_ledger_collation(cur)
    finally:
        conn.close()


def _report_ledger_schema(cur):
    """What a live run would create or alter, writing nothing."""
    pending = []
    for table in _LEDGER_TABLES:
        try:
            cur.execute(
                """SELECT COUNT(*) FROM information_schema.TABLES
                   WHERE table_schema=DATABASE() AND table_name=%s""",
                (table,),
            )
            row = cur.fetchone()
            exists = (row[0] if isinstance(row, (list, tuple))
                      else list(row.values())[0]) if row else 0
            if not exists:
                pending.append("%s does not exist, needs CREATE" % table)
        except Exception:
            pass
    return pending + _align_ledger_collation(cur, allow_ddl=False)


# Statuses a sweep may still resolve. PENDING is an offer nobody answered;
# CONFIRMED is an offer the customer *accepted* whose execution was never
# reported back — the browser navigated away, or the action-result POST never
# landed. Both are unfinished, and only one of them used to be swept.
UNRESOLVED_STATUSES = ('PENDING', 'CONFIRMED')


def find_due_actions(db, limit=500):
    """Return unresolved actions already past their expiry. Bounded by ``limit``
    and served by the idx_status_expires (status, expires_at) index, so the
    filter and the ORDER BY are index range scans rather than a full scan +
    filesort. Read-only; used by both dry-run and live sweeps so the two see the
    same candidate set.

    Each row carries its current ``status`` because the sweep must claim the
    exact status it observed, and because expiring from CONFIRMED is a different
    fact about the customer than expiring from PENDING."""
    cur = db.cursor()
    cur.execute(
        """SELECT action_id, session_id, action_type, status FROM ai_actions
           WHERE status IN ('PENDING','CONFIRMED')
             AND expires_at IS NOT NULL AND expires_at < NOW()
           ORDER BY expires_at ASC LIMIT %s""",
        (int(limit),),
    )
    return cur.fetchall() or []


def expire_due_actions(db, dry_run=True, limit=500):
    """Expire unresolved actions past expires_at. Edge-triggered and idempotent:
    each row is claimed with an atomic UPDATE guarded by the status it was found
    in, and ACTION_EXPIRED is emitted only for rows this run actually
    transitioned.

    An action expiring from CONFIRMED is reported with failure_code
    ``confirmed_never_executed`` rather than ``expired``. The distinction is the
    whole point: "nobody answered the offer" is a conversion metric, while "the
    customer said yes and nothing happened" is a defect, and collapsing them
    into one code loses the only signal that separates the two.

    Returns a dict {'candidates': [...], 'expired': [...], 'event_failed': [...]}.
    In dry_run the 'expired' list is what WOULD be expired and nothing is written.

    A row whose status transition succeeds but whose ACTION_EXPIRED event write
    fails is reported in 'event_failed' so the runner logs it loudly: the action
    state is already correct, but the missing event must be visible rather than
    silently absent from the canonical stream."""
    rows = find_due_actions(db, limit=limit)
    candidates = []
    for r in rows:
        aid = r['action_id'] if isinstance(r, dict) else r[0]
        sid = r['session_id'] if isinstance(r, dict) else r[1]
        at = r['action_type'] if isinstance(r, dict) else r[2]
        st = (r.get('status') if isinstance(r, dict)
              else (r[3] if len(r) > 3 else None)) or 'PENDING'
        candidates.append({'action_id': aid, 'session_id': sid,
                           'action_type': at, 'status': st})
    if dry_run:
        return {'candidates': candidates, 'expired': list(candidates),
                'event_failed': []}
    expired = []
    event_failed = []
    for c in candidates:
        try:
            cur = db.cursor()
            cur.execute(
                """UPDATE ai_actions SET status='EXPIRED', resolved_at=NOW()
                   WHERE action_id=%s AND status=%s""",
                (c['action_id'], c['status']),
            )
            if cur.rowcount and cur.rowcount > 0:
                code = ('confirmed_never_executed' if c['status'] == 'CONFIRMED'
                        else 'expired')
                ok = log_event(db, EV_ACTION_EXPIRED, session_id=c['session_id'],
                               action_id=c['action_id'], action_type=c['action_type'],
                               journey_stage=STAGE_NAVIGATION, success=False,
                               failure_code=code,
                               payload={'from_status': c['status']})
                expired.append(c)
                if not ok:
                    event_failed.append(c)
        except Exception:
            # Best-effort: a single row failing must not abort the batch.
            pass
    return {'candidates': candidates, 'expired': expired,
            'event_failed': event_failed}


def _scalar1(cur):
    row = cur.fetchone()
    if not row:
        return None
    return list(row.values())[0] if isinstance(row, dict) else row[0]


def _probe(db, sql, params):
    """Tri-state existence probe: TRUE if the LIMIT-1 lookup returns a row,
    FALSE if it provably does not, UNKNOWN if the query could not be answered.

    A missing table, a denied grant, a timeout or any other failure yields
    UNKNOWN. It must never be reported as FALSE — the caller writes immutable
    customer outcomes, and "I could not read the orders table" is not the same
    statement as "this customer did not order".
    """
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        return TRUTH_TRUE if _scalar1(cur) is not None else TRUTH_FALSE
    except Exception:
        return TRUTH_UNKNOWN


def _probe_value(db, sql, params):
    """Fetch one scalar as ``(truth, value)``.

    Same tri-state contract as :func:`_probe`, but keeps the value: attribution
    needs the order_id, not merely the knowledge that an order exists.
    """
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        val = _scalar1(cur)
        return (TRUTH_TRUE, val) if val is not None else (TRUTH_FALSE, None)
    except Exception:
        return TRUTH_UNKNOWN, None


# Events that demonstrate the journey carried on working after a failure. A
# provider timeout followed by a recommendation the customer then acted on is a
# recovered blip, not a failed conversation.
_RECOVERY_EVENTS = (
    EV_RECOMMENDATION_GENERATED,
    EV_NAVIGATION_OFFERED,
    EV_ACTION_CONFIRMED,
    EV_ACTION_EXECUTED,
)

# Failure signals considered when deciding whether a session ended in failure.
_FAILURE_EVENTS = (
    EV_ACTION_FAILED,
    EV_PROVIDER_FAILURE,
    EV_MODEL_TIMEOUT,
    EV_ADMISSION_503,
)


def terminal_failure_truth(db, session_id):
    """Whether this session ended in a *terminal unrecovered* failure.

    FAILED must not mean "a failure happened at some point". The common path is
    provider timeout -> retry -> recommendation -> navigation -> purchase, and
    labelling that conversation FAILED would be simply untrue.

    The rule is therefore: take the most recent failure event, then look for any
    successful AI/action event after it. Something succeeding later *is* the
    proof of recovery. No failure at all is FALSE; an unreadable event stream is
    UNKNOWN.
    """
    try:
        cur = db.cursor()
        cur.execute(
            """SELECT MAX(created_at) FROM ai_events
               WHERE session_id=%s AND event_type IN (%s,%s,%s,%s)""",
            (session_id,) + _FAILURE_EVENTS,
        )
        last_failure_at = _scalar1(cur)
    except Exception:
        return TRUTH_UNKNOWN
    if last_failure_at is None:
        return TRUTH_FALSE

    recovered = _probe(
        db,
        """SELECT 1 FROM ai_events
           WHERE session_id=%s AND created_at > %s
             AND (event_type IN (%s,%s,%s,%s)
                  OR (event_type=%s AND success=1)) LIMIT 1""",
        (session_id, last_failure_at) + _RECOVERY_EVENTS + (EV_MODEL_CALL,),
    )
    if recovered == TRUTH_UNKNOWN:
        return TRUTH_UNKNOWN
    return TRUTH_FALSE if recovered == TRUTH_TRUE else TRUTH_TRUE


def gather_session_truth(db, session_id):
    """Collect the tri-state truth signals for one conversation's outcome.

    Indexed LIMIT-1 lookups only, read from chat-event and canonical-event
    tables — never inferred by a model. Returns the kwargs for
    :func:`decide_session_outcome`.

    Order truth is deliberately absent: purchase is commerce attribution, not a
    conversation outcome (see :func:`attribute_session_commerce`).
    """
    # Escalation: a human agent replied, or a canonical handover/ticket event.
    is_escalated = truth_or(
        _probe(
            db,
            """SELECT 1 FROM chat_events
               WHERE session_id=%s AND event_type='agent_reply' LIMIT 1""",
            (session_id,),
        ),
        _probe(
            db,
            """SELECT 1 FROM ai_events
               WHERE session_id=%s AND event_type IN (%s,%s) LIMIT 1""",
            (session_id, EV_HANDOVER_ESCALATED, EV_KET_TICKET_CREATED),
        ),
    )
    is_failed = terminal_failure_truth(db, session_id)
    normally_resolved = _probe(
        db,
        """SELECT 1 FROM chat_events
           WHERE session_id=%s AND event_type='session_resolved' LIMIT 1""",
        (session_id,),
    )
    return {'is_escalated': is_escalated, 'is_failed': is_failed,
            'normally_resolved': normally_resolved}


# How an order was tied to a session. Only one method exists today; naming it
# explicitly means later methods (click-through, cart handoff) are
# distinguishable in analytics instead of silently changing what the column
# means.
ATTRIBUTION_SESSION_WINDOW = "session_window"


def attribute_session_commerce(db, session_id, customer_id, session_created_at,
                               session_last_activity=None):
    """Find the order attributable to this session, if any.

    Returns ``(truth, record)``. ``record`` carries order_id, attribution_type
    and attribution_window so the basis of the claim is auditable rather than
    implied by a bare PURCHASED label.

    The window is bounded at both ends — session start to last activity plus
    PURCHASE_ATTRIBUTION_HOURS. A lower bound alone would let one order be
    attributed to every earlier session that shopper ever had.
    """
    if customer_id is None:
        return TRUTH_FALSE, None
    truth, order_id = _probe_value(
        db,
        """SELECT order_id FROM orders
           WHERE customer_id=%s AND is_test=0
             AND (%s IS NULL OR date_created >= %s)
             AND (%s IS NULL OR
                  date_created <= %s + INTERVAL %s HOUR)
           ORDER BY date_created ASC LIMIT 1""",
        (customer_id, session_created_at, session_created_at,
         session_last_activity, session_last_activity,
         PURCHASE_ATTRIBUTION_HOURS),
    )
    if truth != TRUTH_TRUE:
        return truth, None
    return TRUTH_TRUE, {
        'session_id': session_id,
        'order_id': order_id,
        'attribution_type': ATTRIBUTION_SESSION_WINDOW,
        'attribution_window_hours': PURCHASE_ATTRIBUTION_HOURS,
    }


def find_sessions_awaiting_outcome(db, limit=500):
    """Archived sessions still owed an immutable outcome event (bounded,
    LEFT JOIN anti-join on the ledger). Read-only.

    A session qualifies when it has no ledger row at all, OR it has a ledger row
    whose event_id is still NULL — i.e. a previous run won the claim but its
    SESSION_OUTCOME event never landed. Including the latter is what makes a
    failed event write retryable instead of silently losing the outcome forever.

    The join collation is stated explicitly because the two tables need not
    share one: chat_sessions predates the ai_* tables and production has it in
    utf8mb4_unicode_ci while the ledger is utf8mb4_general_ci, which makes an
    unqualified comparison an error rather than a mismatch.
    """
    cur = db.cursor()
    cur.execute(
        """SELECT s.session_id, s.customer_id, s.created_at, s.last_activity
           FROM chat_sessions s
           LEFT JOIN ai_session_outcomes o
                  ON o.session_id = s.session_id COLLATE utf8mb4_general_ci
           WHERE s.status=%s AND (o.session_id IS NULL OR o.event_id IS NULL)
           ORDER BY s.last_activity ASC LIMIT %s""",
        (TERMINAL_SESSION_STATUS, int(limit)),
    )
    return cur.fetchall() or []


def finalize_archived_session_outcomes(db, dry_run=True, limit=500):
    """Assign exactly one immutable SESSION_OUTCOME to each archived session
    that has none yet. Idempotent via an atomic INSERT-claim into
    ai_session_outcomes (session_id PRIMARY KEY): only the run that wins the
    claim emits the SESSION_OUTCOME event, so overlapping runs cannot duplicate.

    The ledger row is claimed with event_id NULL and backfilled with the id of
    the SESSION_OUTCOME event that actually landed, so the stored reference is
    always joinable to ai_events. If the event write fails, event_id stays NULL
    and find_sessions_awaiting_outcome offers the session again on a later run
    instead of the outcome being lost forever. A retried session first probes for
    an already-stored SESSION_OUTCOME event and only backfills in that case, so a
    failed backfill cannot turn into a duplicate event.

    A session whose authoritative truth cannot be established is *deferred*, not
    guessed: no ledger row is claimed, an OUTCOME_DEFERRED event records the
    reason, and the session is offered again on the next sweep. Writing a wrong
    immutable outcome is unrecoverable; waiting for the next run costs 15
    minutes.

    Returns {'candidates': [{session_id, outcome}...], 'closed': [...],
    'deferred': [{session_id, reason}...], 'event_failed': [...]}. In dry_run
    nothing is written and 'closed' mirrors 'candidates'."""
    rows = find_sessions_awaiting_outcome(db, limit=limit)
    candidates = []
    deferred = []
    for r in rows:
        sid = r['session_id'] if isinstance(r, dict) else r[0]
        truth = gather_session_truth(db, sid)
        outcome, defer_reason = decide_session_outcome(**truth)
        if defer_reason:
            deferred.append({'session_id': sid, 'reason': defer_reason})
            continue
        candidates.append({'session_id': sid, 'outcome': outcome})
    if dry_run:
        return {'candidates': candidates, 'closed': list(candidates),
                'deferred': deferred, 'event_failed': []}
    _record_deferrals(db, deferred)
    closed = []
    event_failed = []
    for c in candidates:
        try:
            cur = db.cursor()
            cur.execute(
                """INSERT IGNORE INTO ai_session_outcomes
                     (session_id, outcome, event_id, created_at)
                   VALUES (%s,%s,NULL,NOW())""",
                (c['session_id'], c['outcome']),
            )
            fresh_claim = bool(cur.rowcount and cur.rowcount > 0)
            if not fresh_claim:
                # Retry of an earlier claim whose event never landed. If the
                # event is in fact present, only repair the reference.
                existing = _existing_outcome_event_id(db, c['session_id'])
                if existing:
                    _backfill_outcome_event_id(db, c['session_id'], existing)
                    continue
            event_id = log_event(db, EV_SESSION_OUTCOME, session_id=c['session_id'],
                                 success=(c['outcome'] != OUTCOME_FAILED),
                                 payload={'outcome': c['outcome']})
            if event_id:
                _backfill_outcome_event_id(db, c['session_id'], event_id)
                closed.append(c)
            else:
                # Claim stays event_id NULL -> retried on a later run.
                event_failed.append(c)
        except Exception:
            pass
    return {'candidates': candidates, 'closed': closed,
            'deferred': deferred, 'event_failed': event_failed}


def find_sessions_awaiting_attribution(db, limit=500):
    """Archived sessions with no commerce-attribution row yet (bounded anti-join).

    Runs independently of the outcome sweep. A session whose conversation was
    already closed as ANSWERED still appears here, because the order may only
    have landed afterwards — that is the whole point of separating the two.

    Collation is stated explicitly for the same reason as the outcome anti-join.
    """
    cur = db.cursor()
    cur.execute(
        """SELECT s.session_id, s.customer_id, s.created_at, s.last_activity
           FROM chat_sessions s
           LEFT JOIN ai_session_commerce c
                  ON c.session_id = s.session_id COLLATE utf8mb4_general_ci
           WHERE s.status=%s AND (c.session_id IS NULL OR c.event_id IS NULL)
           ORDER BY s.last_activity ASC LIMIT %s""",
        (TERMINAL_SESSION_STATUS, int(limit)),
    )
    return cur.fetchall() or []


def attribute_archived_session_commerce(db, dry_run=True, limit=500):
    """Record purchase attribution for archived sessions, separately from their
    conversation outcome.

    Same idempotency mechanism as the outcome ledger — an atomic INSERT-claim on
    a session_id PRIMARY KEY, with event_id backfilled once COMMERCE_OUTCOME has
    actually landed, so a failed event write is retried rather than lost.

    Unknown order truth defers: no row is claimed and the session is retried.
    Recording "no purchase" because the orders table was unreadable would
    understate revenue attribution permanently.

    Returns {'candidates': [...], 'attributed': [...], 'deferred': [...],
    'event_failed': [...]}.
    """
    rows = find_sessions_awaiting_attribution(db, limit=limit)
    candidates = []
    deferred = []
    for r in rows:
        sid = r['session_id'] if isinstance(r, dict) else r[0]
        cid = r['customer_id'] if isinstance(r, dict) else r[1]
        created = r['created_at'] if isinstance(r, dict) else r[2]
        last_act = (r.get('last_activity') if isinstance(r, dict)
                    else (r[3] if len(r) > 3 else None))
        truth, record = attribute_session_commerce(db, sid, cid, created, last_act)
        if truth == TRUTH_UNKNOWN:
            deferred.append({'session_id': sid,
                             'reason': DEFER_ORDER_TRUTH})
            continue
        if truth == TRUTH_TRUE and record:
            candidates.append(record)
    if dry_run:
        return {'candidates': candidates, 'attributed': list(candidates),
                'deferred': deferred, 'event_failed': []}
    _record_deferrals(db, deferred)
    attributed = []
    event_failed = []
    for c in candidates:
        try:
            cur = db.cursor()
            cur.execute(
                """INSERT IGNORE INTO ai_session_commerce
                     (session_id, order_id, attribution_type,
                      attribution_window_hours, event_id, created_at)
                   VALUES (%s,%s,%s,%s,NULL,NOW())""",
                (c['session_id'], c['order_id'], c['attribution_type'],
                 c['attribution_window_hours']),
            )
            fresh_claim = bool(cur.rowcount and cur.rowcount > 0)
            if not fresh_claim:
                existing = _existing_event_id(db, c['session_id'],
                                              EV_COMMERCE_OUTCOME)
                if existing:
                    _backfill_commerce_event_id(db, c['session_id'], existing)
                    continue
            event_id = log_event(db, EV_COMMERCE_OUTCOME,
                                 session_id=c['session_id'], success=True,
                                 payload={'outcome': COMMERCE_PURCHASED,
                                          'order_id': c['order_id'],
                                          'attribution_type': c['attribution_type'],
                                          'attribution_window_hours':
                                              c['attribution_window_hours']})
            if event_id:
                _backfill_commerce_event_id(db, c['session_id'], event_id)
                attributed.append(c)
            else:
                event_failed.append(c)
        except Exception:
            pass
    return {'candidates': candidates, 'attributed': attributed,
            'deferred': deferred, 'event_failed': event_failed}


def _backfill_commerce_event_id(db, session_id, event_id):
    """Point the attribution row at the COMMERCE_OUTCOME event that landed."""
    try:
        cur = db.cursor()
        cur.execute(
            """UPDATE ai_session_commerce SET event_id=%s
               WHERE session_id=%s AND event_id IS NULL""",
            (event_id, session_id),
        )
    except Exception:
        pass


def _existing_event_id(db, session_id, event_type):
    """The event_id of an already-stored event of this type for this session."""
    try:
        cur = db.cursor()
        cur.execute(
            """SELECT event_id FROM ai_events
               WHERE session_id=%s AND event_type=%s
               ORDER BY created_at ASC LIMIT 1""",
            (session_id, event_type),
        )
        return _scalar1(cur)
    except Exception:
        return None


def _existing_outcome_event_id(db, session_id):
    """The event_id of an already-stored SESSION_OUTCOME for this session, if
    any. Used only on the retry path to distinguish 'event never landed' from
    'event landed but the ledger reference did not'."""
    try:
        cur = db.cursor()
        cur.execute(
            """SELECT event_id FROM ai_events
               WHERE session_id=%s AND event_type=%s
               ORDER BY created_at ASC LIMIT 1""",
            (session_id, EV_SESSION_OUTCOME),
        )
        return _scalar1(cur)
    except Exception:
        return None


def _backfill_outcome_event_id(db, session_id, event_id):
    """Point the ledger row at the SESSION_OUTCOME event that actually landed.
    Guarded by event_id IS NULL so an established reference is never rewritten."""
    try:
        cur = db.cursor()
        cur.execute(
            """UPDATE ai_session_outcomes SET event_id=%s
               WHERE session_id=%s AND event_id IS NULL""",
            (event_id, session_id),
        )
    except Exception:
        pass


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

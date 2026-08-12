"""ACR Gate-1 A — QC conversation review, read-only.

Operational visibility into conversation *quality*: which journeys completed,
which answers went nowhere, where the customer had to ask twice, which
recommendations were empty, which navigations failed, and where the assistant
claimed something it did not do.

Two rules shape the whole module.

**Text is read here and never leaves here.** The quality signals that need
message content — a repeated question, a bare-refusal answer, a promise the
assistant did not keep — are *computed* against the text and emitted as a flag,
a count or a similarity bucket. No transcript, no customer wording, no email,
name, phone, prescription or measurement is returned by any function below. The
PII boundary is therefore a property of the interface rather than of a policy
someone has to remember when they write the next consumer.

**It is read-only.** Nothing here writes an event, an outcome or an attribution.
QC review that mutates the stream it reviews cannot be trusted as evidence, and
Step-5 closure already owns those writes.

Sourcing is the canonical stream (``ai_events``/``ai_actions``) with
``chat_messages`` for the content-derived signals only. A session whose
canonical events are absent is reported as *unreviewable* rather than as clean:
silence is not quality.

    from flaskr import acr_qc
    reviews = acr_qc.review_window(db, hours=24)
    summary = acr_qc.summarize(reviews)
"""
import hashlib
import re

try:  # package import inside the app, flat import in tests/tools
    from . import acr
except ImportError:  # pragma: no cover - exercised by the flat-import path
    import acr


# ─── Signal vocabulary ───
#
# Severity is what an operator triages by, so it is declared here rather than
# inferred at the call site: two consumers inferring it differently is how a
# quality board and a daily report come to disagree about the same session.
QC_JOURNEY_COMPLETED = "JOURNEY_COMPLETED"
QC_INCOMPLETE_ANSWER = "INCOMPLETE_ANSWER"
QC_REPEATED_QUESTION = "REPEATED_QUESTION"
QC_ZERO_RESULT_RECOMMENDATION = "ZERO_RESULT_RECOMMENDATION"
QC_FAILED_NAVIGATION = "FAILED_NAVIGATION"
QC_PROMISE_WITHOUT_ACTION = "PROMISE_WITHOUT_ACTION"
QC_ESCALATED = "ESCALATED"
QC_MODEL_FAILURE = "MODEL_FAILURE"
QC_ABANDONED_AFTER_RECOMMENDATION = "ABANDONED_AFTER_RECOMMENDATION"
QC_UNREVIEWABLE = "UNREVIEWABLE"

INFO, WARN, FAIL = "INFO", "WARN", "FAIL"

SIGNALS = {
    QC_JOURNEY_COMPLETED: INFO,
    QC_REPEATED_QUESTION: WARN,
    QC_INCOMPLETE_ANSWER: WARN,
    QC_ZERO_RESULT_RECOMMENDATION: WARN,
    QC_ABANDONED_AFTER_RECOMMENDATION: WARN,
    QC_ESCALATED: WARN,
    QC_UNREVIEWABLE: WARN,
    QC_FAILED_NAVIGATION: FAIL,
    # The assistant told the customer it had done something it had not do. It is
    # the one signal here that is a truthfulness defect rather than a quality
    # one, so it is never below FAIL.
    QC_PROMISE_WITHOUT_ACTION: FAIL,
    QC_MODEL_FAILURE: FAIL,
}

# Replies that answer nothing: a refusal, an apology, or an empty turn. Matched
# on the whole reply, so a message that opens with an apology and then answers
# is not flagged.
_EMPTY_ANSWER_RE = re.compile(
    r"^\W*(i'?m sorry|sorry|unfortunately|i (?:can'?t|cannot|don'?t)\b|"
    r"i (?:do not|don'?t) have\b|no results?|nothing found)"
    r"[^.!?]*[.!?]?\W*$", re.I)

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an the is are do does did have has had get got i you my me we it to of "
    "for in on at with and or can could would will please show me some any "
    "there that this need want looking hi hello thanks ok yes no".split())


def _tokens(text):
    """Content words, lowercased. Stopwords carry no topic, and keeping them
    would make 'do you have X' and 'do you have Y' look like the same question.
    """
    return frozenset(w for w in _WORD_RE.findall((text or "").lower())
                     if w not in _STOPWORDS and len(w) > 1)


def question_fingerprint(text):
    """A stable, non-reversible id for what a message is *about*.

    Returned instead of the message so a repeated-question count can be stored,
    charted and compared across sessions without storing what the customer
    typed. It is a hash of the sorted content words, so it survives reordering
    and punctuation but not a change of topic.
    """
    toks = _tokens(text)
    if not toks:
        return ""
    return hashlib.sha256(" ".join(sorted(toks)).encode("utf-8")).hexdigest()[:16]


def similarity(a, b):
    """Jaccard overlap of content words, 0.0–1.0."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


# A customer rarely repeats themselves word for word; they rephrase. Exact
# fingerprint matching alone would miss "do you have round frames" followed by
# "any round frames in stock", which is the case worth catching.
REPEAT_SIMILARITY = 0.6


def count_repeated_questions(messages, threshold=REPEAT_SIMILARITY):
    """How many customer turns restate an earlier one in the same session.

    Takes messages, returns a number. The wording is not retained.
    """
    asks = [m.get("content") or "" for m in messages
            if (m.get("role") or "").lower() in ("user", "customer")]
    asks = [a for a in asks if _tokens(a)]
    repeats = 0
    for i, later in enumerate(asks):
        for earlier in asks[:i]:
            if similarity(earlier, later) >= threshold:
                repeats += 1
                break
    return repeats


def is_incomplete_answer(text):
    """True when an assistant turn declines or apologises and stops there."""
    if not (text or "").strip():
        return True
    return bool(_EMPTY_ANSWER_RE.match(text.strip()))


def _counts(events):
    out = {}
    for e in events:
        key = e.get("event_type")
        out[key] = out.get(key, 0) + 1
    return out


# Events that only exist once action tracking is instrumented. Their absence in
# a session means the lifecycle was not being recorded then — which is not the
# same as an action having failed, and the difference decides whether a
# pre-instrumentation conversation is reported as a defect.
_LIFECYCLE_EVENTS = (acr.EV_NAVIGATION_OFFERED, acr.EV_ACTION_CONFIRMED,
                     acr.EV_ACTION_EXECUTED, acr.EV_ACTION_FAILED,
                     acr.EV_ACTION_BLOCKED, acr.EV_ACTION_EXPIRED,
                     acr.EV_PROMISE_WITHOUT_ACTION)


def review_session(session_id, events, messages=(), outcome=None, actions=()):
    """Quality signals for one conversation. Pure: no DB, no I/O, no writes.

    ``events`` are canonical ``ai_events`` rows (dicts with at least
    ``event_type``, optionally ``success``/``payload``); ``messages`` are chat
    turns (``role``/``content``); ``actions`` are ``ai_actions`` rows, which
    carry the authoritative per-action status. Returns signals and counts only —
    never text.
    """
    ev = _counts(events)
    signals = []

    def add(name, **detail):
        signals.append(dict(signal=name, severity=SIGNALS[name], **detail))

    if not events:
        # No canonical events at all: this session cannot be judged, and saying
        # so is the honest answer. Reporting it as clean would let an
        # instrumentation outage read as a quality improvement.
        add(QC_UNREVIEWABLE, reason="no canonical events")
        return dict(session_id=session_id, signals=signals, events=ev,
                    reviewable=False)

    executed = ev.get(acr.EV_ACTION_EXECUTED, 0)
    offered = ev.get(acr.EV_NAVIGATION_OFFERED, 0)
    confirmed = ev.get(acr.EV_ACTION_CONFIRMED, 0)
    recommended = ev.get(acr.EV_RECOMMENDATION_GENERATED, 0)

    if executed:
        add(QC_JOURNEY_COMPLETED, executed=executed)

    # Confirmed but never executed is a broken promise the customer *acted on*:
    # they said yes and nothing happened. Failed/blocked actions count the same
    # way, whatever the reason.
    #
    # Prefer ai_actions when we have it. Event arithmetic counts *events*, and a
    # session can legitimately confirm two actions and execute one when the
    # second supersedes the first — subtracting the totals would invent a
    # failure. A row still sitting at CONFIRMED is the real thing.
    if actions:
        stuck = sum(1 for a in actions if (a.get("status") or "") == "CONFIRMED")
        failed = sum(1 for a in actions
                     if (a.get("status") or "") in ("FAILED", "BLOCKED"))
    else:
        stuck = max(confirmed - executed, 0)
        failed = ev.get(acr.EV_ACTION_FAILED, 0) + ev.get(acr.EV_ACTION_BLOCKED, 0)
    # A swept action leaves CONFIRMED for EXPIRED, so counting only live
    # CONFIRMED rows would let the closure sweep erase the very defect this
    # signal exists to report. The expiry event carries which status it came
    # from, and only the confirmed one is a broken journey.
    stuck += sum(1 for e in events
                 if e.get("event_type") == acr.EV_ACTION_EXPIRED
                 and _from_confirmed(e))
    if stuck or failed:
        add(QC_FAILED_NAVIGATION, confirmed_not_executed=stuck, failed=failed)

    if ev.get(acr.EV_PROMISE_WITHOUT_ACTION):
        add(QC_PROMISE_WITHOUT_ACTION,
            count=ev[acr.EV_PROMISE_WITHOUT_ACTION], source="canonical")
    elif any(ev.get(name) for name in _LIFECYCLE_EVENTS):
        # Fall back to the text check only when the canonical event is absent but
        # the lifecycle *was* being recorded, mirroring the gateway: a reply that
        # reads like a promise after the navigation happened is the normal
        # successful journey, not an incident.
        #
        # The gate matters. Conversations from before Part-B have no execution
        # events at all, so an ungated text check would flag every one of them
        # as a broken promise — turning "we were not watching" into "the
        # assistant lied", which is the most damaging thing a QC layer can get
        # wrong.
        claimed = sum(1 for m in messages
                      if (m.get("role") or "").lower() in ("assistant", "ai", "bot")
                      and acr.promises_navigation(m.get("content")))
        if claimed and not executed:
            add(QC_PROMISE_WITHOUT_ACTION, count=claimed, source="text")
    else:
        claimed = sum(1 for m in messages
                      if (m.get("role") or "").lower() in ("assistant", "ai", "bot")
                      and acr.promises_navigation(m.get("content")))
        if claimed:
            add(QC_UNREVIEWABLE,
                reason="action lifecycle not instrumented for this session",
                unverified_claims=claimed)

    empty_recs = sum(1 for e in events
                     if e.get("event_type") == acr.EV_RECOMMENDATION_GENERATED
                     and not _result_count(e))
    if empty_recs:
        add(QC_ZERO_RESULT_RECOMMENDATION, count=empty_recs)

    if recommended and not (confirmed or executed or offered):
        add(QC_ABANDONED_AFTER_RECOMMENDATION, recommended=recommended)

    model_failures = (ev.get(acr.EV_MODEL_TIMEOUT, 0)
                      + ev.get(acr.EV_PROVIDER_FAILURE, 0))
    if model_failures:
        add(QC_MODEL_FAILURE, count=model_failures)

    if ev.get(acr.EV_HANDOVER_ESCALATED) or outcome == acr.OUTCOME_ESCALATED:
        add(QC_ESCALATED, tickets=ev.get(acr.EV_KET_TICKET_CREATED, 0))

    repeats = count_repeated_questions(messages)
    if repeats:
        add(QC_REPEATED_QUESTION, count=repeats)

    incomplete = sum(1 for m in messages
                     if (m.get("role") or "").lower() in ("assistant", "ai", "bot")
                     and is_incomplete_answer(m.get("content")))
    if incomplete:
        add(QC_INCOMPLETE_ANSWER, count=incomplete)

    return dict(session_id=session_id, signals=signals, events=ev,
                outcome=outcome, reviewable=True)


def _from_confirmed(event):
    """True when an ACTION_EXPIRED describes a customer who said yes."""
    if event.get("failure_code") == "confirmed_never_executed":
        return True
    payload = event.get("payload")
    return isinstance(payload, dict) and payload.get("from_status") == "CONFIRMED"


def _result_count(event):
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload.get("result_count", 0) or 0
    if event.get("success") is not None:
        return 1 if event.get("success") else 0
    return 0


def worst_severity(review):
    """The severity an operator should triage this session at."""
    order = {INFO: 0, WARN: 1, FAIL: 2}
    worst = INFO
    for s in review.get("signals", ()):
        if order[s["severity"]] > order[worst]:
            worst = s["severity"]
    return worst


def summarize(reviews):
    """Counts per signal and per severity across a window of reviews."""
    by_signal, by_severity = {}, {INFO: 0, WARN: 0, FAIL: 0}
    reviewable = 0
    for r in reviews:
        reviewable += 1 if r.get("reviewable") else 0
        for s in r.get("signals", ()):
            by_signal[s["signal"]] = by_signal.get(s["signal"], 0) + 1
        by_severity[worst_severity(r)] += 1
    return dict(sessions=len(reviews), reviewable=reviewable,
                unreviewable=len(reviews) - reviewable,
                by_signal=by_signal, by_severity=by_severity)


# ─── Read-only query layer ───

def review_window(db, hours=24, limit=200):
    """Review every session with canonical activity in the last ``hours``.

    Issues SELECTs only.
    """
    cur = db.cursor()
    cur.execute(
        """SELECT DISTINCT session_id FROM ai_events
             WHERE created_at > DATE_SUB(NOW(), INTERVAL %s HOUR)
               AND session_id IS NOT NULL AND session_id <> ''
             ORDER BY session_id LIMIT %s""",
        (hours, limit),
    )
    session_ids = [_col(r, "session_id") for r in (cur.fetchall() or [])]
    return [review_one(db, sid) for sid in session_ids]


def review_one(db, session_id):
    """Review a single session by id. Issues SELECTs only."""
    cur = db.cursor()
    cur.execute(
        # ai_events is keyed by event_id, not an autoincrement id; ordering by a
        # column that does not exist is a 1054, not a slower query.
        """SELECT event_type, success, payload, failure_code FROM ai_events
             WHERE session_id=%s ORDER BY created_at, event_id""",
        (session_id,),
    )
    events = [dict(event_type=_col(r, "event_type"), success=_col(r, "success"),
                   payload=_json(_col(r, "payload")),
                   failure_code=_col(r, "failure_code"))
              for r in (cur.fetchall() or [])]
    cur.execute(
        """SELECT role, content FROM chat_messages
             WHERE session_id=%s ORDER BY created_at, id""",
        (session_id,),
    )
    messages = [dict(role=_col(r, "role"), content=_col(r, "content"))
                for r in (cur.fetchall() or [])]
    cur.execute(
        """SELECT action_id, status FROM ai_actions
             WHERE session_id=%s ORDER BY created_at""",
        (session_id,),
    )
    actions = [dict(action_id=_col(r, "action_id"), status=_col(r, "status"))
               for r in (cur.fetchall() or [])]
    outcome = None
    for e in events:
        if e["event_type"] == acr.EV_SESSION_OUTCOME and isinstance(e["payload"], dict):
            outcome = e["payload"].get("outcome") or outcome
    return review_session(session_id, events, messages, outcome, actions)


def _col(row, name):
    """Read a column from either a dict cursor or a tuple cursor row."""
    if isinstance(row, dict):
        return row.get(name)
    order = {"session_id": 0, "event_type": 0, "success": 1, "payload": 2,
             "failure_code": 3, "role": 0, "content": 1, "action_id": 0,
             "status": 1}
    try:
        return row[order[name]]
    except (KeyError, IndexError, TypeError):
        return None


def _json(value):
    if isinstance(value, dict) or value is None:
        return value
    try:
        import json
        return json.loads(value)
    except Exception:
        return None

"""ACR Gate-1 C — the AI journey timeline, read-only.

The operational answer to one question: *what exactly happened in this
AI-assisted customer journey?* One session, in order, from the moment it started
to the order it is credited with, assembled from the canonical stream we already
emit — this module defines no new event, and writes nothing.

Three rules shape it, and each exists because of a way a timeline goes wrong.

**Ordering is total, and inside a second it follows the lifecycle.**
``ai_events.created_at`` is second-resolution, and a whole navigation — offered,
confirmed, arrived — routinely lands inside one second. Ordering by time alone
lets the database return those in any order; ordering by ``(created_at,
event_id)`` is at least stable, but ``event_id`` is a uuid, and a real harness
session accordingly rendered its expiry *before* the confirmation it expired. A
timeline that shows an effect before its cause is worse than none. So inside one
timestamp the steps are grouped by action and ordered by ``LIFECYCLE_RANK``
(see ``order_events``): no timestamp is invented, no step is hidden, and two
readings of one journey always agree.

**Payload fields are allowed by name, never by default.** ``payload`` is free
JSON written by a dozen call sites, and a timeline that prints all of it becomes
a PII surface the moment someone adds ``{'email': …}`` to an event a year from
now. Unknown keys are dropped and *named* in ``omitted``, so the timeline stays
honest about what it is not showing. No message text, no prompt, no model
reasoning is read here at all: chain-of-thought is not in the canonical stream
and this module does not go looking for it in ``chat_messages``.

**The order reference is gated.** Commerce attribution is analytics, and a
person who may see that a journey completed does not necessarily need the order
number. ``include_order=False`` reports *that* an order is attributed, with the
basis, and withholds the id.

    from flaskr import acr_journey
    t = acr_journey.timeline(db, "sess_abc", include_order=True)
    for step in t["steps"]:
        print(step["at"], step["stage"], step["event_type"], step["detail"])
"""
import json

try:  # package import inside the app, flat import in tests/tools
    from . import acr
except ImportError:  # pragma: no cover - exercised by the flat-import path
    import acr


# ─── Canonical stages ───
#
# The journey as the business describes it. Every canonical event maps onto
# exactly one stage, so an unmapped event is a *new* event type nobody taught
# the timeline about — reported as OTHER rather than hidden, because an
# unexplained step is information and a missing one is not.
STAGE_SESSION = "SESSION"
STAGE_MODEL = "MODEL"
STAGE_RECOMMENDATION = "RECOMMENDATION"
STAGE_NAVIGATION = "NAVIGATION"
STAGE_PRODUCT = "PRODUCT"
STAGE_SUPPORT = "SUPPORT"
STAGE_OUTCOME = "OUTCOME"
STAGE_COMMERCE = "COMMERCE"
STAGE_OTHER = "OTHER"

EVENT_STAGE = {
    acr.EV_SESSION_STARTED: STAGE_SESSION,
    acr.EV_SESSION_RESUMED: STAGE_SESSION,
    acr.EV_MODEL_CALL: STAGE_MODEL,
    acr.EV_MODEL_TIMEOUT: STAGE_MODEL,
    acr.EV_PROVIDER_FAILURE: STAGE_MODEL,
    acr.EV_ADMISSION_503: STAGE_MODEL,
    acr.EV_RECOMMENDATION_GENERATED: STAGE_RECOMMENDATION,
    acr.EV_NAVIGATION_OFFERED: STAGE_NAVIGATION,
    acr.EV_ACTION_CONFIRMED: STAGE_NAVIGATION,
    acr.EV_ACTION_EXECUTED: STAGE_PRODUCT,
    acr.EV_ACTION_FAILED: STAGE_NAVIGATION,
    acr.EV_ACTION_BLOCKED: STAGE_NAVIGATION,
    acr.EV_ACTION_EXPIRED: STAGE_NAVIGATION,
    acr.EV_UNSAFE_URL_REJECTED: STAGE_NAVIGATION,
    acr.EV_PROMISE_WITHOUT_ACTION: STAGE_NAVIGATION,
    acr.EV_HANDOVER_ESCALATED: STAGE_SUPPORT,
    acr.EV_KET_TICKET_CREATED: STAGE_SUPPORT,
    acr.EV_SESSION_OUTCOME: STAGE_OUTCOME,
    acr.EV_OUTCOME_DEFERRED: STAGE_OUTCOME,
    acr.EV_COMMERCE_OUTCOME: STAGE_COMMERCE,
}

# Order within one second. Not a guess at what happened: it is the only order in
# which these events *can* happen for one action, so presenting them this way
# reports the lifecycle instead of the uuid a tie-break would otherwise expose.
LIFECYCLE_RANK = {
    acr.EV_SESSION_STARTED: 0,
    acr.EV_SESSION_RESUMED: 0,
    acr.EV_MODEL_CALL: 1,
    acr.EV_MODEL_TIMEOUT: 1,
    acr.EV_PROVIDER_FAILURE: 1,
    acr.EV_ADMISSION_503: 1,
    acr.EV_RECOMMENDATION_GENERATED: 2,
    acr.EV_UNSAFE_URL_REJECTED: 3,
    acr.EV_NAVIGATION_OFFERED: 4,
    acr.EV_ACTION_BLOCKED: 5,
    acr.EV_ACTION_CONFIRMED: 6,
    acr.EV_ACTION_EXECUTED: 7,
    acr.EV_ACTION_FAILED: 7,
    acr.EV_ACTION_EXPIRED: 8,
    acr.EV_PROMISE_WITHOUT_ACTION: 8,
    acr.EV_HANDOVER_ESCALATED: 9,
    acr.EV_KET_TICKET_CREATED: 10,
    acr.EV_SESSION_OUTCOME: 11,
    acr.EV_OUTCOME_DEFERRED: 11,
    acr.EV_COMMERCE_OUTCOME: 12,
}
UNRANKED = 6   # an event type nobody has ranked sits mid-journey, not last

# Events that record something going wrong, so a reader can see failure and
# recovery rather than infer them from success flags.
FAILURE_EVENTS = frozenset((
    acr.EV_MODEL_TIMEOUT, acr.EV_PROVIDER_FAILURE, acr.EV_ADMISSION_503,
    acr.EV_ACTION_FAILED, acr.EV_ACTION_BLOCKED, acr.EV_ACTION_EXPIRED,
    acr.EV_UNSAFE_URL_REJECTED, acr.EV_PROMISE_WITHOUT_ACTION,
))

# Payload keys a timeline may show, by name. Deliberately small: operational
# facts and identifiers only. Anything not listed is dropped and named.
ALLOWED_PAYLOAD_KEYS = frozenset((
    "outcome", "reason", "attribution_type", "attribution_window_hours",
    "attribution_delta_seconds", "action_type", "count", "result_count",
    "results", "product_ids", "skus", "target_path", "category", "shape",
    "color", "gender", "material", "filters", "ticket_id",
    "escalation_reason", "status", "from_status", "superseded_by", "canary",
    "site", "workload", "provider", "model", "actual_model", "attempt",
    "retries", "authenticated", "input_tokens", "output_tokens",
))

# Never shown, whatever a call site puts in a payload: the order id is gated
# separately, and free text is not a timeline's business.
GATED_PAYLOAD_KEYS = frozenset(("order_id",))

SITE_IN = "optiwar.in"
SITE_COM = "optiwar.com"

# SELECT lists, so a tuple cursor and a dict cursor produce the same records.
SESSION_COLS = ("session_id", "customer_id", "status", "current_page_url",
                "created_at", "last_activity", "resolved_at")
EVENT_COLS = ("event_id", "event_type", "action_id", "journey_stage",
              "action_type", "page_url", "success", "failure_code",
              "duration_ms", "payload", "request_id", "provider", "model",
              "workload", "created_at")
ACTION_COLS = ("action_id", "action_type", "status", "created_at",
               "resolved_at", "expires_at")
ATTRIBUTION_COLS = ("order_id", "attribution_type", "attribution_window_hours",
                    "attribution_delta_seconds", "event_id", "created_at")
RECENT_COLS = ("session_id", "started_at", "last_event_at", "events")


def site_of(url):
    """Which storefront a URL belongs to, or None when it cannot be told.

    A journey that crossed storefronts, or one whose site is unknown, both
    matter operationally — the second is not reported as the first.
    """
    if not url:
        return None
    lowered = str(url).lower()
    host = lowered.split("//", 1)[-1].split("/", 1)[0]
    if host.endswith(".in") or ".in:" in host:
        return SITE_IN
    if host.endswith(".com") or ".com:" in host:
        return SITE_COM
    return None


def _visible_payload(payload, include_order):
    """(detail, omitted) — allowed keys kept, everything else named not shown."""
    if not isinstance(payload, dict):
        return {}, []
    detail, omitted = {}, []
    for key in sorted(payload):
        if key in GATED_PAYLOAD_KEYS:
            if key == "order_id" and include_order:
                detail[key] = payload[key]
            else:
                omitted.append(key)
            continue
        if key not in ALLOWED_PAYLOAD_KEYS:
            omitted.append(key)
            continue
        value = payload[key]
        # A nested structure could hide anything; only scalars and flat lists of
        # scalars are shown, and the rest is named like any other omission.
        if isinstance(value, (str, int, float, bool)) or value is None:
            detail[key] = value
        elif isinstance(value, list) and all(
                isinstance(v, (str, int, float, bool)) for v in value):
            detail[key] = value[:20]
        elif isinstance(value, dict):
            # One level, allow-listed again: RECOMMENDATION_GENERATED nests the
            # filters that produced the recommendation, and those are the whole
            # operational point of the step. Sub-keys get no free pass.
            for sub in sorted(value):
                sub_value = value[sub]
                if (sub in ALLOWED_PAYLOAD_KEYS
                        and isinstance(sub_value, (str, int, float, bool))):
                    detail["%s.%s" % (key, sub)] = sub_value
                else:
                    omitted.append("%s.%s" % (key, sub))
        else:
            omitted.append(key)
    return detail, omitted


def order_events(events):
    """Total order: timestamp, then the action, then the lifecycle, then event_id.

    The database supplies the timestamp and it is only accurate to the second, so
    everything after it is presentation — but presentation that never contradicts
    itself and never shows an effect before its cause. Steps of one action stay
    together in the only sequence they can occur in; two actions inside one
    second are ordered by their first step, and ``action_id`` on every step is
    what tells a reader which offer they are reading about.
    """
    def rank(e):
        return LIFECYCLE_RANK.get(e.get("event_type"), UNRANKED)

    def own(e):
        return (str(e.get("created_at") or ""), rank(e),
                str(e.get("event_id") or ""))

    first_step = {}
    for e in events:
        aid = e.get("action_id")
        if aid and (aid not in first_step or own(e) < first_step[aid]):
            first_step[aid] = own(e)

    def key(e):
        aid = e.get("action_id")
        group = first_step.get(aid, own(e)) if aid else own(e)
        return (str(e.get("created_at") or ""), group, rank(e),
                str(e.get("event_id") or ""))

    return sorted(events, key=key)


def build_steps(events, include_order=False):
    """Canonical events -> timeline steps, in the order defined above."""
    steps = []
    for e in order_events(events):
        detail, omitted = _visible_payload(e.get("payload"), include_order)
        event_type = e.get("event_type")
        steps.append(dict(
            at=_iso(e.get("created_at")),
            stage=(e.get("journey_stage")
                   or EVENT_STAGE.get(event_type, STAGE_OTHER)),
            canonical_stage=EVENT_STAGE.get(event_type, STAGE_OTHER),
            event_type=event_type,
            event_id=e.get("event_id"),
            # Action ids are preserved verbatim: they are how a step here is
            # joined to ai_actions, to a QC finding and to a support question
            # about one specific offer.
            action_id=e.get("action_id"),
            action_type=e.get("action_type"),
            url=e.get("page_url"),
            site=site_of(e.get("page_url")),
            provider=e.get("provider"),
            model=e.get("model"),
            workload=e.get("workload"),
            request_id=e.get("request_id"),
            success=(None if e.get("success") is None else bool(e.get("success"))),
            failure_code=e.get("failure_code"),
            duration_ms=e.get("duration_ms"),
            failure=(event_type in FAILURE_EVENTS or e.get("success") == 0
                     or e.get("success") is False),
            detail=detail,
            omitted=omitted,
        ))
    return steps


def summarize(steps, actions):
    """The shape of the journey, for a reader who has not read every step."""
    stages = []
    for s in steps:
        if s["canonical_stage"] not in stages:
            stages.append(s["canonical_stage"])
    sites = [x for x in sorted({s["site"] for s in steps if s["site"]})]
    failures = [dict(at=s["at"], event_type=s["event_type"],
                     failure_code=s["failure_code"], action_id=s["action_id"])
                for s in steps if s["failure"]]
    # Recovery is a fact about order: a failure with any successful model or
    # navigation step after it was recovered from, whatever the outcome.
    last_failure = max((i for i, s in enumerate(steps) if s["failure"]),
                       default=None)
    recovered = (last_failure is not None
                 and any(s["success"] for s in steps[last_failure + 1:]))
    return dict(
        steps=len(steps), stages=stages, sites=sites,
        models=sorted({(s["model"] or "") for s in steps if s["model"]}),
        providers=sorted({(s["provider"] or "") for s in steps if s["provider"]}),
        failures=failures, recovered=recovered,
        actions=len(actions),
        unresolved_actions=[a["action_id"] for a in actions
                            if a["status"] in acr.UNRESOLVED_STATUSES],
    )


# ─── Read-only query layer ───

def timeline(db, session_id, include_order=False):
    """The full timeline of one session. Issues SELECTs only.

    ``reviewable`` is False when the session has no canonical events at all: a
    session that predates instrumentation must not read as a journey where
    nothing happened.
    """
    cur = db.cursor()
    cur.execute(
        """SELECT session_id, customer_id, status, current_page_url,
                  created_at, last_activity, resolved_at
             FROM chat_sessions WHERE session_id=%s""",
        (session_id,),
    )
    row = _row(cur.fetchone(), SESSION_COLS)
    session = None
    if row:
        session = dict(
            session_id=_col(row, "session_id"),
            # Whether the shopper was authenticated, not who they are: identity
            # is not needed to answer what happened, and attribution already
            # depends on customer_id elsewhere.
            authenticated=bool(_col(row, "customer_id")),
            status=_col(row, "status"),
            site=site_of(_col(row, "current_page_url")),
            started_at=_iso(_col(row, "created_at")),
            last_activity=_iso(_col(row, "last_activity")),
            resolved_at=_iso(_col(row, "resolved_at")),
        )
    cur.execute(
        # created_at is second-resolution; event_id makes the order total.
        """SELECT event_id, event_type, action_id, journey_stage, action_type,
                  page_url, success, failure_code, duration_ms, payload,
                  request_id, provider, model, workload, created_at
             FROM ai_events WHERE session_id=%s
             ORDER BY created_at, event_id""",
        (session_id,),
    )
    events = [dict(
        event_id=_col(r, "event_id"), event_type=_col(r, "event_type"),
        action_id=_col(r, "action_id"), journey_stage=_col(r, "journey_stage"),
        action_type=_col(r, "action_type"), page_url=_col(r, "page_url"),
        success=_col(r, "success"), failure_code=_col(r, "failure_code"),
        duration_ms=_col(r, "duration_ms"), payload=_json(_col(r, "payload")),
        request_id=_col(r, "request_id"), provider=_col(r, "provider"),
        model=_col(r, "model"), workload=_col(r, "workload"),
        created_at=_col(r, "created_at"),
    ) for r in _rows(cur, EVENT_COLS)]
    cur.execute(
        """SELECT action_id, action_type, status, created_at, resolved_at,
                  expires_at
             FROM ai_actions WHERE session_id=%s ORDER BY created_at, action_id""",
        (session_id,),
    )
    actions = [dict(
        action_id=_col(r, "action_id"), action_type=_col(r, "action_type"),
        status=_col(r, "status"), created_at=_iso(_col(r, "created_at")),
        resolved_at=_iso(_col(r, "resolved_at")),
        expires_at=_iso(_col(r, "expires_at")),
    ) for r in _rows(cur, ACTION_COLS)]
    steps = build_steps(events, include_order=include_order)
    return dict(
        session_id=session_id, session=session, reviewable=bool(events),
        steps=steps, actions=actions,
        attribution=attribution_of(db, session_id, include_order=include_order),
        summary=summarize(steps, actions),
    )


def attribution_of(db, session_id, include_order=False):
    """The commerce attribution of this session, with its basis, or None.

    The basis — method, ceiling and delta — is shown even when the order id is
    withheld, because *how* a journey was credited is not confidential and is
    the part a reader needs to judge the number.
    """
    try:
        cur = db.cursor()
        cur.execute(
            """SELECT order_id, attribution_type, attribution_window_hours,
                      attribution_delta_seconds, event_id, created_at
                 FROM ai_session_commerce WHERE session_id=%s""",
            (session_id,),
        )
        row = _row(cur.fetchone(), ATTRIBUTION_COLS)
    except Exception:
        # An unreadable ledger is not "no purchase": the timeline says it does
        # not know rather than reporting an unattributed journey.
        return dict(known=False)
    if not row:
        return None
    record = dict(
        known=True,
        attribution_type=_col(row, "attribution_type"),
        attribution_window_hours=_col(row, "attribution_window_hours"),
        attribution_delta_seconds=_col(row, "attribution_delta_seconds"),
        event_id=_col(row, "event_id"),
        recorded_at=_iso(_col(row, "created_at")),
        order_visible=bool(include_order),
    )
    if include_order:
        record["order_id"] = _col(row, "order_id")
    return record


def recent_sessions(db, hours=24, limit=50):
    """Sessions with canonical activity in the window, most recent first.

    The index into the timelines; deliberately no content, so listing journeys
    needs no more authority than counting them.
    """
    cur = db.cursor()
    cur.execute(
        """SELECT session_id, MIN(created_at) AS started_at,
                  MAX(created_at) AS last_event_at, COUNT(*) AS events
             FROM ai_events
             WHERE created_at > DATE_SUB(NOW(), INTERVAL %s HOUR)
               AND session_id IS NOT NULL AND session_id <> ''
             GROUP BY session_id
             ORDER BY MAX(created_at) DESC, session_id LIMIT %s""",
        (int(hours), int(limit)),
    )
    return [dict(session_id=_col(r, "session_id"),
                 started_at=_iso(_col(r, "started_at")),
                 last_event_at=_iso(_col(r, "last_event_at")),
                 events=_col(r, "events"))
            for r in _rows(cur, RECENT_COLS)]


def _iso(value):
    if value is None or isinstance(value, str):
        return value
    try:
        return value.isoformat(sep=" ")
    except (AttributeError, TypeError):
        return str(value)


def _row(row, columns):
    """One row as a dict, whether the cursor returns dicts or tuples.

    ``columns`` mirrors the SELECT list, so a tuple cursor and a dict cursor give
    the caller the same shape. Named rather than positional at the call sites
    because a reordered SELECT would otherwise silently swap two fields of the
    record an operator is reading as evidence.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(zip(columns, row))


def _rows(cur, columns):
    return [_row(r, columns) for r in (cur.fetchall() or [])]


def _col(row, name):
    return row.get(name) if isinstance(row, dict) else None


def _json(value):
    if isinstance(value, dict) or value is None:
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None

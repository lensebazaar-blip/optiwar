"""Tests for ACR Gate-1 C — the read-only journey timeline.

Two properties are load-bearing and both are asserted rather than reasoned
about: the order is total (so two readings of one journey cannot disagree), and
nothing outside a named allow-list leaves the module (so a payload key added
elsewhere a year from now cannot turn the timeline into a PII surface).

    python3 -m unittest tests.test_acr_journey
"""
import importlib.util
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


acr = _load("acr")
journey = _load("acr_journey")


def _event(event_id, event_type, at, **kw):
    row = dict(event_id=event_id, event_type=event_type, created_at=at,
               action_id=None, journey_stage=None, action_type=None,
               page_url=None, success=None, failure_code=None, duration_ms=None,
               payload=None, request_id=None, provider=None, model=None,
               workload=None)
    row.update(kw)
    return row


class _Cursor:
    """Returns a queued result per query, keyed by the table it names."""

    def __init__(self, db):
        self.db = db
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.db.executed.append((s, params))
        if s.startswith("SELECT session_id, customer_id"):
            self._result = self.db.sessions
        elif "FROM ai_events WHERE session_id" in s:
            self._result = self.db.events
        elif "FROM ai_actions WHERE session_id" in s:
            self._result = self.db.actions
        elif "FROM ai_session_commerce" in s:
            if self.db.commerce_unreadable:
                raise RuntimeError("SELECT command denied on 'ai_session_commerce'")
            self._result = self.db.commerce
        elif "GROUP BY session_id" in s:
            self._result = self.db.recent
        else:  # pragma: no cover - an unexpected query should be loud
            raise AssertionError("unexpected query: %s" % s)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeDB:
    def __init__(self, events=(), actions=(), sessions=(), commerce=(),
                 recent=()):
        self.events = list(events)
        self.actions = list(actions)
        self.sessions = list(sessions)
        self.commerce = list(commerce)
        self.recent = list(recent)
        self.commerce_unreadable = False
        self.executed = []

    def cursor(self):
        return _Cursor(self)


HEALTHY = [
    _event("e1", acr.EV_SESSION_STARTED, "2026-06-01 10:00:00",
           page_url="https://optiwar.in/", success=1),
    _event("e2", acr.EV_MODEL_CALL, "2026-06-01 10:00:03", success=1,
           provider="deepseek", model="deepseek-chat", workload="chat",
           duration_ms=1400, request_id="req-1"),
    _event("e3", acr.EV_RECOMMENDATION_GENERATED, "2026-06-01 10:00:04",
           success=1, payload=json.dumps({"count": 6, "color": "black"})),
    _event("e4", acr.EV_NAVIGATION_OFFERED, "2026-06-01 10:00:05",
           action_id="a1", action_type="navigate_filtered",
           page_url="https://optiwar.in/products?color=black", success=1),
    # Confirmation and arrival inside the same second: the case the tie-break
    # exists for.
    _event("e5", acr.EV_ACTION_CONFIRMED, "2026-06-01 10:00:20", action_id="a1",
           success=1),
    _event("e6", acr.EV_ACTION_EXECUTED, "2026-06-01 10:00:20", action_id="a1",
           success=1, page_url="https://optiwar.in/products?color=black"),
    _event("e7", acr.EV_SESSION_OUTCOME, "2026-06-01 10:30:00", success=1,
           payload=json.dumps({"outcome": "ANSWERED"})),
]


class TimelineShapeTests(unittest.TestCase):

    def _timeline(self, **kw):
        db = FakeDB(
            events=kw.pop("events", HEALTHY),
            actions=kw.pop("actions", [dict(
                action_id="a1", action_type="navigate_filtered",
                status="EXECUTED", created_at="2026-06-01 10:00:05",
                resolved_at="2026-06-01 10:00:20",
                expires_at="2026-06-01 10:30:05")]),
            sessions=kw.pop("sessions", [dict(
                session_id="s1", customer_id=42, status="archived",
                current_page_url="https://optiwar.in/products",
                created_at="2026-06-01 10:00:00",
                last_activity="2026-06-01 10:30:00", resolved_at=None)]),
            commerce=kw.pop("commerce", []))
        self.db = db
        return journey.timeline(db, "s1", **kw)

    def test_the_journey_reads_in_the_order_it_happened(self):
        t = self._timeline()
        self.assertEqual([s["event_type"] for s in t["steps"]],
                         [e["event_type"] for e in HEALTHY])
        self.assertEqual([s["canonical_stage"] for s in t["steps"]],
                         ["SESSION", "MODEL", "RECOMMENDATION", "NAVIGATION",
                          "NAVIGATION", "PRODUCT", "OUTCOME"])

    def test_the_ordering_is_total_not_merely_chronological(self):
        # created_at is second-resolution, so a confirmation and its arrival
        # share a timestamp. Without a tie-break in the ORDER BY the database may
        # return the arrival first, and a timeline that contradicts itself
        # between two readings is not evidence.
        self._timeline()
        sql = [s for s, _ in self.db.executed if "FROM ai_events" in s][0]
        self.assertIn("ORDER BY created_at, event_id", sql)

    def test_action_ids_survive_into_the_timeline(self):
        t = self._timeline()
        offered = [s for s in t["steps"]
                   if s["event_type"] == acr.EV_NAVIGATION_OFFERED][0]
        self.assertEqual(offered["action_id"], "a1")
        self.assertEqual([a["action_id"] for a in t["actions"]], ["a1"])

    def test_provider_model_and_site_are_visible(self):
        t = self._timeline()
        call = [s for s in t["steps"] if s["event_type"] == acr.EV_MODEL_CALL][0]
        self.assertEqual((call["provider"], call["model"], call["workload"]),
                         ("deepseek", "deepseek-chat", "chat"))
        self.assertEqual(t["summary"]["sites"], [journey.SITE_IN])
        self.assertEqual(t["summary"]["models"], ["deepseek-chat"])

    def test_a_guest_is_reported_as_a_guest_and_never_named(self):
        t = self._timeline(sessions=[dict(
            session_id="s1", customer_id=None, status="archived",
            current_page_url="https://optiwar.com/", created_at="x",
            last_activity="y", resolved_at=None)])
        self.assertFalse(t["session"]["authenticated"])
        self.assertNotIn("customer_id", t["session"])
        self.assertNotIn("contact_email", t["session"])

    def test_a_session_without_canonical_events_is_not_a_clean_journey(self):
        t = self._timeline(events=[], actions=[])
        self.assertFalse(t["reviewable"])
        self.assertEqual(t["steps"], [])


class OrderingTests(unittest.TestCase):
    """Inside one second the database's order is arbitrary, so the module owns it.

    This is the defect the harness found on real data: uuid ``event_id``s put an
    expiry above the confirmation it expired.
    """

    SAME_SECOND = "2026-06-01 10:00:00"

    def _order(self, events):
        return [e["event_type"] for e in journey.order_events(events)]

    def test_a_whole_navigation_inside_one_second_reads_in_lifecycle_order(self):
        events = [
            _event("zzz", acr.EV_ACTION_EXECUTED, self.SAME_SECOND, action_id="a1"),
            _event("mmm", acr.EV_ACTION_CONFIRMED, self.SAME_SECOND, action_id="a1"),
            _event("aaa", acr.EV_NAVIGATION_OFFERED, self.SAME_SECOND, action_id="a1"),
            _event("bbb", acr.EV_SESSION_STARTED, self.SAME_SECOND),
        ]
        self.assertEqual(self._order(events),
                         [acr.EV_SESSION_STARTED, acr.EV_NAVIGATION_OFFERED,
                          acr.EV_ACTION_CONFIRMED, acr.EV_ACTION_EXECUTED])

    def test_two_actions_in_one_second_do_not_interleave(self):
        # A second offer that supersedes the first: each action's own steps stay
        # together and in order, so a reader is never shown a1's expiry before
        # a1's confirmation because a2 happened in between.
        events = [
            _event("e4", acr.EV_ACTION_EXPIRED, self.SAME_SECOND, action_id="a1",
                   failure_code="superseded"),
            _event("e3", acr.EV_NAVIGATION_OFFERED, self.SAME_SECOND, action_id="a2"),
            _event("e2", acr.EV_ACTION_CONFIRMED, self.SAME_SECOND, action_id="a1"),
            _event("e1", acr.EV_NAVIGATION_OFFERED, self.SAME_SECOND, action_id="a1"),
        ]
        ordered = journey.order_events(events)
        self.assertEqual([(e["action_id"], e["event_type"]) for e in ordered], [
            ("a1", acr.EV_NAVIGATION_OFFERED),
            ("a1", acr.EV_ACTION_CONFIRMED),
            ("a1", acr.EV_ACTION_EXPIRED),
            ("a2", acr.EV_NAVIGATION_OFFERED),
        ])

    def test_timestamps_still_win_over_the_lifecycle(self):
        # The lifecycle only breaks ties. A later event with a lower rank must
        # not be hoisted above an earlier one, or the timeline would be inventing
        # a sequence rather than presenting one.
        events = [
            _event("e1", acr.EV_ACTION_EXECUTED, "2026-06-01 10:00:00", action_id="a1"),
            _event("e2", acr.EV_SESSION_RESUMED, "2026-06-01 10:05:00"),
        ]
        self.assertEqual(self._order(events),
                         [acr.EV_ACTION_EXECUTED, acr.EV_SESSION_RESUMED])

    def test_the_order_is_the_same_whichever_order_the_database_returns(self):
        events = [
            _event("e9", acr.EV_ACTION_CONFIRMED, self.SAME_SECOND, action_id="a1"),
            _event("e1", acr.EV_NAVIGATION_OFFERED, self.SAME_SECOND, action_id="a1"),
            _event("e5", acr.EV_MODEL_CALL, self.SAME_SECOND),
        ]
        self.assertEqual(self._order(events),
                         self._order(list(reversed(events))))


class FailureVisibilityTests(unittest.TestCase):

    def _t(self, events):
        return journey.timeline(FakeDB(events=events), "s1")

    def test_a_failure_and_its_recovery_are_both_visible(self):
        t = self._t([
            _event("e1", acr.EV_MODEL_TIMEOUT, "2026-06-01 10:00:01",
                   success=0, failure_code="timeout"),
            _event("e2", acr.EV_MODEL_CALL, "2026-06-01 10:00:09", success=1,
                   model="gpt-4o"),
        ])
        self.assertEqual([s["failure"] for s in t["steps"]], [True, False])
        self.assertEqual(t["summary"]["failures"][0]["failure_code"], "timeout")
        self.assertTrue(t["summary"]["recovered"])

    def test_an_unrecovered_failure_is_not_dressed_up_as_recovered(self):
        t = self._t([
            _event("e1", acr.EV_MODEL_CALL, "2026-06-01 10:00:00", success=1),
            _event("e2", acr.EV_ACTION_EXPIRED, "2026-06-01 10:05:00",
                   success=0, action_id="a1",
                   failure_code="confirmed_never_executed"),
        ])
        self.assertFalse(t["summary"]["recovered"])
        self.assertEqual(t["summary"]["failures"][0]["action_id"], "a1")

    def test_an_unresolved_action_is_named(self):
        db = FakeDB(events=HEALTHY, actions=[dict(
            action_id="a9", action_type="navigate", status="CONFIRMED",
            created_at="x", resolved_at="y", expires_at="z")])
        self.assertEqual(journey.timeline(db, "s1")["summary"]
                         ["unresolved_actions"], ["a9"])

    def test_an_event_type_the_timeline_has_never_seen_is_shown_as_other(self):
        # A new event type must appear as an unexplained step rather than vanish:
        # a missing step is not information, an unlabelled one is.
        t = self._t([_event("e1", "SOMETHING_NEW", "2026-06-01 10:00:00")])
        self.assertEqual(t["steps"][0]["canonical_stage"], journey.STAGE_OTHER)


class PayloadBoundaryTests(unittest.TestCase):
    """What a payload may show, by name, and what it says about the rest."""

    def _step(self, payload, include_order=False):
        db = FakeDB(events=[_event("e1", acr.EV_RECOMMENDATION_GENERATED,
                                   "2026-06-01 10:00:00",
                                   payload=json.dumps(payload))])
        return journey.timeline(db, "s1",
                               include_order=include_order)["steps"][0]

    def test_an_unlisted_key_is_named_and_never_shown(self):
        step = self._step({"count": 3, "email": "a@b.com",
                           "customer_message": "my prescription is -2.25"})
        self.assertEqual(step["detail"], {"count": 3})
        self.assertEqual(step["omitted"], ["customer_message", "email"])
        blob = json.dumps(step)
        self.assertNotIn("a@b.com", blob)
        self.assertNotIn("-2.25", blob)

    def test_a_nested_structure_is_allow_listed_again_not_trusted(self):
        # RECOMMENDATION_GENERATED nests the filters that produced it, which are
        # the operational point of the step; allow-listing the outer key must not
        # allow-list whatever a caller nests under it.
        step = self._step({"filters": {"color": "black",
                                       "email": "a@b.com"}})
        self.assertEqual(step["detail"], {"filters.color": "black"})
        self.assertEqual(step["omitted"], ["filters.email"])
        self.assertNotIn("a@b.com", json.dumps(step))

    def test_the_navigation_target_is_shown(self):
        # Where the assistant offered to send the customer is the fact the step
        # exists to record.
        step = self._step({"target_path": "/eyeglasses/aviators.html"})
        self.assertEqual(step["detail"],
                         {"target_path": "/eyeglasses/aviators.html"})

    def test_the_order_reference_is_withheld_unless_asked_for(self):
        withheld = self._step({"order_id": "ORD-1", "outcome": "PURCHASED"})
        self.assertEqual(withheld["detail"], {"outcome": "PURCHASED"})
        self.assertEqual(withheld["omitted"], ["order_id"])
        shown = self._step({"order_id": "ORD-1"}, include_order=True)
        self.assertEqual(shown["detail"], {"order_id": "ORD-1"})

    def test_unparseable_payload_does_not_take_the_timeline_down(self):
        db = FakeDB(events=[_event("e1", acr.EV_MODEL_CALL, "x",
                                   payload="{not json")])
        self.assertEqual(journey.timeline(db, "s1")["steps"][0]["detail"], {})


class AttributionVisibilityTests(unittest.TestCase):
    """The attributed order, under the same rule as everywhere else."""

    def _db(self):
        return FakeDB(events=HEALTHY, commerce=[dict(
            order_id="ORD-9", attribution_type=acr.ATTRIBUTION_NEAREST_PRECEDING,
            attribution_window_hours=24, attribution_delta_seconds=1800,
            event_id="ev-9", created_at="2026-06-02 09:00:00")])

    def test_the_basis_is_shown_even_when_the_order_id_is_not(self):
        # How a journey was credited is what a reader needs to judge the number;
        # the order reference is a separate authority.
        a = journey.timeline(self._db(), "s1")["attribution"]
        self.assertEqual(a["attribution_type"],
                         acr.ATTRIBUTION_NEAREST_PRECEDING)
        self.assertEqual(a["attribution_delta_seconds"], 1800)
        self.assertFalse(a["order_visible"])
        self.assertNotIn("order_id", a)

    def test_the_order_id_appears_when_authorised(self):
        a = journey.timeline(self._db(), "s1", include_order=True)["attribution"]
        self.assertTrue(a["order_visible"])
        self.assertEqual(a["order_id"], "ORD-9")

    def test_an_unattributed_journey_says_so(self):
        self.assertIsNone(journey.timeline(FakeDB(events=HEALTHY), "s1")
                          ["attribution"])

    def test_an_unreadable_ledger_is_not_reported_as_unattributed(self):
        db = self._db()
        db.commerce_unreadable = True
        self.assertEqual(journey.timeline(db, "s1")["attribution"],
                         {"known": False})


class ReadOnlyTests(unittest.TestCase):

    def test_the_timeline_issues_no_writes(self):
        db = FakeDB(events=HEALTHY, commerce=[])
        journey.timeline(db, "s1", include_order=True)
        journey.recent_sessions(db, hours=24, limit=10)
        for sql, _ in db.executed:
            self.assertTrue(sql.startswith("SELECT"), sql)


class SiteTests(unittest.TestCase):

    def test_the_storefront_is_read_from_the_url(self):
        self.assertEqual(journey.site_of("https://optiwar.in/products"),
                         journey.SITE_IN)
        self.assertEqual(journey.site_of("http://www.optiwar.com/"),
                         journey.SITE_COM)

    def test_an_unknown_host_is_unknown_rather_than_com(self):
        # Defaulting to .com would silently file .in journeys under the wrong
        # storefront in every count derived from this.
        self.assertIsNone(journey.site_of("http://localhost:5001/products"))
        self.assertIsNone(journey.site_of(None))


if __name__ == "__main__":
    unittest.main()

"""Tests for the QC conversation review layer (Gate-1 A).

Two properties matter more than the individual signals:

  - **no text escapes**. Every content-derived signal is asserted to return a
    count, a flag or a hash and never the wording it was computed from. A
    reviewer's convenience field added later that leaks a message would fail
    here rather than in a broad-distribution export.
  - **silence is not quality**. A session with no canonical events is
    UNREVIEWABLE, not clean, so an instrumentation outage cannot read as a
    quality improvement.

    python3 -m unittest tests.test_acr_qc
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
qc = _load("acr_qc")


def ev(event_type, **kw):
    row = dict(event_type=event_type, success=None, payload=None)
    row.update(kw)
    return row


def msg(role, content):
    return dict(role=role, content=content)


def signals(review):
    return {s["signal"] for s in review["signals"]}


def detail(review, name):
    for s in review["signals"]:
        if s["signal"] == name:
            return s
    return None


HAPPY = [ev(acr.EV_SESSION_STARTED), ev(acr.EV_RECOMMENDATION_GENERATED,
                                        payload={"result_count": 2}),
         ev(acr.EV_NAVIGATION_OFFERED), ev(acr.EV_ACTION_CONFIRMED),
         ev(acr.EV_ACTION_EXECUTED)]


class JourneyQualityTests(unittest.TestCase):
    def test_a_completed_journey_raises_nothing_worse_than_info(self):
        r = qc.review_session("s1", HAPPY, [
            msg("user", "show me black frames"),
            msg("assistant", "Would you like me to take you to these frames?"),
            msg("user", "yes"),
            msg("assistant", "Opening that for you now."),
        ])
        self.assertIn(qc.QC_JOURNEY_COMPLETED, signals(r))
        self.assertEqual(qc.worst_severity(r), qc.INFO)

    def test_a_promise_after_a_real_navigation_is_not_an_incident(self):
        # "Opening that for you now." matches promises_navigation(), but the
        # navigation happened. Flagging it would put a FAIL on the normal
        # successful journey, which is how an instrumentation layer loses trust.
        r = qc.review_session("s1", HAPPY,
                              [msg("assistant", "Opening that for you now.")])
        self.assertNotIn(qc.QC_PROMISE_WITHOUT_ACTION, signals(r))

    def test_a_promise_with_no_execution_is_a_truthfulness_failure(self):
        r = qc.review_session("s2", [ev(acr.EV_SESSION_STARTED),
                                     ev(acr.EV_NAVIGATION_OFFERED)],
                              [msg("assistant", "I'm taking you there now.")])
        self.assertIn(qc.QC_PROMISE_WITHOUT_ACTION, signals(r))
        self.assertEqual(detail(r, qc.QC_PROMISE_WITHOUT_ACTION)["severity"],
                         qc.FAIL)

    def test_the_canonical_event_is_preferred_over_the_text_check(self):
        r = qc.review_session("s3", [ev(acr.EV_SESSION_STARTED),
                                     ev(acr.EV_PROMISE_WITHOUT_ACTION)], [])
        self.assertEqual(detail(r, qc.QC_PROMISE_WITHOUT_ACTION)["source"],
                         "canonical")

    def test_a_confirmation_that_never_executed_is_a_failed_navigation(self):
        # The customer said yes and nothing happened — worse than never asking.
        r = qc.review_session("s4", [ev(acr.EV_NAVIGATION_OFFERED),
                                     ev(acr.EV_ACTION_CONFIRMED)], [])
        self.assertIn(qc.QC_FAILED_NAVIGATION, signals(r))
        self.assertEqual(
            detail(r, qc.QC_FAILED_NAVIGATION)["confirmed_not_executed"], 1)

    def test_a_blocked_action_counts_as_a_failed_navigation(self):
        r = qc.review_session("s5", [ev(acr.EV_ACTION_BLOCKED)], [])
        self.assertEqual(detail(r, qc.QC_FAILED_NAVIGATION)["failed"], 1)

    def test_a_recommendation_with_no_products_is_flagged(self):
        r = qc.review_session("s6", [ev(acr.EV_RECOMMENDATION_GENERATED,
                                        payload={"result_count": 0})], [])
        self.assertIn(qc.QC_ZERO_RESULT_RECOMMENDATION, signals(r))

    def test_a_recommendation_nobody_acted_on_is_flagged_as_abandoned(self):
        r = qc.review_session("s7", [ev(acr.EV_RECOMMENDATION_GENERATED,
                                        payload={"result_count": 3})], [])
        self.assertIn(qc.QC_ABANDONED_AFTER_RECOMMENDATION, signals(r))

    def test_escalation_is_read_from_the_outcome_as_well_as_the_event(self):
        r = qc.review_session("s8", [ev(acr.EV_SESSION_STARTED)], [],
                              outcome=acr.OUTCOME_ESCALATED)
        self.assertIn(qc.QC_ESCALATED, signals(r))

    def test_model_failures_are_a_fail(self):
        r = qc.review_session("s9", [ev(acr.EV_MODEL_TIMEOUT),
                                     ev(acr.EV_PROVIDER_FAILURE)], [])
        self.assertEqual(detail(r, qc.QC_MODEL_FAILURE)["count"], 2)
        self.assertEqual(qc.worst_severity(r), qc.FAIL)


class PreInstrumentationTests(unittest.TestCase):
    """Conversations from before Part-B have no execution events at all. An
    ungated text check would report every one of them as a broken promise, which
    turns "we were not watching" into "the assistant lied"."""

    PROMISE = [msg("assistant", "I'm taking you there now.")]

    def test_a_pre_instrumentation_session_is_unreviewable_not_a_liar(self):
        r = qc.review_session("old", [ev(acr.EV_SESSION_STARTED),
                                      ev(acr.EV_MODEL_CALL)], self.PROMISE)
        self.assertNotIn(qc.QC_PROMISE_WITHOUT_ACTION, signals(r))
        self.assertEqual(detail(r, qc.QC_UNREVIEWABLE)["unverified_claims"], 1)

    def test_the_text_check_still_fires_once_the_lifecycle_is_recorded(self):
        r = qc.review_session("new", [ev(acr.EV_SESSION_STARTED),
                                      ev(acr.EV_NAVIGATION_OFFERED)],
                              self.PROMISE)
        self.assertEqual(detail(r, qc.QC_PROMISE_WITHOUT_ACTION)["source"], "text")


class SupersededActionTests(unittest.TestCase):
    """A session can confirm two actions and execute one when the second
    supersedes the first. Subtracting event totals invents a failure there."""

    EVENTS = [ev(acr.EV_ACTION_CONFIRMED), ev(acr.EV_ACTION_CONFIRMED),
              ev(acr.EV_ACTION_EXECUTED)]

    def test_ai_actions_status_is_preferred_over_event_arithmetic(self):
        r = qc.review_session("s", self.EVENTS, [], actions=[
            dict(action_id="a", status="SUPERSEDED"),
            dict(action_id="b", status="EXECUTED"),
        ])
        self.assertNotIn(qc.QC_FAILED_NAVIGATION, signals(r))

    def test_an_action_still_sitting_at_confirmed_is_the_real_failure(self):
        r = qc.review_session("s", self.EVENTS, [], actions=[
            dict(action_id="a", status="CONFIRMED", overdue=True),
            dict(action_id="b", status="EXECUTED"),
        ])
        self.assertEqual(
            detail(r, qc.QC_FAILED_NAVIGATION)["confirmed_not_executed"], 1)

    def test_without_ai_actions_the_event_arithmetic_still_applies(self):
        r = qc.review_session("s", self.EVENTS, [])
        self.assertIn(qc.QC_FAILED_NAVIGATION, signals(r))


class InFlightConfirmationTests(unittest.TestCase):
    """Between the customer saying yes and the browser reporting arrival there
    is a legitimate window of seconds. Counting it as a failure would make every
    conversation happening right now look broken."""

    EVENTS = [ev(acr.EV_NAVIGATION_OFFERED), ev(acr.EV_ACTION_CONFIRMED)]

    def test_a_confirmation_within_its_ttl_is_not_a_failure(self):
        r = qc.review_session("live", self.EVENTS, [], actions=[
            dict(action_id="a", status="CONFIRMED", overdue=False)])
        self.assertNotIn(qc.QC_FAILED_NAVIGATION, signals(r))

    def test_the_same_confirmation_past_its_ttl_is(self):
        r = qc.review_session("stranded", self.EVENTS, [], actions=[
            dict(action_id="a", status="CONFIRMED", overdue=True)])
        self.assertIn(qc.QC_FAILED_NAVIGATION, signals(r))

    def test_the_grace_period_is_measured_from_the_confirmation(self):
        # expires_at is the *offer's* deadline. Reading it directly would give a
        # customer who accepts one second before it lapses a one-second grace,
        # and one who accepts immediately the full 30 minutes. The SQL keys on
        # resolved_at for CONFIRMED rows so every confirmation gets the same
        # window; this asserts the query says so.
        sql = _review_sql_for_actions()
        self.assertIn("resolved_at < DATE_SUB(NOW(), INTERVAL", sql)
        self.assertIn("status='CONFIRMED' AND resolved_at IS NOT NULL", sql)

    def test_missing_expiry_evidence_does_not_assume_death(self):
        r = qc.review_session("unknown", self.EVENTS, [], actions=[
            dict(action_id="a", status="CONFIRMED")])
        self.assertNotIn(qc.QC_FAILED_NAVIGATION, signals(r))


def _review_sql_for_actions():
    """The ai_actions SELECT issued by review_one, as text."""
    class _Cur(object):
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql)

        def fetchall(self):
            return []

    class _DB(object):
        def __init__(self):
            self.cur = _Cur()

        def cursor(self):
            return self.cur

    db = _DB()
    qc.review_one(db, "s")
    return [s for s in db.cur.sql if "ai_actions" in s][0]


class ApologyThatAnswersTests(unittest.TestCase):
    """A refusal opener proves nothing on its own; many good answers start with
    one. Over-counting unanswered questions inflates the single number an
    operator would act on."""

    def test_an_apology_followed_by_a_list_is_an_answer(self):
        self.assertFalse(qc.is_incomplete_answer(
            "Sorry, here is what I found:\n- Frame A\n- Frame B"))

    def test_a_long_single_line_apology_that_answers_is_an_answer(self):
        self.assertFalse(qc.is_incomplete_answer(
            "Sorry, we do not have that in black, but we do have it in "
            "tortoise, navy and clear, all in stock in your size"))

    def test_a_bare_refusal_is_still_incomplete(self):
        self.assertTrue(qc.is_incomplete_answer("Sorry, I can't help with that."))
        self.assertTrue(qc.is_incomplete_answer("Unfortunately no results"))


class SilenceIsNotQualityTests(unittest.TestCase):
    def test_a_session_with_no_events_is_unreviewable_not_clean(self):
        r = qc.review_session("s10", [], [msg("user", "hello")])
        self.assertEqual(signals(r), {qc.QC_UNREVIEWABLE})
        self.assertFalse(r["reviewable"])

    def test_the_summary_separates_unreviewable_from_reviewed(self):
        s = qc.summarize([qc.review_session("a", [], []),
                          qc.review_session("b", HAPPY, [])])
        self.assertEqual((s["sessions"], s["reviewable"], s["unreviewable"]),
                         (2, 1, 1))


class RepeatedQuestionTests(unittest.TestCase):
    def test_a_rephrased_question_still_counts_as_repeated(self):
        # Exact matching would miss this, and rephrasing is what customers
        # actually do when the first answer did not land.
        n = qc.count_repeated_questions([
            msg("user", "do you have round frames"),
            msg("assistant", "Here are some options."),
            msg("user", "any round frames in stock?"),
        ])
        self.assertEqual(n, 1)

    def test_a_different_question_is_not_a_repeat(self):
        n = qc.count_repeated_questions([
            msg("user", "do you have round frames"),
            msg("user", "what is your refund policy"),
        ])
        self.assertEqual(n, 0)

    def test_stopwords_alone_never_fingerprint(self):
        self.assertEqual(qc.question_fingerprint("do you have any?"), "")

    def test_the_fingerprint_survives_reordering_but_not_a_topic_change(self):
        a = qc.question_fingerprint("round black frames")
        self.assertEqual(a, qc.question_fingerprint("black frames round"))
        self.assertNotEqual(a, qc.question_fingerprint("round black lenses"))


class IncompleteAnswerTests(unittest.TestCase):
    def test_a_bare_refusal_is_incomplete(self):
        self.assertTrue(qc.is_incomplete_answer("Sorry, I can't help with that."))
        self.assertTrue(qc.is_incomplete_answer("   "))

    def test_an_apology_that_then_answers_is_not_incomplete(self):
        self.assertFalse(qc.is_incomplete_answer(
            "Sorry about that. Here are three round frames in your size."))


class NoTextEscapesTests(unittest.TestCase):
    """The PII boundary is the interface, not a policy someone must remember."""

    SECRET = "my email is a@b.com and my prescription is -2.75"

    def test_no_review_field_contains_message_text(self):
        r = qc.review_session("s11", HAPPY, [
            msg("user", self.SECRET),
            msg("user", self.SECRET),
            msg("assistant", "Sorry, I can't help with that."),
        ])
        blob = json.dumps(r)
        for fragment in ("a@b.com", "-2.75", "prescription", "my email"):
            self.assertNotIn(fragment, blob)
        # ...while still having done the work that needed the text.
        self.assertIn(qc.QC_REPEATED_QUESTION, signals(r))
        self.assertIn(qc.QC_INCOMPLETE_ANSWER, signals(r))

    def test_the_fingerprint_is_not_reversible_to_the_message(self):
        fp = qc.question_fingerprint(self.SECRET)
        self.assertNotIn("a@b.com", fp)
        self.assertRegex(fp, r"^[0-9a-f]{16}$")

    def test_the_summary_carries_counts_only(self):
        s = qc.summarize([qc.review_session("s12", HAPPY,
                                            [msg("user", self.SECRET)])])
        self.assertNotIn("a@b.com", json.dumps(s))


class _Cur:
    """Records SQL and replays canned rows in order."""

    def __init__(self, results):
        self.results = list(results)
        self.sql = []
        self._rows = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self._rows = self.results.pop(0) if self.results else []

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _DB:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


class ReadOnlyQueryTests(unittest.TestCase):
    def test_reviewing_a_session_issues_selects_only(self):
        cur = _Cur([
            [dict(event_type=acr.EV_ACTION_EXECUTED, success=1, payload=None)],
            [dict(role="user", content="hi")],
            [dict(action_id="a", status="EXECUTED", overdue=0)],
        ])
        r = qc.review_one(_DB(cur), "sess-1")
        self.assertIn(qc.QC_JOURNEY_COMPLETED, signals(r))
        for sql in cur.sql:
            self.assertTrue(sql.upper().startswith("SELECT"), sql)

    def test_a_json_payload_string_is_parsed_for_result_count(self):
        cur = _Cur([
            [dict(event_type=acr.EV_RECOMMENDATION_GENERATED, success=None,
                  payload=json.dumps({"result_count": 0}))],
            [],
        ])
        r = qc.review_one(_DB(cur), "sess-2")
        self.assertIn(qc.QC_ZERO_RESULT_RECOMMENDATION, signals(r))

    def test_the_outcome_is_taken_from_the_canonical_outcome_event(self):
        cur = _Cur([
            [dict(event_type=acr.EV_SESSION_OUTCOME, success=1,
                  payload=json.dumps({"outcome": acr.OUTCOME_ESCALATED}))],
            [],
        ])
        r = qc.review_one(_DB(cur), "sess-3")
        self.assertIn(qc.QC_ESCALATED, signals(r))

    def test_a_window_review_reads_the_canonical_stream_for_its_sessions(self):
        cur = _Cur([
            [dict(session_id="sess-a")],
            [dict(event_type=acr.EV_ACTION_EXECUTED, success=1, payload=None)],
            [],
        ])
        reviews = qc.review_window(_DB(cur), hours=24)
        self.assertEqual([r["session_id"] for r in reviews], ["sess-a"])
        for sql in cur.sql:
            self.assertTrue(sql.upper().startswith("SELECT"), sql)

    def test_tuple_cursors_are_supported_too(self):
        cur = _Cur([[(acr.EV_ACTION_EXECUTED, 1, None)], [("user", "hi")],
                    [("a", "EXECUTED", 0)]])
        r = qc.review_one(_DB(cur), "sess-4")
        self.assertIn(qc.QC_JOURNEY_COMPLETED, signals(r))


class SeverityTests(unittest.TestCase):
    def test_every_signal_declares_a_severity(self):
        declared = {v for k, v in vars(qc).items()
                    if k.startswith("QC_") and isinstance(v, str)}
        self.assertEqual(declared, set(qc.SIGNALS))

    def test_a_truthfulness_defect_is_never_below_fail(self):
        self.assertEqual(qc.SIGNALS[qc.QC_PROMISE_WITHOUT_ACTION], qc.FAIL)


if __name__ == "__main__":
    unittest.main()

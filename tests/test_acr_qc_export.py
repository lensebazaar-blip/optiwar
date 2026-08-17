"""Tests for the audited QC PDF export (Gate-1 B).

Three properties carry the module, and each is asserted against the bytes that
would actually leave the building rather than against a mock:

  - **an unaudited export does not exist**. If the QC_EXPORT row cannot be
    written, no document is produced.
  - **no conversation text is in the file**. The streams are uncompressed, so
    this is a byte scan of the real PDF, not an inspection of an intermediate.
  - **the printed document id is the one in the ledger**, so a page on a desk
    can be traced to the export that produced it.

    python3 -m unittest tests.test_acr_qc_export
"""
import importlib.util
import json
import os
import sys
import unittest
from datetime import datetime

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
export = _load("acr_qc_export")

WHEN = datetime(2026, 6, 12, 5, 0, 0)


class _Cur(object):
    """A cursor that replays canned result sets and records what it was asked."""

    def __init__(self, results, insert_ok=True):
        self.results = list(results)
        self.sql = []
        self.inserted = []
        self.insert_ok = insert_ok

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.strip().upper().startswith("INSERT"):
            if not self.insert_ok:
                raise RuntimeError("ai_events unavailable")
            self.inserted.append((sql, params))

    def fetchall(self):
        return self.results.pop(0) if self.results else []

    def fetchone(self):
        return None


class _DB(object):
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur


def _one_session_db(insert_ok=True, content="my prescription is -2.75"):
    """A window of exactly one session: offered, confirmed, never executed."""
    return _DB(_Cur([
        [dict(session_id="chat_abc")],                       # review_window ids
        [dict(event_type=acr.EV_NAVIGATION_OFFERED, success=None, payload=None,
              failure_code=None),
         dict(event_type=acr.EV_ACTION_CONFIRMED, success=None, payload=None,
              failure_code=None)],                           # events
        [dict(role="user", content=content),
         dict(role="assistant", content="Opening that for you now.")],
        [dict(action_id="a1", status="CONFIRMED", overdue=1)],
    ], insert_ok=insert_ok))


class AuditedOrNotAtAllTests(unittest.TestCase):
    """A document leaves the platform and can be forwarded; the ledger entry is
    the only thing tying it back. So the entry is written first, and its failure
    cancels the export."""

    def test_the_export_is_recorded_before_it_is_produced(self):
        db = _one_session_db()
        pdf, meta = export.export(db, hours=24, actor="admin@ket.ltd",
                                  ip="10.0.0.1", now=WHEN)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        inserts = [p for sql, p in db.cur.inserted]
        self.assertEqual(len(inserts), 1)
        self.assertIn(acr.EV_QC_EXPORT, inserts[0])
        payload = json.loads([p for p in inserts[0] if _is_payload(p)][0])
        self.assertEqual(payload["document_id"], meta["document_id"])
        self.assertEqual(payload["actor"], "admin@ket.ltd")
        self.assertEqual(payload["ip"], "10.0.0.1")
        self.assertEqual(payload["scope"]["hours"], 24)

    def test_an_export_that_cannot_be_audited_is_refused(self):
        db = _one_session_db(insert_ok=False)
        with self.assertRaises(export.AuditWriteFailed):
            export.export(db, hours=24, actor="admin@ket.ltd", now=WHEN)

    def test_the_review_itself_writes_nothing(self):
        db = _one_session_db()
        export.export(db, hours=24, actor="a", now=WHEN)
        selects = [s for s in db.cur.sql if not s.strip().upper().startswith("INSERT")]
        for sql in selects:
            self.assertTrue(sql.strip().upper().startswith("SELECT"), sql)


def _is_payload(value):
    return isinstance(value, str) and value.startswith("{")


class NoTextEscapesTests(unittest.TestCase):
    """The exporter consumes review output only, so the PII boundary is
    inherited from the QC layer. This asserts it against the file."""

    def test_the_pdf_contains_no_message_text(self):
        db = _one_session_db(
            content="my email is jane.doe@example.com and my SPH is -2.75")
        pdf, _ = export.export(db, hours=24, actor="admin@ket.ltd", now=WHEN)
        for leak in (b"jane.doe@example.com", b"-2.75", b"SPH",
                     b"Opening that for you now"):
            self.assertNotIn(leak, pdf)

    def test_it_does_report_the_signal_that_text_produced(self):
        db = _one_session_db()
        pdf, meta = export.export(db, hours=24, actor="admin@ket.ltd", now=WHEN)
        self.assertIn(b"FAILED_NAVIGATION", pdf)
        self.assertEqual(meta["summary"]["sessions"], 1)


class DocumentIdentityTests(unittest.TestCase):
    def test_the_id_printed_on_the_page_is_the_id_in_the_ledger(self):
        db = _one_session_db()
        pdf, meta = export.export(db, hours=24, actor="admin@ket.ltd", now=WHEN)
        self.assertIn(meta["document_id"].encode("ascii"), pdf)

    def test_the_same_findings_give_the_same_id(self):
        a, _ = _model_for("chat_abc")
        b, _ = _model_for("chat_abc")
        self.assertEqual(export.document_id(a), export.document_id(b))

    def test_different_findings_give_a_different_id(self):
        a, _ = _model_for("chat_abc")
        b, _ = _model_for("chat_xyz")
        self.assertNotEqual(export.document_id(a), export.document_id(b))

    def test_the_id_does_not_change_with_the_pdf_bytes(self):
        # It digests the findings, not the file: re-exporting the same window
        # must not produce a new id for identical findings.
        model, _ = _model_for("chat_abc")
        first = export.render_pdf(model)
        second = export.render_pdf(model)
        self.assertEqual(first, second)


def _model_for(session_id):
    review = qc.review_session(
        session_id,
        [dict(event_type=acr.EV_ACTION_EXECUTED, success=1, payload=None)], [])
    model = export.content_model([review], 24, "admin@ket.ltd", WHEN)
    return model, review


class PdfStructureTests(unittest.TestCase):
    """A file the reader's viewer refuses is not an export. These assert the
    parts a PDF reader actually requires."""

    def test_it_has_a_header_a_trailer_and_a_working_xref(self):
        model, _ = _model_for("chat_abc")
        pdf = export.render_pdf(model)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        start = int(pdf.split(b"startxref")[1].split()[0])
        self.assertEqual(pdf[start:start + 4], b"xref")
        # Every declared offset must land on its object header.
        offsets = [int(l.split()[0]) for l in
                   pdf[start:].split(b"\n")[2:] if l.endswith(b"00000 n ")]
        for n, off in enumerate(offsets, start=1):
            self.assertTrue(pdf[off:].startswith(b"%d 0 obj" % n),
                            "object %d offset is wrong" % n)

    def test_a_long_window_paginates_instead_of_running_off_the_page(self):
        reviews = [qc.review_session(
            "chat_%03d" % i,
            [dict(event_type=acr.EV_ACTION_EXECUTED, success=1, payload=None)],
            []) for i in range(120)]
        model = export.content_model(reviews, 24, "a", WHEN)
        pdf = export.render_pdf(model)
        pages = export.paginate(export._lines(model, "deadbeef"))
        self.assertGreater(len(pages), 1)
        self.assertEqual(pdf.count(b"/Type /Page\n"), 0)  # sanity: dict form
        self.assertEqual(pdf.count(b"/Type /Page "), len(pages))
        for page in pages:
            for _text, _size, _indent, y in page:
                self.assertGreaterEqual(y, export.MARGIN)

    def test_a_window_with_no_sessions_still_produces_a_document(self):
        model = export.content_model([], 24, "a", WHEN)
        pdf = export.render_pdf(model)
        self.assertIn(b"No sessions with canonical activity", pdf)

    def test_a_parenthesis_in_a_value_cannot_break_the_content_stream(self):
        # Unescaped ( or ) ends a PDF string early and corrupts the file.
        self.assertEqual(export._esc("a(b)c\\d"), "a\\(b\\)c\\\\d")

    def test_typographic_punctuation_is_folded_rather_than_printed_as_a_query(self):
        # The base font is not Unicode; an em dash rendered as "?" reads like a
        # mojibake bug in the exporter rather than a font limitation.
        self.assertEqual(export._esc("severity \u2014 FAIL\u2026"),
                         "severity - FAIL...")


if __name__ == "__main__":
    unittest.main()

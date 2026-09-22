import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports import pendency_register_section as prs  # noqa: E402
from reports.report_severity import INFO, WARNING  # noqa: E402

TABLE = """# reg

| ID | Item | Owner | Status | Since | Note |
|----|------|-------|--------|-------|------|
| PR-1 | face_scan_done Meta review | Meta | WAITING | 2026-09-17 | pending |
| PR-2 | MSG91 delivery webhook | Owner | OPEN | 2026-09-16 | code done |
| PR-3 | ACR Step-5 observability | Devin | open | 2026-08-01 | |
| PR-4 | Contact-lens pilot | Owner | BLOCKED | 2026-09-09 | data |
| PR-5 | PR3 validator | Devin | OPEN | 2026-09-10 | |
| PR-9 | Something finished | Devin | DONE | 2026-09-01 | shipped |
| broken row |
"""
TODAY = datetime.date(2026, 9, 22)


class ParseTests(unittest.TestCase):
    def test_rows_and_statuses(self):
        rows = prs.parse(TABLE)
        self.assertEqual([r["id"] for r in rows],
                         ["PR-1", "PR-2", "PR-3", "PR-4", "PR-5", "PR-9"])
        self.assertEqual(rows[2]["status"], "OPEN")
        self.assertEqual([r["id"] for r in prs.open_rows(rows)],
                         ["PR-1", "PR-2", "PR-3", "PR-4", "PR-5"])

    def test_report_lists_required_items_and_drops_done(self):
        text = prs.build(prs.parse(TABLE), None, today=TODAY)
        for needle in ("face_scan_done", "MSG91 delivery webhook",
                       "ACR Step-5", "Contact-lens pilot", "PR3 validator"):
            self.assertIn(needle, text)
        self.assertNotIn("Something finished", text)
        self.assertIn("5 open, 1 done", text)
        self.assertIn("STALE", text)  # PR-3 since 2026-08-01

    def test_missing_register_is_said_not_rendered_as_empty(self):
        rows, err = prs.load(os.path.join(tempfile.gettempdir(), "no_such_register.md"))
        self.assertEqual(rows, [])
        self.assertIn("not readable", err)
        text = prs.build(rows, err)
        self.assertIn("n/a", text)
        self.assertNotIn("0 open", text)
        self.assertEqual([f.severity for f in prs.findings(rows, err)], [WARNING])

    def test_findings_stale_is_warning_fresh_is_info(self):
        f = {x.message.split()[0]: x.severity
             for x in prs.findings(prs.parse(TABLE), None, today=TODAY)}
        self.assertEqual(f["PR-3"], WARNING)
        self.assertEqual(f["PR-2"], INFO)
        self.assertNotIn("PR-9", f)

    def test_repo_register_has_the_required_items(self):
        rows, err = prs.load()
        self.assertIsNone(err)
        text = " ".join(r["item"] for r in prs.open_rows(rows))
        for needle in ("face_scan_done", "MSG91 delivery webhook", "ACR Step-5",
                       "Contact-lens pilot", "PR3 lens validator"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()

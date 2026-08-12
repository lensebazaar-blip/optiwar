"""Tests for the Daily Report severity aggregator.

The headline requirement from operations review: no subsection may report a
CRITICAL/ACTION condition while the executive summary claims everything is
healthy. These tests pin that invariant plus the coverage rules that stop
silence from being mistaken for health.
"""
import json
import os
import shutil
import tempfile
import unittest

from reports import report_severity as rs


class TestExecutiveInvariant(unittest.TestCase):
    """The false-green architecture must be impossible to reproduce."""

    def test_all_clear_only_when_nothing_blocking(self):
        agg = rs.Aggregator()
        agg.mark_reported("observatory")
        out = agg.render_executive()
        self.assertIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertIsNone(agg.assert_invariant())

    def test_action_in_a_later_section_breaks_the_all_clear(self):
        """The exact production bug: observatory clean, sales section has an ACTION."""
        agg = rs.Aggregator()
        agg.mark_reported("observatory")
        agg.add(rs.ACTION, "sales", "3 product(s) sold-out >14d - review",
                source="sales_reconciliation")
        out = agg.render_executive()
        self.assertNotIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertIn("require attention", out)
        self.assertIn("sold-out >14d", out)
        self.assertIsNone(agg.assert_invariant())

    def test_gmc_disapproval_reaches_the_executive_summary(self):
        """710/710 disapproved must not sit under a green banner."""
        agg = rs.Aggregator()
        agg.add(rs.CRITICAL, "gmc", "710/710 products disapproved", source="gmc")
        out = agg.render_executive()
        self.assertIn("CRITICAL=1", out)
        self.assertNotIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertIsNone(agg.assert_invariant())

    def test_warnings_alone_still_count_as_all_clear(self):
        """WARNING is informational; it must not be escalated into a blocker."""
        agg = rs.Aggregator()
        agg.add(rs.WARNING, "headers", "HSTS max-age too low", source="observatory")
        out = agg.render_executive()
        self.assertIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertIn("WARNING=1", out)

    def test_counts_cover_every_contributing_section(self):
        agg = rs.Aggregator()
        agg.add(rs.CRITICAL, "inventory", "negative stock", source="sales")
        agg.add(rs.ACTION, "gmc", "disapprovals", source="gmc")
        agg.add(rs.WARNING, "headers", "hsts", source="observatory")
        agg.add(rs.INFO, "bots", "crawl volume", source="observatory")
        self.assertEqual(agg.counts(),
                         {rs.CRITICAL: 1, rs.ACTION: 1, rs.WARNING: 1, rs.INFO: 1})
        self.assertEqual(agg.worst(), rs.CRITICAL)


class TestCoverageGaps(unittest.TestCase):
    """A section that does not report is a coverage gap, never a pass."""

    def test_missing_expected_source_becomes_a_finding(self):
        agg = rs.Aggregator(expected_sources=["observatory", "gmc", "acr"])
        agg.mark_reported("observatory")
        agg.mark_reported("gmc")
        out = agg.render_executive()
        self.assertIn("acr did not report", out)
        self.assertEqual(agg.counts()[rs.WARNING], 1)

    def test_section_that_ran_clean_is_not_a_coverage_gap(self):
        agg = rs.Aggregator(expected_sources=["acr"])
        agg.mark_reported("acr")
        out = agg.render_executive()
        self.assertNotIn("did not report", out)

    def test_seal_is_idempotent(self):
        agg = rs.Aggregator(expected_sources=["acr"])
        agg.seal()
        agg.seal()
        agg.render_executive()
        self.assertEqual(agg.counts()[rs.WARNING], 1)

    def test_contributing_sections_are_listed(self):
        agg = rs.Aggregator()
        agg.mark_reported("gmc")
        agg.mark_reported("acr")
        self.assertIn("sections contributing: acr, gmc", agg.render_executive())


class TestSidecar(unittest.TestCase):
    """Out-of-process sections contribute through an atomic JSON sidecar."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_emit_then_load_round_trip(self):
        rs.emit("gmc", [rs.Finding(rs.ACTION, "gmc", "710 disapproved")],
                sidecar_dir=self.dir)
        by_source, stale = rs.load_all(sidecar_dir=self.dir)
        self.assertEqual(stale, [])
        self.assertEqual(len(by_source["gmc"]), 1)
        self.assertEqual(by_source["gmc"][0].severity, rs.ACTION)

    def test_aggregator_consumes_sidecars(self):
        rs.emit("gmc", [rs.Finding(rs.ACTION, "gmc", "710 disapproved")],
                sidecar_dir=self.dir)
        by_source, _ = rs.load_all(sidecar_dir=self.dir)
        agg = rs.Aggregator(expected_sources=["gmc"])
        for source, findings in by_source.items():
            agg.extend(findings, source=source)
        out = agg.render_executive()
        self.assertIn("710 disapproved", out)
        self.assertNotIn("no CRITICAL or ACTION-REQUIRED findings", out)

    def test_stale_sidecar_is_excluded_not_trusted(self):
        """Yesterday's verdict must not stand in for today's run."""
        rs.emit("acr", [rs.Finding(rs.INFO, "acr", "fine")], sidecar_dir=self.dir)
        path = os.path.join(self.dir, "acr.json")
        with open(path) as fh:
            payload = json.load(fh)
        payload["generated_at"] = 0  # epoch => ancient
        with open(path, "w") as fh:
            json.dump(payload, fh)
        by_source, stale = rs.load_all(sidecar_dir=self.dir)
        self.assertEqual(by_source, {})
        self.assertEqual(stale, ["acr"])

    def test_corrupt_sidecar_is_a_coverage_gap_not_a_pass(self):
        with open(os.path.join(self.dir, "gmc.json"), "w") as fh:
            fh.write("{not json")
        by_source, stale = rs.load_all(sidecar_dir=self.dir)
        self.assertEqual(by_source, {})
        self.assertEqual(stale, ["gmc"])

    def test_emit_never_raises_on_unwritable_dir(self):
        self.assertFalse(rs.emit("gmc", [], sidecar_dir="/proc/nope/nope"))

    def test_load_all_on_missing_dir_is_empty(self):
        by_source, stale = rs.load_all(sidecar_dir=os.path.join(self.dir, "absent"))
        self.assertEqual((by_source, stale), ({}, []))

    def test_clear_removes_previous_run(self):
        rs.emit("gmc", [rs.Finding(rs.INFO, "gmc", "x")], sidecar_dir=self.dir)
        rs.clear(sidecar_dir=self.dir)
        by_source, _ = rs.load_all(sidecar_dir=self.dir)
        self.assertEqual(by_source, {})


class TestFindingNormalisation(unittest.TestCase):
    def test_tuple_findings_from_observatory_are_accepted(self):
        """seo_observatory.py already emits (severity, category, message)."""
        agg = rs.Aggregator()
        agg.extend([("CRITICAL", "error-pages", "stack trace leak")],
                   source="observatory")
        self.assertEqual(agg.counts()[rs.CRITICAL], 1)
        self.assertEqual(agg.findings[0].source, "observatory")

    def test_unknown_severity_degrades_to_warning_not_silence(self):
        f = rs.Finding("BOGUS", "x", "y")
        self.assertEqual(f.severity, rs.WARNING)

    def test_dict_findings_are_accepted(self):
        agg = rs.Aggregator()
        agg.extend([{"severity": "ACTION", "category": "c", "message": "m"}],
                   source="s")
        self.assertEqual(agg.counts()[rs.ACTION], 1)

    def test_findings_sort_worst_first(self):
        agg = rs.Aggregator()
        agg.add(rs.INFO, "a", "info")
        agg.add(rs.CRITICAL, "b", "crit")
        agg.add(rs.WARNING, "c", "warn")
        self.assertEqual([f.severity for f in agg.sorted_findings()],
                         [rs.CRITICAL, rs.WARNING, rs.INFO])


if __name__ == "__main__":
    unittest.main()

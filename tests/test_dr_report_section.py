import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports import dr_report_section as drs  # noqa: E402
from reports.report_severity import ACTION, WARNING  # noqa: E402

NOW = 1_800_000_000.0


def _iso(hours_ago):
    return (datetime.fromtimestamp(NOW, timezone.utc)
            - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class _Box(object):
    """A fake /root: legacy dumps, dr_backups dir, releases/previous link."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.dr = os.path.join(self.tmp, "dr_backups")
        os.makedirs(self.dr)
        os.makedirs(os.path.join(self.tmp, "backups"))
        os.makedirs(os.path.join(self.tmp, "deploy_releases", "20260928-060456"))
        self.prev = os.path.join(self.tmp, "deploy_releases", "previous")
        os.symlink(os.path.join(self.tmp, "deploy_releases", "20260928-060456"), self.prev)

    def dump(self, hours_ago):
        p = os.path.join(self.tmp, "backups", "optiwar2_backup_x.sql")
        with open(p, "w") as fh:
            fh.write("-- Dump completed\n")
        os.utime(p, (NOW - hours_ago * 3600, NOW - hours_ago * 3600))

    def status(self, hours_ago, ok=True, release="20260928-060456", stages=None, bundle_present=True):
        name = "optiwar_dr_x.tar"
        path = os.path.join(self.dr, name)
        if bundle_present:
            open(path, "w").close()
        with open(os.path.join(self.dr, "latest_status.json"), "w") as fh:
            json.dump({"ok": ok, "verdict": "OK" if ok else "FAILED", "bundle": path,
                       "created_at": _iso(hours_ago), "release": release,
                       "images_included": 0,
                       "stages": stages or {"db_dump": "OK", "bundle": "OK"}}, fh)
        return name

    def offhost(self, hours_ago, bundle="optiwar_dr_x.tar"):
        with open(os.path.join(self.dr, "offhost_status.json"), "w") as fh:
            json.dump({"bundle": bundle, "sha256": "ab", "where": "owner-download",
                       "confirmed_at": _iso(hours_ago)}, fh)

    def drill(self, hours_ago, result="PASS"):
        with open(os.path.join(self.dr, "last_drill.json"), "w") as fh:
            json.dump({"result": result, "finished_at": _iso(hours_ago), "host": "drill-1",
                       "drill": True, "bundle": "optiwar_dr_x.tar", "open_items": 2}, fh)

    def collect(self):
        return drs.collect(now=NOW, legacy_glob=os.path.join(self.tmp, "backups", "*.sql*"),
                           dr_dir=self.dr, releases_previous=self.prev)


class DrReportSectionTests(unittest.TestCase):
    def test_nothing_recorded_is_red_and_says_so(self):
        f = _Box().collect()
        text = drs.build(f)
        self.assertIn("STATUS: RED", text)
        self.assertIn("NONE CONFIRMED", text)
        self.assertIn("Restore drill (isolated host)      NEVER", text)
        msgs = [(x.severity, x.message) for x in drs.findings(f)]
        self.assertIn((ACTION, "no daily database dump found"), msgs)
        self.assertIn((WARNING, "no verified off-host copy has ever been confirmed"), msgs)
        self.assertIn((WARNING, "restore has never been drilled on an isolated host"), msgs)
        self.assertTrue(any(s == ACTION and "no DR bundle status" in m for s, m in msgs))

    def test_fresh_everything_is_green(self):
        b = _Box()
        b.dump(9)
        b.status(8)
        b.offhost(5)
        b.drill(24 * 10)
        f = b.collect()
        self.assertEqual(drs.problems(f), [])
        text = drs.build(f)
        self.assertIn("STATUS: GREEN", text)
        self.assertIn("captured release                 20260928-060456 (live: 20260928-060456)", text)
        self.assertIn("optiwar_dr_x.tar -> owner-download", text)
        self.assertIn("result=PASS", text)
        self.assertIn("open items from drill            2", text)

    def test_stale_dump_and_bundle_are_action(self):
        b = _Box()
        b.dump(30)
        b.status(27)
        b.offhost(1)
        b.drill(1)
        msgs = [(x.severity, x.message) for x in drs.findings(b.collect())]
        self.assertIn((ACTION, "daily database dump is 30.0 h old (limit 26 h)"), msgs)
        self.assertIn((ACTION, "last DR bundle is 27.0 h old (limit 26 h)"), msgs)
        self.assertEqual(drs.status_of(b.collect()), "RED")

    def test_failed_backup_names_its_stages(self):
        b = _Box()
        b.dump(1)
        b.status(1, ok=False, stages={"db_dump": "OK", "secrets_encrypted": "FAILED",
                                      "bundle": "SKIPPED"})
        b.offhost(1)
        b.drill(1)
        msgs = [m for _, m in drs.problems(b.collect())]
        self.assertIn("last DR backup ended FAILED; failed stages: bundle, secrets_encrypted", msgs)

    def test_bundle_file_removed_after_ok_is_action(self):
        b = _Box()
        b.dump(1)
        b.status(1, bundle_present=False)
        b.offhost(1)
        b.drill(1)
        msgs = [m for _, m in drs.problems(b.collect())]
        self.assertIn("DR bundle recorded as OK but the file is gone from the server", msgs)
        self.assertIn("FILE MISSING", drs.build(b.collect()))

    def test_offhost_and_drill_ages_are_warnings_not_actions(self):
        b = _Box()
        b.dump(1)
        b.status(1)
        b.offhost(24 * 8, bundle="optiwar_dr_old.tar")
        b.drill(24 * 91)
        f = b.collect()
        p = drs.problems(f)
        self.assertEqual(sorted(set(s for s, _ in p)), [WARNING])
        self.assertEqual(drs.status_of(f), "AMBER")
        self.assertIn("(older than latest bundle)", drs.build(f))

    def test_failed_drill_is_action_and_release_drift_is_warning(self):
        b = _Box()
        b.dump(1)
        b.status(1, release="20260927-000000")
        b.offhost(1)
        b.drill(1, result="FAIL")
        msgs = [(x.severity, x.message) for x in drs.findings(b.collect())]
        self.assertIn((ACTION, "last restore drill result: FAIL"), msgs)
        self.assertIn((WARNING, "DR bundle captured release 20260927-000000 but "
                       "20260928-060456 is live"), msgs)

    def test_parse_ts_accepts_prod_formats(self):
        self.assertIsNotNone(drs._parse_ts("2026-09-28T12:11:33+05:30"))
        self.assertIsNotNone(drs._parse_ts("20260928-121147"))
        self.assertIsNone(drs._parse_ts("yesterday"))
        self.assertIsNone(drs._parse_ts(None))


if __name__ == "__main__":
    unittest.main()

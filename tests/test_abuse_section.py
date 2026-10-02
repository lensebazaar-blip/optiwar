"""Daily report: abuse & attack signals from the upload log, nginx and fail2ban."""
import datetime
import importlib.util
import os
import sys
import tempfile
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

spec = importlib.util.spec_from_file_location("abuse_section", os.path.join(REPO, "reports", "abuse_section.py"))
section = importlib.util.module_from_spec(spec)
spec.loader.exec_module(section)

NOW = datetime.datetime(2026, 10, 2, 12, 0, 0)
TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

DEBUG = """[2026-10-02 10:00:00,1] WARNING in chat_gateway: ACTIVITY:ATTACHMENT event=rate_limited code=ATTACHMENT_RATE_LIMITED ip=203.0.113.9 bytes_in=0 bytes_out=0
[2026-10-02 10:20:00,1] WARNING in chat_gateway: ACTIVITY:ATTACHMENT event=rate_limited code=ATTACHMENT_RATE_LIMITED ip=203.0.113.9 bytes_in=0 bytes_out=0
[2026-10-02 10:30:00,1] WARNING in chat_gateway: ACTIVITY:ATTACHMENT event=refused code=ATTACHMENT_TYPE ip=198.51.100.4 bytes_in=300 bytes_out=0
[2026-10-02 10:31:00,1] INFO in chat_gateway: ACTIVITY:ATTACHMENT event=shrunk code=- ip=198.51.100.4 bytes_in=9437184 bytes_out=524288
[2026-10-02 10:32:00,1] WARNING in chat_gateway: ACTIVITY:ATTACHMENT event=ket_refused code=HTTP_413 ip=198.51.100.4 bytes_in=524288 bytes_out=0
[2026-09-30 10:00:00,1] WARNING in chat_gateway: ACTIVITY:ATTACHMENT event=rate_limited code=ATTACHMENT_RATE_LIMITED ip=192.0.2.1 bytes_in=0 bytes_out=0
"""

ACCESS = """optiwar.com 34.62.82.165 - - [02/Oct/2026:10:00:00 +0530] "GET /.env HTTP/1.1" 404 10 "-" "x"
optiwar.com 34.62.82.165 - - [02/Oct/2026:10:00:01 +0530] "GET /wp-login.php HTTP/1.1" 404 10 "-" "x"
optiwar.in 203.0.113.9 - - [02/Oct/2026:10:00:02 +0530] "POST /api/chat/attachment HTTP/1.1" 429 10 "-" "x"
optiwar.in 203.0.113.9 - - [02/Oct/2026:10:00:03 +0530] "POST /api/chat/attachment HTTP/1.1" 413 10 "-" "x"
optiwar.in 127.0.0.1 - - [02/Oct/2026:10:00:04 +0530] "GET / HTTP/1.1" 200 10 "-" "x"
optiwar.in 9.9.9.9 - - [29/Sep/2026:10:00:04 +0530] "GET /.git/config HTTP/1.1" 404 10 "-" "x"
"""


class AbuseSection(unittest.TestCase):

    def _file(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_uploads_are_counted_by_event_within_the_window(self):
        up = section.uploads([self._file(DEBUG)], now=NOW)
        self.assertEqual(up["rate_limited"], {"203.0.113.9": 2})
        self.assertEqual(up["refused"], {"ATTACHMENT_TYPE": 1})
        self.assertEqual((up["shrunk"], up["bytes_in"], up["bytes_out"]), (1, 9437184, 524288))
        self.assertEqual(up["ket_refused"], 1)

    def test_nginx_probes_413_429_and_busiest_outside_ips(self):
        acc = section.access([self._file(ACCESS)], now=NOW.replace(tzinfo=TZ))
        self.assertEqual((acc["requests"], acc["413"], acc["429"]), (5, 1, 1))
        self.assertEqual(acc["probes"], 2)
        self.assertEqual(acc["probe_ips"], {"34.62.82.165": 2})
        self.assertEqual(acc["attach_posts"], 2)
        self.assertNotIn("127.0.0.1", acc["ips"])

    def test_fail2ban_status_is_read_per_jail(self):
        out = "Status for the jail: sshd\n|- Actions\n   |- Currently banned:\t3\n   |- Total banned:\t3127\n"

        def run(cmd, **kw):
            if cmd[-1] == "sshd":
                return types.SimpleNamespace(stdout=out)
            raise OSError("no jail")
        self.assertEqual(section.fail2ban(("sshd", "captcha"), run=run), {"sshd": (3, 3127), "captcha": None})

    def test_a_blocked_ip_is_a_warning_and_a_quiet_day_is_green(self):
        up = section.uploads([self._file(DEBUG)], now=NOW)
        acc = section.access([self._file(ACCESS)], now=NOW.replace(tzinfo=TZ))
        text = section.build(up, acc, {"sshd": (3, 3127)})
        self.assertIn("STATUS: AMBER", text)
        self.assertIn("203.0.113.9 x2", text)
        self.assertIn("8.5 MB saved", text)
        sevs = [f.severity for f in section.findings(up, acc)]
        self.assertIn("WARNING", sevs)
        quiet_up = section.uploads([], now=NOW)
        quiet_acc = section.access([], now=NOW.replace(tzinfo=TZ))
        self.assertIn("STATUS: GREEN", section.build(quiet_up, quiet_acc, {}))
        self.assertEqual([f.severity for f in section.findings(quiet_up, quiet_acc)], ["INFO"])


if __name__ == "__main__":
    unittest.main()

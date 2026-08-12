#!/usr/bin/env python3
"""Turn the nightly SEO Observatory output into structured severity findings.

The Observatory already classifies its own findings as
``[WARNING] (headers) optiwar.com: ...``; it just printed them. The daily
report then copied that text block to the top and called it the executive
status, which is how the report came to be judged by one section's opinion.

Parsing here keeps the Observatory unchanged while letting its findings enter
the same aggregator as every other section.

Freshness is part of the verdict: a stale Observatory file is not a clean bill
of health, it is yesterday's answer, so it is reported as ACTION rather than
being silently reused.
"""
import os
import re
import time

from reports.report_severity import ACTION, Finding, CRITICAL, INFO, WARNING

SOURCE = "observatory"

# "  [WARNING] (headers) optiwar.com: missing x-content-type-options ..."
_LINE = re.compile(
    r"^\s*\[(CRITICAL|ACTION|WARNING|INFO)\]\s*(?:\(([^)]*)\)\s*)?(.+?)\s*$")

_SEVERITIES = {CRITICAL, ACTION, WARNING, INFO}

# Beyond this the nightly 05:30 crawl has clearly not run.
STALE_HOURS = 30


def parse(text):
    """Findings carried by the Observatory's own severity-tagged lines."""
    out = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        severity, category, message = m.group(1), m.group(2) or "", m.group(3)
        if severity in _SEVERITIES:
            out.append(Finding(severity, category, message, SOURCE))
    return out


def findings(path, now=None):
    """Findings for the Observatory file at ``path``, including its freshness."""
    if not os.path.exists(path):
        return [Finding(ACTION, "coverage",
                        "no observatory output — the 05:30 crawl has not run, "
                        "so crawl health is UNKNOWN", SOURCE)]
    try:
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
    except Exception as e:  # noqa: BLE001
        return [Finding(ACTION, "coverage",
                        "observatory output unreadable (%s) — crawl health is "
                        "UNKNOWN" % e, SOURCE)]
    out = parse(text)
    now = time.time() if now is None else now
    age_h = (now - os.path.getmtime(path)) / 3600.0
    if age_h > STALE_HOURS:
        out.append(Finding(
            ACTION, "coverage",
            "observatory data stale (%.1fh) — findings below are not current"
            % age_h, SOURCE))
    return out

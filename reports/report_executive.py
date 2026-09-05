#!/usr/bin/env python3
"""Render the Daily Report's executive status *after* every section has run.

The orchestrator assembles the report in stages — base report, then the GMC
section, then the ACR section, then the mailer. The executive banner used to be
written during the first stage, which made it structurally incapable of seeing
the two sections appended after it. That is how a report came to say
``ACTION=0 / no CRITICAL or ACTION-REQUIRED findings`` at the top while carrying
710 disapproved GMC products and a 19-day sold-out worklist further down.

This script runs last. It replaces the placeholder the base report leaves behind
with a banner computed from everything that actually ended up in the report:

    base report -> GMC -> ACR -> report_executive.py -> mailer

Two contribution paths feed it, deliberately:

1. **Structured findings** via ``report_severity`` sidecars. This is the real
   mechanism and what new sections should use.
2. **A scavenger pass over the assembled text.** Not every section has been
   migrated yet, and a section that has not been migrated must not be able to
   hide a CRITICAL. The scavenger reads the finished report for severity
   markers those sections already print, so the invariant holds during the
   migration rather than only after it.

The invariant is enforced, not assumed: if a blocking finding exists and the
rendered banner still claims an all-clear, this script fails loudly rather than
sending a reassuring report.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports.report_severity import (  # noqa: E402
    ACTION, CRITICAL, WARNING, Aggregator, Finding, load_all,
)

REPORT_DIR = os.environ.get("OPTIWAR_REPORT_DIR", "/root/reports")

# The base report writes this line where the banner belongs. Substituting into a
# placeholder (rather than prepending) keeps the banner in its familiar position
# without the base report having to know what the later sections found.
PLACEHOLDER = "{{EXECUTIVE_STATUS}}"

# Sections expected to contribute every run. A silent section is a coverage gap,
# not a pass, so these names are what turn silence into a WARNING.
EXPECTED_SOURCES = ("base", "observatory", "gmc", "acr", "lens", "payments")

# Severity markers already printed by sections that predate the sidecar
# protocol. Each entry is (compiled pattern, severity, category). Patterns are
# deliberately narrow: a false ACTION erodes trust in the banner just as much as
# a false green does.
_SCAVENGE_PATTERNS = (
    (re.compile(r"^\s*\[(CRITICAL)\]\s*(.+)$"), CRITICAL, "legacy"),
    (re.compile(r"^\s*\[(ACTION)\]\s*(.+)$"), ACTION, "legacy"),
    (re.compile(r"^\s*\[(WARNING)\]\s*(.+)$"), WARNING, "legacy"),
    # "*** ACTION: 1 product(s) sold-out >14d ... ***" (reconciliation),
    # "*** WARNING: observatory data stale (>30h) ***" (base report).
    (re.compile(r"^\s*\*{3}\s*(?:ACTION|WARNING|ALERT)[:\s]+(.+?)\s*\*{3}\s*$"),
     ACTION, "legacy"),
    # "  * ACTION: 1 product(s) for discontinuation review" (summary bullets).
    (re.compile(r"^\s*\*\s*ACTION:\s*(.+?)\s*$"), ACTION, "legacy"),
)

# An already-rendered banner is stripped before scavenging and before writing.
# It quotes the very findings it is reporting, so leaving it in place would both
# double-count on a re-run and leave two contradictory verdicts in one report.
_BANNER_START = "OPERATIONAL STATUS"
# The banner ends at the first section divider. The report draws dividers with
# box-drawing characters in some places and '=' in others.
_DIVIDER_CHARS = ("=", "\u2550")


def _is_divider(line):
    s = line.strip()
    return len(s) >= 20 and any(set(s) == {c} for c in _DIVIDER_CHARS)


def _strip_banner(text):
    """Drop an already-rendered banner so re-running is idempotent."""
    out, skipping = [], False
    for ln in text.splitlines():
        if ln.strip().startswith(_BANNER_START):
            skipping = True
            continue
        if skipping:
            if _is_divider(ln) or ln.strip().startswith("SECTION 1"):
                skipping = False
            else:
                continue
        out.append(ln)
    return "\n".join(out)


def scavenge(text):
    """Findings recovered from report text produced by un-migrated sections.

    Every line is swept, including inside sections that reported structurally:
    those sections are only *partly* migrated, and a marker printed by a part
    that still writes text must not be discarded because a sibling part now
    reports properly. Double counting is handled per message, not per region.
    """
    found = []
    for line in _strip_banner(text).splitlines():
        for pattern, severity, category in _SCAVENGE_PATTERNS:
            m = pattern.match(line)
            if m:
                msg = m.group(m.lastindex or 1).strip()
                found.append((severity, category, msg))
                break
    return found


def _key(message):
    """Normalised message text, for matching a scavenged line to a finding."""
    msg = re.sub(r"^\([^)]*\)\s*", "", str(message).strip())
    return re.sub(r"\s+", " ", msg).lower()


def build_aggregator(report_text, sidecar_dir=None):
    """Aggregate sidecar findings plus anything scavenged from the report."""
    agg = Aggregator(expected_sources=EXPECTED_SOURCES)
    kwargs = {"sidecar_dir": sidecar_dir} if sidecar_dir else {}
    by_source, stale = load_all(**kwargs)
    for source, findings in by_source.items():
        agg.extend(findings, source=source)
        agg.mark_reported(source)
    for source in stale:
        # Stale or corrupt output is not a verdict. Left unreported it would
        # look like a clean section; recorded, it shows up as a coverage gap.
        # Appended directly rather than through add(): a section whose output
        # was discarded must not be listed among the contributors.
        agg.findings.append(Finding(
            WARNING, "coverage",
            "%s findings were stale or unreadable — excluded from the "
            "executive status" % source, source))
    # A migrated section prints the findings it also reported, so the same
    # message arriving from both paths is counted once — by message, so that a
    # marker from an un-migrated part of that same section still gets through.
    reported = {_key(f.message) for f in agg.findings}
    for severity, category, message in scavenge(report_text):
        if _key(message) in reported:
            continue
        agg.add(severity, category, message, source="report-text")
        reported.add(_key(message))
    return agg


class NoPlaceholder(Exception):
    """Raised for an append-only target that today's report did not reach."""


def compute_banner(report_text, sidecar_dir=None):
    """Today's executive banner, from the sidecars plus today's report text.

    Raises RuntimeError if the banner would violate the invariant, so a broken
    aggregation surfaces as a failed cron rather than a false green.
    """
    agg = build_aggregator(report_text, sidecar_dir=sidecar_dir)
    banner = agg.render_executive()
    violation = agg.assert_invariant()
    if violation:
        raise RuntimeError(violation)
    return banner


def finalize(report_text, sidecar_dir=None, require_placeholder=False,
             banner=None):
    """Return the report with its executive status rendered in place.

    ``banner`` is the already-computed verdict for today. The rolling log needs
    it: that file holds every previous day's report, so aggregating from its
    own text would count months of resolved findings as today's. Alongside
    ``require_placeholder``, which stops the fallback below from rewriting an
    older entry's banner, today's verdict lands only on today's entry.
    """
    if require_placeholder and PLACEHOLDER not in report_text:
        raise NoPlaceholder("no placeholder to render into")
    if banner is None:
        banner = compute_banner(report_text, sidecar_dir=sidecar_dir)
    if PLACEHOLDER in report_text:
        return report_text.replace(PLACEHOLDER, banner.rstrip("\n"))
    # Base report not yet migrated: replace its own stale banner in place, so
    # the report carries one verdict rather than an old green above a new red.
    stripped = _strip_banner(report_text)
    if stripped != report_text:
        lines = report_text.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip().startswith(_BANNER_START):
                head = "\n".join(lines[:i])
                tail = _strip_banner("\n".join(lines[i:]))
                return head + "\n" + banner + tail
    return banner + "\n" + report_text


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # Append-only history: render only into today's placeholder, never rewrite
    # an older entry's banner.
    strict = {a[len("--placeholder-only="):]
              for a in sys.argv[1:] if a.startswith("--placeholder-only=")}
    targets = [os.path.join(REPORT_DIR, f) for f in args] or [
        os.path.join(REPORT_DIR, "daily_latest.txt")]
    texts = []
    for path in targets:
        if not os.path.exists(path):
            continue
        with open(path, "r", errors="replace") as f:
            texts.append((path, f.read()))
    if not texts:
        print("report_executive: no report to finalize", file=sys.stderr)
        return 1
    # One verdict for the day, computed from today's report, then rendered into
    # each copy of it — an append-only history file holds every previous day's
    # report, so aggregating from one would count resolved findings as today's.
    # Today's report is the one still carrying the placeholder; argument order
    # is not a guarantee. A history target is never the source.
    candidates = [(p, t) for p, t in texts
                  if os.path.basename(p) not in strict]
    today = next((t for _, t in candidates if PLACEHOLDER in t), None)
    if today is None:
        if not candidates:
            print("report_executive: only history targets given; nothing to "
                  "aggregate from", file=sys.stderr)
            return 1
        today = candidates[0][1]
    try:
        banner = compute_banner(today)
    except RuntimeError as e:
        print("report_executive: INVARIANT VIOLATION: %s" % e, file=sys.stderr)
        return 1
    rc = 0
    missing = [p for p in targets if not os.path.exists(p)]
    for path in missing:
        print("report_executive: missing %s" % path, file=sys.stderr)
        rc = 1
    for path, text in texts:
        try:
            out = finalize(text, banner=banner,
                           require_placeholder=os.path.basename(path) in strict)
        except NoPlaceholder:
            print("report_executive: %s has no placeholder; left unchanged"
                  % path)
            continue
        except RuntimeError as e:
            print("report_executive: INVARIANT VIOLATION in %s: %s" % (path, e),
                  file=sys.stderr)
            rc = 1
            continue
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(out)
        os.replace(tmp, path)
        print("report_executive: executive status rendered into %s" % path)
    return rc


if __name__ == "__main__":
    sys.exit(main())

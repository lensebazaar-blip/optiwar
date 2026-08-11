#!/usr/bin/env python3
"""Shared severity ledger for the Optiwar Daily Operations Report.

Why this module exists
----------------------
The executive banner used to be produced by copying the SEO Observatory's own
summary to the top of the report. That summary only ever counted the
Observatory's findings, and it was rendered *before* the remaining sections had
even run, so a report could legitimately print::

    CRITICAL=0  ACTION=0
    status: no CRITICAL or ACTION-REQUIRED findings

while later sections reported a sold-out worklist, 710 disapproved GMC products
and landing-page errors. That is a false-green architecture: the top-level
verdict was structurally incapable of seeing most of the report.

This module makes severity a first-class value that every section returns into
one aggregator. The executive status is computed only after every section has
run, and it enforces one invariant:

    **No subsection may report CRITICAL/ACTION while the executive summary
    says everything is healthy.**

Silence is also not health. A section that fails to report is a *coverage gap*,
not a pass, so a missing or stale contributor is itself recorded as a finding.

Out-of-process sections (the GMC and ACR sections run as separate scripts)
contribute through a sidecar JSON file written next to the report; see
``emit`` / ``load_all``.
"""
import json
import os
import tempfile
import time

CRITICAL = "CRITICAL"
ACTION = "ACTION"
WARNING = "WARNING"
INFO = "INFO"

# Ordered worst -> least severe. Matches the vocabulary seo_observatory.py
# already uses so existing findings map across without translation.
SEV_ORDER = {CRITICAL: 0, ACTION: 1, WARNING: 2, INFO: 3}

# Severities that must never coexist with an all-clear executive summary.
BLOCKING = (CRITICAL, ACTION)

DEFAULT_SIDECAR_DIR = "/root/reports/findings"

# A contributor's sidecar older than this is treated as "did not report" — it is
# stale output from a previous day rather than today's truth.
DEFAULT_MAX_AGE_S = 6 * 3600


class Finding(object):
    """One severity-classified observation from one report section."""

    __slots__ = ("severity", "category", "message", "source")

    def __init__(self, severity, category, message, source=""):
        sev = (severity or WARNING).upper()
        self.severity = sev if sev in SEV_ORDER else WARNING
        self.category = category or ""
        self.message = message or ""
        self.source = source or ""

    def as_dict(self):
        return {"severity": self.severity, "category": self.category,
                "message": self.message, "source": self.source}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("severity"), d.get("category"), d.get("message"),
                   d.get("source"))

    def __eq__(self, other):
        return isinstance(other, Finding) and self.as_dict() == other.as_dict()

    def __repr__(self):
        return "Finding(%s, %s, %r, %s)" % (self.severity, self.category,
                                            self.message, self.source)


class Aggregator(object):
    """Collects findings from every section and renders the executive banner.

    Usage is deliberately boring: sections call :meth:`add` (or return findings
    that the caller feeds in via :meth:`extend`), and the banner is rendered
    once, last, by :meth:`render_executive`.
    """

    def __init__(self, expected_sources=()):
        # Sources that MUST contribute. A source that never reports produces a
        # coverage finding rather than silently counting as healthy.
        self.expected_sources = list(expected_sources)
        self.findings = []
        self.reported_sources = set()

    def add(self, severity, category, message, source=""):
        f = Finding(severity, category, message, source)
        self.findings.append(f)
        if f.source:
            self.reported_sources.add(f.source)
        return f

    def extend(self, findings, source=""):
        for f in findings or ():
            if isinstance(f, Finding):
                if source and not f.source:
                    f.source = source
                self.findings.append(f)
                if f.source:
                    self.reported_sources.add(f.source)
            elif isinstance(f, dict):
                self.extend([Finding.from_dict(f)], source)
            else:  # (severity, category, message) as used by seo_observatory
                seq = list(f)
                self.extend([Finding(seq[0], seq[1] if len(seq) > 1 else "",
                                     seq[2] if len(seq) > 2 else "", source)])

    def mark_reported(self, source):
        """Record that a section ran, even if it produced no findings.

        Without this, a genuinely clean section is indistinguishable from one
        that crashed, and a clean run would be reported as a coverage gap.
        """
        self.reported_sources.add(source)

    def missing_sources(self):
        return [s for s in self.expected_sources
                if s not in self.reported_sources]

    def seal(self):
        """Convert non-reporting contributors into coverage findings.

        Called by :meth:`render_executive`; idempotent so it is safe to call
        directly when the caller wants counts before rendering.
        """
        for src in self.missing_sources():
            already = any(f.category == "coverage" and f.source == src
                          for f in self.findings)
            if not already:
                # Appended directly rather than through add(): a section noted
                # as absent must not thereby be listed among the contributors.
                self.findings.append(Finding(
                    WARNING, "coverage",
                    "%s did not report — executive status is computed "
                    "without it" % src, src))
        return self

    def counts(self):
        c = {CRITICAL: 0, ACTION: 0, WARNING: 0, INFO: 0}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    def blocking(self):
        return [f for f in self.findings if f.severity in BLOCKING]

    def worst(self):
        """Worst severity present, or None when there are no findings."""
        if not self.findings:
            return None
        return min((f.severity for f in self.findings),
                   key=lambda s: SEV_ORDER.get(s, 9))

    def is_all_clear(self):
        return not self.blocking()

    def sorted_findings(self):
        return sorted(
            self.findings,
            key=lambda f: (SEV_ORDER.get(f.severity, 9), f.source, f.category),
        )

    def render_executive(self, width=70, max_per_severity=25):
        """Render the top-of-report status block.

        Computed *after* all sections have contributed. The all-clear line is
        emitted only when there is genuinely nothing blocking, so the
        report-level invariant holds by construction.
        """
        self.seal()
        c = self.counts()
        out = ["OPERATIONAL STATUS (severity-classified — aggregated across all sections)",
               "-" * width]
        out.append("  CRITICAL=%d  ACTION=%d  WARNING=%d  INFO=%d"
                   % (c[CRITICAL], c[ACTION], c[WARNING], c[INFO]))

        blocking = [f for f in self.sorted_findings() if f.severity in BLOCKING]
        if not blocking:
            out.append("  status: ✅ no CRITICAL or ACTION-REQUIRED findings")
        else:
            out.append("  status: ❌ %d finding(s) require attention"
                       % len(blocking))

        contributed = ", ".join(sorted(self.reported_sources)) or "(none)"
        out.append("  sections contributing: %s" % contributed)

        for sev in (CRITICAL, ACTION, WARNING):
            rows = [f for f in self.sorted_findings() if f.severity == sev]
            if not rows:
                continue
            out.append("  --- %sS ---" % sev)
            for f in rows[:max_per_severity]:
                out.append("    [%s] (%s) %s" % (sev, f.category or f.source,
                                                 f.message))
            if len(rows) > max_per_severity:
                out.append("    ... and %d more" % (len(rows) - max_per_severity))
        return "\n".join(out) + "\n"

    def assert_invariant(self):
        """Self-check used by tests and by the report's own smoke path.

        Returns an error string when the rendered banner claims an all-clear
        while blocking findings exist, else None.
        """
        rendered = self.render_executive()
        if self.blocking() and "no CRITICAL or ACTION-REQUIRED findings" in rendered:
            return ("executive summary claims all-clear while %d blocking "
                    "finding(s) exist" % len(self.blocking()))
        return None


# ---------------------------------------------------------------------------
# Sidecar protocol for sections that run as separate processes.
# ---------------------------------------------------------------------------

def emit(source, findings, sidecar_dir=DEFAULT_SIDECAR_DIR):
    """Write one section's findings so the aggregator can pick them up.

    Written atomically (tmp + rename) so a partially written file can never be
    read as a section's verdict. Best-effort: a section must not fail to
    produce its report just because the sidecar could not be written.
    """
    try:
        os.makedirs(sidecar_dir, exist_ok=True)
        payload = {
            "source": source,
            "generated_at": time.time(),
            "findings": [f.as_dict() if isinstance(f, Finding)
                         else Finding.from_dict(f).as_dict()
                         for f in (findings or ())],
        }
        fd, tmp = tempfile.mkstemp(dir=sidecar_dir, prefix=".%s." % source)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, os.path.join(sidecar_dir, "%s.json" % source))
        return True
    except Exception:  # noqa: BLE001 - never break a report section
        return False


def load_all(sidecar_dir=DEFAULT_SIDECAR_DIR, max_age_s=DEFAULT_MAX_AGE_S,
             now=None):
    """Read every sidecar. Returns ``(by_source, stale_sources)``.

    A sidecar older than ``max_age_s`` is yesterday's answer, not today's, so it
    is excluded and reported as stale — counting it would let a section that
    stopped running keep asserting its last known-good state.
    """
    now = time.time() if now is None else now
    by_source, stale = {}, []
    try:
        names = sorted(os.listdir(sidecar_dir))
    except Exception:  # noqa: BLE001 - directory absent on first run
        return by_source, stale
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(sidecar_dir, name)
        try:
            with open(path) as fh:
                payload = json.load(fh)
            source = payload.get("source") or name[:-5]
            age = now - float(payload.get("generated_at") or 0)
            if age > max_age_s:
                stale.append(source)
                continue
            by_source[source] = [Finding.from_dict(d)
                                 for d in payload.get("findings") or ()]
        except Exception:  # noqa: BLE001 - a corrupt sidecar is a coverage gap
            stale.append(name[:-5])
    return by_source, stale


def clear(sidecar_dir=DEFAULT_SIDECAR_DIR):
    """Remove existing sidecars at the start of a run.

    Prevents a section that fails today from having yesterday's findings
    silently reused as if they were current.
    """
    try:
        for name in os.listdir(sidecar_dir):
            if name.endswith(".json"):
                os.unlink(os.path.join(sidecar_dir, name))
    except Exception:  # noqa: BLE001
        pass

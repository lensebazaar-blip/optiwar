#!/usr/bin/env python3
"""Structured severity for the base daily report's own sections.

The base report already decides severity — the reconciliation section builds
``action_flags`` like ``"ACTION: 1 product(s) for discontinuation review"`` and
the DeepSeek section prints ``ACTION REQUIRED: Renew ... key``. That judgement
was only ever rendered as text in the middle of the report, so nothing upstream
could act on it and the top-of-report banner never saw it.

This converts those existing strings into findings, so the base report becomes
a contributor to the executive status rather than a section that quietly
disagrees with it.
"""
import re

from reports.report_severity import ACTION, CRITICAL, Finding, INFO, WARNING

SOURCE = "base"

_PREFIX = re.compile(r"^\s*(CRITICAL|ACTION|WARNING|INFO)\s*:\s*(.+?)\s*$",
                     re.IGNORECASE)

_BY_NAME = {"CRITICAL": CRITICAL, "ACTION": ACTION, "WARNING": WARNING,
            "INFO": INFO}


def from_flags(flags, category="operations"):
    """Findings from ``SEVERITY: message`` strings.

    An unprefixed flag is treated as ACTION: the section chose to raise it, and
    downgrading an unrecognised alert to INFO is exactly the failure mode this
    work exists to remove.
    """
    out = []
    for flag in flags or ():
        m = _PREFIX.match(str(flag))
        if m:
            out.append(Finding(_BY_NAME[m.group(1).upper()], category,
                               m.group(2), SOURCE))
        else:
            text = str(flag).strip()
            if text:
                out.append(Finding(ACTION, category, text, SOURCE))
    return out

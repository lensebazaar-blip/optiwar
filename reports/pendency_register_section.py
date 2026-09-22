#!/usr/bin/env python3
"""Pendency Register — Daily Report section.

Renders the open rows of ``docs/PENDENCY_REGISTER.md`` (the one register; this
section never holds its own list). A row leaves the report when its Status is
set to DONE there. A missing or unparseable register is said so, not rendered
as "nothing pending".

    PENDENCY_REGISTER_PATH  override the register location (default: repo docs)
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIDTH = 70
BANNER = "=" * WIDTH
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "docs", "PENDENCY_REGISTER.md")
REGISTER_PATH = os.environ.get("PENDENCY_REGISTER_PATH", DEFAULT_PATH)
STALE_DAYS = int(os.environ.get("PENDENCY_STALE_DAYS", "30"))

OPEN_STATUSES = ("OPEN", "BLOCKED", "WAITING")
COLUMNS = ("id", "item", "owner", "status", "since", "note")
GREEN, AMBER = "GREEN", "AMBER"

_ROW = re.compile(r"^\|(.*)\|\s*$")


def parse(text):
    """Rows of the register table as dicts; a malformed row is skipped."""
    rows = []
    header = None
    for line in text.splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if header is None:
            if [c.lower() for c in cells[:6]] == list(COLUMNS):
                header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if len(cells) < 6:
            continue
        row = dict(zip(COLUMNS, cells[:6]))
        row["status"] = row["status"].upper()
        rows.append(row)
    return rows


def load(path=None):
    """(rows, error) — error is a sentence when the register cannot be read."""
    path = path or REGISTER_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return [], "register not readable at %s (%s)" % (path, exc.__class__.__name__)
    rows = parse(text)
    if not rows:
        return [], "register at %s has no parseable rows" % path
    return rows, None


def open_rows(rows):
    return [r for r in rows if r["status"] in OPEN_STATUSES]


def _age_days(since, today):
    try:
        d = datetime.date.fromisoformat(since)
    except ValueError:
        return None
    return (today - d).days


def build(rows=None, error=None, today=None):
    today = today or datetime.date.today()
    if rows is None and error is None:
        rows, error = load()
    L = []
    add = L.append
    add(BANNER)
    if error:
        add("  PENDENCY REGISTER   STATUS: %s" % AMBER)
        add(BANNER)
        add("  n/a — %s" % error)
        add(BANNER)
        return "\n".join(L)
    pend = open_rows(rows)
    done = len(rows) - len(pend)
    add("  PENDENCY REGISTER (docs/PENDENCY_REGISTER.md)   STATUS: %s" % (
        AMBER if pend else GREEN))
    add(BANNER)
    add("  %d open, %d done. Source: the register file; this section adds nothing." % (
        len(pend), done))
    for status in OPEN_STATUSES:
        group = [r for r in pend if r["status"] == status]
        if not group:
            continue
        add("")
        add("  %s (%d)" % (status, len(group)))
        for r in group:
            age = _age_days(r["since"], today)
            age_s = "?" if age is None else "%dd" % age
            stale = " STALE" if age is not None and age >= STALE_DAYS else ""
            add("  - %-6s %s" % (r["id"], r["item"]))
            add("           owner: %s | since %s (%s%s)" % (
                r["owner"], r["since"], age_s, stale))
            if r["note"]:
                add("           %s" % r["note"])
    add(BANNER)
    return "\n".join(L)


def findings(rows=None, error=None, today=None):
    from reports.report_severity import Finding, INFO, WARNING
    if rows is None and error is None:
        rows, error = load()
    if error:
        return [Finding(WARNING, "pendency_register", error, "pendency_register")]
    today = today or datetime.date.today()
    out = []
    for r in open_rows(rows):
        age = _age_days(r["since"], today)
        sev = WARNING if (age is not None and age >= STALE_DAYS) else INFO
        out.append(Finding(sev, "pendency_register",
                           "%s %s (%s, %s)" % (r["id"], r["item"], r["status"], r["owner"]),
                           "pendency_register"))
    return out


def main():
    rows, error = load()
    print(build(rows, error))
    try:
        from reports.report_severity import emit
        emit("pendency_register", findings(rows, error))
    except Exception:  # noqa: BLE001 - the section still stands on its own
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Development team — Daily Report section.

Every defect the application recorded in the window (``ACTIVITY:DEV_DEFECT``
lines written by ``flaskr/dev_defects.py``, from the server and from
customers' browsers), grouped by code: how often, where first and last seen,
and on which page. Each group is one candidate task for the next sprint —
the point is that a defect a customer met is on the engineers' desk the next
morning whether or not anyone wrote in.

Sourcing: the application log only (today's and yesterday's file). The lines
carry a code, an origin, a short place and a path — never a prescription
value, a message text or a customer identifier — so this section prints them
as they are.
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIDTH = 70
BANNER = "=" * WIDTH
WINDOW_HOURS = int(os.environ.get("ACR_REPORT_WINDOW_HOURS", "24"))
LOG_DIR = os.environ.get("OPTIWAR_LOG_DIR", "/var/log/optiwar")
DEBUG_LOG = os.path.join(LOG_DIR, "debug.log")

GREEN, AMBER = "GREEN", "AMBER"

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_LINE = re.compile(
    r"ACTIVITY:DEV_DEFECT code=(?P<code>[A-Z0-9_]+) origin=(?P<origin>\w+) "
    r"where=(?P<where>.*?) page=(?P<page>\S*)\s*$")


def log_files(now=None):
    now = now or datetime.datetime.now()
    files = []
    yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    rotated = os.path.join(LOG_DIR, "debug.log.%s" % yesterday)
    if os.path.exists(rotated):
        files.append(rotated)
    if os.path.exists(DEBUG_LOG):
        files.append(DEBUG_LOG)
    return files


def defects(files=None, now=None):
    """code -> {count, origins, first, last, where, pages} within the window."""
    now = now or datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=WINDOW_HOURS)
    groups = {}
    for path in (files if files is not None else log_files(now)):
        try:
            with open(path, "r", errors="replace") as fh:
                for line in fh:
                    m = _LINE.search(line)
                    if not m:
                        continue
                    ts = _TS.match(line)
                    when = None
                    if ts:
                        try:
                            when = datetime.datetime.strptime(
                                ts.group(1), "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            when = None
                    if when is not None and when < cutoff:
                        continue
                    g = groups.setdefault(m.group("code"), {
                        "count": 0, "origins": set(), "first": None,
                        "last": None, "where": [], "pages": set()})
                    g["count"] += 1
                    g["origins"].add(m.group("origin"))
                    if when is not None:
                        g["first"] = min(g["first"] or when, when)
                        g["last"] = max(g["last"] or when, when)
                    where = m.group("where").strip()
                    if where and where not in g["where"] and len(g["where"]) < 3:
                        g["where"].append(where)
                    if m.group("page"):
                        g["pages"].add(m.group("page"))
        except OSError:
            continue
    return groups


def status(groups):
    if not groups:
        return GREEN, "no defects recorded"
    total = sum(g["count"] for g in groups.values())
    return AMBER, "%d defect code(s), %d occurrence(s) — each is a task" % (
        len(groups), total)


def _fmt(when):
    return when.strftime("%H:%M") if when else "?"


def build(groups=None):
    L = []
    add = L.append
    if groups is None:
        groups = defects()
    verdict, why = status(groups)
    add(BANNER)
    add("  DEVELOPMENT TEAM — DEFECTS (last %dh)   STATUS: %s" % (
        WINDOW_HOURS, verdict))
    add(BANNER)
    add("  %s" % why)
    for code in sorted(groups, key=lambda c: -groups[c]["count"]):
        g = groups[code]
        add("  - %-34s x%-4d %s  %s–%s" % (
            code, g["count"], "/".join(sorted(g["origins"])),
            _fmt(g["first"]), _fmt(g["last"])))
        if g["where"]:
            add("      where: %s" % "; ".join(g["where"]))
        if g["pages"]:
            pages = sorted(g["pages"])
            add("      pages: %s%s" % (", ".join(pages[:3]),
                                        " …" if len(pages) > 3 else ""))
    if groups:
        add("  Loop: one task per code; a code that repeats tomorrow is unfixed.")
    add(BANNER)
    return "\n".join(L)


def findings(groups=None):
    from reports.report_severity import ACTION, Finding
    if groups is None:
        groups = defects()
    out = []
    for code in sorted(groups, key=lambda c: -groups[c]["count"]):
        g = groups[code]
        out.append(Finding(ACTION, "dev_defects",
                           "%s x%d (%s)" % (code, g["count"],
                                            "/".join(sorted(g["origins"]))),
                           "dev_defects"))
    return out


def main():
    groups = defects()
    text = build(groups)
    try:
        from reports.report_severity import emit
        emit("dev_defects", findings(groups))
    except Exception:  # noqa: BLE001 - the section still stands on its own
        pass
    print(text)


if __name__ == "__main__":
    main()

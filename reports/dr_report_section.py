#!/usr/bin/env python3
"""DISASTER RECOVERY — Daily Report section.

Answers, from files the backup tooling leaves behind and never from memory:
when the last database dump was made, when the last full DR bundle was made
and whether every stage of it verified, when a copy of it was last confirmed
off this server, when a restore was last drilled and how it went, and whether
the bundle captured the release that is running now.

Sources (all root-only, all written by deploy/dr/*):

    /root/backups/optiwar2_backup_*.sql*          legacy daily dump (03:15)
    /root/dr_backups/latest_status.json      optiwar_disaster_backup.sh (03:45)
    /root/dr_backups/offhost_status.json     optiwar_dr_confirm_offhost.sh
    /root/dr_backups/last_drill.json         optiwar_restore_server.sh --drill
    /root/deploy_releases/previous           release the harness has live

A fact the files do not hold is printed as ``n/a`` and raised as a finding,
never rendered as "fine".  Thresholds (hours unless stated):

    DR_DB_MAX_AGE_H=26  DR_BUNDLE_MAX_AGE_H=26  DR_OFFHOST_MAX_AGE_D=7
    DR_DRILL_MAX_AGE_D=90
"""
import glob
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIDTH = 70
BANNER = "=" * WIDTH
GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
NA = "n/a"

LEGACY_GLOB = os.environ.get("DR_LEGACY_DUMP_GLOB", "/root/backups/optiwar2_backup_*.sql*")
DR_DIR = os.environ.get("DR_DIR", "/root/dr_backups")
RELEASES_PREVIOUS = os.environ.get("DR_RELEASES_PREVIOUS", "/root/deploy_releases/previous")

DB_MAX_AGE_H = float(os.environ.get("DR_DB_MAX_AGE_H", "26"))
BUNDLE_MAX_AGE_H = float(os.environ.get("DR_BUNDLE_MAX_AGE_H", "26"))
OFFHOST_MAX_AGE_D = float(os.environ.get("DR_OFFHOST_MAX_AGE_D", "7"))
DRILL_MAX_AGE_D = float(os.environ.get("DR_DRILL_MAX_AGE_D", "90"))


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _parse_ts(value):
    """ISO-8601 (with offset) or ``YYYYmmdd-HHMMSS`` -> epoch seconds, else None."""
    if not value:
        return None
    s = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y%m%d-%H%M%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _age_h(epoch, now):
    return None if epoch is None else max(0.0, (now - epoch) / 3600.0)


def _fmt_age(age_h):
    if age_h is None:
        return NA
    if age_h < 48:
        return "%.1f h" % age_h
    return "%.1f d" % (age_h / 24.0)


def collect(now=None, legacy_glob=LEGACY_GLOB, dr_dir=DR_DIR,
            releases_previous=RELEASES_PREVIOUS):
    """Facts as a dict; every missing fact is None."""
    now = time.time() if now is None else now
    f = {}

    dumps = sorted(glob.glob(legacy_glob), key=lambda p: os.path.getmtime(p))
    if dumps:
        f["db_dump_file"] = os.path.basename(dumps[-1])
        f["db_dump_age_h"] = _age_h(os.path.getmtime(dumps[-1]), now)
        f["db_dump_bytes"] = os.path.getsize(dumps[-1])
    else:
        f["db_dump_file"] = f["db_dump_age_h"] = f["db_dump_bytes"] = None

    st = _read_json(os.path.join(dr_dir, "latest_status.json"))
    f["bundle_ok"] = st.get("ok") if st else None
    f["bundle_verdict"] = st.get("verdict") if st else None
    f["bundle"] = os.path.basename(st["bundle"]) if st and st.get("bundle") else None
    f["bundle_exists"] = bool(st and st.get("bundle") and os.path.exists(st["bundle"]))
    f["bundle_age_h"] = _age_h(_parse_ts(st.get("created_at")) if st else None, now)
    f["bundle_release"] = st.get("release") if st else None
    f["bundle_sha256"] = st.get("sha256") or st.get("bundle_sha256") if st else None
    f["bundle_failed_stages"] = sorted(k for k, v in (st or {}).get("stages", {}).items()
                                       if v != "OK")
    f["images_included"] = st.get("images_included") if st else None

    off = _read_json(os.path.join(dr_dir, "offhost_status.json"))
    f["offhost_bundle"] = off.get("bundle") if off else None
    f["offhost_where"] = off.get("where") if off else None
    f["offhost_age_h"] = _age_h(_parse_ts(off.get("confirmed_at")) if off else None, now)
    f["offhost_matches_bundle"] = bool(off and f["bundle"] and off.get("bundle") == f["bundle"])

    drill = _read_json(os.path.join(dr_dir, "last_drill.json"))
    f["drill_result"] = drill.get("result") if drill else None
    f["drill_age_h"] = _age_h(_parse_ts(drill.get("finished_at")) if drill else None, now)
    f["drill_bundle"] = drill.get("bundle") if drill else None
    f["drill_host"] = drill.get("host") if drill else None
    f["drill_open_items"] = drill.get("open_items") if drill else None

    try:
        f["live_release"] = os.path.basename(os.path.realpath(releases_previous)) \
            if os.path.exists(releases_previous) else None
    except OSError:
        f["live_release"] = None
    return f


def problems(f):
    """[(severity, message)] — the section's judgement, used by both renderers."""
    from reports.report_severity import ACTION, WARNING
    out = []
    a = f.get("db_dump_age_h")
    if a is None:
        out.append((ACTION, "no daily database dump found"))
    elif a > DB_MAX_AGE_H:
        out.append((ACTION, "daily database dump is %s old (limit %.0f h)"
                    % (_fmt_age(a), DB_MAX_AGE_H)))

    if f.get("bundle_ok") is None:
        out.append((ACTION, "no DR bundle status recorded (backup never ran or status unreadable)"))
    else:
        if not f["bundle_ok"]:
            out.append((ACTION, "last DR backup ended %s; failed stages: %s"
                        % (f.get("bundle_verdict"), ", ".join(f["bundle_failed_stages"]) or "?")))
        b = f.get("bundle_age_h")
        if b is None or b > BUNDLE_MAX_AGE_H:
            out.append((ACTION, "last DR bundle is %s old (limit %.0f h)"
                        % (_fmt_age(b), BUNDLE_MAX_AGE_H)))
        if f["bundle_ok"] and not f.get("bundle_exists"):
            out.append((ACTION, "DR bundle recorded as OK but the file is gone from the server"))
        if f.get("live_release") and f.get("bundle_release") \
                and f["live_release"] != f["bundle_release"]:
            out.append((WARNING, "DR bundle captured release %s but %s is live"
                        % (f["bundle_release"], f["live_release"])))

    o = f.get("offhost_age_h")
    if o is None:
        out.append((WARNING, "no verified off-host copy has ever been confirmed"))
    elif o > OFFHOST_MAX_AGE_D * 24:
        out.append((WARNING, "verified off-host copy is %s old (limit %.0f d)"
                    % (_fmt_age(o), OFFHOST_MAX_AGE_D)))

    d = f.get("drill_age_h")
    if d is None:
        out.append((WARNING, "restore has never been drilled on an isolated host"))
    else:
        if f.get("drill_result") != "PASS":
            out.append((ACTION, "last restore drill result: %s" % f.get("drill_result")))
        if d > DRILL_MAX_AGE_D * 24:
            out.append((WARNING, "last restore drill is %s old (limit %.0f d)"
                        % (_fmt_age(d), DRILL_MAX_AGE_D)))
    return out


def status_of(f):
    from reports.report_severity import ACTION
    p = problems(f)
    if any(s == ACTION for s, _ in p):
        return RED
    return AMBER if p else GREEN


def build(f=None):
    if f is None:
        f = collect()
    L = []
    add = L.append
    add(BANNER)
    add("  DISASTER RECOVERY                              STATUS: %s" % status_of(f))
    add(BANNER)
    add("  Every age below is read from the artefact itself; nothing is assumed.")
    add("")
    add("  %-34s %s" % ("Database dump (daily 03:15)",
                        NA if f.get("db_dump_age_h") is None
                        else "%s old  %s" % (_fmt_age(f["db_dump_age_h"]), f["db_dump_file"])))
    if f.get("bundle_ok") is None:
        add("  %-34s %s" % ("Full DR bundle (daily 03:45)", NA))
    else:
        add("  %-34s %s old  %s  verdict=%s%s"
            % ("Full DR bundle (daily 03:45)", _fmt_age(f.get("bundle_age_h")),
               f.get("bundle") or NA, f.get("bundle_verdict"),
               "" if f.get("bundle_exists") else "  FILE MISSING"))
        add("  %-34s %s" % ("  captured release",
                            "%s (live: %s)" % (f.get("bundle_release") or NA,
                                               f.get("live_release") or NA)))
        add("  %-34s %s" % ("  static images in this run",
                            "yes" if f.get("images_included") else "no (weekly)"))
    if f.get("offhost_age_h") is None:
        add("  %-34s %s" % ("Off-host copy (verified)", "NONE CONFIRMED"))
    else:
        add("  %-34s %s old  %s -> %s%s"
            % ("Off-host copy (verified)", _fmt_age(f["offhost_age_h"]),
               f.get("offhost_bundle"), f.get("offhost_where"),
               "" if f.get("offhost_matches_bundle") else "  (older than latest bundle)"))
    if f.get("drill_age_h") is None:
        add("  %-34s %s" % ("Restore drill (isolated host)", "NEVER"))
    else:
        add("  %-34s %s old  result=%s  host=%s  bundle=%s"
            % ("Restore drill (isolated host)", _fmt_age(f["drill_age_h"]),
               f.get("drill_result"), f.get("drill_host") or NA, f.get("drill_bundle") or NA))
        if f.get("drill_open_items"):
            add("  %-34s %s" % ("  open items from drill", f["drill_open_items"]))
    p = problems(f)
    if p:
        add("")
        for sev, msg in p:
            add("  [%s] %s" % (sev, msg))
    add("  Off-host = owner's verified download; passphrase lives outside this server.")
    add(BANNER)
    return "\n".join(L)


def findings(f=None):
    from reports.report_severity import Finding
    if f is None:
        f = collect()
    return [Finding(sev, "dr", msg, "dr") for sev, msg in problems(f)]


def main():
    f = collect()
    print(build(f))
    try:
        from reports.report_severity import emit
        emit("dr", findings(f))
    except Exception:  # noqa: BLE001 - the section still stands on its own
        pass


if __name__ == "__main__":
    main()

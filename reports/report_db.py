#!/usr/bin/env python3
"""Read-only DB access shared by the out-of-process report sections.

The ACR and contact-lens sections run as separate scripts appended to the 06:00
report, and both need the same thing: a query, credentials from the environment,
and a failure that degrades one metric to ``n/a`` instead of aborting the
report. Two copies of that would be two chances for one section to authenticate
as the wrong identity or to swallow an error the other reports.
"""
import os
import subprocess

# What a metric prints when it could not be read or is not instrumented yet. A
# report that renders 0 for an unknown is worse than one that admits the gap.
NA = "n/a (pending instrumentation)"


def db_conf():
    return dict(
        host=os.environ.get("ACR_REPORT_DB_HOST") or os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("ACR_REPORT_DB_USER") or os.environ.get("MYSQL_USER", ""),
        passwd=os.environ.get("ACR_REPORT_DB_PASS") or os.environ.get("MYSQL_PASSWORD", ""),
        name=(os.environ.get("ACR_REPORT_DB_NAME") or os.environ.get("MYSQL_DB")
              or os.environ.get("MYSQL_DATABASE", "")),
    )


class SqlError(Exception):
    pass


def run_sql(query):
    """Run a read-only query via the mysql client; return rows as list of tuples.

    Credentials come from the environment (never inlined). Raises SqlError so the
    caller can degrade a single metric to n/a without aborting the whole report.
    """
    c = db_conf()
    if not (c["user"] and c["name"]):
        raise SqlError("db credentials not configured (ACR_REPORT_DB_* / MYSQL_*)")
    # --no-defaults: use ONLY the explicit connection params below. A cron/root
    # my.cnf ([client] user/password) would otherwise override MYSQL_PWD and make
    # the client authenticate as the wrong (privileged) identity.
    cmd = ["mysql", "--no-defaults", "-h", c["host"], "-u", c["user"],
           c["name"], "-N", "-e", query]
    env = dict(os.environ, MYSQL_PWD=c["passwd"])  # avoid -p on argv
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    except Exception as e:  # noqa: BLE001 - degrade to n/a
        raise SqlError(str(e))
    if res.returncode != 0:
        raise SqlError((res.stderr or "query failed").strip().split("\n")[-1])
    rows = []
    for line in res.stdout.strip().split("\n"):
        if line.strip():
            rows.append(tuple(line.split("\t")))
    return rows


def scalar(query, default=None):
    rows = run_sql(query)
    if not rows or not rows[0]:
        return default
    return rows[0][0]


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

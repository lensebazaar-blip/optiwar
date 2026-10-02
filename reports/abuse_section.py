#!/usr/bin/env python3
"""Abuse & attack signals — Daily Report section.

What a bot or an attacker did in the window, from three sources the server
already keeps:

* photo uploads (``ACTIVITY:ATTACHMENT`` lines from ``chat_gateway``): IPs the
  per-IP limit stopped, photos refused and why, photos shrunk and the bytes
  saved, photos KET refused;
* the nginx access log: 413 (body too large) and 429 (rate-limited)
  answers, probes for paths a shop never serves (``/.env``, ``wp-login``,
  ``/.git``, ...), and the busiest IPs;
* fail2ban: each jail's current and total bans.

Read-only; no database. Prints IP addresses, never request bodies.
"""
import datetime
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIDTH = 70
BANNER = "=" * WIDTH
WINDOW_HOURS = int(os.environ.get("ACR_REPORT_WINDOW_HOURS", "24"))
LOG_DIR = os.environ.get("OPTIWAR_LOG_DIR", "/var/log/optiwar")
DEBUG_LOG = os.path.join(LOG_DIR, "debug.log")
ACCESS_LOG = os.path.join(LOG_DIR, "access.log")
JAILS = ("sshd", "captcha")
SELF_IPS = frozenset(["127.0.0.1", "::1"] + [
    ip for ip in os.environ.get("OPTIWAR_SELF_IPS", "172.105.54.11").split(",") if ip])
TOP = 5
PROBE_WARN = 200
REFUSED_WARN = 20

GREEN, AMBER = "GREEN", "AMBER"

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_ATTACH = re.compile(
    r"ACTIVITY:ATTACHMENT event=(?P<event>\w+) code=(?P<code>\S+) ip=(?P<ip>\S+) "
    r"bytes_in=(?P<bin>\d+) bytes_out=(?P<bout>\d+)")
_ACCESS = re.compile(
    r'^\S+ (?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+)[^"]*" '
    r'(?P<status>\d{3}) ')
PROBE = re.compile(
    r"(/\.env|/\.git|wp-login|wp-admin|xmlrpc\.php|phpmyadmin|/cgi-bin/|\.\./|"
    r"/vendor/phpunit|/boaform|/actuator|/HNAP1|\.php(\?|$))", re.I)


def _files(base, now):
    yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    out = []
    for rotated in (base + "." + yesterday, base + "-" + now.strftime("%Y%m%d")):
        if os.path.exists(rotated):
            out.append(rotated)
    if os.path.exists(base):
        out.append(base)
    return out


def uploads(files=None, now=None):
    now = now or datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=WINDOW_HOURS)
    out = {"rate_limited": {}, "refused": {}, "shrunk": 0, "bytes_in": 0,
           "bytes_out": 0, "ket_refused": 0}
    for path in (files if files is not None else _files(DEBUG_LOG, now)):
        try:
            with open(path, "r", errors="replace") as fh:
                for line in fh:
                    m = _ATTACH.search(line)
                    if not m:
                        continue
                    ts = _TS.match(line)
                    if ts:
                        try:
                            if datetime.datetime.strptime(ts.group(1), "%Y-%m-%d %H:%M:%S") < cutoff:
                                continue
                        except ValueError:
                            pass
                    ev = m.group("event")
                    if ev == "rate_limited":
                        ip = m.group("ip")
                        out["rate_limited"][ip] = out["rate_limited"].get(ip, 0) + 1
                    elif ev == "refused":
                        code = m.group("code")
                        out["refused"][code] = out["refused"].get(code, 0) + 1
                    elif ev == "shrunk":
                        out["shrunk"] += 1
                        out["bytes_in"] += int(m.group("bin"))
                        out["bytes_out"] += int(m.group("bout"))
                    elif ev == "ket_refused":
                        out["ket_refused"] += 1
        except OSError:
            continue
    return out


def access(files=None, now=None):
    now = now or datetime.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    cutoff = now - datetime.timedelta(hours=WINDOW_HOURS)
    out = {"requests": 0, "413": 0, "429": 0, "probes": 0, "probe_ips": {},
           "ips": {}, "attach_posts": 0}
    for path in (files if files is not None else _files(ACCESS_LOG, now.replace(tzinfo=None))):
        try:
            with open(path, "r", errors="replace") as fh:
                for line in fh:
                    m = _ACCESS.match(line)
                    if not m:
                        continue
                    try:
                        when = datetime.datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S %z")
                    except ValueError:
                        continue
                    if when < cutoff:
                        continue
                    ip, status, p = m.group("ip"), m.group("status"), m.group("path")
                    out["requests"] += 1
                    if ip not in SELF_IPS:
                        out["ips"][ip] = out["ips"].get(ip, 0) + 1
                    if status in ("413", "429"):
                        out[status] += 1
                    if m.group("method") == "POST" and p.startswith("/api/chat/attachment"):
                        out["attach_posts"] += 1
                    if PROBE.search(p):
                        out["probes"] += 1
                        out["probe_ips"][ip] = out["probe_ips"].get(ip, 0) + 1
        except OSError:
            continue
    return out


def fail2ban(jails=JAILS, run=subprocess.run):
    out = {}
    for jail in jails:
        try:
            text = run(["fail2ban-client", "status", jail], capture_output=True,
                       text=True, timeout=10).stdout
        except Exception:  # noqa: BLE001
            out[jail] = None
            continue
        cur = re.search(r"Currently banned:\s*(\d+)", text or "")
        tot = re.search(r"Total banned:\s*(\d+)", text or "")
        out[jail] = (int(cur.group(1)), int(tot.group(1))) if cur and tot else None
    return out


def _top(counts):
    return ", ".join("%s x%d" % (k, v) for k, v in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]) or "none"


def findings(up, acc):
    from reports.report_severity import WARNING, INFO, Finding
    out = []
    if up["rate_limited"]:
        out.append(Finding(WARNING, "abuse", "%d IP(s) hit the photo-upload limit: %s"
                           % (len(up["rate_limited"]), _top(up["rate_limited"])), "abuse"))
    refused = sum(up["refused"].values())
    if refused >= REFUSED_WARN:
        out.append(Finding(WARNING, "abuse", "%d photo upload(s) refused" % refused, "abuse"))
    if up["ket_refused"]:
        out.append(Finding(WARNING, "abuse", "%d photo(s) refused by KET" % up["ket_refused"], "abuse"))
    if acc["probes"] >= PROBE_WARN:
        out.append(Finding(WARNING, "abuse", "%d attack-probe request(s); top: %s"
                           % (acc["probes"], _top(acc["probe_ips"])), "abuse"))
    if not out:
        out.append(Finding(INFO, "abuse", "no abuse above thresholds", "abuse"))
    return out


def build(up, acc, bans):
    L = []
    add = L.append
    warn = [f for f in findings(up, acc) if f.severity != "INFO"]
    add(BANNER)
    add("  ABUSE & ATTACK SIGNALS (last %dh)   STATUS: %s" % (WINDOW_HOURS, AMBER if warn else GREEN))
    add(BANNER)
    add("  Photo uploads (chat)")
    add("    POSTs to /api/chat/attachment     %d" % acc["attach_posts"])
    add("    IPs stopped by the per-IP limit   %d  (%s)" % (len(up["rate_limited"]), _top(up["rate_limited"])))
    add("    refused                           %d  (%s)" % (sum(up["refused"].values()), _top(up["refused"])))
    saved = up["bytes_in"] - up["bytes_out"]
    add("    shrunk before keeping/sending     %d  (%.1f MB in -> %.1f MB out, %.1f MB saved)" % (
        up["shrunk"], up["bytes_in"] / 1048576.0, up["bytes_out"] / 1048576.0, saved / 1048576.0))
    add("    refused by KET                    %d" % up["ket_refused"])
    add("  Web (nginx)")
    add("    requests                          %d" % acc["requests"])
    add("    413 body too large                %d" % acc["413"])
    add("    429 rate-limited                  %d" % acc["429"])
    add("    attack probes (.env, wp-login, .git, .php ...)  %d" % acc["probes"])
    add("      top probing IPs: %s" % _top(acc["probe_ips"]))
    add("    busiest outside IPs: %s" % _top(acc["ips"]))
    add("  fail2ban")
    for jail, v in bans.items():
        add("    %-10s %s" % (jail, "n/a" if v is None else "currently banned %d, total %d" % v))
    for f in warn:
        add("  [%s] %s" % (f.severity, f.message))
    add(BANNER)
    return "\n".join(L)


def main():
    up, acc, bans = uploads(), access(), fail2ban()
    try:
        from reports.report_severity import emit
        emit("abuse", findings(up, acc))
    except Exception:  # noqa: BLE001 - the section still stands on its own
        pass
    print(build(up, acc, bans))


if __name__ == "__main__":
    main()

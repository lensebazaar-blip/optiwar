"""Every site error, mailed to admin@optiwar.com (owner, 9 Nov 2026).

One door: a logging handler on the application logger. Anything logged at ERROR
or above becomes an alert, which covers unhandled exceptions (Flask logs them),
errors the code caught and logged, and ``dev_defects`` lines from the server and
from customers' browsers (static/js/ow-errors.js). A 5xx sent without any of
those is logged as a defect here, so it arrives the same way.

* The same error mails once, then again every ``WINDOW_MINUTES`` while it keeps
  happening, saying how many times it happened since the last mail. It keeps
  arriving until it is fixed.
* Each source has an hourly cap, so a flood (or someone posting junk browser
  reports) cannot bury the mailbox; the mail that hits the cap says so.
* Mail goes from a background thread, so an error page is never slowed by SMTP.
  If the database is the thing that is down, repeats are limited per process
  and the mail still goes.
* Addresses, phone numbers, prescription powers and API keys are masked. Form
  fields and query strings are never included.

Stdlib at import, so the deploy tool can read ``TABLES`` plainly.
"""
import datetime
import hashlib
import logging
import re
import threading
import traceback

ALERT_TO = "admin@optiwar.com"
WINDOW_MINUTES = 30
HOURLY_CAP = {"server": 60, "browser": 30}
MAX_THREADS = 8
TRACE_MAX = 6000
FLAG = "_site_error_reported"

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_error_alerts (
    signature VARCHAR(48) NOT NULL PRIMARY KEY,
    label VARCHAR(191) NOT NULL,
    first_seen DATETIME NOT NULL,
    last_seen DATETIME NOT NULL,
    last_sent_at DATETIME NULL,
    repeats INT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
TABLES = (("site_error_alerts", SCHEMA),)

_DEFECT = re.compile(r"ACTIVITY:DEV_DEFECT code=(\S+) origin=(\S+) where=(.*) page=(\S*)$")
_MASKS = ((re.compile(r"sk-[A-Za-z0-9_\-*]+"), "[key]"),
          (re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+"), "[email]"),
          (re.compile(r"(?<![\w.])\+?\d[\d \-]{8,}\d(?![\w.])"), "[phone]"),
          (re.compile(r"(?<![\w.])[+\-]\d{1,2}\.\d{2}(?!\d)"), "[power]"))

_local = threading.local()
_slots = threading.BoundedSemaphore(MAX_THREADS)
_memory = {}
_memory_lock = threading.Lock()
_schema_ready = False


def scrub(text):
    out = str(text or "")
    for pattern, mask in _MASKS:
        out = pattern.sub(mask, out)
    return out


def _request_facts():
    from flask import g, has_request_context, request, session  # noqa: PLC0415
    if not has_request_context():
        return {}
    setattr(g, FLAG, True)
    return {"site": request.host, "method": request.method, "path": request.path, "ip": request.remote_addr,
            "customer": session.get("user_id") or "guest", "browser": (request.user_agent.string or "")[:160]}


def event_from(record):
    """What one log record says went wrong, or None when it is not for this mailbox."""
    message = record.getMessage()
    defect = _DEFECT.search(message)
    if "ACTIVITY:" in message and not defect:
        return None                 # business events; prohibited uploads have their own alert
    if message.startswith("Content alert mail"):
        return None
    if defect:
        code, source, where, page = defect.groups()
    else:
        kind = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "LOGGED_ERROR"
        code, source, where, page = kind, "server", "%s:%s" % (record.module, record.lineno), ""
    source = "browser" if source == "browser" else "server"
    event = {"code": code, "source": source, "where": scrub(where), "message": scrub(message)[:1000], "page": page,
             "signature": hashlib.sha1(("%s|%s|%s" % (source, code, where)).encode("utf-8", "replace")).hexdigest(),
             "when": datetime.datetime.now(), "trace": ""}
    if record.exc_info and record.exc_info[0]:
        event["trace"] = scrub("".join(traceback.format_exception(*record.exc_info)))[-TRACE_MAX:]
    event.update(_request_facts())
    return event


def mail_text(event, repeats):
    where = "%s%s" % (event.get("site", "optiwar"), event.get("page") or event.get("path") or "")
    lines = [("What", event["code"]), ("Seen in", "the customer's browser" if event["source"] == "browser" else "the server"),
             ("Where in the code", event["where"]), ("Page", where), ("Method", event.get("method")),
             ("When", event["when"].strftime("%A, %d %B %Y %H:%M:%S")),
             ("Times since the last mail", repeats or 1), ("IP address", event.get("ip")),
             ("Customer id", event.get("customer")), ("Device", event.get("browser"))]
    body = "A customer or the site met an error. Please fix it and confirm.\n\n"
    body += "".join("%s: %s\n" % (k, v) for k, v in lines if v not in (None, ""))
    body += "\nLogged message:\n%s\n" % event["message"]
    if event["trace"]:
        body += "\nTraceback:\n%s\n" % event["trace"]
    body += "\nThis mail repeats every %d minutes for as long as the error keeps happening.\n" % WINDOW_MINUTES
    return "[Optiwar ERROR] %s: %s" % (where[:90], event["code"]), body


def _bump(cursor, key, label, now):
    cursor.execute("INSERT INTO site_error_alerts (signature, label, first_seen, last_seen, repeats) "
                   "VALUES (%s, %s, %s, %s, 1) ON DUPLICATE KEY UPDATE last_seen = VALUES(last_seen), "
                   "repeats = repeats + 1", (key, label[:191], now, now))
    cursor.execute("SELECT repeats FROM site_error_alerts WHERE signature = %s", (key,))
    row = cursor.fetchone() or {}
    return int(row.get("repeats") or 1)


def claim_in_db(db, cursor, event, now):
    """``(decision, repeats)``: decision is "send", "cap" (say the cap was hit) or "skip"."""
    repeats = _bump(cursor, event["signature"], "%s %s" % (event["code"], event["where"]), now)
    cursor.execute("UPDATE site_error_alerts SET last_sent_at = %s, repeats = 0 WHERE signature = %s "
                   "AND (last_sent_at IS NULL OR last_sent_at <= %s)",
                   (now, event["signature"], now - datetime.timedelta(minutes=WINDOW_MINUTES)))
    won = cursor.rowcount == 1
    decision = "skip"
    if won:
        cap = HOURLY_CAP[event["source"]]
        sent = _bump(cursor, "cap_%s_%s" % (event["source"], now.strftime("%Y%m%d%H")), "hourly cap", now)
        decision = "send" if sent <= cap else ("cap" if sent == cap + 1 else "skip")
        cursor.execute("DELETE FROM site_error_alerts WHERE last_seen < %s", (now - datetime.timedelta(days=30),))
    db.commit()
    return decision, repeats


def claim_in_memory(signature, now):
    """The database is unavailable: limit repeats within this process."""
    with _memory_lock:
        last = _memory.get(signature)
        if last and now - last < datetime.timedelta(minutes=WINDOW_MINUTES):
            return "skip", 0
        if len(_memory) > 500:
            _memory.clear()
        _memory[signature] = now
    return "send", 0


def _claim(event):
    global _schema_ready
    now = datetime.datetime.now().replace(microsecond=0)
    try:
        from .db import get_db  # noqa: PLC0415
        db = get_db()
        cursor = db.cursor()
        if not _schema_ready:
            cursor.execute(SCHEMA)
            _schema_ready = True
        return claim_in_db(db, cursor, event, now)
    except Exception:  # noqa: BLE001 - the alert matters most when the database is down
        return claim_in_memory(event["signature"], now)


def deliver(app, event):
    """Decide and mail. Anything logged from here is not reported again."""
    _local.delivering = True
    try:
        with app.app_context():
            from .notice_mail import send_content_alert  # noqa: PLC0415
            decision, repeats = _claim(event)
            if decision == "skip":
                return
            subject, body = mail_text(event, repeats)
            if decision == "cap":
                subject = "[Optiwar ERROR] More than %d %s errors this hour" % (HOURLY_CAP[event["source"]], event["source"])
                body = "Further %s errors will not be mailed until the next hour. Latest:\n\n%s" % (event["source"], body)
            send_content_alert(ALERT_TO, subject, body)
    except Exception:  # noqa: BLE001 - reporting must never become the failure
        pass
    finally:
        _local.delivering = False
        _slots.release()


def _start(app, event):
    if not _slots.acquire(blocking=False):
        return
    try:
        threading.Thread(target=deliver, args=(app, event), daemon=True).start()
    except Exception:  # noqa: BLE001
        _slots.release()


class _AlertHandler(logging.Handler):

    def __init__(self, app):
        super().__init__(level=logging.ERROR)
        self.app = app

    def emit(self, record):
        try:
            if record.levelno < logging.ERROR:  # log_hygiene resets every app.logger handler's level
                return
            if getattr(_local, "delivering", False) or not self.app.config.get("ERROR_ALERTS_ENABLED", True):
                return
            if self.app.testing and not self.app.config.get("ERROR_ALERTS_IN_TESTS"):
                return
            event = event_from(record)
            if event:
                _start(self.app, event)
        except Exception:  # noqa: BLE001 - never let reporting break the request
            pass


def init_error_alerts(app):
    app.logger.addHandler(_AlertHandler(app))

    @app.after_request
    def report_error_response(response):
        from flask import g, request  # noqa: PLC0415
        if response.status_code >= 500 and not g.get(FLAG):
            from . import dev_defects  # noqa: PLC0415
            dev_defects.record("HTTP_%d" % response.status_code, where=request.endpoint or "no_route")
        return response

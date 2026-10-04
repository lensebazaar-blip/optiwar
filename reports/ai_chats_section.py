#!/usr/bin/env python3
"""AI chats — Daily Report section and transcript attachment.

Every chat with a message in the window, written out in full as one HTML file
(``ai_chats_<date>.html`` next to the daily report) that the mailer attaches,
plus a short summary in the report body. The owner reads these to audit and
learn how the assistant answers, so the transcript is shown as written:
customer names, emails and phones are not masked. The file is created 0600.

Read-only DB user (chat_sessions, chat_messages). Text columns are selected as
HEX so a message's newlines and tabs survive the mysql client's batch output.
"""
import datetime
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from reports.report_db import SqlError, run_sql
except ImportError:  # pragma: no cover - flat import when run from reports/
    from report_db import SqlError, run_sql

WIDTH = 70
BANNER = "=" * WIDTH
WINDOW_HOURS = int(os.environ.get("ACR_REPORT_WINDOW_HOURS", "24"))
REPORT_DIR = os.environ.get("OPTIWAR_REPORT_DIR", "/root/reports")
MAX_SESSIONS = 500

SPEAKER = {"customer": "Customer", "ai": "AI", "human": "Agent", "system": "System"}


def file_name(day):
    return "ai_chats_%s.html" % day


def _text(hexed):
    if not hexed or hexed == "NULL":
        return ""
    return bytes.fromhex(hexed).decode("utf-8", "replace")


def _in_window(hours):
    return ("SELECT DISTINCT session_id FROM chat_messages "
            "WHERE created_at >= NOW() - INTERVAL %d HOUR" % int(hours))


def collect(sql=run_sql, hours=WINDOW_HOURS):
    """(sessions, errors). Each session carries its whole transcript, so a chat
    that began before the window is still read from its first message."""
    errors = []
    window = _in_window(hours)
    try:
        heads = sql(
            "SELECT session_id, IFNULL(customer_id,''), HEX(IFNULL(contact_name,'')), "
            "HEX(IFNULL(contact_email,'')), IFNULL(status,''), IFNULL(ket_ticket_ref,''), "
            "HEX(IFNULL(current_page_url,'')) FROM chat_sessions "
            "WHERE session_id IN (%s)" % window)
    except SqlError as exc:
        heads = []
        errors.append("chat_sessions: %s" % exc)
    try:
        rows = sql(
            "SELECT session_id, source, HEX(IFNULL(content,'')), HEX(IFNULL(metadata,'')), "
            "created_at FROM chat_messages WHERE session_id IN (%s) "
            "ORDER BY session_id, id" % window)
    except SqlError as exc:
        return [], errors + ["chat_messages: %s" % exc]
    info = {}
    for h in heads:
        info[h[0]] = {"customer_id": h[1], "name": _text(h[2]), "email": _text(h[3]),
                      "status": h[4], "ket_ref": h[5], "page": _text(h[6])}
    sessions = {}
    for sid, source, content, meta, at in rows:
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = dict(info.get(sid) or {}, session_id=sid, messages=[])
        s["messages"].append({"source": source, "text": _text(content),
                              "meta": _text(meta), "at": at})
    ordered = sorted(sessions.values(), key=lambda s: s["messages"][0]["at"])
    if len(ordered) > MAX_SESSIONS:
        errors.append("only the first %d of %d chats are in the file" % (MAX_SESSIONS, len(ordered)))
        ordered = ordered[:MAX_SESSIONS]
    return ordered, errors


def summary(sessions):
    msgs = [m for s in sessions for m in s["messages"]]
    return {"chats": len(sessions), "messages": len(msgs),
            "customer_messages": sum(m["source"] == "customer" for m in msgs),
            "signed_in": sum(bool(s.get("customer_id")) for s in sessions),
            "escalated": sum(bool(s.get("ket_ref")) for s in sessions),
            "agent_messages": sum(m["source"] == "human" for m in msgs)}


def _meta_line(raw):
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict) and set(data) == {"actions"} and not data["actions"]:
        return ""
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def render_html(sessions, day, hours=WINDOW_HOURS, errors=()):
    e = html.escape
    s = summary(sessions)
    out = ["<!doctype html><html><head><meta charset=\"utf-8\">",
           "<title>Optiwar AI chats %s</title><style>" % e(day),
           "body{font-family:system-ui,Arial,sans-serif;font-size:14px;margin:16px;color:#111}",
           ".chat{border:1px solid #ccc;border-radius:6px;margin:0 0 18px;padding:10px}",
           ".head{font-size:13px;color:#333;margin-bottom:8px}",
           ".m{margin:6px 0;padding:6px 8px;border-radius:6px;white-space:pre-wrap}",
           ".customer{background:#eef4ff}.ai{background:#f3f3f3}.human{background:#fff4e0}",
           ".system{background:#fafafa;color:#666;font-size:12px}",
           ".who{font-weight:bold;font-size:12px}.t{color:#777;font-size:11px}",
           ".meta{color:#555;font-size:11px;font-family:monospace}</style></head><body>",
           "<h2>Optiwar AI chats — %s</h2>" % e(day),
           "<p>Chats with a message in the last %d hours: <b>%d</b>, messages %d "
           "(customer %d), signed in %d, escalated to KET %d. Full transcripts, as written.</p>"
           % (hours, s["chats"], s["messages"], s["customer_messages"], s["signed_in"],
              s["escalated"])]
    for err in errors:
        out.append("<p style=\"color:#b00\">%s</p>" % e(err))
    for i, c in enumerate(sessions, 1):
        who = " · ".join(x for x in (
            c.get("name"), c.get("email"),
            "customer #%s" % c["customer_id"] if c.get("customer_id") else "guest") if x)
        bits = [who, "status %s" % c["status"] if c.get("status") else "",
                "KET %s" % c["ket_ref"] if c.get("ket_ref") else "",
                "page %s" % c["page"] if c.get("page") else "", "session %s" % c["session_id"]]
        out.append("<div class=\"chat\"><div class=\"head\"><b>%d. %s</b><br>%s</div>"
                   % (i, e(c["messages"][0]["at"]), e(" · ".join(b for b in bits if b))))
        for m in c["messages"]:
            src = m["source"] if m["source"] in SPEAKER else "system"
            meta = _meta_line(m["meta"])
            out.append("<div class=\"m %s\"><span class=\"who\">%s</span> <span class=\"t\">%s</span>\n%s%s</div>"
                       % (src, SPEAKER[src], e(m["at"]), e(m["text"]),
                          "\n<span class=\"meta\">%s</span>" % e(meta) if meta else ""))
        out.append("</div>")
    out.append("</body></html>")
    return "\n".join(out)


def write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def build(sessions, name, errors=(), hours=WINDOW_HOURS):
    s = summary(sessions)
    lines = [BANNER, "AI CHATS (last %dh) — full transcripts attached" % hours, BANNER,
             "  Chats                         %d" % s["chats"],
             "  Messages (customer / all)     %d / %d" % (s["customer_messages"], s["messages"]),
             "  Signed-in customers           %d" % s["signed_in"],
             "  Escalated to KET              %d" % s["escalated"],
             "  Agent replies                 %d" % s["agent_messages"],
             "  Attachment                    %s" % (name if s["chats"] else "none (no chats)")]
    for err in errors:
        lines.append("  ! %s" % err)
    lines.append(BANNER)
    return "\n".join(lines)


def main(day=None):
    day = day or datetime.date.today().isoformat()
    sessions, errors = collect()
    name = file_name(day)
    path = os.path.join(REPORT_DIR, name)
    if sessions:
        try:
            write(path, render_html(sessions, day, errors=errors))
        except OSError as exc:
            errors.append("could not write %s: %s" % (name, exc))
    elif os.path.exists(path):
        os.remove(path)
    print(build(sessions, name, errors))


if __name__ == "__main__":
    main()

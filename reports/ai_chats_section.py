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
import re
import sys
from urllib.parse import urlparse

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
    that began before the window is still read from its first message, plus
    the canonical events, actions, attributed orders and account phone that
    explain it."""
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
            "created_at, IFNULL(status,'') FROM chat_messages WHERE session_id IN (%s) "
            "ORDER BY session_id, id" % window)
    except SqlError as exc:
        return [], errors + ["chat_messages: %s" % exc]
    info = {}
    for h in heads:
        info[h[0]] = {"customer_id": h[1], "name": _text(h[2]), "email": _text(h[3]),
                      "status": h[4], "ket_ref": h[5], "page": _text(h[6])}
    sessions = {}
    for r in rows:
        sid, source, content, meta, at = r[:5]
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = dict(info.get(sid) or {}, session_id=sid, messages=[],
                                     events=[], actions=[], orders=[])
        s["messages"].append({"source": source, "text": _text(content),
                              "meta": _text(meta), "at": at,
                              "status": r[5] if len(r) > 5 else ""})
    ordered = sorted(sessions.values(), key=lambda s: s["messages"][0]["at"])
    if len(ordered) > MAX_SESSIONS:
        errors.append("only the first %d of %d chats are in the file" % (MAX_SESSIONS, len(ordered)))
        ordered = ordered[:MAX_SESSIONS]
    _attach_ledgers(sql, window, sessions, errors)
    _attach_phones(sql, ordered)
    for s in ordered:
        s["kind"], s["kind_basis"] = classify(s)
    return ordered, errors


def _attach_ledgers(sql, window, sessions, errors):
    try:
        for r in sql(
                "SELECT session_id, event_type, created_at, IFNULL(action_id,''), "
                "IFNULL(action_type,''), IFNULL(success,''), IFNULL(failure_code,''), "
                "IFNULL(journey_stage,''), IFNULL(provider,''), IFNULL(model,''), "
                "IFNULL(duration_ms,''), HEX(IFNULL(payload,'')) FROM ai_events "
                "WHERE session_id COLLATE utf8mb4_general_ci IN (%s) "
                "ORDER BY session_id, created_at" % window):
            s = sessions.get(r[0])
            if s is not None:
                s["events"].append({
                    "type": r[1], "at": r[2], "action_id": r[3], "action_type": r[4],
                    "success": r[5], "failure": r[6], "stage": r[7], "provider": r[8],
                    "model": r[9], "ms": r[10], "payload": _json(_text(r[11]))})
    except SqlError as exc:
        errors.append("ai_events: %s" % exc)
    try:
        for r in sql(
                "SELECT session_id, action_id, action_type, HEX(IFNULL(target,'')), status, "
                "IFNULL(result_code,''), created_at, IFNULL(resolved_at,''), "
                "IFNULL(expires_at,'') FROM ai_actions "
                "WHERE session_id COLLATE utf8mb4_general_ci IN (%s) "
                "ORDER BY session_id, created_at" % window):
            s = sessions.get(r[0])
            if s is not None:
                s["actions"].append({"id": r[1], "type": r[2], "target": _text(r[3]),
                                     "status": r[4], "result": r[5], "created": r[6],
                                     "resolved": r[7], "expires": r[8]})
    except SqlError as exc:
        errors.append("ai_actions: %s" % exc)
    try:
        for r in sql(
                "SELECT session_id, order_id, attribution_type FROM ai_session_commerce "
                "WHERE session_id COLLATE utf8mb4_general_ci IN (%s)" % window):
            s = sessions.get(r[0])
            if s is not None:
                s["orders"].append({"order_id": r[1], "attribution": r[2]})
    except SqlError as exc:
        errors.append("ai_session_commerce: %s" % exc)


def _attach_phones(sql, sessions):
    """The phone on a signed-in customer's account (ACCOUNT_VERIFIED). The
    report user may only read customer_id and customer_phone of customers;
    without that grant the phone says so instead of guessing."""
    ids = sorted({s["customer_id"] for s in sessions
                  if str(s.get("customer_id") or "").isdigit()})
    if not ids:
        return
    try:
        rows = sql("SELECT customer_id, HEX(IFNULL(customer_phone,'')) FROM customers "
                   "WHERE customer_id IN (%s)" % ",".join(ids))
        phones = {r[0]: _text(r[1]).strip() for r in rows}
        state = None
    except SqlError:
        phones, state = {}, "not readable (report user has no grant on customers.customer_phone)"
    for s in sessions:
        if s.get("customer_id") in ids:
            s["account_phone"] = phones.get(s["customer_id"]) or ""
            s["account_phone_state"] = state


def _json(raw):
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ── Classification ──

INTERNAL_DOMAINS = ("optiwar.com", "optiwar.in", "lensbazaar.com", "ket.ltd")
TEST_EMAILS = tuple(e.strip().lower() for e in os.environ.get(
    "OPTIWAR_CHAT_TEST_EMAILS", "").split(",") if e.strip())

REAL, TEST, CANARY, WIDGET = "REAL CUSTOMER", "TEST", "CANARY", "WIDGET OPENED"


def _customer_turns(s):
    return [m for m in s["messages"] if m["source"] == "customer"]


def classify(s):
    """(kind, basis). WIDGET OPENED: no customer message. CANARY: a browser
    enrolled in the ACR canary (recorded on SESSION_STARTED), or, for a chat
    started before that was recorded, a guest chat with an ACR action while
    ACR is canary-only. TEST: an internal or listed test email."""
    if not _customer_turns(s):
        return WIDGET, "no customer message"
    started = next((e for e in s.get("events", ()) if e["type"] == "SESSION_STARTED"), None)
    flag = (started or {}).get("payload", {}).get("acr_canary")
    email = (s.get("email") or "").lower()
    if flag is True:
        return CANARY, "ACR canary browser"
    if email and (email in TEST_EMAILS or email.rsplit("@", 1)[-1] in INTERNAL_DOMAINS
                  or email.startswith("deploy-canary+")):
        return TEST, "internal/test email"
    if flag is None and s.get("actions"):
        if s.get("customer_id"):
            return TEST, "inferred: ACR action on a signed-in chat (ACR allow-list only)"
        return CANARY, "inferred: ACR action on a guest chat (ACR is canary-only)"
    return REAL, ""


# ── Per-turn trace ──

BADGE = {"DETERMINISTIC": "RULE", "MODEL": "AI", "MODEL + TOOL": "AI + TOOL",
         "TOOL-ONLY": "TOOL", "FALLBACK": "FALLBACK", "SUPPORT/KET": "KET"}


def _turn_events(s, idx):
    """Canonical events belonging to AI message ``idx``: after the previous AI
    reply and from the customer message that triggered this one (to the
    second; used only for chats without a stored trace)."""
    msgs = s["messages"]
    at = msgs[idx]["at"]
    prev_ai = next((m["at"] for m in reversed(msgs[:idx]) if m["source"] == "ai"), "")
    cust = next((m["at"] for m in reversed(msgs[:idx]) if m["source"] == "customer"), None)
    if cust is None:
        return []
    return [e for e in s.get("events", ())
            if cust <= e["at"] <= at and (not prev_ai or e["at"] > prev_ai or prev_ai < cust)]


def trace_for(s, idx):
    """The stored trace of AI message ``idx``, or one rebuilt from events."""
    m = s["messages"][idx]
    meta = _json(m["meta"])
    if isinstance(meta.get("trace"), dict):
        return dict(meta["trace"], basis="stored")
    if not any(x["source"] == "customer" for x in s["messages"][:idx]):
        return {"source": "DETERMINISTIC", "trigger": "session_start", "tools": [],
                "model_calls": [], "basis": "greeting"}
    evs = _turn_events(s, idx)
    model_evs = [e for e in evs if e["type"] == "MODEL_CALL"]
    calls = [{"provider": e["provider"], "model": e["model"],
              "ok": e["success"] == "1", "ms": e["ms"],
              "in": e["payload"].get("input_tokens"),
              "out": e["payload"].get("output_tokens"),
              # Before tool rounds were recorded as such, a round that asked
              # for a tool was stored as an empty, failed reply.
              "tool_call": (e["payload"].get("tool_call") or
                            (e["success"] != "1" and k < len(model_evs) - 1))}
             for k, e in enumerate(model_evs)]
    tools = []
    for e in evs:
        if e["type"] == "RECOMMENDATION_GENERATED":
            p = e["payload"]
            tools.append({"tool": "search_products", "args": p.get("filters") or {},
                          "returned": p.get("result_count"), "skus": p.get("skus") or []})
        elif e["type"] in ("TOOL_USED", "PRESCRIPTION_LOOKUP") and e["action_type"]:
            tools.append({"tool": e["action_type"]})
    understood = next((e["payload"] for e in evs if e["type"] == "TURN_UNDERSTOOD"), {})
    escalated = any(e["type"] in ("KET_TICKET_CREATED", "TICKET_CREATED") for e in evs)
    if m.get("status") == "failed":
        source = "FALLBACK"
    elif escalated:
        source = "SUPPORT/KET"
    elif calls:
        source = "MODEL + TOOL" if tools else "MODEL"
    else:
        source = "DETERMINISTIC"
    action = next(({"id": e["action_id"], "type": e["action_type"] or "NAVIGATE",
                    "state": "CONFIRMED" if e["type"] == "ACTION_CONFIRMED" else "OFFERED"}
                   for e in evs if e["type"] in ("NAVIGATION_OFFERED", "ACTION_CONFIRMED")
                   and e["action_id"]), None)
    if action is None and "navigate" in (meta.get("actions") or []):
        action = {"type": "NAVIGATE", "state": "AUTO_NAVIGATE", "recorded": False}
    return {"source": source, "trigger": "customer_message",
            "language": understood.get("detected_language"),
            "intent": understood.get("turn_intent") or understood.get("intent"),
            "confidence": understood.get("intent_confidence"),
            "tools": tools, "model_calls": calls, "action": action,
            "basis": "rebuilt from events"}


def _fmt_args(args):
    return ", ".join("%s=%s" % (k, v) for k, v in sorted((args or {}).items())) or "none"


def trace_lines(t):
    """Readable, safe trace lines (no prompt, no reasoning, no customer data)."""
    out = ["Source: %s · Trigger: %s · %s" % (BADGE.get(t.get("source"), t.get("source")),
                                             t.get("trigger") or "-", t.get("basis") or "")]
    bits = []
    if t.get("language"):
        bits.append("Language %s" % t["language"])
    if t.get("intent"):
        bits.append("Intent %s" % t["intent"])
    if t.get("confidence") not in (None, ""):
        bits.append("Confidence %s" % t["confidence"])
    if bits:
        out.append(" · ".join(bits))
    for tool in t.get("tools") or ():
        if tool.get("tool") == "search_products":
            line = "Tool search_products: filters %s" % _fmt_args(tool.get("args"))
            if tool.get("matched") is not None:
                line += " · matched %s" % tool["matched"]
            line += " · returned %s" % tool.get("returned")
            if tool.get("ranking"):
                line += " · ranking %s" % tool["ranking"]
            if tool.get("catalog_at"):
                line += " · catalogue of %s" % tool["catalog_at"]
            out.append(line)
            if tool.get("skus"):
                out.append("  SKUs: %s" % ", ".join(tool["skus"]))
        else:
            extra = ""
            if "found" in tool:
                extra = " (found)" if tool["found"] else " (nothing on file)"
            out.append("Tool %s%s" % (tool.get("tool"), extra))
    calls = t.get("model_calls") or []
    if calls:
        tin = sum(int(c.get("in") or 0) for c in calls)
        tout = sum(int(c.get("out") or 0) for c in calls)
        ms = sum(int(c.get("ms") or 0) for c in calls)
        names = sorted({"%s/%s" % (c.get("provider") or "-", c.get("model") or "-")
                        for c in calls})
        bad = [c for c in calls if c.get("ok") is False and not c.get("tool_call")]
        out.append("Model: %s · %d call(s)%s · tokens in %d / out %d · %d ms · "
                   "cost basis not declared" % (", ".join(names), len(calls),
                                                " (%d failed)" % len(bad) if bad else "",
                                                tin, tout, ms))
    else:
        out.append("Model call: NONE · provider cost 0")
    page = t.get("page") or {}
    if page:
        out.append("Page: %s on %s · page facts given to the model: %s"
                   % (page.get("kind") or "-", page.get("site") or "-",
                      ", ".join(page.get("facts_used") or []) or "none"))
    a = t.get("action")
    if a:
        line = "Action %s %s" % (a.get("type"), a.get("state"))
        if a.get("id"):
            line += " · %s" % a["id"]
        if a.get("bound_to_offer"):
            line += " · bound to the earlier offer"
        if a.get("recorded") is False:
            line += " · widget navigates, no action record (ACR off for this chat)"
        if a.get("target"):
            line += " · %s" % a["target"]
        out.append(line)
    return out


# ── Per-chat blocks ──

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")


def identity(s):
    """Identity lines. Account values are ACCOUNT_VERIFIED; anything a guest
    typed is CHAT_PROVIDED and never treated as verified."""
    site = urlparse(s.get("page") or "").netloc or "-"
    if s.get("customer_id"):
        phone = s.get("account_phone_state") or s.get("account_phone") or "not on the account"
        rows = [("Customer", s.get("name") or "-"), ("Customer ID", s["customer_id"]),
                ("Email", s.get("email") or "-"),
                ("Phone", phone + (" (ACCOUNT_VERIFIED)" if s.get("account_phone") else "")),
                ("Signed in", "YES")]
    else:
        rows = [("Customer", "Guest"),
                ("Email", "%s (CHAT_PROVIDED)" % s["email"] if s.get("email") else "not known"),
                ("Phone", "not known"), ("Signed in", "NO")]
    typed = []
    for m in s["messages"]:
        if m["source"] == "customer":
            typed += _EMAIL_RE.findall(m["text"]) + _PHONE_RE.findall(m["text"])
    known = {(s.get("email") or "").lower(), (s.get("account_phone") or "")}
    typed = [x for x in dict.fromkeys(typed) if x.lower() not in known]
    if typed:
        rows.append(("Provided during chat", ", ".join(typed) + " (CHAT_PROVIDED)"))
    rows += [("Site", site), ("Session", s["session_id"]), ("Current page", s.get("page") or "-")]
    return rows


STAGE_ORDER = ("LANDING", "LISTING", "PRODUCT", "CART", "CHECKOUT", "PURCHASE")


def outcome(s):
    ev = s.get("events", ())
    stages = {e["stage"] for e in ev if e["type"] == "JOURNEY_STAGE" and e["stage"]}
    deepest = next((x for x in reversed(STAGE_ORDER) if x in stages), None)
    recs = sum(1 for e in ev if e["type"] == "RECOMMENDATION_GENERATED" and e["success"] == "1")
    executed = sum(1 for e in ev if e["type"] == "ACTION_EXECUTED")
    end = next((e["payload"].get("outcome") for e in reversed(ev)
                if e["type"] == "SESSION_OUTCOME"), None)
    rows = [("Recommendations shown", str(recs)),
            ("Actions executed", str(executed)),
            ("Furthest page reached", deepest or "no page recorded after the chat"),
            ("Orders attributed", ", ".join("%s (%s)" % (o["order_id"], o["attribution"])
                                            for o in s.get("orders", ())) or "none"),
            ("Session outcome", end or "not swept yet")]
    return rows, deepest, end


def support(s):
    ev = s.get("events", ())
    cls = next((e["payload"] for e in reversed(ev) if e["type"] == "TICKET_CLASSIFIED"), None)
    offered = any(e["type"] == "ESCALATION_OFFERED" for e in ev)
    if not (s.get("ket_ref") or cls or offered):
        return [("Ticket", "none")]
    rows = [("Ticket", s.get("ket_ref") or "not created"),
            ("Escalation offered", "YES" if offered else "NO")]
    if cls:
        for k in ("ticket_reason", "final_action", "model_reason", "data_available",
                  "tool_available"):
            if cls.get(k) not in (None, ""):
                rows.append((k.replace("_", " ").capitalize(), str(cls[k])))
    return rows


def action_rows(s):
    ev = {}
    for e in s.get("events", ()):
        if e["action_id"]:
            ev.setdefault(e["action_id"], {}).setdefault(e["type"], e["at"])
    out = []
    for a in s.get("actions", ()):
        when = ev.get(a["id"], {})
        out.append("%s %s → %s · %s · offered %s · confirmed %s · executed %s · expires %s%s"
                   % (a["type"], a["id"], a["target"] or "-", a["status"], a["created"],
                      when.get("ACTION_CONFIRMED", "-"), when.get("ACTION_EXECUTED", "-"),
                      a["expires"] or "-", " · %s" % a["result"] if a["result"] else ""))
    return out


def _ai_indexes(s):
    return [i for i, m in enumerate(s["messages"]) if m["source"] == "ai"]


def management(s, traces):
    turns = _customer_turns(s)
    need = turns[0]["text"].strip().replace("\n", " ") if turns else "-"
    intents = [t.get("intent") for t in traces if t.get("intent")]
    sources = {}
    for t in traces:
        if t.get("basis") != "greeting":
            b = BADGE.get(t.get("source"), t.get("source"))
            sources[b] = sources.get(b, 0) + 1
    _, deepest, end = outcome(s)
    acts = [a for a in s.get("actions", ())]
    autos = [t for t in traces if (t.get("action") or {}).get("state") == "AUTO_NAVIGATE"]
    if acts:
        action = "; ".join("%s %s" % (a["type"], a["status"]) for a in acts)
    elif autos:
        action = "%d auto-navigation(s), not recorded" % len(autos)
    else:
        action = "none"
    who = s.get("name") or "Guest" if s.get("customer_id") else "Guest"
    return [("Customer", "%s · %s" % (who, s.get("kind"))),
            ("Need", need[:160] + ("…" if len(need) > 160 else "")),
            ("AI understood", ", ".join(dict.fromkeys(intents)) or "-"),
            ("How answered", ", ".join("%s ×%d" % kv for kv in sources.items()) or "-"),
            ("Action", action),
            ("Commercial outcome", ("order attributed" if s.get("orders") else
                                    "reached %s" % deepest if deepest else "none recorded")),
            ("Support", s.get("ket_ref") or "none"),
            ("Status", "%s%s" % (s.get("status") or "-", " · %s" % end if end else ""))]


def summary(sessions):
    msgs = [m for s in sessions for m in s["messages"]]
    return {"chats": len(sessions), "messages": len(msgs),
            "customer_messages": sum(m["source"] == "customer" for m in msgs),
            "signed_in": sum(bool(s.get("customer_id")) for s in sessions),
            "escalated": sum(bool(s.get("ket_ref")) for s in sessions),
            "agent_messages": sum(m["source"] == "human" for m in msgs)}


def headline(sessions):
    """The AI CHAT SUMMARY counts. Counts only, so it is safe for the body."""
    kinds = [s.get("kind") or classify(s)[0] for s in sessions]
    traces = [trace_for(s, i) for s in sessions for i in _ai_indexes(s)]
    traces = [t for t in traces if t.get("basis") != "greeting"]
    src = lambda *names: sum(t.get("source") in names for t in traces)  # noqa: E731
    with_input = [s for s, k in zip(sessions, kinds) if k != WIDGET]
    escalated = sum(bool(s.get("ket_ref")) for s in with_input)
    return {"widget_sessions": len(sessions),
            "with_input": len(with_input),
            "authenticated": sum(bool(s.get("customer_id")) for s in with_input),
            "guests": sum(not s.get("customer_id") for s in with_input),
            "test_canary": sum(k in (TEST, CANARY) for k in kinds),
            "real": kinds.count(REAL),
            "ai_resolved": sum(1 for s in with_input if not s.get("ket_ref") and any(
                e["type"] == "SESSION_OUTCOME" and e["payload"].get("outcome") == "ANSWERED"
                for e in s.get("events", ()))),
            "escalated": escalated,
            "model_replies": src("MODEL", "MODEL + TOOL"),
            "deterministic_replies": src("DETERMINISTIC"),
            "tool_replies": src("MODEL + TOOL", "TOOL-ONLY"),
            "fallback_replies": src("FALLBACK"),
            "actions_executed": sum(1 for s in sessions for e in s.get("events", ())
                                    if e["type"] == "ACTION_EXECUTED")}


HEADLINE_ROWS = (("widget_sessions", "Widget sessions"),
                 ("with_input", "Sessions with customer input"),
                 ("authenticated", "Authenticated customers"),
                 ("guests", "Guest customers"),
                 ("test_canary", "Test/canary sessions"),
                 ("real", "Real customer sessions"),
                 ("ai_resolved", "AI-resolved (swept ANSWERED)"),
                 ("escalated", "Escalated to KET"),
                 ("model_replies", "Model-generated replies"),
                 ("deterministic_replies", "Deterministic (excl. greeting)"),
                 ("tool_replies", "Tool-assisted replies"),
                 ("fallback_replies", "Fallback replies"),
                 ("actions_executed", "ACR actions executed"))


def _meta_line(raw):
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k != "trace"}
        if not data or (set(data) == {"actions"} and not data["actions"]):
            return ""
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _table(e, rows):
    return ("<table class=\"kv\">%s</table>"
            % "".join("<tr><th>%s</th><td>%s</td></tr>" % (e(k), e(v)) for k, v in rows))


def render_html(sessions, day, hours=WINDOW_HOURS, errors=()):
    e = html.escape
    s = summary(sessions)
    h = headline(sessions)
    out = ["<!doctype html><html><head><meta charset=\"utf-8\">",
           "<title>Optiwar AI chats %s</title><style>" % e(day),
           "body{font-family:system-ui,Arial,sans-serif;font-size:14px;margin:16px;color:#111}",
           ".chat{border:1px solid #ccc;border-radius:6px;margin:0 0 18px;padding:10px}",
           ".chat.widget{opacity:.7}",
           ".head{font-size:13px;color:#333;margin-bottom:8px}",
           ".m{margin:6px 0;padding:6px 8px;border-radius:6px;white-space:pre-wrap}",
           ".customer{background:#eef4ff}.ai{background:#f3f3f3}.human{background:#fff4e0}",
           ".system{background:#fafafa;color:#666;font-size:12px}",
           ".who{font-weight:bold;font-size:12px}.t{color:#777;font-size:11px}",
           ".meta{color:#555;font-size:11px;font-family:monospace}",
           ".trace{color:#333;font-size:11px;font-family:monospace;border-left:3px solid #bbb;"
           "padding-left:6px;margin-top:4px}",
           ".badge{display:inline-block;font-size:11px;font-weight:bold;padding:1px 6px;"
           "border-radius:8px;background:#ddd;margin-left:4px}",
           ".b-REAL{background:#cfe9d4}.b-TEST,.b-CANARY{background:#f3e3b5}",
           ".b-WIDGET{background:#e5e5e5}",
           "h4{margin:10px 0 4px;font-size:12px;color:#555;letter-spacing:.04em}",
           ".kv{border-collapse:collapse;font-size:12px}.kv th{text-align:left;color:#555;"
           "font-weight:normal;padding:1px 10px 1px 0;vertical-align:top}.kv td{padding:1px 0}",
           "</style></head><body>",
           "<h2>Optiwar AI chats — %s</h2>" % e(day),
           "<p>Chats with a message in the last %d hours: <b>%d</b>, messages %d "
           "(customer %d), signed in %d, escalated to KET %d. Full transcripts, as written.</p>"
           % (hours, s["chats"], s["messages"], s["customer_messages"], s["signed_in"],
              s["escalated"]),
           "<h3>AI CHAT SUMMARY</h3>",
           _table(e, [(label, str(h[k])) for k, label in HEADLINE_ROWS]
                  + [("Customer satisfied / unresolved", "not collected yet")])]
    for err in errors:
        out.append("<p style=\"color:#b00\">%s</p>" % e(err))
    for i, c in enumerate(sessions, 1):
        kind = c.get("kind") or classify(c)[0]
        who = " · ".join(x for x in (
            c.get("name"), c.get("email"),
            "customer #%s" % c["customer_id"] if c.get("customer_id") else "guest") if x)
        bits = [who, "status %s" % c["status"] if c.get("status") else "",
                "KET %s" % c["ket_ref"] if c.get("ket_ref") else "",
                "page %s" % c["page"] if c.get("page") else "", "session %s" % c["session_id"]]
        css = kind.split()[0]
        out.append("<div class=\"chat%s\"><div class=\"head\"><b>%d. %s</b>"
                   "<span class=\"badge b-%s\">%s</span>%s<br>%s</div>"
                   % (" widget" if kind == WIDGET else "", i, e(c["messages"][0]["at"]),
                      e(css), e(kind),
                      " <span class=\"t\">(%s)</span>" % e(c["kind_basis"])
                      if c.get("kind_basis") else "",
                      e(" · ".join(b for b in bits if b))))
        idx = _ai_indexes(c)
        traces = {j: trace_for(c, j) for j in idx}
        out.append("<h4>CUSTOMER IDENTITY</h4>" + _table(e, identity(c)))
        if kind != WIDGET:
            out.append("<h4>MANAGEMENT SUMMARY</h4>"
                       + _table(e, management(c, list(traces.values()))))
        out.append("<h4>TRANSCRIPT AND AI EXECUTION TRACE</h4>")
        for j, m in enumerate(c["messages"]):
            src = m["source"] if m["source"] in SPEAKER else "system"
            meta = _meta_line(m["meta"])
            badge = ""
            trace = ""
            if j in traces:
                t = traces[j]
                badge = "<span class=\"badge\">%s</span>" % e(
                    BADGE.get(t.get("source"), t.get("source") or ""))
                if (t.get("action") or {}).get("id"):
                    badge += "<span class=\"badge\">ACR ACTION</span>"
                if t.get("basis") != "greeting":
                    trace = "\n<div class=\"trace\">%s</div>" % "<br>".join(
                        e(x) for x in trace_lines(t))
            out.append("<div class=\"m %s\"><span class=\"who\">%s</span>%s <span class=\"t\">%s</span>\n%s%s%s</div>"
                       % (src, SPEAKER[src], badge, e(m["at"]), e(m["text"]), trace,
                          "\n<span class=\"meta\">%s</span>" % e(meta) if meta else ""))
        if kind != WIDGET:
            acts = action_rows(c)
            out.append("<h4>ACTIONS</h4>" + ("<br>".join(
                "<span class=\"meta\">%s</span>" % e(a) for a in acts) if acts else
                "<span class=\"t\">no action record</span>"))
            out.append("<h4>BUSINESS OUTCOME</h4>" + _table(e, outcome(c)[0]))
            out.append("<h4>SUPPORT / TICKET</h4>" + _table(e, support(c)))
            out.append("<h4>SATISFACTION</h4><span class=\"t\">not collected yet</span>")
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
             "  Agent replies                 %d" % s["agent_messages"]]
    h = headline(sessions)
    lines += ["  %-30s%d" % (label, h[k]) for k, label in HEADLINE_ROWS[1:]
              if k != "escalated"]
    lines += [
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

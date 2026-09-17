"""The account owner hears, once, that a face scan landed — and what it said.

Every completed scan into a Face Profile (the owner's own from the try-on
page, or a remote invitee's through ``/face-scan/guest``) ends with one
WhatsApp and one email to the *account* — never to the invitee — carrying
the person's label and the labelled measurements. Nothing else: no capture,
no link, no token.

Exactly once per scan: the send is claimed by writing ``face.scan.completed``
into ``face_events`` under an event id derived from the scan id. A browser
retry, a duplicate worker or a second call for the same scan finds the claim
already there and sends nothing. Delivery outcome is recorded per channel as
``face.scan.notified`` / ``face.scan.notify_failed``; a failed delivery does
not un-claim the scan (the measurement is in the profile regardless, and the
customer sees it on My Faces).

Gated by the remote-scan gate (flag + allow-list): only accounts inside it
receive the message, so nothing reaches a customer before the copy is
approved and the WhatsApp template exists at the provider.
"""
import os

from . import face_profiles as fp
from . import face_scan_invites as fsi

ENABLED_ENV = "FACE_SCAN_DONE_NOTIFY_ENABLED"
WA_TEMPLATE_ENV = "FACE_SCAN_DONE_WA_TEMPLATE"
DEFAULT_WA_TEMPLATE = "face_scan_done"

EV_COMPLETED = "face.scan.completed"
EV_NOTIFIED = "face.scan.notified"
EV_NOTIFY_FAILED = "face.scan.notify_failed"

# The text submitted to MSG91/Meta as template ``face_scan_done`` (Utility,
# en). Variables: {{1}} account name, {{2}} person label, {{3}} PD distance,
# {{4}} PD near, {{5}} face width, {{6}} recommended frame size. Meta refuses
# a body that starts or ends with a variable.
WA_TEMPLATE_HEADER = "Optiwar Face Scan Complete"
WA_TEMPLATE_BODY = (
    "Hello {{1}},\n\n"
    "The face scan for {{2}} is complete and has been saved to your Optiwar "
    "profile.\n\n"
    "Measurements:\n"
    "PD (distance): {{3}} mm\n"
    "PD (near): {{4}} mm\n"
    "Face width: {{5}} mm\n"
    "Recommended frame size: {{6}}\n\n"
    "You can now see frames recommended for this person under My Faces in "
    "your Optiwar profile.\n\n"
    "If you did not expect this scan, please contact Optiwar Support."
)

EMAIL_SUBJECT = "Optiwar — Face scan complete for {person}"
EMAIL_TEXT = (
    "Hello {name},\n\n"
    "The face scan for {person} is complete and has been saved to your Optiwar "
    "profile.\n\n"
    "Measurements\n"
    "  PD (distance):          {pd_far} mm\n"
    "  PD (near):              {pd_near} mm\n"
    "  Face width:             {face_width} mm\n"
    "  Recommended frame size: {size}\n\n"
    "You can now see frames recommended for {person} under My Faces:\n"
    "{url}\n\n"
    "If you did not expect this scan, please contact Optiwar Support.\n\n"
    "Regards,\n"
    "Optiwar Support\n"
    "Factory Outlet Opticals\n"
)
EMAIL_HTML = (
    "<p>Hello {name},</p>"
    "<p>The face scan for <strong>{person}</strong> is complete and has been "
    "saved to your Optiwar profile.</p>"
    "<table cellpadding='4' style='border-collapse:collapse'>"
    "<tr><td>PD (distance)</td><td><strong>{pd_far} mm</strong></td></tr>"
    "<tr><td>PD (near)</td><td><strong>{pd_near} mm</strong></td></tr>"
    "<tr><td>Face width</td><td><strong>{face_width} mm</strong></td></tr>"
    "<tr><td>Recommended frame size</td><td><strong>{size}</strong></td></tr>"
    "</table>"
    "<p>You can now see frames recommended for {person} under My Faces:<br>"
    "<a href='{url}'>{url}</a></p>"
    "<p>If you did not expect this scan, please contact Optiwar Support.</p>"
    "<p>Regards,<br>Optiwar Support<br>Factory Outlet Opticals</p>"
)


def enabled_for(email, environ=None):
    env = os.environ if environ is None else environ
    if str(env.get(ENABLED_ENV, "1")).strip().lower() in ("0", "false", "no", "off"):
        return False
    return fsi.enabled_for(email, env)


def _fmt(v):
    if v is None:
        return "—"
    try:
        return ("%.2f" % float(v)).rstrip("0").rstrip(".") if float(v) != int(float(v)) \
            else "%d" % int(float(v))
    except (TypeError, ValueError):
        return str(v)


def person_label(profile):
    """"Sudhanshu (you)" for Self, "Mother (Parent)" for anyone else."""
    if profile.get("is_self"):
        return "%s (you)" % profile["display_name"]
    rel = fp.RELATIONSHIP_LABELS.get(profile.get("relationship_type"), "")
    return "%s (%s)" % (profile["display_name"], rel) if rel else profile["display_name"]


def measurements_for(db, scan_id):
    cur = db.cursor()
    cur.execute("SELECT customer_id, face_profile_id, pd_far, pd_near, face_width, "
                "recommended_diameter, recommended_bridge, recommended_length "
                "FROM face_scans WHERE id=%s", (int(scan_id),))
    s = cur.fetchone()
    if not s:
        return None
    size = "—"
    if s["recommended_diameter"] and s["recommended_bridge"] and s["recommended_length"]:
        size = "%s-%s-%s" % (s["recommended_diameter"], s["recommended_bridge"],
                             s["recommended_length"])
    return {"customer_id": int(s["customer_id"]),
            "face_profile_id": int(s["face_profile_id"]),
            "pd_far": _fmt(s["pd_far"]), "pd_near": _fmt(s["pd_near"]),
            "face_width": _fmt(s["face_width"]), "size": size}


def account_for(db, customer_id):
    cur = db.cursor()
    cur.execute("SELECT customer_name, customer_email, customer_phone FROM customers "
                "WHERE customer_id=%s LIMIT 1", (int(customer_id),))
    return cur.fetchone()


def my_faces_url(site_host):
    host = (site_host or "optiwar.com").strip()
    if not (host.startswith("http://") or host.startswith("https://")):
        host = "https://" + host
    return host.rstrip("/") + "/profile/?tab=faces"


def _clean(fn, value):
    try:
        return fn(value)
    except fsi.InviteError:
        return None


def _claim(db, customer_id, profile_id, scan_id, source):
    return fsi.emit(db, EV_COMPLETED,
                    {"customer_id": customer_id, "face_profile_id": profile_id,
                     "request_uuid": None, "scan_group_id": None},
                    {"scan_id": scan_id, "source": source},
                    event_id=fsi.event_id_for(EV_COMPLETED, "scan", str(int(scan_id))),
                    scan_id=scan_id)


def notify(db, scan_id, site_host, source=fp.SRC_TRYON, environ=None,
           mailer=None, whatsapp=None):
    """Send the owner their labelled result for ``scan_id``, once.

    Returns a dict describing what happened; never raises for a delivery
    failure (the scan is already saved and this runs after that commit).
    """
    env = os.environ if environ is None else environ
    m = measurements_for(db, scan_id)
    if not m:
        return {"sent": False, "reason": "no_scan"}
    acct = account_for(db, m["customer_id"])
    if not acct or not enabled_for(acct.get("customer_email"), env):
        return {"sent": False, "reason": "gated"}
    if not _claim(db, m["customer_id"], m["face_profile_id"], scan_id, source):
        return {"sent": False, "reason": "duplicate"}
    try:
        profile = fp.get_profile(db, m["customer_id"], m["face_profile_id"])
    except fp.ProfileError:
        return {"sent": False, "reason": "no_profile"}
    name = (acct.get("customer_name") or "").strip() or "Customer"
    person = person_label(profile)
    fields = dict(m, name=name, person=person, url=my_faces_url(site_host))
    base = {"customer_id": m["customer_id"], "face_profile_id": m["face_profile_id"],
            "request_uuid": None, "scan_group_id": None}
    out = {"sent": True, "whatsapp": None, "email": None}

    phone = _clean(fsi.clean_phone, acct.get("customer_phone"))
    if phone:
        wa = whatsapp or fsi._default_whatsapp
        try:
            r = wa(phone.lstrip("+"), env.get(WA_TEMPLATE_ENV, DEFAULT_WA_TEMPLATE), {
                "body_1": {"type": "text", "value": name},
                "body_2": {"type": "text", "value": person},
                "body_3": {"type": "text", "value": m["pd_far"]},
                "body_4": {"type": "text", "value": m["pd_near"]},
                "body_5": {"type": "text", "value": m["face_width"]},
                "body_6": {"type": "text", "value": m["size"]},
            }) or {}
            ok, err = bool(r.get("ok")), r.get("error")
        except Exception as exc:  # noqa: BLE001 - provider failure is a record, not a crash
            ok, err = False, str(exc)[:160]
        out["whatsapp"] = "SENT" if ok else "FAILED"
        fsi.emit(db, EV_NOTIFIED if ok else EV_NOTIFY_FAILED, base,
                 {"scan_id": scan_id, "channel": fsi.CH_WHATSAPP, "error": err},
                 event_id=fsi.event_id_for(EV_NOTIFIED if ok else EV_NOTIFY_FAILED,
                                           "scan", "%d:whatsapp" % int(scan_id)),
                 scan_id=scan_id)

    email = _clean(fsi.clean_email, acct.get("customer_email"))
    if email:
        mail = mailer or fsi._default_mailer
        h = {k: fsi._html(str(v)) for k, v in fields.items()}
        try:
            mail(email, EMAIL_SUBJECT.format(person=person),
                 EMAIL_HTML.format(**h), EMAIL_TEXT.format(**fields),
                 sender=env.get(fsi.MAIL_SENDER_ENV, fsi.DEFAULT_MAIL_SENDER))
            ok, err = True, None
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)[:160]
        out["email"] = "SENT" if ok else "FAILED"
        fsi.emit(db, EV_NOTIFIED if ok else EV_NOTIFY_FAILED, base,
                 {"scan_id": scan_id, "channel": fsi.CH_EMAIL, "error": err},
                 event_id=fsi.event_id_for(EV_NOTIFIED if ok else EV_NOTIFY_FAILED,
                                           "scan", "%d:email" % int(scan_id)),
                 scan_id=scan_id)
    return out

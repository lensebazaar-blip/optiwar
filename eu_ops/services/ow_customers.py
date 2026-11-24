"""Small Optiwar Ops API wrappers for customer onboarding."""

from urllib.parse import urlencode

from services.frame_offers import api_call


def lookup(email="", phone=""):
    query = urlencode({key: value for key, value in
                       (("email", email), ("phone", phone)) if value})
    return api_call("/ops/api/customers/lookup" +
                    (("?" + query) if query else ""))


def message_preview(kind, name="", email="", phone=""):
    query = urlencode({
        "kind": kind,
        "name": name or "",
        "email": email or "",
        "phone": phone or "",
    })
    return api_call("/ops/api/customers/message-preview?" + query)


def create_customer(name, email, phone, created_by):
    return api_call("/ops/api/customers", "POST", {
        "name": name, "email": email, "phone": phone, "created_by": created_by,
    })


def face_scan_request(customer_id, channels, created_by):
    return api_call(
        f"/ops/api/customers/{int(customer_id)}/face-scan-request",
        "POST", {"channels": list(channels), "created_by": created_by})


def face_scan_status(customer_id):
    return api_call(f"/ops/api/customers/{int(customer_id)}/face-scan-request")

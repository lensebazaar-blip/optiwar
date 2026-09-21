"""
The frame-fit engine, and the person a customer is currently shopping for.

One implementation of the fit arithmetic serves the product page, the frame
listings' MATCHED badges, the matching-frames API and — later — cart lines,
Favourites and the assistant. Every surface presents what ``evaluate``
returns; none of them derives a fit of its own.

The rule is the one the shop has used since face measurement shipped:

  frame width  = 2 * eye size + bridge + 10 mm           (must be within 8 mm
                                                          of the face width)
  decentration = |(eye size + bridge) - PD| / 2           (at most 6 mm)
  temple       = within 10 mm of the recommended length

and inside that envelope EXCELLENT is width <= 3 / decentration <= 4,
VERY GOOD is width <= 5 / decentration <= 5, otherwise GOOD.

The shopping context is the profile the customer chose to shop for on this
device (a session value), their default profile when they have not chosen,
or nobody — "No person / Gift" — which produces no fit rather than a
fit for the wrong face.
"""
import re

from . import face_profiles as fp

WIDTH_TOLERANCE_MM = 8.0
DECENTRATION_LIMIT_MM = 6.0
TEMPLE_TOLERANCE_MM = 10.0

EXCELLENT = "excellent"
VERY_GOOD = "very_good"
GOOD = "good"
NOT_MATCHED = "not_matched"
NO_MEASUREMENT = "no_measurement"
NO_PERSON = "no_person"
NO_SIZE = "no_size"

MATCHED = (EXCELLENT, VERY_GOOD, GOOD)

LABELS = {
    EXCELLENT: "EXCELLENT",
    VERY_GOOD: "VERY GOOD",
    GOOD: "GOOD",
    NOT_MATCHED: "Not a match",
    NO_MEASUREMENT: "Face not measured",
    NO_PERSON: "Face fit not checked",
    NO_SIZE: "Frame size not stated",
}

# What /api/tryon/matching-frames has always called the three grades.
LEGACY_API_LABELS = {EXCELLENT: "Perfect", VERY_GOOD: "Good", GOOD: "Fair"}

SESSION_KEY = "face_shop_pid"
NOBODY = 0

_SIZE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)\s*$")


def parse_size(size):
    """``'52-18-140'`` -> ``(52, 18, 140)``; anything else -> None."""
    m = _SIZE_RE.match(str(size or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _result(classification, recommended=None, actual=None, delta=None,
            reasons=(), measured=None):
    return {
        "classification": classification,
        "label": LABELS[classification],
        "matched": classification in MATCHED,
        "recommended_dimensions": recommended,
        "actual_dimensions": actual,
        "measurement_delta": delta,
        "reasons": list(reasons),
        "measurements": measured,
    }


def recommended_dimensions(measurement):
    """The recommended size carried by a scan, as the engine reports it."""
    d = measurement.get("recommended_diameter")
    b = measurement.get("recommended_bridge")
    l = measurement.get("recommended_length")
    if not (d and b and l):
        return None
    return {"diameter": int(d), "bridge": int(b), "length": int(l),
            "size": "%d-%d-%d" % (int(d), int(b), int(l))}


def evaluate(measurement, size):
    """Fit of one frame size against one person's measurements.

    ``measurement`` is a mapping with ``pd_far``, ``face_width`` and the three
    ``recommended_*`` fields (a face_profiles row, a face_measurements row or
    ``public_view()['measurements']`` all qualify); ``size`` is the product's
    ``product_size``. ``None`` for the measurement means no person.
    """
    if measurement is None:
        return _result(NO_PERSON)
    pd = _num(measurement.get("pd_far"))
    fw = _num(measurement.get("face_width"))
    if pd is None or fw is None:
        return _result(NO_MEASUREMENT)
    rec = recommended_dimensions(measurement)
    rec_len = _num(measurement.get("recommended_length"), 140.0)
    measured = {"pd_far": pd, "face_width": fw,
                "recommended_size": rec["size"] if rec else None}
    dims = parse_size(size)
    if not dims:
        return _result(NO_SIZE, recommended=rec, measured=measured,
                       reasons=["frame size not stated"])
    d, b, l = dims
    actual = {"diameter": d, "bridge": b, "length": l,
              "size": "%d-%d-%d" % (d, b, l), "frame_width": 2 * d + b + 10}
    width_diff = abs(actual["frame_width"] - fw)
    decentration = abs((d + b) - pd) / 2.0
    temple_diff = abs(l - rec_len)
    delta = {"frame_width_mm": round(width_diff, 1),
             "decentration_mm": round(decentration, 1),
             "temple_mm": round(temple_diff, 1)}
    reasons = []
    if width_diff > WIDTH_TOLERANCE_MM:
        reasons.append("frame %s mm %s than the face" % (
            round(width_diff, 1),
            "wider" if actual["frame_width"] > fw else "narrower"))
    if decentration > DECENTRATION_LIMIT_MM:
        reasons.append("lens centres %s mm off the pupils" % round(decentration, 1))
    if temple_diff > TEMPLE_TOLERANCE_MM:
        reasons.append("temple %s mm %s than recommended" % (
            round(temple_diff, 1), "longer" if l > rec_len else "shorter"))
    if reasons:
        return _result(NOT_MATCHED, rec, actual, delta, reasons, measured)
    if width_diff <= 3 and decentration <= 4:
        grade = EXCELLENT
    elif width_diff <= 5 and decentration <= 5:
        grade = VERY_GOOD
    else:
        grade = GOOD
    return _result(grade, rec, actual, delta,
                   ["width within %s mm, decentration %s mm" % (
                       round(width_diff, 1), round(decentration, 1))],
                   measured)


def score(result):
    """Lower is better; the order the matching-frames API has always used."""
    delta = result.get("measurement_delta") or {}
    return (delta.get("frame_width_mm", 0) * 1.5
            + delta.get("decentration_mm", 0) * 2
            + delta.get("temple_mm", 0) * 0.3)


def matching_product_ids(rows, measurement):
    """The ids among ``rows`` (``product_id``, ``product_size``) that match."""
    out = []
    for row in rows:
        if evaluate(measurement, row.get("product_size"))["matched"]:
            out.append(str(row["product_id"]))
    return out


# --------------------------------------------------------------------------
# profile-bound evaluation
# --------------------------------------------------------------------------

def profile_measurement(row):
    """A profile row's measurement mapping (None values when it has no scan
    yet, so the verdict is "not measured"); None only when there is no row."""
    if not row:
        return None
    return {k: row.get(k) for k in fp.MEASUREMENT_FIELDS}


def evaluate_frame_fit(db, customer_id, profile_id, product):
    """The fit of ``product`` for one of this customer's profiles.

    Somebody else's profile id is a 404 (``fp.NotFound``), exactly like a
    missing one; ``profile_id`` None or 0 is "No person / Gift".
    """
    if profile_id in (None, "", 0, "0"):
        result = _result(NO_PERSON)
        result["profile"] = None
        return result
    row = fp.get_profile(db, customer_id, profile_id)
    result = evaluate(profile_measurement(row), product.get("product_size"))
    result["profile"] = _profile_ref(row)
    return result


def _profile_ref(row):
    return {"id": int(row["id"]), "display_name": row["display_name"],
            "relationship_type": row["relationship_type"],
            "relationship_label": fp.RELATIONSHIP_LABELS.get(
                row["relationship_type"], "Other"),
            "is_self": bool(row.get("is_self")),
            "is_default": bool(row.get("is_default")),
            "has_scan": row.get("pd_far") is not None}


def cache_scope(customer_id, profile_row, gated):
    """The key suffix under which a listing may cache this customer's
    matches: per person and per scan for a multi-person account (so a
    switch or a rescan misses the cache), the bare customer id otherwise."""
    if profile_row:
        return "%s:p%d:s%s" % (customer_id, int(profile_row["id"]),
                               profile_row.get("latest_scan_id") or "none")
    return "%s:nobody" % customer_id if gated else str(customer_id)


# --------------------------------------------------------------------------
# shopping context
# --------------------------------------------------------------------------

def active_profile(db, customer_id, session):
    """The profile this customer is shopping for right now.

    The one chosen on this device if it is still theirs and still active;
    their default otherwise; None when they chose "No person" — and None
    for an account with no profiles at all.
    """
    chosen = session.get(SESSION_KEY)
    if chosen == NOBODY:
        return None
    if chosen:
        try:
            return fp.get_profile(db, customer_id, chosen)
        except fp.ProfileError:
            session.pop(SESSION_KEY, None)
    return fp.default_profile(db, customer_id)


def set_active(db, customer_id, session, profile_id):
    """Choose who to shop for. ``None``/0 is "No person / Gift"; a profile
    that is not this customer's raises ``fp.NotFound``."""
    if profile_id in (None, "", 0, "0"):
        session[SESSION_KEY] = NOBODY
        return None
    row = fp.get_profile(db, customer_id, profile_id)
    session[SESSION_KEY] = int(row["id"])
    return row


def context(db, customer_id, session):
    """Everything a selector needs: who is active, and who else there is."""
    active = active_profile(db, customer_id, session)
    rows = fp.list_profiles(db, customer_id)
    return {
        "active_profile_id": int(active["id"]) if active else None,
        "active": _profile_ref(active) if active else None,
        "explicit_nobody": session.get(SESSION_KEY) == NOBODY,
        "profiles": [_profile_ref(r) for r in rows],
    }

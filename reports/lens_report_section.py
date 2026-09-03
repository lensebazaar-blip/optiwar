#!/usr/bin/env python3
"""Contact lenses — Daily Report section.

Two questions this section exists to answer every morning:

  1. Of the contact lenses loaded into the catalogue, how many are actually
     live, and for the rest, *why not* — one reason list, produced by the same
     release gate the storefront, the model, the sitemap and the merchant feed
     read. A report that re-derives "live" invents a second definition, and the
     first morning the two disagree nobody knows which one customers saw.

  2. Contact lenses exposed on optiwar.in. The answer must be 0. It is stated
     as an invariant rather than a metric: anything above 0 is RED, because the
     vertical is not released there and a single row with ``sell_on_in = 1``
     means product pages, search, the API, the model and the sitemap could all
     have shown it.

Sourcing: the catalogue database, read-only, through the shared reader. The
release verdict comes from the deployed application's ``catalogue`` module; when
that cannot be loaded the section says so and reports a coverage gap instead of
counting rows by rules of its own.

Data protection: counts, statuses, SKUs and product titles only. Prescription
values are never read, let alone printed — the matrix is reported as a number of
combinations, and a refused order as its reason code.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from report_db import SqlError, run_sql, scalar, to_int  # noqa: E402

WIDTH = 70
BANNER = "=" * WIDTH
WINDOW_HOURS = int(os.environ.get("ACR_REPORT_WINDOW_HOURS", "24"))
SINCE = "NOW() - INTERVAL %d HOUR" % WINDOW_HOURS

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
_ORDER = {GREEN: 0, AMBER: 1, RED: 2}

CONTACT_LENS = "CONTACT_LENS"
SITE_COM = "optiwar.com"
SITE_IN = "in.optiwar.com"

# Where the deployed application lives, so the gate this report quotes is the
# gate that actually served customers today.
APP_DIR = os.environ.get(
    "OPTIWAR_APP_DIR",
    "/var/www/flask-optiwar-ow-release-090525/venv/lib/python3.11/"
    "site-packages/flaskr")

# The columns ``lens_release_blockers`` reads, in the order the query selects
# them. Named here so the row dicts the gate is handed are the same shape the
# application hands it.
GATE_COLUMNS = (
    "product_id", "product_code", "product_name", "product_slug",
    "product_image", "product_status", "product_vertical",
    "sell_on_com", "sell_on_in",
    "product_price_eur", "product_special_price_eur",
    "brand", "manufacturer", "gtin", "manufacturer_mpn",
    "modality", "lens_type", "availability", "lead_time_days",
    "merchant_enabled", "param_mode", "variant_count", "rule_count",
    "image_count",
)

LENS_ROWS_SQL = """
SELECT p.product_id, p.product_code, p.product_name, p.product_slug,
       p.product_image, p.product_status, p.product_vertical,
       p.sell_on_com, p.sell_on_in,
       p.product_price_eur, p.product_special_price_eur,
       c.brand, c.manufacturer, c.gtin, c.manufacturer_mpn,
       c.modality, c.lens_type, c.availability, c.lead_time_days,
       c.merchant_enabled, c.param_mode,
       (SELECT COUNT(*) FROM contact_lens_variants v
         WHERE v.product_id = p.product_id AND v.available = 1),
       (SELECT COUNT(*) FROM contact_lens_param_rules r
         WHERE r.product_id = p.product_id AND r.available = 1),
       (SELECT COUNT(*) FROM contact_lens_images i
         WHERE i.product_id = p.product_id
           AND i.image_type <> 'WITHDRAWN')
FROM contact_lens_products c
JOIN products p ON p.product_id = c.product_id
ORDER BY c.brand, p.product_name
"""

# Lens types as the report groups them. The value is the catalogue's own
# ``lens_type``; the grouping only decides which line a product is counted on.
TYPE_ORDER = ("SPHERICAL", "TORIC", "MULTIFOCAL", "TORIC_MULTIFOCAL", "COLOR")


class GateUnavailable(Exception):
    """The deployed release gate could not be loaded."""


def load_gate(app_dir=None):
    """``lens_release_blockers`` from the deployed application.

    Loaded by path rather than imported as ``flaskr.catalogue``: the report runs
    outside the application's virtualenv, and the module needs nothing from the
    package around it.
    """
    path = os.path.join(app_dir or APP_DIR, "catalogue.py")
    if not os.path.exists(path):
        raise GateUnavailable("catalogue.py not found at %s" % path)
    try:
        spec = importlib.util.spec_from_file_location("optiwar_catalogue", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 - degrade to a coverage gap
        raise GateUnavailable(str(e))
    gate = getattr(module, "lens_release_blockers", None)
    if gate is None:
        raise GateUnavailable("catalogue.py has no lens_release_blockers")
    return gate


def lens_rows():
    """Every contact lens as a dict keyed like the application's own rows."""
    return [dict(zip(GATE_COLUMNS, row)) for row in run_sql(LENS_ROWS_SQL)]


def _group(row):
    t = (row.get("lens_type") or "").strip().upper().replace(" ", "_")
    t = t.replace("-", "_")
    if t in TYPE_ORDER:
        return t
    if "TORIC" in t and "MULTIFOCAL" in t:
        return "TORIC_MULTIFOCAL"
    if "MULTIFOCAL" in t or t == "PRESBYOPIA":
        return "MULTIFOCAL"
    if "TORIC" in t or "ASTIGMAT" in t:
        return "TORIC"
    if "COLOR" in t or "COLOUR" in t:
        return "COLOR"
    if t:
        return "SPHERICAL"
    return "unclassified"


def type_split(rows):
    counts = {}
    for row in rows:
        key = _group(row)
        counts[key] = counts.get(key, 0) + 1
    return counts


def in_exposure(rows):
    """Contact lenses a .in surface could have shown. Must be empty."""
    out = []
    for row in rows:
        flag = row.get("sell_on_in")
        try:
            exposed = int(flag) == 1
        except (TypeError, ValueError):
            exposed = flag not in (None, "", "0")
        if exposed:
            out.append(row.get("product_code") or row.get("product_id"))
    return out


def orderable_count(rows):
    """What is orderable, counted in whichever shape each lens states it.

    Combinations for a MATRIX lens, stated values for a RULES one. Counting
    only combinations would report a live rules lens as having nothing
    orderable, and the report would disagree with the storefront.
    """
    return sum(to_int(r.get("rule_count"))
               if (r.get("param_mode") or "").strip().upper() == "RULES"
               else to_int(r.get("variant_count")) for r in rows)


def blocker_tally(rows, gate):
    """(live rows, held rows, reason -> count) for the .com storefront."""
    live, held, reasons = [], [], {}
    for row in rows:
        blockers = tuple(gate(row, SITE_COM))
        if blockers:
            held.append((row, blockers))
            for reason in blockers:
                reasons[reason] = reasons.get(reason, 0) + 1
        else:
            live.append(row)
    return live, held, reasons


def _collect():
    """(metrics, errors). A metric is None when it could not be read."""
    m, errs = {}, []

    def safe(key, fn):
        try:
            m[key] = fn()
        except (SqlError, GateUnavailable) as e:
            m[key] = None
            errs.append("%s: %s" % (key, e))

    safe("rows", lens_rows)
    safe("gate", load_gate)

    rows, gate = m.get("rows"), m.get("gate")
    if rows is not None and gate is not None:
        live, held, reasons = blocker_tally(rows, gate)
        m["live"] = live
        m["held"] = held
        m["reasons"] = reasons
        m["types"] = type_split(live)
        m["on_order"] = [r for r in live
                         if (r.get("availability") or "").strip().upper()
                         == "ON_ORDER"]
        m["variants_live"] = orderable_count(live)
    if rows is not None:
        m["in_exposed"] = in_exposure(rows)

    # Matrix refusals and accepted orders in the window, from the canonical
    # event stream: a refusal is what tells us the loaded matrix is narrower
    # than what customers ask for.
    def refusals():
        rows_ = run_sql(
            "SELECT COALESCE(failure_code,'UNKNOWN'), COUNT(*) FROM ai_events "
            "WHERE event_type='LENS_ORDER_REFUSED' AND created_at >= %s "
            "GROUP BY 1 ORDER BY 2 DESC" % SINCE)
        return {r[0]: to_int(r[1]) for r in rows_}
    safe("refusals", refusals)

    safe("accepted", lambda: to_int(scalar(
        "SELECT COUNT(*) FROM ai_events WHERE event_type='LENS_ORDER_VALIDATED'"
        " AND created_at >= %s" % SINCE)))

    return m, errs


_CACHE = []


def _collect_once():
    if not _CACHE:
        _CACHE.append(_collect())
    return _CACHE[0]


def _reset_cache():
    del _CACHE[:]


def _worst(*statuses):
    real = [s for s in statuses if s in _ORDER]
    return max(real, key=lambda s: _ORDER[s]) if real else GREEN


def status(m):
    """The section's verdict, and the sentence that justifies it."""
    exposed = m.get("in_exposed")
    if exposed:
        return RED, ("%d contact lens(es) exposed on optiwar.in: %s"
                     % (len(exposed), ", ".join(str(x) for x in exposed[:10])))
    if m.get("rows") is None or m.get("gate") is None:
        return AMBER, "catalogue or release gate unreadable — coverage gap"
    if not m.get("rows"):
        return GREEN, "no contact lenses loaded yet"
    if not m.get("live"):
        return AMBER, ("%d lens(es) loaded, none released"
                       % len(m["rows"]))
    return GREEN, None


def _brand_tally(rows):
    out = {}
    for row in rows:
        brand = (row.get("brand") or "unbranded").strip()
        out[brand] = out.get(brand, 0) + 1
    return out


def _pairs(counts):
    return ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items())) or "-"


def build():
    L = []
    add = L.append
    from datetime import datetime

    try:
        m, errs = _collect_once()
    except Exception as e:  # noqa: BLE001 - never break the daily report
        return "\n".join([BANNER, "  CONTACT LENSES (.com)", BANNER,
                          "  [WARN] section unavailable: %s" % e, BANNER])

    verdict, why = status(m)
    loaded = len(m["rows"]) if m.get("rows") is not None else None
    live = m.get("live")
    exposed = m.get("in_exposed")

    add(BANNER)
    add("  CONTACT LENSES (.com)%sSTATUS: %s" % (" " * 26, verdict))
    add(BANNER)
    if why:
        add("  %s" % why)
    add("  Loaded %s | live %s | held back %s | orderable combinations %s"
        % (loaded if loaded is not None else "n/a",
           len(live) if live is not None else "n/a",
           len(m["held"]) if m.get("held") is not None else "n/a",
           m.get("variants_live", "n/a")))
    add("  Brands (live)   %s"
        % (_pairs(_brand_tally(live)) if live else "-"))
    add("  Types (live)    %s"
        % (_pairs({k: v for k, v in (m.get("types") or {}).items()})
           if live else "-"))
    add("  ON_ORDER (live) %s"
        % (len(m["on_order"]) if m.get("on_order") is not None else "n/a"))

    add("")
    add("  Held back, by reason:")
    reasons = m.get("reasons")
    if reasons is None:
        add("    unreadable — release gate unavailable")
    elif not reasons:
        add("    none")
    else:
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            add("    %-34s %d" % (reason, count))
        for row, blockers in (m.get("held") or [])[:10]:
            add("      %-10s %s" % (row.get("product_code") or "?",
                                    ", ".join(blockers)))

    add("")
    add("  Orders (last %dh): accepted %s | refused by the matrix %s"
        % (WINDOW_HOURS,
           m.get("accepted") if m.get("accepted") is not None else "n/a",
           sum((m.get("refusals") or {}).values())
           if m.get("refusals") is not None else "n/a"))
    for code, count in sorted((m.get("refusals") or {}).items(),
                              key=lambda kv: -kv[1]):
        add("      %-34s %d" % (code, count))

    add("")
    # The invariant, stated even when it holds: a line that only appears on
    # failure is a line nobody notices has stopped being checked.
    add("  INVARIANT  contact lenses exposed on optiwar.in: %s%s"
        % ("n/a" if exposed is None else len(exposed),
           "   [RED]" if exposed else ""))
    for e in errs[:6]:
        add("  [degraded] %s" % e)
    add("  Source: contact_lens_products/variants + the deployed release gate. "
        "No prescription values are read or printed.")
    add("  Generated %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add(BANNER)
    return "\n".join(L)


def findings():
    """Severity findings this section contributes to the executive summary."""
    from reports.report_severity import ACTION, CRITICAL, Finding, WARNING

    try:
        m, errs = _collect_once()
    except Exception as e:  # noqa: BLE001
        return [Finding(ACTION, "contact_lens",
                        "contact-lens section unavailable: %s" % e, "lens")]

    out = []
    exposed = m.get("in_exposed")
    if exposed:
        out.append(Finding(
            CRITICAL, "contact_lens",
            "%d contact lens(es) sellable on optiwar.in (%s) — the vertical is "
            "not released there"
            % (len(exposed), ", ".join(str(x) for x in exposed[:10])),
            "lens"))
    if m.get("rows") is None or m.get("gate") is None:
        out.append(Finding(WARNING, "contact_lens",
                           "contact-lens release state unreadable — the live "
                           "count is a coverage gap, not a zero", "lens"))
    elif m.get("rows") and not m.get("live"):
        out.append(Finding(WARNING, "contact_lens",
                           "%d contact lens(es) loaded, none released"
                           % len(m["rows"]), "lens"))
    refusals = m.get("refusals") or {}
    not_made = refusals.get("COMBINATION_NOT_MADE", 0)
    if not_made:
        out.append(Finding(WARNING, "contact_lens",
                           "%d lens order(s) refused in %dh: the requested "
                           "combination is not in the loaded matrix"
                           % (not_made, WINDOW_HOURS), "lens"))
    for e in errs[:6]:
        out.append(Finding(WARNING, "contact_lens",
                           "degraded metric %s" % e, "lens"))
    return out


def main():
    text = build()
    try:
        from reports.report_severity import emit
        emit("lens", findings())
    except Exception:  # noqa: BLE001 - the section still stands on its own
        pass
    print(text)


if __name__ == "__main__":
    main()

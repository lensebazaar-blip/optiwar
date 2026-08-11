#!/usr/bin/env python3
"""Google Merchant Center severity classification for the Daily Report.

Why this module exists
----------------------
The report printed::

    Submitted 710  |  Approved 0  |  Pending 0  |  Disapproved 710

which reads as a catastrophe and is not true. It came from the deprecated
Content API v2.1 rollup ``destinationStatuses[].status``, which returns
``disapproved`` for every product in this account even when the *same payload*
lists 35 ``approvedCountries`` and zero ``disapprovedCountries``. The modern
Merchant API reporting view says 708 ELIGIBLE / 2 disapproved.

Two counting faults follow from that:

* **Rollup status is not evidence.** Eligibility is decided per country per
  reporting context, so the only trustworthy signal is the country lists (or the
  Merchant API ``aggregatedReportingContextStatus``), never the rollup string.
* **Per-destination issues were summed.** Each issue appears once for
  SHOPPING_ADS and once for FREE_LISTINGS, so "4 image_link_internal_error"
  was 2 products counted twice.

"710 disapproved" is also not actionable on its own: an ops team needs to know
whether it is 710 broken products or one account/program-level condition. So
classification here always splits by reporting context and separates
*systemic* causes (whole catalogue affected identically — configuration,
eligibility, feed-level) from *product-specific* defects (a bounded set of
offers), and marks whether the fix is ours or Google's.
"""
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports.report_severity import (  # noqa: E402
    ACTION, CRITICAL, INFO, WARNING, Finding, emit,
)

SOURCE = "gmc"

# Above this share of the catalogue, a single issue is a systemic condition
# (configuration, eligibility or feed-level), not a per-product defect.
SYSTEMIC_SHARE = 0.5

# Issues Google resolves on its own side. Reporting them as merchant work sends
# the team to fix products that are not broken.
_GOOGLE_SIDE_CODES = (
    "image_link_internal_error",
    "image_link_pending_crawl",
    "pending_initial_policy_review_shopping_ads",
    "pending_initial_policy_review_free_listings",
)

# Severity of an issue by how it affects serving, not by how alarming it sounds.
_SEVERITY_BY_IMPACT = {
    "DISAPPROVED": ACTION,
    "DEMOTED": WARNING,
    "UNAFFECTED": INFO,
    "PENDING_PROCESSING": INFO,
}


def _norm(value):
    return (value or "").strip().upper()


def summarize_product_views(rows):
    """Reduce Merchant API ``product_view`` rows to a classified summary.

    ``rows`` are productView dicts. Returns a dict with per-status totals, and
    per-issue detail keyed by (code, severity) carrying the affected offers and
    the reporting contexts involved.
    """
    total = 0
    by_status = Counter()
    issues = defaultdict(lambda: {"offers": [], "contexts": set(),
                                  "countries": set(), "resolution": "",
                                  "attributes": set()})
    for pv in rows or ():
        total += 1
        by_status[_norm(pv.get("aggregatedReportingContextStatus")) or "UNKNOWN"] += 1
        offer = pv.get("offerId") or pv.get("id") or "?"
        for iss in pv.get("itemIssues") or ():
            code = (iss.get("type") or {}).get("code") or "unknown"
            sev = (iss.get("severity") or {})
            key = (code, _norm(sev.get("aggregatedSeverity")))
            rec = issues[key]
            # One entry per product: an issue reported under both SHOPPING_ADS
            # and FREE_LISTINGS is one defect, not two.
            if offer not in rec["offers"]:
                rec["offers"].append(offer)
            rec["resolution"] = _norm(iss.get("resolution")) or rec["resolution"]
            attr = (iss.get("type") or {}).get("canonicalAttribute")
            if attr:
                # Names the field to fix; "missing attribute" alone is not a
                # work item.
                rec["attributes"].add(attr)
            for per_ctx in sev.get("severityPerReportingContext") or ():
                ctx = per_ctx.get("reportingContext")
                if ctx:
                    rec["contexts"].add(ctx)
                for bucket in ("disapprovedCountries", "demotedCountries"):
                    rec["countries"].update(per_ctx.get(bucket) or ())
    return {"total": total, "by_status": dict(by_status), "issues": dict(issues)}


def _scope(count, total):
    """Whether an issue is catalogue-wide or a bounded set of products."""
    if total and count >= max(1, int(total * SYSTEMIC_SHARE)):
        return "systemic"
    return "product-specific"


def findings_from_summary(summary):
    """Structured severity for the executive aggregator.

    Every message states the governing cause and the split, because the point
    of the exercise is that "710 disapproved" told the team nothing they could
    act on.
    """
    out = []
    total = summary.get("total") or 0
    by_status = summary.get("by_status") or {}
    if not total:
        out.append(Finding(ACTION, "gmc",
                           "no product data returned — GMC status is UNKNOWN, "
                           "not healthy", SOURCE))
        return out

    disapproved = by_status.get("NOT_ELIGIBLE_OR_DISAPPROVED", 0)
    eligible = by_status.get("ELIGIBLE", 0)
    pending = by_status.get("PENDING", 0)

    if disapproved:
        share = 100.0 * disapproved / total
        scope = _scope(disapproved, total)
        severity = CRITICAL if scope == "systemic" else ACTION
        out.append(Finding(
            severity, "gmc",
            "%d/%d products (%.1f%%) not eligible or disapproved — %s"
            % (disapproved, total, share, scope), SOURCE))

    for (code, impact), rec in sorted(
            summary.get("issues", {}).items(),
            key=lambda kv: -len(kv[1]["offers"])):
        n = len(rec["offers"])
        severity = _SEVERITY_BY_IMPACT.get(impact, WARNING)
        if severity == INFO:
            continue
        contexts = ", ".join(sorted(rec["contexts"])) or "unspecified context"
        owner = ("Google-side (retry/re-crawl; the merchant fix is a no-op)"
                 if code in _GOOGLE_SIDE_CODES else "merchant action")
        offers = ", ".join(rec["offers"][:5])
        if n > 5:
            offers += ", +%d more" % (n - 5)
        attrs = ", ".join(sorted(rec["attributes"]))
        out.append(Finding(
            severity, "gmc",
            "%s%s: %d product(s) %s in %s [%s] — %s (%s)"
            % (code, " [%s]" % attrs if attrs else "", n, impact.lower(),
               contexts, _scope(n, total), owner, offers), SOURCE))

    out.append(Finding(INFO, "gmc",
                       "eligible %d / pending %d / disapproved %d of %d"
                       % (eligible, pending, disapproved, total), SOURCE))
    return out


def render_diagnosis(summary):
    """Human-readable root-cause block for the report body."""
    total = summary.get("total") or 0
    by_status = summary.get("by_status") or {}
    lines = ["  PRODUCT ELIGIBILITY (authoritative: Merchant API product_view)"]
    if not total:
        lines.append("    UNKNOWN — no rows returned")
        return "\n".join(lines)
    for status in sorted(by_status):
        lines.append("    %-28s %d" % (status, by_status[status]))
    lines.append("")
    lines.append("  WHY (by issue, deduplicated per product, split by context)")
    rows = sorted(summary.get("issues", {}).items(),
                  key=lambda kv: -len(kv[1]["offers"]))
    if not rows:
        lines.append("    no item-level issues")
    for (code, impact), rec in rows:
        n = len(rec["offers"])
        lines.append("    %5d  %-42s %-10s %s" % (
            n, code, impact.lower(),
            ",".join(sorted(rec["contexts"])) or "-"))
        lines.append("           scope=%s  owner=%s  countries=%d  attr=%s" % (
            _scope(n, total),
            "google" if code in _GOOGLE_SIDE_CODES else "merchant",
            len(rec["countries"]),
            ",".join(sorted(rec["attributes"])) or "-"))
    return "\n".join(lines)


def main():
    """Query the Merchant API, print the diagnosis and emit findings."""
    from reports.gmc_client import fetch_product_views  # local, keeps this
    # module importable (and unit-testable) without API credentials.
    try:
        rows = fetch_product_views()
    except Exception as e:  # noqa: BLE001 - the report must still be produced
        emit(SOURCE, [Finding(ACTION, "gmc",
                              "GMC status unavailable: %s — reported as "
                              "UNKNOWN, not healthy" % e, SOURCE)])
        print("  PRODUCT ELIGIBILITY: UNKNOWN (%s)" % e)
        return 1
    summary = summarize_product_views(rows)
    emit(SOURCE, findings_from_summary(summary))
    print(render_diagnosis(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

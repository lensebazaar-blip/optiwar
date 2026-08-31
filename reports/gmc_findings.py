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
import datetime
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports.report_severity import (  # noqa: E402
    ACTION, CRITICAL, INFO, WARNING, Finding, emit,
)

SOURCE = "gmc"

# The feed label our own XML feed publishes under. Offers under any other label
# were not submitted by us: Google's automatic product crawl (the AUTOFEED data
# source) invents offers from crawled pages, and an issue on one of those is
# fixed by turning the crawl off, not by editing the feed.
OUR_FEED_LABEL = os.environ.get("GMC_FEED_LABEL", "GLOBAL-EUR")

# Yesterday's issue membership, so the report can say what changed instead of
# reprinting the same totals every morning.
STATE_FILE = os.environ.get("GMC_STATE_FILE",
                            "/var/lib/optiwar/gmc_issues.json")

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
    by_source = Counter()
    demoted_offers = set()
    issues = defaultdict(lambda: {"offers": [], "contexts": set(),
                                  "countries": set(), "resolution": "",
                                  "attributes": set(), "labels": set(),
                                  "currencies": set()})
    for pv in rows or ():
        total += 1
        status = _norm(pv.get("aggregatedReportingContextStatus")) or "UNKNOWN"
        by_status[status] += 1
        label = pv.get("feedLabel") or "(none)"
        currency = (pv.get("price") or {}).get("currencyCode") or "?"
        by_source[(label, currency)] += 1
        offer = pv.get("offerId") or pv.get("id") or "?"
        for iss in pv.get("itemIssues") or ():
            code = (iss.get("type") or {}).get("code") or "unknown"
            sev = (iss.get("severity") or {})
            key = (code, _norm(sev.get("aggregatedSeverity")))
            rec = issues[key]
            rec["labels"].add(label)
            rec["currencies"].add(currency)
            if key[1] == "DEMOTED" and status == "ELIGIBLE":
                # Demoted is not disapproved: the offer still serves, lower.
                demoted_offers.add(offer)
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
    return {"total": total, "by_status": dict(by_status),
            "by_source": dict(by_source), "demoted": len(demoted_offers),
            "issues": dict(issues)}


def headline(summary):
    """The one line an operator reads first, in serving terms."""
    by_status = summary.get("by_status") or {}
    return ("Submitted %d  |  Eligible %d  |  Disapproved %d  |  "
            "Demoted %d  |  Pending %d"
            % (summary.get("total") or 0,
               by_status.get("ELIGIBLE", 0),
               by_status.get("NOT_ELIGIBLE_OR_DISAPPROVED", 0),
               summary.get("demoted") or 0,
               by_status.get("PENDING", 0) + by_status.get(
                   "PENDING_PROCESSING", 0)))


def issue_membership(summary):
    """{issue code: sorted offers} — the shape compared between days."""
    out = defaultdict(set)
    for (code, _impact), rec in (summary.get("issues") or {}).items():
        out[code].update(rec["offers"])
    return {code: sorted(offers) for code, offers in out.items()}


def load_state(path=None):
    try:
        with open(path or STATE_FILE) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 - a first run has no yesterday
        return {}


def save_state(summary, path=None):
    """Record today's membership for tomorrow's comparison, best effort.

    A report that cannot write its state still has to be produced, so failure
    here is reported by the absence of a delta rather than by an exception.
    """
    path = path or STATE_FILE
    payload = {"date": datetime.date.today().isoformat(),
               "issues": issue_membership(summary)}
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "w") as fh:
            json.dump(payload, fh)
        return True
    except Exception:  # noqa: BLE001
        return False


def delta(summary, previous):
    """What changed since the last run, per issue and per offer.

    Without this the section repeats identical totals for weeks and nobody can
    tell a new defect from an old one nobody has fixed yet.
    """
    today = issue_membership(summary)
    if not previous or not previous.get("issues"):
        return None
    was = {code: set(offers)
           for code, offers in (previous.get("issues") or {}).items()}
    new, cleared = {}, {}
    for code, offers in today.items():
        gained = sorted(set(offers) - was.get(code, set()))
        if gained:
            new[code] = gained
    for code, offers in was.items():
        lost = sorted(offers - set(today.get(code, ())))
        if lost:
            cleared[code] = lost
    return {"since": previous.get("date") or "unknown",
            "new": new, "cleared": cleared}


def _owner(code):
    return "google" if code in _GOOGLE_SIDE_CODES else "merchant"


def ownership(summary):
    """Affected offers split by who can actually fix them."""
    merchant, google = set(), set()
    for (code, impact), rec in (summary.get("issues") or {}).items():
        if _SEVERITY_BY_IMPACT.get(impact, WARNING) == INFO:
            continue
        (google if _owner(code) == "google" else merchant).update(rec["offers"])
    return len(merchant - google), len(google)


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
        foreign = sorted(rec["labels"] - {OUR_FEED_LABEL})
        origin = ""
        if foreign and OUR_FEED_LABEL not in rec["labels"]:
            # Nothing in our feed is affected, so no feed change fixes it: the
            # offers exist because something else publishes them.
            origin = (" — not from our %s feed: feed label %s, currency %s"
                      % (OUR_FEED_LABEL, ",".join(foreign),
                         ",".join(sorted(rec["currencies"]))))
        out.append(Finding(
            severity, "gmc",
            "%s%s: %d product(s) %s in %s [%s] — %s (%s)%s"
            % (code, " [%s]" % attrs if attrs else "", n, impact.lower(),
               contexts, _scope(n, total), owner, offers, origin), SOURCE))

    out.append(Finding(INFO, "gmc", headline(summary), SOURCE))
    return out


def diagnose(summary, state_path=None):
    """Render the block *and* journal today's issues for tomorrow's delta.

    One call so a caller cannot render the section and forget to record the
    state it compares against — the delta is only ever as good as the last run
    that remembered to write it.
    """
    text = render_diagnosis(summary, delta(summary, load_state(state_path)))
    save_state(summary, state_path)
    return text


def render_diagnosis(summary, day_delta=None):
    """Human-readable root-cause block for the report body."""
    total = summary.get("total") or 0
    by_status = summary.get("by_status") or {}
    lines = ["  PRODUCT ELIGIBILITY (authoritative: Merchant API product_view)"]
    if not total:
        lines.append("    UNKNOWN — no rows returned")
        return "\n".join(lines)
    lines.append("    " + headline(summary))
    merchant, google = ownership(summary)
    lines.append("    Merchant-owned %d  |  Google/transient %d"
                 % (merchant, google))
    if day_delta:
        lines.append("    New issues today %d  |  Cleared since %s %d"
                     % (sum(len(v) for v in day_delta["new"].values()),
                        day_delta["since"],
                        sum(len(v) for v in day_delta["cleared"].values())))
        for code, offers in sorted(day_delta["new"].items()):
            lines.append("      + %-46s %s" % (code, ",".join(offers[:12])))
        for code, offers in sorted(day_delta["cleared"].items()):
            lines.append("      - %-46s %s" % (code, ",".join(offers[:12])))
    else:
        lines.append("    New/cleared: no previous run to compare with")
    lines.append("")
    for status in sorted(by_status):
        lines.append("    %-28s %d" % (status, by_status[status]))
    lines.append("")
    by_source = summary.get("by_source") or {}
    if by_source:
        lines.append("  WHERE THE OFFERS COME FROM (feed label / currency)")
        for (label, currency), count in sorted(by_source.items(),
                                              key=lambda kv: -kv[1]):
            ours = "ours" if label == OUR_FEED_LABEL else "NOT our feed"
            lines.append("    %-14s %-4s %5d  %s" % (label, currency, count,
                                                     ours))
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
            _scope(n, total), _owner(code), len(rec["countries"]),
            ",".join(sorted(rec["attributes"])) or "-"))
        lines.append("           feed=%s  currency=%s" % (
            ",".join(sorted(rec["labels"])) or "-",
            ",".join(sorted(rec["currencies"])) or "-"))
        # The SKUs, not only the count: for our own feed the offer id *is* the
        # product_code, so this is the work list. Autofeed offers carry Google's
        # invented ids, which is itself the finding.
        lines.append("           offers: %s" % ", ".join(rec["offers"]))
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
    print(diagnose(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

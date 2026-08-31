#!/usr/bin/env python3
"""Which SKUs an issue affects, and what about them differs from the rest.

The report has been saying "25 disapproved: shipping/currency mismatch" for
days, which is a count and not a lead: nobody can act on it without knowing
which product codes, in which feed label, at which price currency, against
which shipping service. So this prints the offers, and next to each the fields
the issue is about — the account's shipping services and the item's own
currency — so the common cause is visible rather than inferred.

Reads the same /etc/optiwar/gmc.env as the daily report; no credentials here.

    python3 reports/gmc_issue_triage.py                # every issue, grouped
    python3 reports/gmc_issue_triage.py shipping age_group
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports import gmc_client  # noqa: E402

# Everything the triage needs about an offer, not only its issues: the mismatch
# being diagnosed is between an item's currency and a shipping service, so the
# currency has to come back with the issue.
# product_view.id is mandatory in every query on this table; without it the API
# rejects the request rather than ignoring the omission.
TRIAGE_QUERY = (
    "SELECT product_view.id, product_view.offer_id, product_view.title, "
    "product_view.feed_label, product_view.language_code, "
    "product_view.price, product_view.availability, "
    "product_view.shipping_label, product_view.brand, "
    "product_view.product_type_l1, "
    "product_view.aggregated_reporting_context_status, "
    "product_view.item_issues FROM product_view"
)

# v1beta was switched off on 2026-02-28 and answers HTTP 200 with a
# discontinuation notice, which reads as an account with no shipping services
# rather than as an error.
SHIPPING_URL = ("https://merchantapi.googleapis.com/accounts/v1/"
                "accounts/%s/shippingSettings")
DATASOURCES_URL = ("https://merchantapi.googleapis.com/datasources/v1/"
                   "accounts/%s/dataSources")


def data_sources(env):
    """Where offers come from — the answer when a feed label nobody configured
    turns up in the catalogue."""
    requests, headers = gmc_client._authed_session(env)
    payload = requests.get(DATASOURCES_URL % env["GMC_ACCOUNT"],
                           headers=headers, timeout=60).json()
    out = []
    for ds in payload.get("dataSources", ()):
        primary = ds.get("primaryProductDataSource") or {}
        out.append((ds.get("displayName", "?"), ds.get("input", "?"),
                    primary.get("feedLabel") or "(none)",
                    ",".join(primary.get("countries", ()) or ())))
    return out, payload.get("error")


def _rows(env):
    """product_view rows, paged, with the triage projection."""
    requests, headers = gmc_client._authed_session(env)
    url = gmc_client.REPORTS_URL % env["GMC_ACCOUNT"]
    body = {"query": TRIAGE_QUERY, "pageSize": 250}
    rows, token, pages = [], None, 0
    while True:
        if token:
            body["pageToken"] = token
        payload = requests.post(url, headers=headers, json=body,
                                timeout=60).json()
        if "error" in payload:
            raise RuntimeError(payload["error"].get("message", "query failed"))
        rows += [r.get("productView") or {} for r in payload.get("results", ())]
        token = payload.get("nextPageToken")
        pages += 1
        if not token or pages >= gmc_client.MAX_PAGES:
            break
    return rows


def shipping_services(env):
    """The account's shipping services, as (name, currency, countries, rate)."""
    requests, headers = gmc_client._authed_session(env)
    payload = requests.get(SHIPPING_URL % env["GMC_ACCOUNT"],
                           headers=headers, timeout=60).json()
    out = []
    for svc in payload.get("services", ()):
        rate = ""
        for group in svc.get("rateGroups", ()):
            single = (group.get("singleValue") or {}).get("flatRate") or {}
            if single:
                rate = "%s %s" % (single.get("amountMicros", "0"),
                                  single.get("currencyCode", "?"))
        out.append((svc.get("serviceName", "?"),
                    svc.get("currencyCode", "?"),
                    ",".join(svc.get("deliveryCountries", ()) or ()),
                    "active" if svc.get("active") else "inactive",
                    rate))
    return out, payload.get("error")


def _price(view):
    price = view.get("price") or {}
    micros = price.get("amountMicros")
    try:
        amount = "%.2f" % (int(micros) / 1000000.0)
    except (TypeError, ValueError):
        amount = str(micros)
    return "%s %s" % (amount, price.get("currencyCode") or "?")


def by_issue(rows, wanted=()):
    """Offers grouped by issue code, keeping the fields the issue is about."""
    groups = defaultdict(list)
    for view in rows:
        for issue in view.get("itemIssues", ()) or ():
            kind = issue.get("type") or {}
            code = kind.get("code") or kind.get("canonicalAttribute") or "?"
            attribute = kind.get("canonicalAttribute") or ""
            if wanted and not any(w.lower() in (code + " " + attribute).lower()
                                  for w in wanted):
                continue
            severity = issue.get("severity") or {}
            countries = set()
            for ctx in severity.get("severityPerReportingContext", ()) or ():
                countries |= set(ctx.get("disapprovedCountries") or ())
                countries |= set(ctx.get("demotedCountries") or ())
            groups[code].append({
                "offer": view.get("offerId") or view.get("id"),
                "feed_label": view.get("feedLabel") or "",
                "lang": view.get("languageCode") or "",
                "price": _price(view),
                "availability": view.get("availability") or "",
                "shipping_label": view.get("shippingLabel") or "",
                "brand": view.get("brand") or "",
                "type": view.get("productTypeL1") or "",
                "status": view.get("aggregatedReportingContextStatus") or "",
                "attribute": attribute,
                "severity": severity.get("aggregatedSeverity") or "",
                "countries": ",".join(sorted(countries)),
                "resolution": issue.get("resolution") or "",
            })
    return groups


def main(argv):
    env = gmc_client.read_env()
    if not env.get("GMC_ACCOUNT"):
        print("no GMC credentials at %s" % gmc_client.GMC_ENV)
        return 2
    services, err = shipping_services(env)
    print("SHIPPING SERVICES (account %s)" % env["GMC_ACCOUNT"])
    if err:
        print("  unreadable: %s" % err.get("message"))
    for name, currency, countries, active, rate in services:
        print("  %-28s %-4s %-8s %s  %s"
              % (name, currency, active, rate, countries[:120]))
    if not services and not err:
        print("  none — every offer then lacks a matching service")

    sources, ds_err = data_sources(env)
    print("\nDATA SOURCES")
    if ds_err:
        print("  unreadable: %s" % ds_err.get("message"))
    for name, kind, label, countries in sources:
        print("  %-24s input=%-10s feed_label=%-12s %s"
              % (name[:24], kind, label, countries[:100]))

    rows = _rows(env)
    print("\n%d offers in product_view" % len(rows))
    currencies = defaultdict(int)
    for view in rows:
        currencies["%s/%s" % ((view.get("price") or {}).get("currencyCode"),
                              view.get("feedLabel"))] += 1
    print("offers by price currency / feed label:")
    for key, count in sorted(currencies.items(), key=lambda kv: -kv[1]):
        print("  %-16s %d" % (key, count))

    groups = by_issue(rows, argv)
    for code, offers in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print("\n== %s — %d offer(s)" % (code, len(offers)))
        for o in sorted(offers, key=lambda o: str(o["offer"])):
            print("   %-14s feed=%-10s %-11s %-27s %-9s %-14s %s"
                  % (o["offer"], o["feed_label"], o["price"], o["status"],
                     o["severity"], o["resolution"], o["countries"][:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

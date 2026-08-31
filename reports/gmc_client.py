#!/usr/bin/env python3
"""Merchant API access for the GMC report section.

Isolated from the classification logic so that logic stays unit-testable
without credentials or network. Reads the same /etc/optiwar/gmc.env the
existing section uses; no secrets are held in the repo.

Uses the Merchant API reporting view rather than the Content API v2.1
``productstatuses`` rollup: the v2.1 ``destinationStatuses[].status`` field
reports ``disapproved`` for products that the same payload shows as approved in
35 countries, which is what produced the "710/710 disapproved" line.
"""
import os

GMC_ENV = os.environ.get("GMC_ENV_FILE", "/etc/optiwar/gmc.env")
REPORTS_URL = ("https://merchantapi.googleapis.com/reports/v1/accounts/%s"
               "/reports:search")
SCOPE = "https://www.googleapis.com/auth/content"

# feed_label and price come back with the issues on purpose: a currency mismatch
# cannot be diagnosed without the offer's currency, and an issue on an offer this
# account never submitted (Google's automatic crawl publishes under its own feed
# label) is a different problem from a defect in our feed.
PRODUCT_VIEW_QUERY = (
    "SELECT product_view.id, product_view.offer_id, product_view.title, "
    "product_view.feed_label, product_view.price, "
    "product_view.aggregated_reporting_context_status, "
    "product_view.item_issues FROM product_view"
)

# A runaway-pagination guard, not a catalogue limit: 20 pages x 250 rows. If it
# is ever reached the answer is incomplete, and an incomplete catalogue must be
# reported as unknown rather than rolled up as though it were the whole one.
MAX_PAGES = 20


class IncompleteCatalogue(RuntimeError):
    """Pagination stopped before the catalogue was exhausted."""


def read_env(path=None):
    env = {}
    try:
        with open(path or GMC_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception:  # noqa: BLE001 - caller reports UNKNOWN, not healthy
        pass
    return env


def _authed_session(env):
    import requests
    from google.oauth2 import service_account
    import google.auth.transport.requests as gt

    creds = service_account.Credentials.from_service_account_file(
        env["GMC_SA_KEY"], scopes=[SCOPE])
    creds.refresh(gt.Request())
    return requests, {"Authorization": "Bearer " + creds.token,
                      "Content-Type": "application/json"}


def fetch_product_views(env=None):
    """All product_view rows for the account (paged)."""
    env = env or read_env()
    account = env["GMC_ACCOUNT"]
    requests, headers = _authed_session(env)
    url = REPORTS_URL % account
    body = {"query": PRODUCT_VIEW_QUERY, "pageSize": 250}
    rows, token, pages = [], None, 0
    while True:
        if token:
            body["pageToken"] = token
        payload = requests.post(url, headers=headers, json=body,
                                timeout=60).json()
        if "error" in payload:
            raise RuntimeError(payload["error"].get("message", "query failed"))
        for row in payload.get("results", ()):
            rows.append(row.get("productView") or {})
        token = payload.get("nextPageToken")
        pages += 1
        if not token:
            break
        if pages >= MAX_PAGES:
            raise IncompleteCatalogue(
                "stopped after %d pages (%d products) with more remaining — "
                "an eligibility rollup over part of the catalogue would "
                "understate disapprovals" % (pages, len(rows)))
    return rows

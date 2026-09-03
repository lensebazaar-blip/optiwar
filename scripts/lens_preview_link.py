#!/usr/bin/env python3
"""Issue an owner-preview link for one unreleased contact lens.

Run on the production box with the service's environment, so the link is
signed with the secret the server verifies against:

    set -a; . /etc/optiwar/optiwar-secrets.env; set +a
    python3 scripts/lens_preview_link.py --product-id 1015 \
        --slug precision1-daily-disposable-contact-lenses--30-pack --hours 72

The link opens the lens's page on optiwar.com for whoever holds it, for the
stated hours, and nothing else: see ``lens_preview.py``.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lens_preview  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--product-id", type=int, required=True)
    parser.add_argument("--slug", required=True,
                        help="products.product_slug of the lens")
    parser.add_argument("--category", default="contact-lenses",
                        help="products.product_category, slugified")
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument("--base", default="https://optiwar.com")
    args = parser.parse_args(argv)
    key = lens_preview.secret(os.environ)
    if not key:
        sys.exit("no LENS_PREVIEW_SECRET or real SECRET_KEY in the environment")
    token = lens_preview.issue(key, args.product_id, args.hours)
    print("%s/categories/%s/%s?pid=%d&preview=%s"
          % (args.base.rstrip("/"), args.category, args.slug,
             args.product_id, token))
    return 0


if __name__ == "__main__":
    sys.exit(main())

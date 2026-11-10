#!/usr/bin/env python3
"""Pre-deploy proof for the prescription reader (Find, Read, Compare). Not deployed.

Run on a host with live keys (ANTHROPIC_API_KEY in the environment), with the
directory that holds the ``flaskr`` package on PYTHONPATH:

    python rx_reader_proof.py --pdf /path/file.pdf --expected /path/expected.json --runs 10

Neither file belongs in the repo. It prints PASS, AMBER or FAIL per field and
never a value. ``expected.json`` (values as written on the paper, "" if none):

    {"choices": [4, 1],
     "pages": {"1": {"right": {"sph": "", "cyl": "", "axis": "", "add": ""},
                     "left":  {"sph": "", "cyl": "", "axis": "", "add": ""}},
               "4": {...}},
     "forbidden": {"cyl": [""], "axis": [""]}}

Add ``--find-only`` to repeat just the sorting of pages, which is quick and cheap
enough to run many times. Pages the two Find readers sorted differently are
printed by kind; ``second=none`` means the second look did not answer in time.

``forbidden`` lists the values on the contact-lens note, per field; none of them
may be filled in or offered. Exit status 0 only if every rule passes. Calls
outside a request, so it uses no customer's limit and adds nothing to the Ops tile.
"""
import argparse
import json
import statistics
import sys
import time

from flask import Flask

from flaskr import content_guard, lens_documents, rx_photo

BUDGET = content_guard.BUDGET


def expected_option(field, value):
    return rx_photo._select_value(field, value)[0] if value else ""


def judge(run, page, expected, forbidden, eyes, check):
    """(failures, ambers): printed per field, never with a value."""
    failures = ambers = 0
    amber = {(c["eye"], c["field"]): c.get("options") or [] for c in check}
    for eye in rx_photo.EYES:
        for field in rx_photo.FIELDS:
            want = expected_option(field, expected[eye].get(field, ""))
            got = ((eyes or {}).get(eye) or {}).get(field, "")
            options = amber.get((eye, field))
            banned = {expected_option(field, v) for v in forbidden.get(field, []) if v} - {want}
            seen = set(options or []) | ({got} if got else set())
            if seen & banned:
                result = "FAIL-contact-lens-note"
            elif options is not None and (not got or got == want):
                result = "AMBER"
            elif got == want or (field == "cyl" and not want and got == "0.00") or (field == "axis" and not want and got == "0"):
                result = "PASS"
            else:
                result = "FAIL"
            failures += result.startswith("FAIL")
            ambers += result == "AMBER"
            print("run=%d page=%s %s.%s %s" % (run, page, eye, field, result))
    return failures, ambers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--find-only", action="store_true")
    args = parser.parse_args()
    data = open(args.pdf, "rb").read()
    wanted = json.load(open(args.expected, encoding="utf-8"))
    rx_photo._count = lambda provider: None
    app = Flask("rx_reader_proof")
    bad, times = 0, []
    with app.app_context():
        norm = lens_documents.normalise(data, pdf_pages=rx_photo.READER_PAGES)
        for run in range(1, args.runs + 1):
            started = time.monotonic()
            rows, _by = rx_photo.find_pages(norm["pages"])
            found = time.monotonic() - started
            tables = rx_photo.tables_newest_first(rows)
            order = [r["page"] for r in tables]
            ok = order == wanted["choices"]
            bad += not ok
            print("run=%d choice-order %s" % (run, "PASS" if ok else "FAIL"))
            answered = any(r["second"] for r in rows)
            if not answered:
                print("run=%d find second=none" % run)
            for r in rows if answered else []:
                if r["first"] != r["second"]:
                    print("run=%d find page=%d first=%s second=%s used=%s" % (run, r["page"], r["first"] or "none", r["second"] or "none", r["kind"]))
            slowest = found
            for row in ([] if args.find_only else tables):
                key = str(row["page"])
                if key not in wanted["pages"]:
                    print("run=%d page=%s not-expected FAIL" % (run, key))
                    bad += 1
                    continue
                begun = time.monotonic()
                jpeg = rx_photo._turned(lens_documents.pdf_page_jpeg(data, row["page"] - 1), row["rotate"])
                eyes, check, _notes, readers = rx_photo.read_page(jpeg, BUDGET - found)
                took = time.monotonic() - begun
                slowest = max(slowest, found + took)
                failures, ambers = judge(run, key, wanted["pages"][key], wanted.get("forbidden", {}), eyes, check)
                bad += failures
                print("run=%d page=%s readers=%d amber=%d find=%.1fs read=%.1fs" % (run, key, len(readers), ambers, found, took))
            times.append(slowest)
    worst = max(times)
    print("time p50=%.1fs worst=%.1fs budget=%ds %s" % (statistics.median(times), worst, BUDGET, "PASS" if worst <= BUDGET else "FAIL"))
    bad += worst > BUDGET
    print("RESULT %s" % ("PASS" if not bad else "FAIL (%d)" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

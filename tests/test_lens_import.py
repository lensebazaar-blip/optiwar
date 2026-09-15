"""PR-D2: the Ops lens import console.

A lens enters the catalogue through one path: a JSON payload is parsed by the
same deterministic validator the CLI uses, a model may point at what looks
wrong but writes nothing, a human confirms one product in one transaction and
it lands hidden (``merchant_enabled=0``); release is a separate act behind the
shared readiness gate, and withdrawal never deletes. Every route is behind the
Ops gate and every step is an append-only log row.

Unit tests need no database. ``ConsoleRoutes`` proves the routes against the
CI MariaDB and is skipped without one.
"""
import csv
import json
import os
import sys
import tempfile
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests import test_lens_upload as harness  # noqa: E402

import cl_import  # noqa: E402
import lens_feed  # noqa: E402
import lens_import  # noqa: E402
import lens_import_schema  # noqa: E402

PRECISION1 = os.path.join(REPO, "lens_data", "PRECISION1")


def _csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def precision1_payload():
    (product,) = _csv(os.path.join(PRECISION1, "products.csv"))
    return {"product": product,
            "rules": _csv(os.path.join(PRECISION1, "rules.csv"))}


def toric_payload(ref="LB-2001", **over):
    product = {
        "source_ref": ref, "manufacturer": "CooperVision", "brand": "MyDay",
        "product_name": "MyDay Toric 30 Pack", "modality": "Daily",
        "lens_type": "Toric", "pack_quantity": "30", "material": "stenfilcon A",
        "water_content": "54", "replacement_days": "1",
        "availability": "IN_STOCK", "price_eur": "39.90",
        "image_url": "https://x/myday-toric.jpg",
        "description": "Daily toric lens.", "min_boxes_single_eye": "4",
        "min_boxes_both_per_eye": "2",
    }
    product.update(over)
    variants = [
        {"source_ref": ref, "sph": sph, "cyl": "-1.25", "axis": "180",
         "base_curve": "8.6", "diameter": "14.5"}
        for sph in ("-1.00", "-1.25", "-1.50")]
    return {"product": product, "variants": variants}


# ---------------------------------------------------------------------------
# the validator is the CLI's validator
# ---------------------------------------------------------------------------

class ParseTests(unittest.TestCase):
    def test_precision1_parses_to_exactly_what_the_cli_imports(self):
        payload = precision1_payload()
        via_cli, errors = cl_import.parse([payload["product"]], [],
                                          payload["rules"])
        self.assertEqual(errors, [])
        via_console, report = lens_import.validate(payload)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(json.dumps(via_console, sort_keys=True, default=str),
                         json.dumps(via_cli[0], sort_keys=True, default=str))
        self.assertEqual(report["identity"]["merchant_enabled"], 0)
        self.assertEqual(report["identity"]["sell_on"],
                         {"sell_on_com": 1, "sell_on_in": 0})
        self.assertEqual(report["identity"]["product_code"], "CL-PRECISION1")

    def test_a_matrix_is_stated_once(self):
        payload = toric_payload()
        payload["rules"] = [{"source_ref": "LB-2001", "parameter": "sph",
                             "value": "-1.00"}]
        with self.assertRaises(lens_import.ImportRefused) as ctx:
            lens_import.validate(payload)
        self.assertEqual(ctx.exception.code, "payload_shape")

    def test_a_validator_error_is_a_rejected_report_not_a_product(self):
        payload = toric_payload(price_eur="-1")
        product, report = lens_import.validate(payload)
        self.assertIsNone(product)
        self.assertFalse(report["ok"])
        self.assertTrue(report["errors"])

    def test_a_gtin_without_its_power_is_warned_at_parse(self):
        product, report = lens_import.validate(
            toric_payload(gtin="5060502210012"))
        self.assertIsNotNone(product)
        self.assertTrue(any("gtin_reference_power" in w["reason"]
                            for w in report["warnings"]))

    def test_a_reference_power_without_a_gtin_is_refused(self):
        product, report = lens_import.validate(
            toric_payload(gtin_reference_power="-3.00"))
        self.assertIsNone(product)


class ReviewTests(unittest.TestCase):
    def test_the_model_is_told_it_writes_nothing(self):
        prompt = lens_import.REVIEW_PROMPT.lower()
        for word in ("do not", "finding"):
            self.assertIn(word, prompt)

    def test_only_findings_survive_the_answer(self):
        text = json.dumps({
            "findings": [{"severity": "warn", "field": "water_content",
                          "message": "chart says 54%"}],
            "corrected_product": {"water_content": "54"},
            "price_eur": "12.00",
        })
        findings = lens_import.parse_findings(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(sorted(findings[0]), ["field", "message", "severity"])
        self.assertNotIn("corrected_product", json.dumps(findings))

    def test_prose_is_one_info_note_about_no_field(self):
        (note,) = lens_import.parse_findings("Looks fine to me.")
        self.assertEqual((note["severity"], note["field"]), ("INFO", ""))


# ---------------------------------------------------------------------------
# the feed's identifier rules
# ---------------------------------------------------------------------------

class FeedGuardTests(unittest.TestCase):
    def test_a_gtin_needs_the_power_it_was_read_from(self):
        gtin, _ = lens_feed.lens_identifiers({"gtin": "5060502210012"})
        self.assertEqual(gtin, "")
        gtin, _ = lens_feed.lens_identifiers(
            {"gtin": "5060502210012", "gtin_reference_power": "-3.00"})
        self.assertEqual(gtin, "5060502210012")

    def test_our_product_code_is_never_the_mpn(self):
        _, mpn = lens_feed.lens_identifiers(
            {"product_code": "CL-MYDAY", "manufacturer_mpn": "cl-myday"})
        self.assertEqual(mpn, "")
        _, mpn = lens_feed.lens_identifiers(
            {"product_code": "CL-MYDAY", "manufacturer_mpn": "MDT-30"})
        self.assertEqual(mpn, "MDT-30")

    def test_south_korea_is_excluded(self):
        self.assertIn("KR", lens_feed.EXCLUDED_COUNTRIES)
        self.assertFalse(lens_feed.lens_ships_to("kr"))
        self.assertTrue(lens_feed.lens_ships_to("DE"))

    def test_the_offer_says_identifier_exists_false_without_a_real_one(self):
        row = dict(harness.LENS) if hasattr(harness, "LENS") else {}
        row.update({"gtin": "5060502210012", "gtin_reference_power": None,
                    "manufacturer_mpn": None, "product_code": "CL-X",
                    "release_blockers": (), "merchant_enabled": 1,
                    "product_price_eur": "26.95", "product_image": "x.jpg",
                    "product_slug": "x", "product_id": 1, "brand": "Alcon",
                    "product_name": "X", "availability": "IN_STOCK",
                    "images": [{"image_url": "x.jpg", "gmc_eligible": 1,
                                "image_type": "PRIMARY"}]})
        xml = lens_feed.lens_item_xml(row, "https://optiwar.com")
        self.assertNotIn("<g:gtin>", xml)
        self.assertNotIn("<g:mpn>", xml)
        self.assertIn("<g:identifier_exists>false</g:identifier_exists>", xml)


# ---------------------------------------------------------------------------
# wiring contracts
# ---------------------------------------------------------------------------

ROUTES = (
    ("POST", "/api/ops/lenses/import/parse"),
    ("POST", "/api/ops/lenses/import/review"),
    ("GET", "/api/ops/lenses/import/preview/1"),
    ("POST", "/api/ops/lenses/import/confirm"),
    ("POST", "/api/ops/lenses/import/withdraw"),
    ("POST", "/api/ops/lenses/release"),
    ("GET", "/api/ops/lenses/import/log"),
)


class WiringTests(unittest.TestCase):
    def _read(self, *parts):
        with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_the_console_schema_is_part_of_the_lens_schema(self):
        import contact_lens
        names = [name for name, _ in contact_lens.TABLES]
        for name, _ in lens_import_schema.TABLES:
            self.assertIn(name, names)
        self.assertIn("contact_lens_import_staging", names)
        self.assertIn("contact_lens_import_log", names)

    def test_the_deploy_set_carries_the_console(self):
        deploy = self._read("deploy", "deploy.py")
        for name in ("lens_import.py", "lens_import_write.py",
                     "lens_import_schema.py", "cl_import.py",
                     "image_pipeline.py"):
            self.assertIn('"%s"' % name, deploy)

    def test_every_route_checks_the_ops_gate_first(self):
        src = self._read("lens_upload.py")
        for _, path in ROUTES:
            rule = path.replace("/1", "/<int:staging_id>")
            at = src.index('"%s"' % rule)
            body = src[at:at + 400]
            self.assertIn("if not _ops_auth():", body, rule)
            self.assertIn("401", body, rule)

    def test_the_writer_never_touches_merchant_enabled(self):
        src = self._read("lens_import_write.py")
        self.assertNotRegex(src, r'"merchant_enabled"')
        self.assertNotRegex(src, r"merchant_enabled\s*=")


# ---------------------------------------------------------------------------
# the routes, against MariaDB
# ---------------------------------------------------------------------------

PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS products (
    product_id       INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_code     VARCHAR(40) NOT NULL,
    product_name     VARCHAR(200) NOT NULL,
    product_category VARCHAR(80) NOT NULL DEFAULT 'Contact Lenses',
    product_slug     VARCHAR(200) NULL,
    product_status   VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    product_quantity INT NOT NULL DEFAULT 0,
    show_in_listings TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB
"""

# Other suites create ``products`` with the columns they need; the writer
# needs these, so they are added where absent (MariaDB IF NOT EXISTS).
PRODUCT_COLUMNS = (
    ("product_details", "TEXT NULL"),
    ("product_price_eur", "DECIMAL(10,2) NULL"),
    ("product_special_price_eur", "DECIMAL(10,2) NULL"),
    ("product_price", "INT NULL"),
    ("product_special_price", "INT NULL"),
    ("product_image", "VARCHAR(500) NULL"),
    ("product_category", "VARCHAR(80) NOT NULL DEFAULT 'Contact Lenses'"),
)

REF = "OPS-T-2001"


@unittest.skipUnless(harness.AVAILABLE,
                     "no MariaDB test database (see scripts/setup_test_db.sh)")
class ConsoleRoutes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        cls.db = harness._connect()
        cls.authorised = [False]
        cls.site = ["optiwar.com"]
        cls.answers = []
        cls.calls = []

        def call_model(**kw):
            cls.calls.append(kw)
            answer = cls.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            resp = types.SimpleNamespace(model="gpt-4o-test")
            resp.choices = [types.SimpleNamespace(
                message=types.SimpleNamespace(content=answer))]
            return resp

        cls.up, cls.docs = harness._load_upload(
            lambda: cls.db, lambda: cls.authorised[0], call_model, cls.site)
        cls.pkg = sys.modules["lu_pkg"]
        cls.tmp = tempfile.mkdtemp(prefix="owimport")
        os.makedirs(os.path.join(cls.tmp, "app"))
        cls.app = Flask(__name__, root_path=os.path.join(cls.tmp, "app"))
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        bp = Blueprint("main", __name__)
        cls.up.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()
        cursor = cls.db.cursor()
        cursor.execute(PRODUCTS_DDL)
        for name, ddl in PRODUCT_COLUMNS:
            cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS"
                           " %s %s" % (name, ddl))
        cursor.execute("ALTER TABLE products MODIFY product_id INT NOT NULL"
                       " AUTO_INCREMENT")
        cursor.execute("ALTER TABLE products MODIFY product_category"
                       " VARCHAR(80) NOT NULL DEFAULT 'Contact Lenses'")
        cls.pkg.contact_lens._SCHEMA_READY = False
        cls.pkg.contact_lens.ensure_schema(cursor)
        cls.db.commit()
        os.environ["LENS_PREVIEW_SECRET"] = "preview-secret-for-tests"

    @classmethod
    def tearDownClass(cls):
        cls._clean()
        cls.db.close()
        os.environ.pop("LENS_PREVIEW_SECRET", None)

    @classmethod
    def _clean(cls):
        cursor = cls.db.cursor()
        cursor.execute("SELECT product_id FROM contact_lens_products WHERE"
                       " source_system=%s AND source_ref LIKE %s",
                       (cls.pkg.cl_import.SOURCE_SYSTEM
                        if hasattr(cls.pkg.cl_import, "SOURCE_SYSTEM")
                        else "lensbazaar", "OPS-T-%"))
        pids = [r["product_id"] for r in cursor.fetchall()]
        for pid in pids:
            for table in ("contact_lens_variants", "contact_lens_param_rules",
                          "contact_lens_configs", "contact_lens_rule_sets",
                          "contact_lens_aliases", "contact_lens_specs",
                          "contact_lens_images", "contact_lens_products"):
                cursor.execute("DELETE FROM %s WHERE product_id=%%s" % table,
                               (pid,))
            cursor.execute("DELETE FROM products WHERE product_id=%s", (pid,))
        cursor.execute("DELETE FROM contact_lens_import_log WHERE"
                       " source_ref LIKE 'OPS-T-%' OR staging_id IN (SELECT"
                       " staging_id FROM contact_lens_import_staging WHERE"
                       " source_ref LIKE 'OPS-T-%')")
        cursor.execute("DELETE FROM contact_lens_import_staging WHERE"
                       " source_ref LIKE 'OPS-T-%'")
        cls.db.commit()

    def setUp(self):
        self._clean()
        self.authorised[0] = True
        del self.answers[:]
        del self.calls[:]

    # -- helpers ---------------------------------------------------------

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body),
                                content_type="application/json")

    def _stage(self, payload=None):
        r = self._post("/api/ops/lenses/import/parse",
                       payload or toric_payload(REF))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["staging"]

    def _confirm(self, staging_id, by="ops.tester"):
        return self._post("/api/ops/lenses/import/confirm",
                          {"staging_id": staging_id, "by": by, "confirm": True})

    def _profile(self, product_id):
        cursor = self.db.cursor()
        cursor.execute("SELECT * FROM contact_lens_products WHERE product_id=%s",
                       (product_id,))
        return cursor.fetchone()

    def _count(self, table, product_id, where="1=1"):
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM %s WHERE product_id=%%s AND %s"
                       % (table, where), (product_id,))
        return cursor.fetchone()["n"]

    def _log(self, **filters):
        query = "&".join("%s=%s" % kv for kv in filters.items())
        r = self.client.get("/api/ops/lenses/import/log?" + query)
        self.assertEqual(r.status_code, 200)
        return r.get_json()["log"]

    # -- auth --------------------------------------------------------------

    def test_anonymous_and_wrong_token_get_401_on_every_route(self):
        self.authorised[0] = False
        for method, path in ROUTES:
            for headers in ({}, {"Authorization": "Bearer wrong-token"}):
                r = self.client.open(path, method=method, headers=headers,
                                     data="{}", content_type="application/json")
                self.assertEqual(r.status_code, 401, (method, path, headers))
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM contact_lens_import_log"
                       " WHERE source_ref LIKE 'OPS-T-%'")
        self.assertEqual(cursor.fetchone()["n"], 0)
        cursor.execute("SELECT COUNT(*) AS n FROM contact_lens_import_staging"
                       " WHERE source_ref LIKE 'OPS-T-%'")
        self.assertEqual(cursor.fetchone()["n"], 0)

    # -- parse -------------------------------------------------------------

    def test_parse_stages_and_logs_but_writes_no_product(self):
        staged = self._stage()
        self.assertEqual(staged["status"], "STAGED")
        self.assertTrue(staged["report"]["ok"])
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM contact_lens_products WHERE"
                       " source_ref=%s", (REF,))
        self.assertEqual(cursor.fetchone()["n"], 0)
        (entry,) = self._log(staging_id=staged["staging_id"])
        self.assertEqual((entry["action"], entry["outcome"]), ("PARSE", "OK"))

    def test_a_rejected_payload_is_kept_as_rejected_and_cannot_be_confirmed(self):
        staged = self._stage(toric_payload(REF, price_eur="-5"))
        self.assertEqual(staged["status"], "REJECTED")
        r = self._confirm(staged["staging_id"])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "not_stageable")

    def test_a_non_object_body_is_refused(self):
        r = self.client.post("/api/ops/lenses/import/parse", data="[1,2]",
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    # -- review ------------------------------------------------------------

    def test_review_returns_findings_only_and_changes_nothing(self):
        staged = self._stage()
        self.answers.append(json.dumps({
            "findings": [{"severity": "warn", "field": "water_content",
                          "message": "MyDay is 54%: matches"}],
            "corrected_product": {"price_eur": "1.00"}}))
        r = self._post("/api/ops/lenses/import/review",
                       {"staging_id": staged["staging_id"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        review = r.get_json()["review"]
        self.assertFalse(review["authoritative"])
        self.assertEqual(len(review["findings"]), 1)
        cursor = self.db.cursor()
        cursor.execute("SELECT payload_sha256, status FROM"
                       " contact_lens_import_staging WHERE staging_id=%s",
                       (staged["staging_id"],))
        row = cursor.fetchone()
        self.assertEqual(row["payload_sha256"], staged["payload_sha256"])
        self.assertEqual(row["status"], "STAGED")
        self.assertEqual(len(self.calls), 1)

    def test_a_model_outage_is_a_502_not_a_write(self):
        staged = self._stage()
        self.answers.append(self.pkg.ai_client.ModelUnavailable("down"))
        r = self._post("/api/ops/lenses/import/review",
                       {"staging_id": staged["staging_id"]})
        self.assertEqual(r.status_code, 502)

    # -- confirm -----------------------------------------------------------

    def test_confirm_writes_one_hidden_product_in_one_transaction(self):
        staged = self._stage()
        r = self._confirm(staged["staging_id"])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        result = r.get_json()["result"]
        pid = result["product_id"]
        self.assertTrue(result["created"])
        profile = self._profile(pid)
        self.assertEqual(profile["merchant_enabled"], 0)
        self.assertEqual(self._count("contact_lens_variants", pid,
                                     "available=1"), 3)
        cursor = self.db.cursor()
        cursor.execute("SELECT sell_on_com, sell_on_in, product_code FROM"
                       " products WHERE product_id=%s", (pid,))
        row = cursor.fetchone()
        self.assertEqual((row["sell_on_com"], row["sell_on_in"]), (1, 0))
        self.assertEqual(row["product_code"], "CL-" + REF)
        self.assertNotEqual(profile["manufacturer_mpn"], row["product_code"])
        actions = [(e["action"], e["outcome"]) for e in self._log(product_id=pid)]
        self.assertIn(("CONFIRM", "OK"), actions)

    def test_confirm_needs_a_named_human_and_an_explicit_yes(self):
        staged = self._stage()
        r = self._post("/api/ops/lenses/import/confirm",
                       {"staging_id": staged["staging_id"], "by": "ops"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["code"], "confirm_required")
        r = self._confirm(staged["staging_id"], by="")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "actor_required")
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM contact_lens_products WHERE"
                       " source_ref=%s", (REF,))
        self.assertEqual(cursor.fetchone()["n"], 0)

    def test_the_same_payload_confirmed_twice_writes_once(self):
        first = self._confirm(self._stage()["staging_id"]).get_json()["result"]
        second = self._confirm(self._stage()["staging_id"]).get_json()["result"]
        self.assertEqual(second["product_id"], first["product_id"])
        self.assertTrue(second["already_confirmed"])
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM contact_lens_products WHERE"
                       " source_ref=%s", (REF,))
        self.assertEqual(cursor.fetchone()["n"], 1)
        self.assertEqual(self._count("contact_lens_variants",
                                     first["product_id"]), 3)

    def test_a_changed_payload_for_the_same_ref_updates_not_duplicates(self):
        first = self._confirm(self._stage()["staging_id"]).get_json()["result"]
        second = self._confirm(self._stage(
            toric_payload(REF, price_eur="41.00"))["staging_id"]).get_json()["result"]
        self.assertEqual(second["product_id"], first["product_id"])
        self.assertFalse(second["created"])
        cursor = self.db.cursor()
        cursor.execute("SELECT product_price_eur FROM products WHERE product_id=%s",
                       (first["product_id"],))
        self.assertEqual(str(cursor.fetchone()["product_price_eur"]), "41.00")

    def test_a_failing_write_rolls_the_whole_product_back(self):
        staged = self._stage()
        real = self.pkg.lens_import_write.upsert_variants

        def boom(*a, **k):
            raise RuntimeError("disk on fire")

        self.pkg.lens_import_write.upsert_variants = boom
        try:
            with self.assertRaises(RuntimeError):
                self.pkg.lens_import.confirm(self.db, staged["staging_id"],
                                             "ops.tester")
        finally:
            self.pkg.lens_import_write.upsert_variants = real
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM contact_lens_products WHERE"
                       " source_ref=%s", (REF,))
        self.assertEqual(cursor.fetchone()["n"], 0)
        cursor.execute("SELECT COUNT(*) AS n FROM products WHERE product_code=%s",
                       ("CL-" + REF,))
        self.assertEqual(cursor.fetchone()["n"], 0)
        cursor.execute("SELECT status FROM contact_lens_import_staging WHERE"
                       " staging_id=%s", (staged["staging_id"],))
        self.assertEqual(cursor.fetchone()["status"], "STAGED")
        outcomes = [e["outcome"] for e in self._log(staging_id=staged["staging_id"])]
        self.assertIn("FAILED", outcomes)

    # -- preview / release / withdraw --------------------------------------

    def test_preview_is_signed_for_the_hidden_product_only(self):
        staged = self._stage()
        pid = self._confirm(staged["staging_id"]).get_json()["result"]["product_id"]
        r = self.client.get("/api/ops/lenses/import/preview/%d"
                            % staged["staging_id"])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        url = r.get_json()["preview"]["url"]
        self.assertIn("pid=%d" % pid, url)
        token = url.rsplit("preview=", 1)[1]
        self.assertTrue(self.pkg.lens_preview.verify(
            os.environ["LENS_PREVIEW_SECRET"], pid, token))
        self.assertFalse(self.pkg.lens_preview.verify(
            os.environ["LENS_PREVIEW_SECRET"], pid + 1, token))
        self.assertEqual(self._profile(pid)["merchant_enabled"], 0)

    def test_preview_of_an_unconfirmed_row_is_refused(self):
        staged = self._stage()
        r = self.client.get("/api/ops/lenses/import/preview/%d"
                            % staged["staging_id"])
        self.assertEqual(r.status_code, 409)

    def test_release_is_separate_and_gated(self):
        staged = self._stage()
        pid = self._confirm(staged["staging_id"]).get_json()["result"]["product_id"]
        self.assertEqual(self._profile(pid)["merchant_enabled"], 0)
        r = self._post("/api/ops/lenses/release", {"product_id": pid, "by": "owner"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._profile(pid)["merchant_enabled"], 1)
        cursor = self.db.cursor()
        live_com = self.pkg.catalogue.live_lenses(cursor, "optiwar.com")
        self.assertIn(pid, [r["product_id"] for r in live_com])
        live_in = self.pkg.catalogue.live_lenses(cursor, "in.optiwar.com")
        self.assertEqual([r for r in live_in if r["product_id"] == pid], [])

    def test_release_refuses_a_gtin_without_its_reference_power(self):
        staged = self._stage(toric_payload(REF, gtin="5060502210012"))
        pid = self._confirm(staged["staging_id"]).get_json()["result"]["product_id"]
        r = self._post("/api/ops/lenses/release", {"product_id": pid, "by": "owner"})
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["code"], "not_ready")
        self.assertIn("gtin without gtin_reference_power",
                      body["detail"]["release_blockers"])
        self.assertEqual(self._profile(pid)["merchant_enabled"], 0)
        self.assertIn(("RELEASE", "REFUSED"),
                      [(e["action"], e["outcome"]) for e in self._log(product_id=pid)])

    def test_withdraw_hides_and_keeps_every_row(self):
        staged = self._stage()
        pid = self._confirm(staged["staging_id"]).get_json()["result"]["product_id"]
        self._post("/api/ops/lenses/release", {"product_id": pid, "by": "owner"})
        r = self._post("/api/ops/lenses/import/withdraw",
                       {"product_id": pid, "by": "owner"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._profile(pid)["merchant_enabled"], 0)
        self.assertEqual(self._count("contact_lens_variants", pid), 3)
        self.assertEqual(self._count("contact_lens_variants", pid, "available=1"), 0)
        self.assertEqual(self._count("contact_lens_images", pid), 1)
        self.assertEqual(self._count("contact_lens_images", pid,
                                     "image_type='WITHDRAWN' AND gmc_eligible=0"), 1)
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM products WHERE product_id=%s", (pid,))
        self.assertEqual(cursor.fetchone()["n"], 1)
        self.assertNotIn(pid, [r["product_id"] for r in
                               self.pkg.catalogue.live_lenses(cursor, "optiwar.com")])

    def test_dot_in_sees_nothing_at_any_stage(self):
        staged = self._stage()
        cursor = self.db.cursor()
        self.assertEqual(self.pkg.catalogue.lens_rows(cursor, "in.optiwar.com"), [])
        pid = self._confirm(staged["staging_id"]).get_json()["result"]["product_id"]
        self.assertEqual(self.pkg.catalogue.lens_rows(cursor, "in.optiwar.com"), [])
        self._post("/api/ops/lenses/release", {"product_id": pid, "by": "owner"})
        self.assertEqual(self.pkg.catalogue.lens_rows(cursor, "in.optiwar.com"), [])

    def test_the_log_is_append_only_and_newest_first(self):
        staged = self._stage()
        self._confirm(staged["staging_id"])
        entries = self._log(staging_id=staged["staging_id"])
        self.assertEqual([e["action"] for e in entries], ["CONFIRM", "PARSE"])
        self.assertTrue(entries[0]["log_id"] > entries[1]["log_id"])


if __name__ == "__main__":
    unittest.main()

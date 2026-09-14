"""lens_rules / lens_config / lens_identity: a chart's tiers become one
immutable compiled configuration, read through a cache that is never truth.

Unit cases need no database. The ``OnMariaDB`` cases run against the CI
MariaDB (OPTIWAR_TEST_MYSQL_*) and are skipped without one; they prove the
publication is one transaction, the rows the runtime validates against are the
rows the config names, and an alias is one product's and one storefront's.
"""
import importlib.util
import json
import os
import sys
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lens_order = _load("lens_order")
lens_rules = _load("lens_rules")
lens_config = _load("lens_config")
lens_identity = _load("lens_identity")
lens_rx = _load("lens_rx")
contact_lens = _load("contact_lens")

ALCON = "Alcon Precision1 for Astigmatism parameter sheet 2026-01"

# Precision 1 for Astigmatism, as the chart states it: minus and low plus
# powers stocked, +0.25..+4.00 made to order in 8-10 weeks.
TORIC = {
    "lens_type": "TORIC",
    "base_curve": ["8.5"],
    "diameter": "14.5",
    "tiers": [
        {"sph": {"from": "-6.00", "to": "0.00", "step": "0.25"},
         "cyl": ["-0.75", "-1.25", "-1.75"],
         "axis": [10, 20, 90, 180]},
        {"sph": {"from": "0.25", "to": "4.00", "step": "0.25"},
         "cyl": ["-0.75", "-1.25", "-1.75"],
         "axis": [10, 20, 90, 180],
         "fulfilment": "MADE_TO_ORDER", "lead_time": "8-10 weeks",
         "source": ALCON},
    ],
}

SPHERICAL = {
    "lens_type": "SPHERICAL", "base_curve": "8.3", "diameter": "14.2",
    "tiers": [{"sph": {"from": "-4.00", "to": "-1.00", "step": "0.25"}}],
}


def _compile(rules, **kw):
    return lens_rules.compile_rules(rules, **kw)


class LeadTime(unittest.TestCase):
    def test_wordings_the_owner_confirmed(self):
        self.assertEqual(lens_rules.lead_time_days("8-10 weeks"), (56, 70))
        self.assertEqual(lens_rules.lead_time_days("Up to 45 days"), (None, 45))
        self.assertEqual(lens_rules.lead_time_days("Made to order - 6-8 weeks"),
                         (42, 56))
        self.assertEqual(lens_rules.lead_time_days("Ships within 3-5 days"),
                         (3, 5))

    def test_words_nobody_can_parse_keep_their_words_and_no_range(self):
        self.assertEqual(lens_rules.lead_time_days("on request"), (None, None))
        self.assertEqual(lens_rules.lead_time_days(""), (None, None))

    def test_a_range_that_runs_backwards_is_refused(self):
        with self.assertRaises(lens_rules.RuleError):
            lens_rules.lead_time_days("10-8 weeks")


class Compiler(unittest.TestCase):
    def test_tiers_compile_to_exact_combinations_with_their_fulfilment(self):
        out = _compile(TORIC, product_id=7, rule_version=1)
        rows = out["rows"]
        # 25 minus/zero powers + 16 plus powers, x3 cyl x4 axis.
        self.assertEqual(len(rows), (25 + 16) * 3 * 4)
        self.assertEqual(out["counts"]["made_to_order"], 16 * 3 * 4)
        by_sig = {lens_rules.signature(r): r for r in rows}
        stocked = by_sig["|8.50|-2.25|-1.25|90|"]
        self.assertEqual(stocked["fulfilment_status"], "STANDARD")
        self.assertIsNone(stocked["lead_time_text"])
        made = by_sig["|8.50|2.25|-1.25|90|"]
        self.assertEqual(made["fulfilment_status"], "MADE_TO_ORDER")
        self.assertEqual(made["lead_time_text"], "8-10 weeks")
        self.assertEqual((made["lead_time_min_days"],
                          made["lead_time_max_days"]), (56, 70))
        self.assertEqual(made["lead_time_source"], ALCON)
        self.assertEqual(made["diameter"], "14.50")

    def test_compilation_is_deterministic(self):
        a = _compile(TORIC, product_id=7, rule_version=1)["config"]
        b = _compile(json.loads(json.dumps(TORIC)), product_id=7,
                     rule_version=1)["config"]
        self.assertEqual(a, b)
        self.assertEqual(a["checksum"], lens_rules.checksum(a))
        # The checksum covers the rows: a changed combination is a changed config.
        c = dict(a, rows=list(a["rows"][1:]))
        self.assertNotEqual(lens_rules.checksum(c), a["checksum"])

    def test_config_rows_read_back_as_the_same_variants(self):
        out = _compile(TORIC, product_id=7, rule_version=1)
        back = lens_rules.rows_from_config(out["config"])
        self.assertEqual(len(back), len(out["rows"]))
        for want, got in zip(out["rows"], back):
            for k in ("base_curve", "sph", "cyl", "axis", "add_power",
                      "diameter", "fulfilment_status", "lead_time_text",
                      "lead_time_min_days", "lead_time_max_days"):
                self.assertEqual(got[k], want[k], k)

    def test_axis_specific_exception_inside_a_standard_range_needs_override(self):
        rules = json.loads(json.dumps(TORIC))
        rules["tiers"] = [rules["tiers"][0], {
            "sph": {"from": "-6.00", "to": "0.00", "step": "0.25"},
            "cyl": ["-1.75"], "axis": [20],
            "fulfilment": "MADE_TO_ORDER", "lead_time": "Up to 45 days",
            "source": "CooperVision MyDay toric chart 2026-01"}]
        with self.assertRaises(lens_rules.RuleError):
            _compile(rules)
        rules["tiers"][1]["override"] = True
        out = _compile(rules)
        by_sig = {lens_rules.signature(r): r for r in out["rows"]}
        self.assertEqual(by_sig["|8.50|-2.00|-1.75|20|"]["fulfilment_status"],
                         "MADE_TO_ORDER")
        self.assertEqual(by_sig["|8.50|-2.00|-1.75|20|"]["lead_time_max_days"],
                         45)
        self.assertEqual(by_sig["|8.50|-2.00|-1.75|10|"]["fulfilment_status"],
                         "STANDARD")
        self.assertEqual(out["counts"]["made_to_order"], 25)

    def test_made_to_order_without_lead_time_or_chart_is_refused(self):
        for missing in ("lead_time", "source"):
            rules = json.loads(json.dumps(TORIC))
            del rules["tiers"][1][missing]
            with self.assertRaises(lens_rules.RuleError, msg=missing):
                _compile(rules)

    def test_what_the_chart_did_not_say_clearly_is_refused(self):
        bad = [
            dict(SPHERICAL, tiers=[{"sph": {"from": "-4.00", "to": "-1.00",
                                            "step": "0.30"}}]),
            dict(SPHERICAL, tiers=[{"sph": ["-2.10"]}]),
            dict(SPHERICAL, tiers=[{"sph": ["-2.00"], "cyl": ["-0.75"]}]),
            dict(TORIC, tiers=[{"sph": ["-2.00"], "cyl": ["-0.75"]}]),
            dict(TORIC, tiers=[{"sph": ["-2.00"], "cyl": ["0.00"],
                                "axis": [90]}]),
            dict(TORIC, tiers=[{"sph": ["-2.00"], "cyl": ["-0.75"],
                                "axis": [190]}]),
            dict(SPHERICAL, diameter=["14.2", "14.5"]),
            dict(SPHERICAL, lens_type="BIFOCAL"),
            dict(SPHERICAL, tiers=[]),
            dict(SPHERICAL, tiers=[{"sph": ["-2.00"],
                                    "fulfilment": "SOMETIMES"}]),
            dict(SPHERICAL, tiers=[{"sph": ["-2.00"], "base_curve": "8.6"}]),
            dict(SPHERICAL, colors=[{"code": "BLUE"}]),
        ]
        for rules in bad:
            with self.assertRaises(lens_rules.RuleError, msg=rules):
                _compile(rules)

    def test_unavailable_tier_removes_what_it_names(self):
        rules = dict(SPHERICAL, tiers=SPHERICAL["tiers"] + [
            {"sph": ["-3.25"], "fulfilment": "UNAVAILABLE", "override": True}])
        out = _compile(rules)
        sphs = {r["sph"] for r in out["rows"]}
        self.assertNotIn("-3.25", sphs)
        self.assertEqual(len(sphs), 12)

    def test_a_colour_lens_states_its_colours_once_each(self):
        rules = {"lens_type": "COLOR", "base_curve": "8.6", "diameter": "14.2",
                 "colors": [{"code": "GRY", "name": "Grey"},
                            {"code": "BLU", "name": "Blue"}],
                 "tiers": [{"sph": ["0.00", "-1.00"]}]}
        out = _compile(rules)
        self.assertEqual(len(out["rows"]), 4)
        self.assertEqual(sorted({r["color_code"] for r in out["rows"]}),
                         ["BLU", "GRY"])
        rules["colors"].append({"code": "GRY", "name": "Grey again"})
        with self.assertRaises(lens_rules.RuleError):
            _compile(rules)


class Cascade(unittest.TestCase):
    """The compiled object drives SPH -> CYL -> AXIS with no recompilation."""

    def setUp(self):
        out = _compile(TORIC, product_id=7, rule_version=1)
        self.matrix = lens_order.Matrix(lens_rules.rows_from_config(
            out["config"]))

    def test_the_tree_narrows_colour_bc_sph_cyl_axis_in_that_order(self):
        opts = self.matrix.options()
        self.assertEqual(opts["base_curves"], ["8.50"])
        per_bc = opts["tree"][""]["8.50"]
        self.assertEqual(len(per_bc), 41)
        self.assertEqual(sorted(per_bc["2.00"]), ["-0.75", "-1.25", "-1.75"])
        self.assertEqual(per_bc["2.00"]["-1.75"]["axes"],
                         ["10", "20", "90", "180"])
        self.assertEqual(per_bc["2.00"]["-1.75"]["adds"], [])

    def test_only_the_exceptions_are_announced_by_exact_signature(self):
        ful = self.matrix.options()["fulfilment"]
        self.assertEqual(len(ful), 16 * 3 * 4)
        self.assertEqual(ful["8.50|2.25|-1.25|90||"],
                         ["MADE_TO_ORDER", "8-10 weeks"])
        self.assertNotIn("8.50|-2.25|-1.25|90||", ful)

    def test_a_matrix_with_no_exceptions_has_nothing_to_say(self):
        m = lens_order.Matrix(lens_rules.rows_from_config(
            _compile(SPHERICAL)["config"]))
        self.assertEqual(m.options()["fulfilment"], {})


def _product():
    return {"product_id": 7, "product_name": "P1 Astig", "product_code": "P1A",
            "product_category": "Contact Lenses", "product_special_price": 30,
            "product_price": 35, "image_url": "", "vertical": "CONTACT_LENS",
            "lens_type": "TORIC"}


def _variant(sph, status="STANDARD", text=None, max_days=None):
    return {"variant_id": 1, "sph": sph, "cyl": "-1.25", "axis": "90",
            "add_power": "", "base_curve": "8.5", "diameter": "14.5",
            "color_code": "", "fulfilment_status": status,
            "lead_time_text": text, "lead_time_max_days": max_days}


class CartDisclosure(unittest.TestCase):
    def test_a_stocked_line_says_nothing_about_lead_time(self):
        item = lens_order.cart_item(_product(), [
            {"eye": "right", "variant": _variant("-2.00"), "boxes": 3},
            {"eye": "left", "variant": _variant("-2.50"), "boxes": 3}])
        self.assertEqual(item["fulfilment_status"], "STANDARD")
        self.assertIsNone(item["lead_time_text"])
        self.assertEqual(item["right_fulfilment"], "STANDARD")
        self.assertIsNone(item["right_lead_time"])

    def test_one_made_to_order_eye_makes_the_line_wait_for_it(self):
        item = lens_order.cart_item(_product(), [
            {"eye": "right", "variant": _variant("-2.00"), "boxes": 3},
            {"eye": "left", "variant": _variant(
                "2.00", "MADE_TO_ORDER", "8-10 weeks", 70), "boxes": 3}])
        self.assertEqual(item["fulfilment_status"], "MADE_TO_ORDER")
        self.assertEqual(item["lead_time_text"], "8-10 weeks")
        self.assertEqual(item["right_fulfilment"], "STANDARD")
        self.assertEqual(item["left_fulfilment"], "MADE_TO_ORDER")
        self.assertEqual(item["left_lead_time"], "8-10 weeks")

    def test_two_made_to_order_eyes_wait_for_the_slower_one(self):
        item = lens_order.cart_item(_product(), [
            {"eye": "right", "variant": _variant(
                "2.00", "MADE_TO_ORDER", "Up to 45 days", 45), "boxes": 3},
            {"eye": "left", "variant": _variant(
                "3.00", "MADE_TO_ORDER", "8-10 weeks", 70), "boxes": 3}])
        self.assertEqual(item["lead_time_text"], "8-10 weeks")

    def test_an_unparsed_lead_time_still_reaches_the_line(self):
        item = lens_order.cart_item(_product(), [
            {"eye": "right", "variant": _variant(
                "2.00", "MADE_TO_ORDER", "on request", None), "boxes": 3}])
        self.assertEqual(item["fulfilment_status"], "MADE_TO_ORDER")
        self.assertEqual(item["lead_time_text"], "on request")

    def test_the_prescription_snapshot_carries_the_fulfilment(self):
        names = [n for n, _d in lens_rx.COLUMNS]
        for n in ("right_fulfilment", "right_lead_time", "left_fulfilment",
                  "left_lead_time", "rule_version"):
            self.assertIn(n, names)
        self.assertIn(("contact_lens_prescriptions", lens_rx.COLUMNS),
                      contact_lens.ADDED_COLUMNS)


class _Cursor(object):
    """A cursor that serves one config row and counts its reads."""

    def __init__(self, config_json):
        self.config_json = config_json
        self.reads = 0
        self.lock = threading.Lock()
        self._row = None

    def execute(self, sql, args=()):
        with self.lock:
            self.reads += 1
        self._row = ({"config_json": self.config_json}
                     if self.config_json is not None else None)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _Store(object):
    """A Redis stand-in: a dict, or a client that fails every call."""

    def __init__(self, fail=False):
        self.data, self.fail, self.sets = {}, fail, 0

    def get(self, key):
        if self.fail:
            raise IOError("redis down")
        return self.data.get(key)

    def set(self, key, value, ex=None):
        if self.fail:
            raise IOError("redis down")
        self.sets += 1
        self.data[key] = value


def _cache(store):
    cache = lens_config._Cache(url="redis://nowhere", factory=lambda: store)
    cache.disabled = False
    return cache


class ConfigRead(unittest.TestCase):
    def setUp(self):
        self.config = _compile(TORIC, product_id=7, rule_version=3)["config"]
        self.raw = json.dumps(self.config, separators=(",", ":"))
        self.lens = {"product_id": 7, "rule_version": 3, "lens_type": "TORIC",
                     "param_mode": "MATRIX"}

    def test_first_read_is_the_database_and_fills_the_cache(self):
        store = _Store()
        cur = _Cursor(self.raw)
        cache = _cache(store)
        shape, source = lens_config.shape_with_source(cur, self.lens, cache)
        self.assertEqual(source, "db")
        self.assertEqual(store.sets, 1)
        shape2, source2 = lens_config.shape_with_source(cur, self.lens, cache)
        self.assertEqual(source2, "cache")
        self.assertEqual(cur.reads, 1)
        self.assertEqual(shape.options(), shape2.options())

    def test_cache_failure_falls_back_to_the_database_silently(self):
        cur = _Cursor(self.raw)
        shape, source = lens_config.shape_with_source(
            cur, self.lens, _cache(_Store(fail=True)))
        self.assertEqual(source, "db")
        self.assertEqual(len(shape.rows), len(self.config["rows"]))

    def test_a_corrupt_or_tampered_cache_entry_is_ignored(self):
        store = _Store()
        key = lens_config.KEY % (7, 3)
        store.data[key] = "{not json"
        cur = _Cursor(self.raw)
        self.assertEqual(lens_config.shape_with_source(
            cur, self.lens, _cache(store))[1], "db")
        tampered = json.loads(self.raw)
        tampered["rows"] = tampered["rows"][:1]
        store.data[key] = json.dumps(tampered)
        self.assertEqual(lens_config.shape_with_source(
            cur, self.lens, _cache(store))[1], "db")

    def test_a_config_row_failing_its_checksum_is_not_served(self):
        tampered = json.loads(self.raw)
        tampered["legend"][1]["status"] = "STANDARD"
        cur = _Cursor(json.dumps(tampered))
        with self.assertLogs(lens_config.log, level="ERROR"):
            found, source = lens_config.config(cur, 7, 3, _cache(_Store()))
        self.assertIsNone(found)

    def test_the_cache_key_is_the_version_so_versions_never_mix(self):
        store = _Store()
        cache = _cache(store)
        lens_config.config(_Cursor(self.raw), 7, 3, cache)
        other = json.loads(self.raw)
        other["rule_version"] = 4
        other["checksum"] = lens_rules.checksum(other)
        found, source = lens_config.config(
            _Cursor(json.dumps(other, separators=(",", ":"))), 7, 4, cache)
        self.assertEqual(source, "db")
        self.assertEqual(found["rule_version"], 4)
        self.assertEqual(set(store.data), {lens_config.KEY % (7, 3),
                                           lens_config.KEY % (7, 4)})

    def test_a_lens_with_no_published_version_reads_its_rows(self):
        cur = _Cursor(None)
        lens = dict(self.lens, rule_version=0)
        shape, source = lens_config.shape_with_source(cur, lens, _cache(_Store()))
        self.assertEqual(source, "rows")

    def test_concurrent_readers_share_one_client_and_agree(self):
        store = _Store()
        cache = _cache(store)
        created = []
        real_factory = cache._factory
        lock = threading.Lock()

        def factory():
            with lock:
                created.append(1)
            return real_factory()
        cache._factory = factory
        results, errors = [], []

        def read():
            try:
                cur = _Cursor(self.raw)
                shape, _src = lens_config.shape_with_source(cur, self.lens,
                                                            cache)
                results.append(shape.options()["fulfilment"])
            except Exception as exc:  # noqa: BLE001 - collected below
                errors.append(exc)
        threads = [threading.Thread(target=read) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(created), 1)
        self.assertEqual(len(results), 24)
        self.assertTrue(all(r == results[0] for r in results))


class Identity(unittest.TestCase):
    def test_names_compare_without_case_spacing_or_the_registered_mark(self):
        self.assertEqual(lens_identity.normalise("Clariti\u00ae 1  Day"),
                         "clariti 1 day")

    def test_a_legacy_slug_names_its_storefront(self):
        with self.assertRaises(ValueError):
            lens_identity.add_alias(None, 7, lens_identity.ALIAS_LEGACY_SLUG,
                                    "clariti-1-day")

    def test_an_unknown_type_or_source_is_refused(self):
        with self.assertRaises(ValueError):
            lens_identity.add_alias(None, 7, "NICKNAME", "x")
        with self.assertRaises(ValueError):
            lens_identity.set_spec(None, 7, "water_content", "56", "RUMOUR")

    def test_a_condition_signs_the_same_whatever_its_key_order(self):
        a = lens_identity.condition_signature({"sph_min": "0.25", "cyl": "-0.75"})
        b = lens_identity.condition_signature({"cyl": "-0.75", "sph_min": "0.25"})
        self.assertEqual(a, b)
        self.assertEqual(lens_identity.condition_signature(None), "")


class Schema(unittest.TestCase):
    def test_the_new_tables_and_columns_are_declared_to_the_schema_authority(self):
        names = [n for n, _d in contact_lens.TABLES]
        for n in ("contact_lens_rule_sets", "contact_lens_configs",
                  "contact_lens_aliases", "contact_lens_specs"):
            self.assertIn(n, names)
        added = dict(contact_lens.ADDED_COLUMNS)
        self.assertEqual([n for n, _d in added["contact_lens_variants"]],
                         [n for n, _d in lens_rules.VARIANT_COLUMNS])
        profile = [n for n, _d in added["contact_lens_products"]]
        for n in ("rule_version", "ships_within_text", "gtin_reference_power",
                  "canonical_name", "legacy_ref_id"):
            self.assertIn(n, profile)

    def test_every_added_column_is_additive(self):
        for _table, columns in contact_lens.ADDED_COLUMNS:
            for name, decl in columns:
                self.assertRegex(decl.upper(), r"DEFAULT |\bNULL\b", name)


DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)

PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS products (
    product_id       INT NOT NULL PRIMARY KEY,
    product_code     VARCHAR(40) NOT NULL,
    product_name     VARCHAR(200) NOT NULL,
    product_category VARCHAR(80) NOT NULL,
    product_slug     VARCHAR(200) NULL,
    product_status   VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    product_quantity INT NOT NULL DEFAULT 0,
    show_in_listings TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB
"""

PID = 990071


def _connect():
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError:
        return None
    try:
        return pymysql.connect(cursorclass=DictCursor, autocommit=False,
                               **DB_CONF)
    except Exception:  # noqa: BLE001 - no database here
        return None


def _by_sph(rows):
    """variant_id by sph along the cyl -1.25 / axis 90 line, canonically."""
    canon = lens_order._canonical
    return {canon("sph", r["sph"]): r["variant_id"] for r in rows
            if canon("cyl", r["cyl"]) == "-1.25"
            and canon("axis", r["axis"]) == "90"}


class OnMariaDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        if cls.db is None:
            raise unittest.SkipTest("no MariaDB (OPTIWAR_TEST_MYSQL_*)")
        contact_lens._SCHEMA_READY = False
        with cls.db.cursor() as cur:
            cur.execute(PRODUCTS_DDL)
            contact_lens.ensure_schema(cur)
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        if cls.db is not None:
            cls._clean()
            cls.db.close()

    @classmethod
    def _clean(cls):
        with cls.db.cursor() as cur:
            for table in ("contact_lens_variants", "contact_lens_configs",
                          "contact_lens_rule_sets", "contact_lens_aliases",
                          "contact_lens_specs", "contact_lens_products"):
                cur.execute("DELETE FROM %s WHERE product_id = %%s" % table,
                            (PID,))
            cur.execute("DELETE FROM products WHERE product_id = %s", (PID,))
        cls.db.commit()

    def setUp(self):
        self._clean()
        with self.db.cursor() as cur:
            cur.execute("INSERT INTO products (product_id, product_code, "
                        "product_name, product_category, product_vertical, "
                        "sell_on_com, sell_on_in) VALUES (%s,%s,%s,%s,%s,1,0)",
                        (PID, "T-P1A", "Test P1 Astig", "Contact Lenses",
                         contact_lens.VERTICAL))
            cur.execute("INSERT INTO contact_lens_products (product_id, "
                        "brand, manufacturer, lens_type, modality, "
                        "pack_quantity, param_mode) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (PID, "Precision1", "Alcon", "TORIC", "DAILY", 30,
                         "MATRIX"))
        self.db.commit()

    def _profile(self, cur):
        cur.execute("SELECT rule_version, param_mode FROM contact_lens_products"
                    " WHERE product_id=%s", (PID,))
        return cur.fetchone()

    def test_store_compile_publish_in_one_transaction(self):
        with self.db.cursor() as cur:
            stored = lens_rules.store_rule_set(
                cur, PID, TORIC, source_type="MANUFACTURER_CHART",
                source_ref=ALCON, confirmed_by="owner")
            self.assertEqual(stored["rule_version"], 1)
            self.assertEqual(self._profile(cur)["rule_version"], 0)
            out = lens_rules.compile_and_publish(cur, PID, 1)
            self.assertEqual(out["counts"]["made_to_order"], 192)
            self.assertEqual(self._profile(cur)["rule_version"], 1)
        self.db.commit()
        with self.db.cursor() as cur:
            rows = lens_order.variants(cur, PID)
            self.assertEqual(len(rows), 41 * 12)
            found = lens_order.Matrix(rows).find({
                "sph": "2.25", "cyl": "-1.25", "axis": "90",
                "base_curve": "8.5"})
            self.assertIsNotNone(found)
            self.assertEqual(found["fulfilment_status"], "MADE_TO_ORDER")
            self.assertEqual(found["lead_time_text"], "8-10 weeks")
            lens = {"product_id": PID, "rule_version": 1, "lens_type": "TORIC",
                    "param_mode": "MATRIX"}
            shape, source = lens_config.shape_with_source(
                cur, lens, _cache(_Store()))
            self.assertEqual(source, "db")
            # The config names the very variant_ids checkout validates against.
            ids = {r["variant_id"] for r in rows}
            self.assertEqual({r["variant_id"] for r in shape.rows}, ids)

    def test_a_rolled_back_publication_leaves_no_trace(self):
        with self.db.cursor() as cur:
            lens_rules.store_rule_set(cur, PID, TORIC)
        self.db.commit()
        with self.db.cursor() as cur:
            lens_rules.compile_and_publish(cur, PID, 1)
        self.db.rollback()
        with self.db.cursor() as cur:
            self.assertEqual(self._profile(cur)["rule_version"], 0)
            cur.execute("SELECT COUNT(*) AS n FROM contact_lens_configs WHERE "
                        "product_id=%s", (PID,))
            self.assertEqual(cur.fetchone()["n"], 0)
            self.assertEqual(lens_order.variants(cur, PID), [])

    def test_a_new_version_retires_rows_without_deleting_them(self):
        with self.db.cursor() as cur:
            lens_rules.store_rule_set(cur, PID, TORIC)
            lens_rules.compile_and_publish(cur, PID, 1)
            before = _by_sph(lens_order.variants(cur, PID))
            narrower = json.loads(json.dumps(TORIC))
            narrower["tiers"][1]["sph"]["to"] = "2.00"
            self.assertEqual(lens_rules.store_rule_set(
                cur, PID, narrower)["rule_version"], 2)
            lens_rules.compile_and_publish(cur, PID, 2)
            self.assertEqual(self._profile(cur)["rule_version"], 2)
            after = _by_sph(lens_order.variants(cur, PID))
            self.assertNotIn("3.00", after)
            self.assertEqual(after["-2.00"], before["-2.00"])
            cur.execute("SELECT available FROM contact_lens_variants WHERE "
                        "variant_id=%s", (before["3.00"],))
            self.assertEqual(cur.fetchone()["available"], 0)
            # Version 1's config is still readable, verbatim.
            v1, _src = lens_config.config(cur, PID, 1, _cache(_Store()))
            self.assertEqual(v1["counts"]["made_to_order"], 192)
        self.db.commit()

    def test_a_rule_set_that_cannot_compile_is_never_a_version(self):
        with self.db.cursor() as cur:
            with self.assertRaises(lens_rules.RuleError):
                lens_rules.store_rule_set(cur, PID, dict(TORIC, tiers=[]))
            self.assertEqual(lens_rules.next_version(cur, PID), 1)

    def test_aliases_are_one_products_and_never_on_in(self):
        with self.db.cursor() as cur:
            lens_identity.add_alias(cur, PID, "ALSO_KNOWN_AS",
                                    "Clariti\u00ae 1 Day Toric",
                                    source_type="MANUFACTURER_CHART",
                                    source_ref="CooperVision price list 2026-01")
            lens_identity.add_alias(cur, PID, "LEGACY_REF", "330",
                                    source_type="LEGACY_DATABASE")
            lens_identity.add_alias(cur, PID, "LEGACY_SLUG",
                                    "/clariti-1-day-toric/", site="in")
            self.assertEqual(lens_identity.also_known_as(cur, PID),
                             ["Clariti\u00ae 1 Day Toric"])
            self.assertEqual(lens_identity.find_by_name(
                cur, "clariti 1 day  toric"), [PID])
            self.assertEqual(lens_identity.legacy_redirect(
                cur, "in", "clariti-1-day-toric"), PID)
            self.assertIsNone(lens_identity.legacy_redirect(
                cur, "com", "clariti-1-day-toric"))
            # Staged for .in means stored, not exposed: writing the alias
            # leaves the flag every .in surface reads exactly where it was.
            cur.execute("SELECT sell_on_in FROM products WHERE product_id=%s",
                        (PID,))
            self.assertEqual(int(cur.fetchone()["sell_on_in"]), 0)
        self.db.rollback()

    def test_a_spec_yields_to_a_better_source_and_refuses_a_peer_conflict(self):
        with self.db.cursor() as cur:
            self.assertTrue(lens_identity.set_spec(
                cur, PID, "water_content", "56", "LEGACY_DATABASE", unit="%"))
            self.assertEqual(lens_identity.specs(cur, PID), [])
            self.assertTrue(lens_identity.set_spec(
                cur, PID, "water_content", "46", "CARTON_PHOTO", unit="%",
                verified=True, verified_by="owner"))
            self.assertFalse(lens_identity.set_spec(
                cur, PID, "water_content", "56", "LEGACY_DATABASE", unit="%"))
            with self.assertRaises(ValueError):
                lens_identity.set_spec(cur, PID, "water_content", "47",
                                       "CARTON_PHOTO", unit="%")
            got = lens_identity.specs(cur, PID)
            self.assertEqual([(s["spec_value"], s["source_type"])
                              for s in got], [("46", "CARTON_PHOTO")])
            self.assertTrue(lens_identity.set_spec(
                cur, PID, "base_curve", "8.6", "MANUFACTURER_CHART",
                condition={"sph_from": "0.25"}, verified=True))
            self.assertTrue(lens_identity.set_spec(
                cur, PID, "base_curve", "8.5", "MANUFACTURER_CHART",
                condition={"sph_to": "0.00"}, verified=True))
            conds = [s["condition"] for s in lens_identity.specs(cur, PID)
                     if s["spec_key"] == "base_curve"]
            self.assertEqual(len(conds), 2)
        self.db.rollback()


if __name__ == "__main__":
    unittest.main()

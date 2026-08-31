"""The deploy tool's migration must be exactly what ensure_schema() would do.

If the two lists can disagree, ``plan`` can report "nothing pending" while the
restart quietly runs DDL — the single guarantee the schema step exists to buy.
deploy.py derives its migration from acr.py's constants, and these tests hold
that wiring in place: a column or index added to one is added to both, and the
labels ``pending_ddl`` parses stay parseable.

    python3 -m unittest tests.test_deploy_migration
"""
import ast
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DeployMigrationTest(unittest.TestCase):
    def setUp(self):
        self.deploy = _load("deploy_under_test",
                            os.path.join(REPO, "deploy", "deploy.py"))
        self.acr = _load("acr_for_deploy_test", os.path.join(REPO, "acr.py"))
        self.cl = _load("cl_for_deploy_test",
                        os.path.join(REPO, "contact_lens.py"))

    def test_covers_every_column_and_index_ensure_schema_adds(self):
        labels = [label for label, _sql in self.deploy.migration()]
        expected = ["order_refunds (table)"]
        expected += ["ai_events.%s (column)" % n
                    for n, _d in self.acr._AI_EVENTS_EXTRA_COLS]
        expected += ["ai_events.%s (index)" % n
                     for n, _c in self.acr._AI_EVENTS_EXTRA_IDX]
        expected += ["ai_actions.%s (index)" % n
                     for n, _c in self.acr._AI_ACTIONS_EXTRA_IDX]
        expected += ["%s (table)" % n for n, _d in self.cl.TABLES]
        expected += ["products.%s (column)" % n
                     for n, _d in self.cl.PRODUCTS_COLUMNS]
        expected += ["contact_lens_products.%s (column)" % n
                     for n, _d in self.cl.PROFILE_COLUMNS]
        expected += ["contact_lens_products.%s (index)" % n
                     for n, _d in self.cl.PROFILE_INDEXES]
        expected += ["products.%s (index)" % n
                     for n, _c in self.cl.PRODUCTS_INDEXES]
        expected += ["products.%s (column)" % n
                     for n, _d in self.deploy.catalogue_columns()]
        self.assertEqual(labels, expected)

    def test_the_catalogue_columns_are_the_ones_catalogue_ensures(self):
        """Read out of the source rather than imported, because catalogue.py
        imports flask and the deploy tool must not."""
        with open(os.path.join(REPO, "catalogue.py"), encoding="utf-8") as fh:
            src = fh.read()
        declared = None
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "GMC_COLUMNS"
                    for t in node.targets):
                declared = list(ast.literal_eval(node.value))
        self.assertEqual(self.deploy.catalogue_columns(), declared)
        self.assertIn("def ensure_gmc_columns", src)

    def test_columns_are_nullable_so_the_old_code_keeps_running(self):
        # The migration is applied before the code, and stays after a rollback,
        # so pre-Part-B code must be able to INSERT without these columns.
        for name, decl in self.acr._AI_EVENTS_EXTRA_COLS:
            self.assertIn("NULL", decl.upper(), name)
            self.assertNotIn("NOT NULL", decl.upper(), name)

    def test_products_columns_default_so_the_old_code_keeps_inserting(self):
        # These three are NOT NULL, unlike the ai_events columns, so the thing
        # that keeps them additive is the DEFAULT: the running release inserts a
        # product without naming them, and every existing row reads back as the
        # eyewear it already was.
        for name, decl in self.cl.PRODUCTS_COLUMNS:
            self.assertIn("DEFAULT", decl.upper(), name)
        # Same reasoning for a column added to an existing lens profile table,
        # which is either nullable or defaulted. The release flag's default must
        # be 0 in particular: a lens whose readiness nobody has asserted is not
        # released, and an import must not put one on a surface.
        for name, decl in self.cl.PROFILE_COLUMNS:
            self.assertRegex(decl.upper(), r"DEFAULT |\bNULL\b", name)
            if name == "merchant_enabled":
                self.assertIn("DEFAULT 0", decl.upper(), name)

    def test_lens_tables_are_created_before_the_columns_that_reference_them(self):
        labels = [label for label, _sql in self.deploy.migration()]
        self.assertLess(labels.index("contact_lens_products (table)"),
                        labels.index("contact_lens_variants (table)"))

    def test_ddl_is_additive_only(self):
        for label, sql in self.deploy.migration():
            self.assertRegex(
                sql,
                r"^(ALTER TABLE \w+ ADD (COLUMN|KEY|UNIQUE KEY) "
                r"|CREATE TABLE IF NOT EXISTS \w+ )", label)

    def test_refund_ledger_ddl_comes_from_the_application(self):
        # Restating the CREATE TABLE here is what would let plan() report
        # "nothing pending" while the application still had schema work to do.
        refunds = _load("refunds_for_deploy_test",
                        os.path.join(REPO, "refunds.py"))
        self.assertEqual(self.deploy.refunds_schema(),
                         " ".join(refunds.SCHEMA.split()))

    def test_a_symbol_production_does_not_have_blocks_the_deploy(self):
        # models.py importing embed_helper.age_group booted ImportError on the
        # box and 502'd both storefronts until the rollback: production keeps
        # its own embed_helper.py, which this deploy does not carry. The check
        # reads names, so it must see one that is genuinely absent there and
        # not complain about the ones that are present.
        asked = []

        def fake_remote(cmd, check=True):
            asked.append(cmd)
            # Production's module, minus whatever main added to it.
            return "NAMES build_media_list build_media_primary build_media_one"

        self.deploy.remote = fake_remote
        missing = self.deploy.unresolved_imports()
        self.assertTrue(any("embed_helper" in m for m in missing), missing)
        self.assertFalse(any("build_media_list" in m for m in missing), missing)
        # `from requests.auth import HTTPBasicAuth` is third-party and merely
        # shares a name with our auth.py — reading it as ours blocks the deploy
        # over a symbol production is not expected to have.
        self.assertFalse(any("HTTPBasicAuth" in m for m in missing), missing)
        # Never asks about a module the deploy carries: those arrive together.
        for cmd in asked:
            self.assertNotIn("catalogue.py", cmd)
            self.assertNotIn("contact_lens.py", cmd)

    def test_an_unreadable_module_is_not_reported_as_missing_symbols(self):
        # An answer without the sentinel means the check did not read the file
        # — an ssh banner, a stderr warning — which says nothing about the
        # symbols; treating it as absence would block every deploy on a hiccup.
        self.deploy.remote = lambda cmd, check=True: "bash: python3: not found"
        self.assertEqual(self.deploy.unresolved_imports(), [])

    def test_a_column_arriving_with_its_own_new_table_is_not_pending(self):
        # A box without the lens tables: the CREATE brings merchant_enabled
        # with it, so listing the ALTER as pending makes migrate fail half-way
        # with "Duplicate column name" — the schema left part-applied and the
        # deploy stopped after the tables and before the products indexes.
        replies = {}

        def fake_remote(cmd, check=True):
            if "information_schema.tables" in cmd:
                return "products ai_events ai_actions order_refunds"
            if "information_schema.columns" in cmd:
                table = cmd.split("table_name='", 1)[1].split("'", 1)[0]
                return replies.get(("cols", table), "")
            if "information_schema.statistics" in cmd:
                return ""
            raise AssertionError("unexpected remote: %s" % cmd)

        self.deploy.remote = fake_remote
        labels = [label for label, _sql in self.deploy.pending_ddl()]
        self.assertIn("contact_lens_products (table)", labels)
        self.assertNotIn("contact_lens_products.merchant_enabled (column)",
                         labels)
        # The columns on a table that already exists are still reported.
        self.assertIn("products.product_vertical (column)", labels)

    def test_labels_parse_back_into_table_and_object_name(self):
        # pending_ddl() splits these to look each object up in
        # information_schema; a label it cannot parse silently checks the
        # wrong name.
        known = {"ai_events", "ai_actions", "products",
                 "contact_lens_products"}
        for label, sql in self.deploy.migration():
            if label.endswith("(table)"):
                self.assertIn(label.split(" ", 1)[0], sql, label)
                continue
            table, rest = label.split(".", 1)
            name = rest.split(" ", 1)[0]
            self.assertIn(table, known, label)
            self.assertIn(" %s " % name, " %s " % sql, label)
            self.assertTrue(label.endswith("(column)")
                            or label.endswith("(index)"), label)


if __name__ == "__main__":
    unittest.main()

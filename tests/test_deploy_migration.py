"""The deploy tool's migration must be exactly what ensure_schema() would do.

If the two lists can disagree, ``plan`` can report "nothing pending" while the
restart quietly runs DDL — the single guarantee the schema step exists to buy.
deploy.py derives its migration from acr.py's constants, and these tests hold
that wiring in place: a column or index added to one is added to both, and the
labels ``pending_ddl`` parses stay parseable.

    python3 -m unittest tests.test_deploy_migration
"""
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

    def test_covers_every_column_and_index_ensure_schema_adds(self):
        labels = [label for label, _sql in self.deploy.migration()]
        expected = ["order_refunds (table)"]
        expected += ["ai_events.%s (column)" % n
                    for n, _d in self.acr._AI_EVENTS_EXTRA_COLS]
        expected += ["ai_events.%s (index)" % n
                     for n, _c in self.acr._AI_EVENTS_EXTRA_IDX]
        expected += ["ai_actions.%s (index)" % n
                     for n, _c in self.acr._AI_ACTIONS_EXTRA_IDX]
        self.assertEqual(labels, expected)

    def test_columns_are_nullable_so_the_old_code_keeps_running(self):
        # The migration is applied before the code, and stays after a rollback,
        # so pre-Part-B code must be able to INSERT without these columns.
        for name, decl in self.acr._AI_EVENTS_EXTRA_COLS:
            self.assertIn("NULL", decl.upper(), name)
            self.assertNotIn("NOT NULL", decl.upper(), name)

    def test_ddl_is_additive_only(self):
        for label, sql in self.deploy.migration():
            self.assertRegex(
                sql,
                r"^(ALTER TABLE \w+ ADD (COLUMN|KEY) "
                r"|CREATE TABLE IF NOT EXISTS \w+ )", label)

    def test_refund_ledger_ddl_comes_from_the_application(self):
        # Restating the CREATE TABLE here is what would let plan() report
        # "nothing pending" while the application still had schema work to do.
        refunds = _load("refunds_for_deploy_test",
                        os.path.join(REPO, "refunds.py"))
        self.assertEqual(self.deploy.refunds_schema(),
                         " ".join(refunds.SCHEMA.split()))

    def test_labels_parse_back_into_table_and_object_name(self):
        # pending_ddl() splits these to look each object up in
        # information_schema; a label it cannot parse silently checks the
        # wrong name.
        known = {"ai_events", "ai_actions"}
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

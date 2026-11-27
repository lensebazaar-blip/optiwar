import ast
import html
import re
import unittest
from pathlib import Path

import india_states
from flask import Flask, flash, get_flashed_messages, redirect, request, session
from jinja2 import Environment


REPO = Path(__file__).resolve().parents[1]

EXPECTED_STATES = (
    ("01", "Jammu & Kashmir"),
    ("02", "Himachal Pradesh"),
    ("03", "Punjab"),
    ("04", "Chandigarh"),
    ("05", "Uttarakhand"),
    ("06", "Haryana"),
    ("07", "Delhi"),
    ("08", "Rajasthan"),
    ("09", "Uttar Pradesh"),
    ("10", "Bihar"),
    ("11", "Sikkim"),
    ("12", "Arunachal Pradesh"),
    ("13", "Nagaland"),
    ("14", "Manipur"),
    ("15", "Mizoram"),
    ("16", "Tripura"),
    ("17", "Meghalaya"),
    ("18", "Assam"),
    ("19", "West Bengal"),
    ("20", "Jharkhand"),
    ("21", "Odisha"),
    ("22", "Chhattisgarh"),
    ("23", "Madhya Pradesh"),
    ("24", "Gujarat"),
    ("26", "Dadra & Nagar Haveli and Daman & Diu"),
    ("27", "Maharashtra"),
    ("29", "Karnataka"),
    ("30", "Goa"),
    ("31", "Lakshadweep"),
    ("32", "Kerala"),
    ("33", "Tamil Nadu"),
    ("34", "Puducherry"),
    ("35", "Andaman & Nicobar Islands"),
    ("36", "Telangana"),
    ("37", "Andhra Pradesh"),
    ("38", "Ladakh"),
)

EXPECTED_ALIASES = {
    "orissa": "Odisha",
    "pondicherry": "Puducherry",
    "uttaranchal": "Uttarakhand",
    "new delhi": "Delhi",
    "nct of delhi": "Delhi",
    "jammu and kashmir": "Jammu & Kashmir",
    "andaman and nicobar islands": "Andaman & Nicobar Islands",
    "dadra and nagar haveli and daman and diu":
        "Dadra & Nagar Haveli and Daman & Diu",
    "chattisgarh": "Chhattisgarh",
    "chhatisgarh": "Chhattisgarh",
}


class IndiaStatesTest(unittest.TestCase):
    def test_state_list_and_case_variants_normalize_in_order(self):
        self.assertEqual(india_states.STATES, EXPECTED_STATES)
        self.assertEqual(
            india_states.STATE_NAMES, tuple(name for _code, name in EXPECTED_STATES))
        self.assertEqual(
            india_states.GST_CODE,
            {name: code for code, name in EXPECTED_STATES})
        self.assertEqual(len(india_states.STATES), 36)
        for _code, name in EXPECTED_STATES:
            with self.subTest(state=name, case="upper"):
                self.assertEqual(india_states.normalize(name.upper()), name)
            with self.subTest(state=name, case="lower"):
                self.assertEqual(india_states.normalize(name.lower()), name)

    def test_every_owner_alias_and_state_code_normalizes(self):
        self.assertEqual(india_states.ALIASES, EXPECTED_ALIASES)
        for alias, name in EXPECTED_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(india_states.normalize(alias), name)
        for code, name in india_states.STATE_CODES.items():
            with self.subTest(code=code):
                self.assertEqual(india_states.normalize(code), name)
                self.assertEqual(india_states.STATE_LOOKUP[code], name)
        for name in india_states.STATE_NAMES:
            self.assertEqual(india_states.STATE_LOOKUP[name.lower()], name)
        for alias, name in EXPECTED_ALIASES.items():
            self.assertEqual(india_states.STATE_LOOKUP[alias], name)
        for value, expected in (
                ("Up", "Uttar Pradesh"),
                ("MH", "Maharashtra"),
                ("DL", "Delhi"),
                ("CG", "Chhattisgarh"),
                ("TS", "Telangana"),
                ("Odisha ", "Odisha")):
            with self.subTest(value=value):
                self.assertEqual(india_states.normalize(value), expected)
        self.assertEqual(india_states.normalize(" Jammu   & Kashmir "), "Jammu & Kashmir")

    def test_invalid_values_are_rejected(self):
        for value in ("Berlin", "Harayana", "Karanataka", "Utter Pradesh",
                      "India", "Bharat", ""):
            with self.subTest(value=value):
                self.assertIsNone(india_states.normalize(value))
                with self.assertRaisesRegex(
                        india_states.InvalidState,
                        "^Please choose your state from the list\\.$"):
                    india_states.require(value)
        self.assertIsNone(india_states.normalize(None))
        with self.assertRaises(india_states.InvalidState):
            india_states.require(None)

    def test_is_india_address_uses_country_and_host(self):
        for country in ("India", " india ", "IN", " in ", "Bharat", " bharat "):
            with self.subTest(country=country):
                self.assertTrue(india_states.is_india_address(country, False))
        self.assertTrue(india_states.is_india_address("", True))
        self.assertTrue(india_states.is_india_address(None, True))
        self.assertFalse(india_states.is_india_address("", False))
        self.assertFalse(india_states.is_india_address("Germany", True))

    def test_eu_ops_copy_is_byte_identical(self):
        self.assertEqual(
            (REPO / "india_states.py").read_bytes(),
            (REPO / "eu_ops" / "services" / "india_states.py").read_bytes())

    def test_india_checkout_and_profile_render_state_selects_only(self):
        checkout_source = (REPO / "templates" / "checkout.html").read_text()
        checkout_match = re.search(
            r'<label for="ck_state">State</label>(.*?)'
            r'<small class="owNcrHint"[^>]*></small>',
            checkout_source, re.S)
        self.assertIsNotNone(checkout_match)
        checkout_fragment = checkout_match.group(0)

        profile_source = (REPO / "templates" / "profile.html").read_text()
        profile_fragments = re.findall(
            r'<label>State</label>\s*(.*?)(?=</div>)',
            profile_source, re.S)
        self.assertEqual(len(profile_fragments), 2)

        env = Environment()
        india_context = {
            "is_india": True,
            "india_state_names": india_states.STATE_NAMES,
            "prefill": {"state": "Uttar Pradesh"},
        }
        checkout_in = env.from_string(checkout_fragment).render(**india_context)
        self.assertIn('<select id="ck_state" name="customer_state" required>', checkout_in)
        self.assertEqual(_state_options(checkout_in), [
            ("", "Choose state"),
        ] + [(name, name) for name in india_states.STATE_NAMES])
        self.assertIn('<option value="Uttar Pradesh" selected>', checkout_in)

        checkout_com = env.from_string(checkout_fragment).render(
            is_india=False, prefill={"state": "Bavaria"})
        self.assertIn(
            '<input type="text" id="ck_state" name="customer_state"',
            checkout_com)
        self.assertNotIn("<select", checkout_com)

        for fragment in profile_fragments:
            with self.subTest(fragment=fragment[:40]):
                profile_in = env.from_string(fragment).render(**india_context)
                self.assertIn("<select ", profile_in)
                self.assertEqual(_state_options(profile_in), [
                    ("", "Choose state"),
                ] + [(name, name) for name in india_states.STATE_NAMES])
                profile_com = env.from_string(fragment).render(is_india=False)
                self.assertIn('<input type="text" name="state"', profile_com)
                self.assertNotIn("<select", profile_com)

    def test_checkout_address_check_blocks_unmapped_india_state(self):
        source = (REPO / "templates" / "checkout.html").read_text()
        match = re.search(
            r'function owValidateForm\(\) \{.*?(?=function owPostProgressiveScanEvent)',
            source, re.S)
        self.assertIsNotNone(match)
        rendered_js = Environment().from_string(match.group(0)).render()
        self.assertIn("var stateEl = document.getElementById('ck_state');", rendered_js)
        self.assertIn(
            "var missingState = isIndiaCheckout && stateEl && !stateEl.value.trim();",
            rendered_js)
        self.assertIn("stateEl.classList.add('ow-ck-field-missing')", rendered_js)
        self.assertIn("'Please choose your state from the list'", rendered_js)
        self.assertIn("missingState && !missingRequiredField", rendered_js)

    def test_eu_ops_order_entry_renders_and_preselects_canonical_state(self):
        source = (REPO / "eu_ops" / "templates" / "pages"
                  / "ow_new_order.html").read_text()
        match = re.search(
            r'<div><label class="text-small text-muted">State</label>(.*?)'
            r'</div>\s*<div><label class="text-small text-muted">Zipcode',
            source, re.S)
        self.assertIsNotNone(match)
        fragment = match.group(0)
        rendered = Environment().from_string(fragment).render(
            site="in.optiwar.com",
            india_state_names=india_states.STATE_NAMES,
            data={"address": {"state": "Uttar Pradesh"}},
        )
        self.assertEqual(_state_options(rendered), [
            ("", "Choose state"),
        ] + [(name, name) for name in india_states.STATE_NAMES])
        self.assertIn('<option value="Uttar Pradesh" selected>', rendered)
        global_render = Environment().from_string(fragment).render(
            site="optiwar.com", data={"address": {"state": "Bavaria"}})
        self.assertIn('<input type="text" name="state"', global_render)
        self.assertNotIn("<select", global_render)

    def test_profile_add_address_rejects_invalid_and_normalizes_accepted_state(self):
        add_address = _load_profile_handler("add_address")
        app = Flask("profile_state_tests")
        app.secret_key = "test"

        class Cursor:
            def __init__(self):
                self.calls = []

            def execute(self, query, params=None):
                self.calls.append((query, params))

        class Database:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.commits = 0

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.commits += 1

            def rollback(self):
                raise AssertionError("unexpected rollback")

        invalid_db = Database()
        with app.test_request_context(
                "/profile/address/add", method="POST", base_url="https://optiwar.in",
                data={"address": "1 Main St", "state": "Berlin", "country": "India"}):
            session["user_id"] = 9
            add_address.__globals__.update(
                get_db=lambda: invalid_db, india_states=india_states,
                reship=_india_host_helper())
            response = add_address()
            messages = get_flashed_messages(with_categories=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(invalid_db.cursor_instance.calls, [])
        self.assertEqual(messages, [
            ("danger", "Please choose your state from the list.")
        ])

        valid_db = Database()
        with app.test_request_context(
                "/profile/address/add", method="POST", base_url="https://optiwar.in",
                data={"address": "1 Main St", "state": "up", "country": "India"}):
            session["user_id"] = 9
            add_address.__globals__.update(
                get_db=lambda: valid_db, india_states=india_states,
                reship=_india_host_helper())
            response = add_address()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(valid_db.cursor_instance.calls[0][1][2], "Uttar Pradesh")
        self.assertEqual(valid_db.commits, 1)

    def test_profile_edit_rejects_invalid_state_before_database_access(self):
        edit_address = _load_profile_handler("edit_address")
        app = Flask("profile_edit_state_tests")
        app.secret_key = "test"
        with app.test_request_context(
                "/profile/address/edit/1", method="POST", base_url="https://optiwar.in",
                data={"address": "1 Main St", "state": "Berlin", "country": "India"}):
            session["user_id"] = 9
            edit_address.__globals__.update(
                get_db=lambda: self.fail("database must not be used"),
                india_states=india_states, reship=_india_host_helper())
            response = edit_address(1)
        self.assertEqual(response.status_code, 302)

    def test_profile_address_data_json_preserves_ampersand_states(self):
        source = (REPO / "templates" / "profile.html").read_text()
        match = re.search(
            r'var addressData = \{.*?^\};',
            source, re.S | re.M)
        self.assertIsNotNone(match)
        rendered_js = Environment().from_string(match.group(0)).render(
            addresses=[{
                "address_id": 1,
                "address": "1 Main Street",
                "state": "Jammu & Kashmir",
                "zipcode": "123456",
                "country": "India",
            }],
            is_india=True,
        )
        self.assertIn('state: "Jammu \\u0026 Kashmir"', rendered_js)
        self.assertNotIn("Jammu &amp; Kashmir", rendered_js)


def _state_options(source):
    return [
        (html.unescape(value), html.unescape(re.sub(r"<[^>]*>", "", label)).strip())
        for value, label in re.findall(
            r'<option\b[^>]*value="([^"]*)"[^>]*>(.*?)</option>', source, re.S)
    ]


def _load_profile_handler(name):
    path = REPO / "profile.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    namespace = {
        "flash": flash,
        "redirect": redirect,
        "request": request,
        "session": session,
        "url_for": lambda _endpoint, **_values: "/profile/",
        "india_states": india_states,
        "reship": _india_host_helper(),
        "get_db": None,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]),
                 str(path), "exec"), namespace)
    return namespace[name]


def _india_host_helper():
    class Reship:
        @staticmethod
        def is_india_host(host):
            return "optiwar.in" in (host or "").lower()

    return Reship()


if __name__ == "__main__":
    unittest.main()

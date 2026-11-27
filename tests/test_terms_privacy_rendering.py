import datetime
import ast
import importlib.util
import json
import os
import re
import unittest

from flask import Flask, make_response, render_template, request
from jinja2 import ChoiceLoader, DictLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module(name):
    path = os.path.join(REPO, name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_constant(name):
    path = os.path.join(REPO, "crm.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name
                   for target in node.targets):
                return ast.literal_eval(node.value)
    raise AssertionError("missing %s in crm.py" % name)


def _faq_text(_template, faq_sections, all_faq_items, **kwargs):
    return "\n".join(
        item["a"]
        for section in faq_sections
        for item in section["questions"])


def _load_model_helpers():
    path = os.path.join(REPO, "models.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = {"_req_is_india", "ai_faq", "llms_txt"}
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    for function in functions:
        function.decorator_list = []
    if {function.name for function in functions} != names:
        raise AssertionError("could not locate model route helpers")
    namespace = {
        "make_response": make_response,
        "policy_terms": policy_terms,
        "render_template": _faq_text,
        "request": request,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), path, "exec"),
         namespace)
    return namespace


policy_terms = _load_module("policy_terms")
model_helpers = _load_model_helpers()
ASSISTANT_URL = _source_constant("ASSISTANT_URL")


def app_context():
    return {
        "current_year": datetime.datetime.now().year,
        "support_url": ASSISTANT_URL,
    }


def _app():
    app = Flask(
        "terms_privacy_tests",
        template_folder=os.path.join(REPO, "templates"))
    app.secret_key = "terms-privacy-test"
    app.jinja_loader = ChoiceLoader([
        DictLoader({
            "base.html": (
                "<!doctype html><title>{% block title %}{% endblock %}</title>"
                "{% block content %}{% endblock %}"),
        }),
        app.jinja_loader,
    ])
    app.jinja_env.globals["_"] = lambda text: text

    @app.context_processor
    def inject_test_context():
        return app_context()

    def terms_and_conditions():
        site = "in" if model_helpers["_req_is_india"]() else "com"
        current = policy_terms.current(site)
        return render_template(
            "terms-and-conditions.html",
            returns_policy_html=policy_terms.returns_html(site),
            returns_policy_version=current["returns"]["version"],
            terms_version=current["terms"]["version"],
            is_india=site == "in",
        )

    def privacy_policy():
        return render_template("privacy_policy.html")

    app.add_url_rule(
        "/", endpoint="main.index", view_func=lambda: "")
    app.add_url_rule(
        "/terms_and_conditions", endpoint="main.terms_and_conditions",
        view_func=terms_and_conditions)
    app.add_url_rule(
        "/privacy_policy", endpoint="main.privacy_policy",
        view_func=privacy_policy)
    app.add_url_rule(
        "/ip-notice", endpoint="main.ip_notice", view_func=lambda: "")
    app.add_url_rule(
        "/ai-faq", endpoint="main.ai_faq",
        view_func=model_helpers["ai_faq"])
    app.add_url_rule(
        "/llms.txt", endpoint="main.llms_txt",
        view_func=model_helpers["llms_txt"])
    return app


class TermsAndPrivacyRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.client = cls.app.test_client()

    def _get(self, path, host):
        return self.client.get(path, base_url="https://%s" % host)

    def test_terms_render_for_both_hosts_with_the_owner_placeholders(self):
        placeholders = (
            "[OWNER TO FILL: Registered office]",
            "[OWNER TO FILL: CIN]",
            "[OWNER TO CONFIRM: GSTIN]",
            "[OWNER TO FILL: Grievance Officer name, designation]",
            "[OWNER TO FILL: Grievance email]",
        )
        for host in ("optiwar.in", "optiwar.com"):
            with self.subTest(host=host):
                response = self._get("/terms_and_conditions", host)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                self.assertIn("OPTIWAR RETURNS AND REPLACEMENTS POLICY", body)
                self.assertIn("Disclaimer of Warranties", body)
                self.assertIn("Severability", body)
                self.assertIn("26 November 2026", body)
                self.assertNotIn("Limited Warranty", body)
                self.assertNotIn("juristication", body)
                self.assertNotIn("Conact us form", body)
                for placeholder in placeholders:
                    self.assertIn(placeholder, body)
                self.assertIn('id="grievance-redressal"', body)

    def test_site_specific_cancellation_copy_uses_the_single_window(self):
        in_body = self._get(
            "/terms_and_conditions", "optiwar.in").get_data(as_text=True)
        com_body = self._get(
            "/terms_and_conditions", "optiwar.com").get_data(as_text=True)
        self.assertEqual(policy_terms.CANCELLATION_WINDOW_HOURS, 4)
        start = in_body.index("<h2>Cancellation Policy</h2>")
        end = in_body.index("</li>", start)
        in_cancel = in_body[start:end]
        self.assertIn("free of charge within 4 hours", in_cancel)
        self.assertIn("After 4 hours the order is in processing", in_cancel)
        self.assertIn("within 3 to 5 business days", in_cancel)
        self.assertIn("the one published when that order was placed", in_cancel)
        self.assertIn(
            'href="/?support=legacy_contact_us">support assistant</a>', in_cancel)
        self.assertNotRegex(in_cancel, r"(?i)6\s*hours?")
        self.assertNotRegex(in_cancel, r"\b(?:50|7|57)\s*%")
        self.assertIn(
            "Orders placed on optiwar.com are international orders. Once an "
            "order has been placed and paid it cannot be cancelled, returned "
            "or refunded, as set out in the policy below.",
            com_body)
        self.assertNotIn("within 4 hours", com_body)

    def test_shipping_copy_is_site_aware_and_keeps_checkout_options_unchanged(self):
        in_body = self._get(
            "/terms_and_conditions", "optiwar.in").get_data(as_text=True)
        com_body = self._get(
            "/terms_and_conditions", "optiwar.com").get_data(as_text=True)
        self.assertIn(
            "Standard delivery within India is free. Faster paid delivery "
            "options, where available for your address, are shown at checkout "
            "with their fee.", in_body)
        self.assertNotIn("Standard delivery within India is free", com_body)
        for expected in (
                "Delivery within India takes between 1 and 45 days",
                "maximum delivery time is 45 days",
                "Delivery outside India takes from 3 days",
                'choose "Others" as the country of delivery'):
            self.assertIn(expected, in_body)
            self.assertIn(expected, com_body)

    def test_privacy_page_renders_bold_labels_and_the_working_support_link(self):
        response = self._get("/privacy_policy", "optiwar.in")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        for label in ("Access", "Correction", "Deletion", "Opt-Out"):
            self.assertIn("<strong>%s:</strong>" % label, body)
            self.assertNotIn("**%s:**" % label, body)
        self.assertIn(
            'href="/?support=legacy_contact_us">support assistant</a>', body)
        self.assertIn('id="grievance-redressal"', body)
        for placeholder in (
                "[OWNER TO FILL: Registered office]",
                "[OWNER TO FILL: CIN]",
                "[OWNER TO CONFIRM: GSTIN]",
                "[OWNER TO FILL: Grievance Officer name, designation]",
                "[OWNER TO FILL: Grievance email]"):
            self.assertIn(placeholder, body)
        self.assertNotIn("Contact Email Form", body)
        self.assertNotIn("Press here", body)
        self.assertIn("9 November 2026", body)

    def test_footer_year_and_grievance_link_are_dynamic_and_shared(self):
        with self.app.test_request_context(
                "/", base_url="https://optiwar.in"):
            self.assertEqual(
                app_context()["current_year"],
                datetime.datetime.now().year)
        with open(os.path.join(REPO, "templates", "base.html"),
                  encoding="utf-8") as fh:
            base = fh.read()
        self.assertIn("&copy; {{ current_year }}", base)
        self.assertNotIn("&copy; 2025", base)
        self.assertIn(
            'href="/terms_and_conditions#grievance-redressal"', base)
        self.assertIn("Grievance redressal", base)

    def test_faq_and_llms_cancellation_copy_matches_the_four_hour_setting(self):
        in_faq = self._get("/ai-faq", "optiwar.in").get_data(as_text=True)
        com_faq = self._get("/ai-faq", "optiwar.com").get_data(as_text=True)
        in_llms = self._get("/llms.txt", "optiwar.in").get_data(as_text=True)
        self.assertIn("within 4 hours", in_faq)
        self.assertIn("within 4 hours", in_llms)
        self.assertNotIn("__CANCELLATION_WINDOW_HOURS__", in_llms)
        self.assertIn("cannot be cancelled once placed and paid", com_faq)
        for source in (in_faq, com_faq, in_llms):
            self.assertNotRegex(source, r"(?i)6\s*hours?")
            for sentence in re.split(r"[.!?\n]", source):
                if re.search(r"(?i)cancel", sentence):
                    self.assertNotRegex(sentence, r"\b(?:50|7|57)\s*%")
        kb_path = os.path.join(
            REPO, "static", "ai", "optiwar_ai_knowledge_base.json")
        with open(kb_path, encoding="utf-8") as fh:
            kb = json.load(fh)
        cancellation = next(
            entry["answer"] for entry in kb["faq"]
            if entry.get("intent") == "cancellation_policy")
        self.assertIn("within 4 hours", cancellation)
        self.assertNotRegex(cancellation, r"(?i)6\s*hours?")
        self.assertNotRegex(cancellation, r"\b(?:50|7|57)\s*%")

    def test_support_entry_matches_the_assistant_redirect_and_widget_handler(self):
        with self.app.test_request_context(
                "/", base_url="https://optiwar.in"):
            self.assertEqual(
                app_context()["support_url"], ASSISTANT_URL)
        self.assertEqual(ASSISTANT_URL, "/?support=legacy_contact_us")
        with open(os.path.join(REPO, "static", "js", "chat-widget.js"),
                  encoding="utf-8") as fh:
            widget = fh.read()
        self.assertIn("support=([a-z_]{1,32})", widget)
        self.assertIn("window.owChatOpen('text', supportEntry)", widget)


if __name__ == "__main__":
    unittest.main()

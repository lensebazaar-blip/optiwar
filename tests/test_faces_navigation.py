"""Faces and the Scanner are different destinations.

The header's Faces link, the try-on page's "see your measurements" and the
completion notice all land on My Faces (``/profile/?tab=faces``), rendered
active by the server on the first paint; a bare ``/profile/`` is Account.
"""
import ast
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(REPO, *parts)) as fh:
        return fh.read()


def _profile_helpers():
    """``active_tab`` / ``focus_face`` / ``TABS`` from profile.py without its
    Flask-relative imports."""
    tree = ast.parse(_read("profile.py"))
    keep = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.Assign))
            and getattr(n, "name", None) in ("active_tab", "focus_face")
            or (isinstance(n, ast.Assign) and n.targets[0].id == "TABS")]
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "profile.py", "exec"), ns)
    return ns


class TabSelectionTests(unittest.TestCase):

    def setUp(self):
        self.ns = _profile_helpers()

    def test_bare_profile_is_account_and_tab_faces_is_my_faces(self):
        at = self.ns["active_tab"]
        self.assertEqual(at({}), "account")
        self.assertEqual(at({"tab": "faces"}), "myface")
        self.assertEqual(at({"tab": "FACES "}), "myface")
        self.assertEqual(at({"tab": "myface"}), "myface")
        self.assertEqual(at({"tab": "orders"}), "orders")
        self.assertEqual(at({"tab": "addresses"}), "addresses")
        self.assertEqual(at({"tab": "nonsense"}), "account")

    def test_focus_face_is_an_int_or_nothing(self):
        ff = self.ns["focus_face"]
        self.assertEqual(ff({"face": "12"}), 12)
        self.assertIsNone(ff({}))
        self.assertIsNone(ff({"face": "abc"}))
        self.assertIsNone(ff({"face": "0"}))

    def test_faces_route_redirects_to_the_tab_and_the_page_passes_the_tab(self):
        src = _read("profile.py")
        self.assertIn("@bp.route('/faces')", src)
        self.assertIn("redirect(url_for('profile.profile_page', tab='faces'))", src)
        self.assertIn("active_tab=active_tab(request.args)", src)
        self.assertIn("focus_face=focus_face(request.args)", src)


class TemplateTests(unittest.TestCase):

    def _render(self, **ctx):
        from flask import Flask
        app = Flask(__name__, template_folder=os.path.join(REPO, "templates"))
        src = _read("templates", "profile.html")
        start = src.index("<!-- Tab Navigation -->")
        end = src.index('<div class="ow-profile-card">', start)
        block = src[start:end]
        with app.app_context():
            return app.jinja_env.from_string(block).render(
                face_profiles_enabled=True, **ctx)

    def _active(self, html_):
        btns = re.findall(r'<button class="ow-tab-btn([^"]*)" onclick="switchTab\(\'(\w+)\'\)"', html_)
        active = [name for cls, name in btns if "ow-tab-active" in cls]
        panes = re.findall(r'<div id="tab-(\w+)" class="ow-tab-content([^"]*)"', html_)
        visible = [name for name, cls in panes if "ow-tab-visible" in cls]
        return active, visible

    def test_account_active_by_default_my_faces_active_for_tab_faces(self):
        self.assertEqual(self._active(self._render(active_tab="account")),
                         (["account"], ["account"]))
        html_ = self._render(active_tab="myface")
        self.assertEqual(self._active(html_), (["myface"], []))   # the pane is later in the file
        self.assertIn(">My Faces<", html_)

    def test_every_pane_takes_its_visibility_from_the_server(self):
        src = _read("templates", "profile.html")
        for tab in ("account", "addresses", "orders", "myface"):
            self.assertIn('<div id="tab-%s" class="ow-tab-content{%% if active_tab == \'%s\' %%} '
                          'ow-tab-visible{%% endif %%}">' % (tab, tab), src, tab)
        self.assertNotIn('class="ow-tab-content ow-tab-visible"', src)
        self.assertNotIn('class="ow-tab-btn ow-tab-active"', src)
        # switching client-side keeps the URL honest, hash links still work
        self.assertIn("history.replaceState(null, '', location.pathname + q)", src)
        self.assertIn("location.hash === '#myface' || location.hash === '#my-faces'", src)
        self.assertIn(".mf-card[data-id=\"{{ focus_face }}\"]", src)


class LinkTargetTests(unittest.TestCase):

    def test_header_faces_goes_to_my_faces_not_the_scanner(self):
        src = _read("templates", "base.html")
        block = src[src.index("{% block header_face_scan %}"):src.index("{% endblock %}", src.index("{% block header_face_scan %}"))]
        self.assertIn('href="/profile/faces"', block)
        self.assertNotIn('href="/tryon"', block)

    def test_tryon_see_measurements_opens_my_faces_for_that_person(self):
        src = _read("templates", "tryon.html")
        self.assertIn('href="/profile/?tab=faces{% if scan_for %}&amp;face={{ scan_for.id }}{% endif %}" '
                      'class="returning-btn returning-btn--view" id="viewMeasBtn"', src)
        self.assertIn('<a href="/profile/?tab=faces" style="color:inherit">change</a>', src)
        self.assertNotIn('href="/profile"', src)
        self.assertNotIn("/profile#myface", src)

    def test_completion_notice_links_to_the_tab(self):
        src = _read("face_scan_done.py")
        self.assertIn('"/profile/?tab=faces"', src)
        self.assertNotIn("/profile#my-faces", src)


if __name__ == "__main__":
    unittest.main()

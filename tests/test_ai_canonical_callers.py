"""B1: /search intent, prescription vision and the CRM AI health check all go
through ai_client; nothing in models.py, chat.py or crm.py builds its own
OpenAI client.

    python3 -m unittest tests.test_ai_canonical_callers
"""
import ast
import os
import sys
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import ai_client  # noqa: E402


def _src(name):
    with open(os.path.join(REPO, name)) as fh:
        return fh.read()


def _function(module_file, func_name, namespace):
    """Compile one top-level function from a module file into ``namespace``."""
    tree = ast.parse(_src(module_file))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == func_name)
    code = compile(ast.Module(body=[node], type_ignores=[]), module_file, "exec")
    exec(code, namespace)
    return namespace[func_name]


def _completion(text):
    msg = types.SimpleNamespace(content=text)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class NoDirectClientTests(unittest.TestCase):
    def test_no_openai_client_in_search_crm_or_vision(self):
        for name in ("models.py", "crm.py"):
            src = _src(name)
            self.assertNotIn("OpenAI(", src, name)
            self.assertNotIn("from openai import", src, name)
        chat = _src("chat.py")
        self.assertNotIn("_get_openai_client", chat)
        self.assertNotIn('model="gpt-4o"', chat)


class SearchIntentTests(unittest.TestCase):
    def _fn(self, call_model, popped):
        ns = {"call_model": call_model, "pop_calls": lambda: popped.append(1),
              "g": types.SimpleNamespace(request_id="rid-1"), "ast": ast,
              "print": lambda *a, **k: None}
        return _function("models.py", "extract_search_intent", ns)

    def test_intent_is_read_through_the_search_workload(self):
        seen, popped = {}, []

        def fake(**kw):
            seen.update(kw)
            return _completion('{"keywords": ["round"], "filters": {"shape": "Round"}}')

        out = self._fn(fake, popped)("round frames")
        self.assertEqual(out, {"keywords": ["round"], "filters": {"shape": "Round"}})
        self.assertEqual(seen["workload"], "openai_search")
        self.assertEqual(seen["endpoint"], "models.search")
        self.assertEqual(seen["request_id"], "rid-1")
        self.assertEqual(popped, [1], "search telemetry must not reach a chat turn")

    def test_a_model_failure_falls_back_to_the_query_words(self):
        popped = []

        def boom(**kw):
            raise ai_client.ModelDeadlineExceeded("deadline")

        out = self._fn(boom, popped)("Blue Cat Eye")
        self.assertEqual(out, {"keywords": ["blue", "cat", "eye"], "filters": {}})
        self.assertEqual(popped, [1])

    def test_search_workload_is_openai_with_a_bounded_deadline(self):
        wl = ai_client._WORKLOADS["openai_search"]
        self.assertEqual(wl["provider"], "openai")
        self.assertEqual(wl["model"](), os.environ.get("OPENAI_SEARCH_MODEL", "gpt-4"))
        self.assertLessEqual(wl["deadline"](), 30)


class VisionTests(unittest.TestCase):
    def test_vision_always_uses_the_model_layer_and_drains_telemetry(self):
        seen, popped = {}, []

        def fake(**kw):
            seen.update(kw)
            return _completion("YES")

        ns = {"call_model": fake, "pop_calls": lambda: popped.append(1),
              "g": types.SimpleNamespace(request_id="rid-2")}
        fn = _function("chat.py", "_vision_chat", ns)
        with mock.patch.dict(os.environ, {"AI_WRAPPER_ENABLED": "false"}):
            resp = fn([{"role": "user", "content": "x"}], 10, "chat.classify_prescription")
        self.assertEqual(resp.choices[0].message.content, "YES")
        self.assertEqual(seen["workload"], "openai_vision")
        self.assertEqual(seen["temperature"], 0)
        self.assertEqual(seen["max_tokens"], 10)
        self.assertEqual(popped, [1])


class ProviderHealthTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k",
                                                "DEEPSEEK_CHAT_MODEL": "deepseek-v4-flash"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _client(self, ids=None, exc=None):
        listing = mock.Mock()
        if exc:
            listing.models.list.side_effect = exc
        else:
            listing.models.list.return_value = types.SimpleNamespace(
                data=[types.SimpleNamespace(id=i) for i in ids])
        cli = mock.Mock()
        cli.with_options.return_value = listing
        return cli

    def test_no_key_is_reported_without_a_request(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), \
                mock.patch.object(ai_client, "_client") as c:
            self.assertEqual(ai_client.provider_health(), (False, "no_api_key"))
            c.assert_not_called()

    def test_an_unlisted_alias_is_not_an_outage(self):
        # Production: DeepSeek lists deepseek-flash / deepseek-v4-pro, and
        # serves deepseek-v4-flash.
        cli = self._client(ids=["deepseek-flash", "deepseek-v4-pro"])
        with mock.patch.object(ai_client, "_client", return_value=cli):
            self.assertEqual(ai_client.provider_health(), (True, "ok_model_unlisted"))
        cli.with_options.assert_called_once_with(timeout=4.0)

    def test_listed_model_is_ok(self):
        with mock.patch.object(ai_client, "_client",
                               return_value=self._client(ids=["deepseek-v4-flash"])):
            self.assertEqual(ai_client.provider_health(), (True, "ok"))

    def test_a_provider_error_is_named_and_never_raised(self):
        with mock.patch.object(ai_client, "_client",
                               return_value=self._client(exc=ConnectionError("x"))):
            self.assertEqual(ai_client.provider_health(), (False, "ConnectionError"))

    def test_unknown_workload(self):
        self.assertEqual(ai_client.provider_health("nope"), (False, "unknown_workload"))


if __name__ == "__main__":
    unittest.main()

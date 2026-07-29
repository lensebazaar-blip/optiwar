"""Regression tests for the AI capacity/deadline wrapper.

Covers the provider-compatibility bug found during the 100% rollout and the
capacity/routing contracts, per the deployment close-out list:

  - assistant tool-call message carrying `reasoning_content` is sanitized;
  - provider follow-up after a tool result returns the reply;
  - no-tools fallback runs when the follow-up is empty or rejected;
  - empty provider response triggers the fallback (never surfaces empty);
  - a follow-up ModelError (e.g. deadline expiry) returns the busy contract
    instead of falling through;
  - bounded retry (fallback runs at most once - no duplicate-retry loop);
  - capacity rejection is a 503 with Retry-After;
  - deterministic endpoint-percentage canary routing.

Runs without pytest and without the full Flask app: external deps not present
in the test env (openai, httpx) are stubbed, and flaskr.mail is stubbed so the
two real modules under test (ai_client, chat_gateway) import in isolation.

    python3 -m unittest tests.test_ai_wrapper
"""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# stub external deps that aren't installed in the test environment
# --------------------------------------------------------------------------
def _stub_openai():
    if "openai" in sys.modules:
        return
    m = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *a, **k):
            pass

    m.OpenAI = OpenAI
    for name in ("APIConnectionError", "APITimeoutError",
                 "InternalServerError", "RateLimitError"):
        m.__dict__[name] = type(name, (Exception,), {})
    sys.modules["openai"] = m


def _stub_httpx():
    if "httpx" in sys.modules:
        return
    h = types.ModuleType("httpx")

    class Timeout:
        def __init__(self, *a, **k):
            pass

    h.Timeout = Timeout
    sys.modules["httpx"] = h


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_stub_openai()
_stub_httpx()

# fake `flaskr` package so the modules' relative imports resolve
_pkg = types.ModuleType("flaskr")
_pkg.__path__ = [REPO]
sys.modules["flaskr"] = _pkg
_mail = types.ModuleType("flaskr.mail")
_mail.create_ticket_in_db = lambda *a, **k: None
sys.modules["flaskr.mail"] = _mail

aic = _load("flaskr.ai_client", "ai_client.py")
cg = _load("flaskr.chat_gateway", "chat_gateway.py")


# --------------------------------------------------------------------------
# minimal fakes mimicking the OpenAI SDK response shape
# --------------------------------------------------------------------------
class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.type = "function"
        self.function = _Fn(name, arguments)


class _Message:
    def __init__(self, content=None, tool_calls=None, **extra):
        self.content = content
        self.tool_calls = tool_calls
        for k, v in extra.items():
            setattr(self, k, v)


class _Choice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _Resp:
    def __init__(self, message, finish_reason="stop"):
        self.choices = [_Choice(message, finish_reason)]


def _tool_resp(id="call_1", args="{}"):
    # first-turn response: model asks for search_products, and (like v4-flash)
    # carries a provider-specific reasoning_content field that must be stripped.
    return _Resp(
        _Message(content=None,
                 tool_calls=[_ToolCall(id, "search_products", args)],
                 reasoning_content="internal chain-of-thought that must not leak"),
        finish_reason="tool_calls",
    )


def _text_resp(text):
    return _Resp(_Message(content=text), finish_reason="stop")


class _ScriptedModel:
    """Stand-in for ai_client.call_model. Returns/raises scripted items in order
    and records the kwargs of every call (so message-shaping can be asserted)."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _run_wrapped(script, catalog=None, endpoint="chat_gateway.message"):
    sm = _ScriptedModel(script)
    aic.call_model = sm
    cg._search_catalog = lambda **k: (catalog if catalog is not None
                                      else [{"id": 1, "name": "Frame A"}])
    reply, err = cg._call_deepseek_wrapped(
        [{"role": "user", "content": "show me blue frames"}],
        False, endpoint, None,
    )
    return reply, err, sm


# --------------------------------------------------------------------------
# _sanitize_tool_call_message
# --------------------------------------------------------------------------
class SanitizeToolCallMessage(unittest.TestCase):
    def test_drops_reasoning_content_keeps_only_valid_fields(self):
        msg = _Message(
            content="picking frames",
            tool_calls=[_ToolCall("c1", "search_products", '{"color":"blue"}')],
            reasoning_content="chain of thought",
            some_other_provider_field="x",
        )
        out = cg._sanitize_tool_call_message(msg)
        self.assertEqual(set(out.keys()), {"role", "content", "tool_calls"})
        self.assertNotIn("reasoning_content", out)
        self.assertEqual(out["role"], "assistant")
        self.assertEqual(out["content"], "picking frames")
        tc = out["tool_calls"][0]
        self.assertEqual(tc["id"], "c1")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "search_products")
        self.assertEqual(tc["function"]["arguments"], '{"color":"blue"}')

    def test_none_content_becomes_empty_string(self):
        out = cg._sanitize_tool_call_message(_Message(content=None, tool_calls=[]))
        self.assertEqual(out["content"], "")
        self.assertEqual(out["tool_calls"], [])


# --------------------------------------------------------------------------
# _call_deepseek_wrapped
# --------------------------------------------------------------------------
class CallDeepseekWrapped(unittest.TestCase):
    def test_tool_followup_success_sends_sanitized_message(self):
        reply, err, sm = _run_wrapped([_tool_resp(), _text_resp("Here are 3 blue frames.")])
        self.assertIsNone(err)
        self.assertEqual(reply, "Here are 3 blue frames.")
        self.assertEqual(len(sm.calls), 2)
        followup_msgs = sm.calls[1]["messages"]
        assistant_msgs = [m for m in followup_msgs
                          if isinstance(m, dict) and m.get("role") == "assistant"]
        self.assertTrue(assistant_msgs, "follow-up must include the assistant tool-call turn")
        self.assertNotIn("reasoning_content", assistant_msgs[0])
        self.assertTrue(any(isinstance(m, dict) and m.get("role") == "tool"
                            for m in followup_msgs), "tool result must be attached")

    def test_empty_followup_triggers_no_tools_fallback(self):
        reply, err, sm = _run_wrapped(
            [_tool_resp(), _text_resp("   "), _text_resp("Recommended: Frame A")])
        self.assertIsNone(err)
        self.assertEqual(reply, "Recommended: Frame A")
        self.assertEqual(len(sm.calls), 3)
        fb_msgs = sm.calls[2]["messages"]
        self.assertTrue(any(isinstance(m, dict) and m.get("role") == "system"
                            and "recommend ONLY" in (m.get("content") or "")
                            for m in fb_msgs), "fallback injects results as context")
        # fallback is a plain completion: no tool_calls structure, no tool role
        self.assertFalse(any(isinstance(m, dict) and "tool_calls" in m for m in fb_msgs))
        self.assertFalse(any(isinstance(m, dict) and m.get("role") == "tool" for m in fb_msgs))

    def test_provider_400_on_followup_triggers_fallback(self):
        reply, err, sm = _run_wrapped(
            [_tool_resp(), ValueError("Error code: 400 - bad request"),
             _text_resp("Fallback reply")])
        self.assertIsNone(err)
        self.assertEqual(reply, "Fallback reply")
        self.assertEqual(len(sm.calls), 3)

    def test_modelerror_on_followup_returns_busy_contract(self):
        # deadline expiry (or capacity) during the tool follow-up must NOT fall
        # through to the fallback - it returns the busy signal.
        reply, err, sm = _run_wrapped([_tool_resp(), aic.ModelError("deadline_exceeded")])
        self.assertIsNone(reply)
        self.assertEqual(err, "ai_temporarily_unavailable")
        self.assertEqual(len(sm.calls), 2)

    def test_first_call_modelerror_returns_busy_contract(self):
        reply, err, sm = _run_wrapped([aic.ModelError("capacity")])
        self.assertIsNone(reply)
        self.assertEqual(err, "ai_temporarily_unavailable")
        self.assertEqual(len(sm.calls), 1)

    def test_no_tool_call_returns_direct_reply(self):
        reply, err, sm = _run_wrapped([_text_resp("Direct answer, no tools.")])
        self.assertIsNone(err)
        self.assertEqual(reply, "Direct answer, no tools.")
        self.assertEqual(len(sm.calls), 1)

    def test_fallback_runs_at_most_once_no_retry_loop(self):
        # empty follow-up AND empty fallback: returns bounded (no 4th call).
        reply, err, sm = _run_wrapped([_tool_resp(), _text_resp(""), _text_resp("")])
        self.assertIsNone(err)
        self.assertEqual(reply, "")
        self.assertEqual(len(sm.calls), 3)


# --------------------------------------------------------------------------
# capacity / routing contracts (pure functions)
# --------------------------------------------------------------------------
class CapacityContract(unittest.TestCase):
    def test_unavailable_contract_is_503_with_retry_after(self):
        status, body, headers = aic.unavailable_contract(request_id="r1", retry_after=3)
        self.assertEqual(status, 503)
        self.assertEqual(headers["Retry-After"], "3")
        self.assertEqual(body["error"]["code"], "AI_TEMPORARILY_UNAVAILABLE")
        self.assertTrue(body["error"]["retryable"])
        self.assertEqual(body["error"]["retry_after_seconds"], 3)
        self.assertEqual(body["error"]["request_id"], "r1")

    def test_http_error_for_maps_exception_to_503(self):
        status, body, headers = aic.http_error_for(aic.ModelError("x"), request_id="r2")
        self.assertEqual(status, 503)
        self.assertIn("Retry-After", headers)


class CanaryRouting(unittest.TestCase):
    ENV_KEYS = ("AI_WRAPPER_ENABLED", "AI_WRAPPER_ENDPOINTS", "AI_WRAPPER_PERCENT",
                "AI_WRAPPER_ENDPOINT_PERCENT_CHAT_GATEWAY_MESSAGE")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_disabled_flag_routes_direct(self):
        os.environ["AI_WRAPPER_ENABLED"] = "false"
        self.assertEqual(aic.wrapper_route("k", "chat_gateway.message"), (False, -1, 0))

    def test_endpoint_not_in_allowlist_routes_direct(self):
        os.environ["AI_WRAPPER_ENABLED"] = "true"
        os.environ["AI_WRAPPER_ENDPOINTS"] = "ai_api.answer"
        self.assertEqual(aic.wrapper_route("k", "chat_gateway.message"), (False, -1, 0))

    def test_full_rollout_routes_all_traffic(self):
        os.environ["AI_WRAPPER_ENABLED"] = "true"
        os.environ["AI_WRAPPER_ENDPOINTS"] = "chat_gateway.message"
        os.environ["AI_WRAPPER_PERCENT"] = "100"
        enabled, bucket, pct = aic.wrapper_route("any-key", "chat_gateway.message")
        self.assertTrue(enabled)
        self.assertEqual(pct, 100)

    def test_zero_percent_routes_direct(self):
        os.environ["AI_WRAPPER_ENABLED"] = "true"
        os.environ["AI_WRAPPER_ENDPOINTS"] = "chat_gateway.message"
        os.environ["AI_WRAPPER_PERCENT"] = "0"
        enabled, bucket, pct = aic.wrapper_route("any-key", "chat_gateway.message")
        self.assertFalse(enabled)
        self.assertEqual(pct, 0)

    def test_endpoint_override_beats_global_and_is_deterministic(self):
        os.environ["AI_WRAPPER_ENABLED"] = "true"
        os.environ["AI_WRAPPER_ENDPOINTS"] = "chat_gateway.message"
        os.environ["AI_WRAPPER_PERCENT"] = "0"
        os.environ["AI_WRAPPER_ENDPOINT_PERCENT_CHAT_GATEWAY_MESSAGE"] = "100"
        r1 = aic.wrapper_route("conv-42", "chat_gateway.message")
        r2 = aic.wrapper_route("conv-42", "chat_gateway.message")
        self.assertEqual(r1, r2)  # deterministic per (key, endpoint)
        self.assertTrue(r1[0])
        self.assertEqual(r1[2], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)

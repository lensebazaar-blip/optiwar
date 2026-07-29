"""Single AI model client: bounded capacity + monotonic deadline.

Purpose: stop slow/stalled model calls from starving the 20-slot Gunicorn web
tier, and bound total wall-clock per request. This is the ONLY place model calls
should be made from going forward, so no call site hard-codes a model name,
timeout, or retry policy.

Concurrency (per worker process):
- A total per-worker AI cap AND a per-workload pool, both BoundedSemaphores.
  With the shipped defaults (total=1, each workload=1) at most one model call
  runs per worker => aggregate 5 across 5 workers, leaving nominal headroom for
  non-AI traffic. NOTE: this bounds only WRAPPED calls; it is not an absolute
  storefront capacity guarantee until every model call uses this wrapper.
- Acquisition uses a short bounded wait (default 50 ms) -- not truly
  non-blocking -- then sheds load with ModelCapacityExceeded rather than queuing.

Deadline:
- time.monotonic() budget; remaining time is recomputed before the attempt and
  the per-attempt read timeout is bounded by it. SDK retries are disabled
  (max_retries=0); application retries are disabled (0). Structured httpx
  timeouts bound connect/write/pool so connection setup can't eat the budget.

Errors are raised as model-layer exceptions (no HTTP/Flask knowledge here). The
web layer maps them via ``http_error_for`` to one public contract.

Model names are configuration-driven (DeepSeek is deprecating the
deepseek-chat/deepseek-reasoner aliases on 2026-07-24; today they map to
deepseek-v4-flash). Set DEEPSEEK_CHAT_MODEL / OPENAI_CHAT_MODEL / etc. to switch
without code changes. The provider's actual returned model is logged.
"""
import hashlib
import logging
import os
import random
import re
import threading
import time

import httpx
from openai import OpenAI
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

try:
    from flask import current_app, has_app_context
except Exception:  # allow use outside Flask (future workers/CLI)
    current_app = None

    def has_app_context():
        return False


# --------------------------------------------------------------------------
# model-layer exceptions (no HTTP knowledge)
# --------------------------------------------------------------------------
class ModelError(Exception):
    pass


class ModelCapacityExceeded(ModelError):
    def __init__(self, retry_after=3, workload=None):
        self.retry_after = retry_after
        self.workload = workload
        super().__init__(f"model capacity exceeded (workload={workload})")


class ModelDeadlineExceeded(ModelError):
    pass


class ModelProviderUnavailable(ModelError):
    def __init__(self, retry_after=3):
        self.retry_after = retry_after
        super().__init__("model provider unavailable")


# --------------------------------------------------------------------------
# config helpers (env first, Flask config override if in app context)
# --------------------------------------------------------------------------
def _cfg(key, default=None):
    if has_app_context() and current_app is not None:
        val = current_app.config.get(key)
        if val not in (None, ""):
            return val
    val = os.environ.get(key)
    return val if val not in (None, "") else default


def _cfg_int(key, default):
    try:
        return int(_cfg(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_bool(key, default=False):
    val = _cfg(key, None)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


# workload -> (provider, base_url cfg, api_key cfg, model cfg + fallback, default deadline)
_WORKLOADS = {
    "deepseek_chat": {
        "provider": "deepseek",
        "base_url": lambda: _cfg("LLM_BASE_URL", "https://api.deepseek.com"),
        "api_key": lambda: _cfg("DEEPSEEK_API_KEY", ""),
        "model": lambda: _cfg("DEEPSEEK_CHAT_MODEL", _cfg("LLM_MODEL", "deepseek-chat")),
        "deadline": lambda: _cfg_int("AI_DEADLINE_CHAT", 18),
        # DeepSeek v4 models default to THINKING mode; the legacy deepseek-chat
        # alias is non-thinking. Keep non-thinking by default so an explicit
        # deepseek-v4-flash switch is behaviour-neutral (no latency/cost/output
        # regression). Set AI_DEEPSEEK_THINKING=enabled to opt in.
        "thinking": lambda: _cfg("AI_DEEPSEEK_THINKING", "disabled"),
    },
    "deepseek_classify": {
        "provider": "deepseek",
        "base_url": lambda: _cfg("LLM_BASE_URL", "https://api.deepseek.com"),
        "api_key": lambda: _cfg("DEEPSEEK_API_KEY", ""),
        "model": lambda: _cfg("DEEPSEEK_CHAT_MODEL", _cfg("LLM_MODEL", "deepseek-chat")),
        "deadline": lambda: _cfg_int("AI_DEADLINE_CLASSIFY", 10),
        "thinking": lambda: _cfg("AI_DEEPSEEK_THINKING", "disabled"),
    },
    "deepseek_recommend": {
        "provider": "deepseek",
        "base_url": lambda: _cfg("LLM_BASE_URL", "https://api.deepseek.com"),
        "api_key": lambda: _cfg("DEEPSEEK_API_KEY", ""),
        "model": lambda: _cfg("DEEPSEEK_CHAT_MODEL", _cfg("LLM_MODEL", "deepseek-chat")),
        "deadline": lambda: _cfg_int("AI_DEADLINE_RECOMMEND", 20),
        "thinking": lambda: _cfg("AI_DEEPSEEK_THINKING", "disabled"),
    },
    "openai_chat": {
        "provider": "openai",
        "base_url": lambda: _cfg("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key": lambda: _cfg("OPENAI_API_KEY", ""),
        "model": lambda: _cfg("OPENAI_CHAT_MODEL", "gpt-4o"),
        "deadline": lambda: _cfg_int("AI_DEADLINE_CHAT", 18),
    },
    "openai_vision": {
        "provider": "openai",
        "base_url": lambda: _cfg("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key": lambda: _cfg("OPENAI_API_KEY", ""),
        "model": lambda: _cfg("OPENAI_VISION_MODEL", "gpt-4o"),
        "deadline": lambda: _cfg_int("AI_DEADLINE_VISION", 28),
    },
}


# --------------------------------------------------------------------------
# per-process capacity pools + own active-call counter
# --------------------------------------------------------------------------
class _Pools:
    def __init__(self, total_slots, workload_slots):
        self.total = threading.BoundedSemaphore(total_slots)
        self.total_limit = total_slots
        self.workload = {w: threading.BoundedSemaphore(n) for w, n in workload_slots.items()}
        self.workload_limit = dict(workload_slots)
        self.lock = threading.Lock()
        self.active = 0


_pools = None
_pools_lock = threading.Lock()


def _get_pools():
    global _pools
    if _pools is None:
        with _pools_lock:
            if _pools is None:
                total = _cfg_int("AI_TOTAL_SLOTS_PER_WORKER", 1)
                ds = _cfg_int("AI_DEEPSEEK_SLOTS_PER_WORKER", 1)
                oc = _cfg_int("AI_OPENAI_CHAT_SLOTS_PER_WORKER", 1)
                ov = _cfg_int("AI_OPENAI_VISION_SLOTS_PER_WORKER", 1)
                _pools = _Pools(total, {
                    "deepseek_chat": ds,
                    "deepseek_classify": ds,
                    "deepseek_recommend": ds,
                    "openai_chat": oc,
                    "openai_vision": ov,
                })
    return _pools


# cache one client per (base_url, api_key) with retries disabled
_clients = {}
_clients_lock = threading.Lock()


def _client(base_url, api_key):
    ck = (base_url, api_key)
    cli = _clients.get(ck)
    if cli is None:
        with _clients_lock:
            cli = _clients.get(ck)
            if cli is None:
                cli = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
                _clients[ck] = cli
    return cli


def _retry_after():
    return random.choice([2, 3, 4])  # jitter to avoid synchronized retry waves


ACQUIRE_WAIT = 0.05  # 50 ms short bounded wait
MIN_ATTEMPT = 2.0    # don't start an attempt with < 2s of budget left
PER_ATTEMPT_TIMEOUT = 15.0  # cap one provider read at min(this, remaining budget)


_metrics_logger = None
_metrics_lock = threading.Lock()


def _metrics_log():
    """Dedicated, durable AI-metrics logger.

    The app logger writes to debug.log which is rotated daily, so a canary sample
    can't survive long enough to compute p50/p95/p99 etc. This writes the same
    ``ai-call`` line to a separate file (default /var/log/optiwar/ai_metrics.log)
    using a plain append FileHandler -- safe across the Gunicorn workers (each
    record is a single O_APPEND write) -- with rotation handled externally by
    logrotate (copytruncate). Fails open: never raises into the request path.
    """
    global _metrics_logger
    if _metrics_logger is not None:
        return _metrics_logger
    with _metrics_lock:
        if _metrics_logger is not None:
            return _metrics_logger
        lg = logging.getLogger("optiwar.ai_metrics")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            path = _cfg("AI_METRICS_LOG", "/var/log/optiwar/ai_metrics.log")
            try:
                h = logging.FileHandler(path, delay=True)
                h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                lg.addHandler(h)
            except Exception:
                lg.addHandler(logging.NullHandler())
        _metrics_logger = lg
        return _metrics_logger


def _emit(prefix, logger, **fields):
    line = prefix + " " + " ".join(f"{k}={v}" for k, v in fields.items())
    if logger is not None:
        try:
            logger.info(line)
        except Exception:
            pass
    try:
        _metrics_log().info(line)
    except Exception:
        pass


def _log(logger, **fields):
    _emit("ai-call", logger, **fields)


def log_route(endpoint, path, bucket, percent, request_id=None, **fields):
    """Emit a per-request routing marker (wrapper vs direct SDK) to the durable
    metrics log so the canary split, its bucketing, and direct-path latency are
    observable. Contains only counts/labels -- no prompt content, no PII. Fails
    open: never raises into the request path."""
    if request_id is None:
        try:
            from flask import g, has_request_context
            request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        except Exception:
            request_id = "-"
    _emit("ai-route", None, endpoint=endpoint, path=path,
          canary_percent=percent, bucket=bucket, rid=request_id, **fields)


def _logger():
    if has_app_context() and current_app is not None:
        return current_app.logger
    return None


_VALID_THINKING = {"disabled", "enabled"}


def validate_config():
    """Reject an invalid thinking value for the DeepSeek workloads at startup so a
    typo can't silently restore thinking mode (which can yield empty content).
    Returns the resolved thinking value."""
    val = str(_cfg("AI_DEEPSEEK_THINKING", "disabled")).lower()
    if val not in _VALID_THINKING:
        raise ValueError(
            f"AI_DEEPSEEK_THINKING={val!r} is invalid; must be one of {sorted(_VALID_THINKING)}"
        )
    return val


def _check_model_available(app, model):
    """Best-effort startup probe: warn loudly if the configured DeepSeek chat
    model is not in the account's /models list (e.g. a future rename or the
    2026-07-24 alias retirement). Never raises and never auto-switches — the
    model is config-driven so an operator changes one env var to fix it."""
    api_key = _cfg("DEEPSEEK_API_KEY", "")
    if not api_key:
        return
    try:
        base_url = _cfg("LLM_BASE_URL", "https://api.deepseek.com")
        names = {m.id for m in OpenAI(
            api_key=api_key, base_url=base_url, max_retries=0, timeout=5.0
        ).models.list().data}
        if model not in names:
            app.logger.warning(
                "ai-wrapper MODEL CHECK: configured DEEPSEEK_CHAT_MODEL=%s is NOT in "
                "the provider's available models %s -- DeepSeek chat calls will fail "
                "until DEEPSEEK_CHAT_MODEL is updated to a supported name.",
                model, sorted(names),
            )
        else:
            app.logger.info("ai-wrapper model check: %s available (of %s)", model, sorted(names))
    except Exception as e:
        app.logger.info("ai-wrapper model check skipped (%s)", type(e).__name__)


def init_ai_client(app):
    """Validate config at app startup and log the resolved AI wrapper state."""
    thinking = validate_config()
    model = _cfg("DEEPSEEK_CHAT_MODEL", _cfg("LLM_MODEL", "deepseek-chat"))
    overrides = {
        k: v for k, v in os.environ.items()
        if k.startswith("AI_WRAPPER_ENDPOINT_PERCENT_")
    }
    app.logger.info(
        "ai-wrapper init enabled=%s percent=%s endpoints=%s endpoint_overrides=%s "
        "deepseek_thinking=%s deepseek_model=%s total_slots/worker=%s deepseek_slots/worker=%s "
        "deadline_chat=%ss per_attempt=%ss acquire_wait=%sms",
        _cfg_bool("AI_WRAPPER_ENABLED", False),
        _cfg_int("AI_WRAPPER_PERCENT", 0),
        _cfg("AI_WRAPPER_ENDPOINTS", "(none)"),
        overrides or "(none)",
        thinking,
        model,
        _cfg_int("AI_TOTAL_SLOTS_PER_WORKER", 1),
        _cfg_int("AI_DEEPSEEK_SLOTS_PER_WORKER", 1),
        _cfg_int("AI_DEADLINE_CHAT", 18),
        _cfg_int("AI_PER_ATTEMPT_TIMEOUT", int(PER_ATTEMPT_TIMEOUT)),
        int(ACQUIRE_WAIT * 1000),
    )
    if _cfg_bool("AI_MODEL_STARTUP_CHECK", True):
        _check_model_available(app, model)


def _endpoint_percent_env(endpoint):
    """Env var name holding a per-endpoint rollout override, e.g.
    ``chat_gateway.message`` -> ``AI_WRAPPER_ENDPOINT_PERCENT_CHAT_GATEWAY_MESSAGE``."""
    norm = re.sub(r"[^0-9A-Za-z]+", "_", endpoint or "").strip("_").upper()
    return f"AI_WRAPPER_ENDPOINT_PERCENT_{norm}"


def _resolve_percent(endpoint):
    """Endpoint-specific rollout percentage if configured, else the global
    ``AI_WRAPPER_PERCENT``. Lets one endpoint (e.g. the hot widget path) canary at
    a low percent while the already-proven endpoints stay at 100%."""
    if endpoint:
        raw = _cfg(_endpoint_percent_env(endpoint), None)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return _cfg_int("AI_WRAPPER_PERCENT", 0)


def _bucket(key, endpoint):
    """Deterministic 0-99 bucket, stable across processes/restarts (SHA-256, not
    the process-salted builtin hash) and scoped per (key, endpoint) so a single
    conversation stays in one canary group and never alternates paths."""
    seed = f"{key or ''}|{endpoint or ''}"
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % 100


def wrapper_route(key, endpoint=None):
    """Routing decision for the WEB LAYER: returns ``(enabled, bucket, percent)``.

    ``enabled`` True => route via the guarded wrapper; False => existing direct
    path. Decision is deterministic per (key, endpoint). This ONLY selects the
    path; it changes nothing about prompts, model, formatting, idempotency, DB
    writes, retries, or error contracts.
    """
    if not _cfg_bool("AI_WRAPPER_ENABLED", False):
        return (False, -1, 0)
    allow = _cfg("AI_WRAPPER_ENDPOINTS", "")
    if allow:
        allowed = {e.strip() for e in str(allow).split(",") if e.strip()}
        if endpoint is None or endpoint not in allowed:
            return (False, -1, 0)
    pct = _resolve_percent(endpoint)
    bucket = _bucket(key, endpoint)
    if pct <= 0:
        return (False, bucket, 0)
    if pct >= 100:
        return (True, bucket, 100)
    return (bucket < pct, bucket, pct)


def wrapper_enabled_for(key, endpoint=None):
    """Back-compat boolean gate; see ``wrapper_route`` for the full decision."""
    return wrapper_route(key, endpoint)[0]


def unavailable_contract(request_id="-", retry_after=None):
    """The single public 'AI temporarily busy' contract as (status, body, headers).
    Shared by the web layer and the chat routes so retryable 503s look identical."""
    ra = int(retry_after) if retry_after else _retry_after()
    body = {
        "error": {
            "code": "AI_TEMPORARILY_UNAVAILABLE",
            "message": "The assistant is temporarily busy. Please try again shortly.",
            "retryable": True,
            "retry_after_seconds": ra,
            "request_id": request_id,
        }
    }
    return 503, body, {"Retry-After": str(ra)}


def http_error_for(exc, request_id="-"):
    """Map a model-layer exception to (status_code, body_dict, headers). One
    public contract; the internal reason stays in logs only."""
    return unavailable_contract(request_id, getattr(exc, "retry_after", None))


def call_model(*, workload, messages, model=None, deadline=None, max_tokens=None,
               temperature=None, endpoint="-", request_id="-", **extra):
    """Single guarded chat-completion entry point. Raises ModelError subclasses."""
    if workload not in _WORKLOADS:
        raise ValueError(f"unknown workload {workload!r}")
    wl = _WORKLOADS[workload]
    base_url = wl["base_url"]()
    api_key = wl["api_key"]()
    model = model or wl["model"]()
    deadline_s = float(deadline if deadline is not None else wl["deadline"]())
    logger = _logger()

    pools = _get_pools()
    deadline_at = time.monotonic() + deadline_s
    wait_start = time.monotonic()

    got_total = pools.total.acquire(timeout=ACQUIRE_WAIT)
    if not got_total:
        _log(logger, endpoint=endpoint, workload=workload, provider=wl["provider"],
             outcome="capacity_rejected", reason="total_full",
             capacity_limit=pools.total_limit, rid=request_id)
        raise ModelCapacityExceeded(retry_after=_retry_after(), workload=workload)

    got_wl = False
    try:
        got_wl = pools.workload[workload].acquire(timeout=ACQUIRE_WAIT)
        if not got_wl:
            _log(logger, endpoint=endpoint, workload=workload, provider=wl["provider"],
                 outcome="capacity_rejected", reason="workload_full",
                 capacity_limit=pools.workload_limit.get(workload), rid=request_id)
            raise ModelCapacityExceeded(retry_after=_retry_after(), workload=workload)

        with pools.lock:
            pools.active += 1
            in_use = pools.active
        queue_wait_ms = int((time.monotonic() - wait_start) * 1000)

        remaining = deadline_at - time.monotonic()
        if remaining <= MIN_ATTEMPT:
            _log(logger, endpoint=endpoint, workload=workload, provider=wl["provider"],
                 outcome="deadline_exceeded", reason="pre_attempt",
                 queue_wait_ms=queue_wait_ms, rid=request_id)
            raise ModelDeadlineExceeded()

        per_attempt = float(_cfg_int("AI_PER_ATTEMPT_TIMEOUT", int(PER_ATTEMPT_TIMEOUT)))
        read_budget = max(MIN_ATTEMPT, min(per_attempt, remaining))
        timeout = httpx.Timeout(read_budget, connect=3.0, write=5.0, pool=1.0)
        call_kwargs = {"model": model, "messages": messages}
        if max_tokens is not None:
            call_kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            call_kwargs["temperature"] = temperature
        call_kwargs.update(extra)
        # Explicit DeepSeek thinking-mode selection (default disabled) unless the
        # caller already set it. Avoids silently enabling thinking mode when the
        # model is switched from the deepseek-chat alias to deepseek-v4-flash.
        thinking = wl.get("thinking")
        thinking_mode = str(thinking()).lower() if thinking is not None else "-"
        if thinking_mode == "disabled":
            eb = dict(call_kwargs.get("extra_body") or {})
            eb.setdefault("thinking", {"type": "disabled"})
            call_kwargs["extra_body"] = eb

        provider_start = time.monotonic()
        try:
            resp = _client(base_url, api_key).with_options(timeout=timeout).chat.completions.create(**call_kwargs)
        except APITimeoutError as e:
            _log(logger, endpoint=endpoint, workload=workload, provider=wl["provider"],
                 outcome="deadline_exceeded", reason="provider_timeout",
                 requested_model=model, queue_wait_ms=queue_wait_ms,
                 provider_duration_ms=int((time.monotonic() - provider_start) * 1000),
                 rid=request_id)
            raise ModelDeadlineExceeded() from e
        except RateLimitError as e:
            _log(logger, endpoint=endpoint, workload=workload, provider=wl["provider"],
                 outcome="provider_429", requested_model=model,
                 queue_wait_ms=queue_wait_ms, rid=request_id)
            raise ModelCapacityExceeded(retry_after=_retry_after(), workload=workload) from e
        except (APIConnectionError, InternalServerError) as e:
            _log(logger, endpoint=endpoint, workload=workload, provider=wl["provider"],
                 outcome="provider_unavailable", reason=type(e).__name__,
                 requested_model=model, queue_wait_ms=queue_wait_ms, rid=request_id)
            raise ModelProviderUnavailable(retry_after=_retry_after()) from e

        usage = getattr(resp, "usage", None)
        try:
            _msg = resp.choices[0].message
            reasoning_present = bool(getattr(_msg, "reasoning_content", None))
            content_empty = not (getattr(_msg, "content", None) or "").strip()
        except Exception:
            reasoning_present = False
            content_empty = True
        _log(logger, endpoint=endpoint, workload=workload, provider=wl["provider"],
             outcome="ok", requested_model=model, actual_model=getattr(resp, "model", "-"),
             thinking_mode=thinking_mode, reasoning_present=reasoning_present,
             content_empty=content_empty,
             queue_wait_ms=queue_wait_ms,
             provider_duration_ms=int((time.monotonic() - provider_start) * 1000),
             total_duration_ms=int((time.monotonic() - wait_start) * 1000),
             capacity_limit=pools.total_limit, capacity_in_use=in_use,
             input_tokens=getattr(usage, "prompt_tokens", "-") if usage else "-",
             output_tokens=getattr(usage, "completion_tokens", "-") if usage else "-",
             rid=request_id)
        return resp
    finally:
        if got_wl:
            with pools.lock:
                pools.active -= 1
            pools.workload[workload].release()
        pools.total.release()

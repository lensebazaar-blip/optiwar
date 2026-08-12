#!/usr/bin/env python3
"""Declared registry of every production AI workload.

`ai_client._WORKLOADS` is the *runtime* half: it says which provider, model and
deadline a workload uses. This module is the *declared* half — the facts a
runtime dict cannot carry, and the ones needed to answer "which AI produced this
production behaviour, and what was it allowed to do":

  purpose        what the workload exists to do, in one line
  fallback       what happens when it fails; None means "the caller degrades"
  timeout_env    the variable that governs its wall-clock deadline
  cost_basis     price per million tokens, and where that number came from
  declared_on    when this entry was written or last materially changed

Registry and runtime are cross-checked by a test rather than by convention: a
workload added to `ai_client` without an entry here, or an entry here for a
workload that no longer exists, fails the suite. That is the whole mechanism —
a registry that depends on someone remembering to update it is wrong within a
month.

Run it to see the declared registry beside what the code will actually use:

    python3 ai_model_registry.py
"""
from __future__ import annotations

# Cost basis is deliberately not guessed. Provider list prices change and an
# invented number in an operations registry is worse than an absent one, because
# it will be quoted. UNDECLARED means "fill this from the provider invoice";
# the printer and the drift test both surface it as missing rather than hiding it.
UNDECLARED = None

REGISTRY = {
    "deepseek_chat": {
        "purpose": "Customer conversation turns in the shopping assistant.",
        "fallback": "AI_TEMPORARILY_UNAVAILABLE (503); the widget soft-retries.",
        "timeout_env": "AI_DEADLINE_CHAT",
        "cost_basis": UNDECLARED,
        "declared_on": "2026-08-12",
    },
    "deepseek_classify": {
        "purpose": "Short structured classification of a customer message "
                   "(intent, escalation, product vs support).",
        "fallback": "Caller treats an unavailable classifier as 'unclassified' "
                    "and takes the conservative branch.",
        "timeout_env": "AI_DEADLINE_CLASSIFY",
        "cost_basis": UNDECLARED,
        "declared_on": "2026-08-12",
    },
    "deepseek_recommend": {
        "purpose": "Product recommendation over the catalogue search results.",
        "fallback": "Deterministic catalogue search without model ranking.",
        "timeout_env": "AI_DEADLINE_RECOMMEND",
        "cost_basis": UNDECLARED,
        "declared_on": "2026-08-12",
    },
    "openai_chat": {
        "purpose": "Secondary conversation provider; not the default path.",
        "fallback": "AI_TEMPORARILY_UNAVAILABLE (503).",
        "timeout_env": "AI_DEADLINE_CHAT",
        "cost_basis": UNDECLARED,
        "declared_on": "2026-08-12",
    },
    "openai_vision": {
        "purpose": "Image understanding for picture-led product search.",
        "fallback": "Text-only search; the image is not retried elsewhere.",
        "timeout_env": "AI_DEADLINE_VISION",
        "cost_basis": UNDECLARED,
        "declared_on": "2026-08-12",
    },
}

REQUIRED_FIELDS = ("purpose", "fallback", "timeout_env", "cost_basis",
                   "declared_on")


def runtime_workloads():
    """Workload -> resolved runtime facts, read from the single place that
    performs model calls. Imported lazily so the registry stays inspectable
    (and testable) without Flask or a provider key present."""
    from ai_client import _WORKLOADS  # noqa: PLC0415 - see docstring

    out = {}
    for name, wl in _WORKLOADS.items():
        def _resolve(key):
            v = wl.get(key)
            try:
                return v() if callable(v) else v
            except Exception:
                return None
        out[name] = {
            "provider": wl.get("provider"),
            "model": _resolve("model"),
            "deadline_s": _resolve("deadline"),
        }
    return out


def drift(runtime=None):
    """Names declared but not implemented, and implemented but not declared."""
    rt = runtime if runtime is not None else runtime_workloads()
    return {
        "declared_not_implemented": sorted(set(REGISTRY) - set(rt)),
        "implemented_not_declared": sorted(set(rt) - set(REGISTRY)),
    }


def main():
    rt = runtime_workloads()
    print("DECLARED AI WORKLOADS")
    for name in sorted(set(REGISTRY) | set(rt)):
        d = REGISTRY.get(name)
        r = rt.get(name)
        print("\n  %s" % name)
        if r is None:
            print("    NOT IMPLEMENTED — declared here but absent from ai_client")
        else:
            print("    provider/model  %s / %s" % (r["provider"], r["model"]))
            print("    deadline        %ss (%s)" % (
                r["deadline_s"], d["timeout_env"] if d else "?"))
        if d is None:
            print("    UNDECLARED WORKLOAD — implemented but not in the registry")
            continue
        print("    purpose         %s" % d["purpose"])
        print("    fallback        %s" % d["fallback"])
        print("    cost basis      %s" % (d["cost_basis"] or "UNDECLARED"))
        print("    declared on     %s" % d["declared_on"])

    dr = drift(rt)
    if dr["declared_not_implemented"] or dr["implemented_not_declared"]:
        print("\nDRIFT: %s" % dr)
        return 1
    print("\nregistry and runtime agree on %d workload(s)" % len(rt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared host-LLM face fake for filemem consolidation tests (epic card 2).

Card 2 switched consolidation/llm_caller from the ctx.llm.complete_structured
face to the host call surface (agent.auxiliary_client.call_llm) driven by an
EXPLICIT main_runtime snapshot (card 1's _main_runtime). The real llm_caller
now routes every face through the module-level ``_HOST_CALL_LLM`` and parses
the host's raw JSON reply itself.

Tests drive the REAL llm_caller by monkeypatching ``llm_caller._HOST_CALL_LLM``
with a fake that returns host-shaped responses:
    {"choices": [{"message": {"content": <json string>}}]}

Replies are matched two ways (mirrors the old ctx.llm fakes):
  * list  -> positional by call order (old confirm FakeLLM shape)
  * dict  -> substring in the prompt (old observe / alias shape)
An Exception-valued reply (or a dict of Exceptions) is raised.
"""
import json

_MAIN_RUNTIME = {
    "model": "test-main-model",
    "provider": "test-provider",
    "base_url": "https://test.invalid/v1",
    "api_key": "test-key",
    "api_mode": "openai",
}


def make_host_fake(replies=None, raise_exc=None):
    """Return (fake, state). ``fake`` is callable like the host call_llm:
    fake(main_runtime=..., messages=..., temperature=..., max_tokens=...)."""
    state = {
        "calls": 0,
        "kwarg_log": [],     # per-call kwargs (proves no task=, temp/mt budgets)
        "prompts": [],       # per-call user-message content
        "fake": None,
    }

    def _pick(content):
        reps = replies if replies is not None else {}
        if isinstance(reps, list):
            idx = min(state["calls"] - 1, max(len(reps) - 1, 0))
            return reps[idx]
        # dict: substring match, "*" fallback
        picked = None
        for key, payload in reps.items():
            if key != "*" and key in content:
                picked = payload
                break
        if picked is None:
            picked = reps.get("*", {})
        # dynamic reply: a callable payload receives the prompt content and
        # returns the parsed reply for that call (per-topic canned answers).
        if callable(picked):
            picked = picked(content)
        return picked

    def fake(main_runtime=None, messages=None, temperature=None,
             max_tokens=None, **kw):
        state["calls"] += 1
        state["kwarg_log"].append({
            "main_runtime": main_runtime, "temperature": temperature,
            "max_tokens": max_tokens, **kw})
        content = ""
        if messages:
            m0 = messages[0]
            content = m0.get("content", "") if isinstance(m0, dict) else str(m0)
        state["prompts"].append(content)
        if raise_exc is not None:
            raise raise_exc
        picked = _pick(content)
        if isinstance(picked, Exception):
            raise picked
        body = json.dumps(picked, ensure_ascii=False)
        return {"choices": [{"message": {"content": body}}]}

    state["fake"] = fake
    return fake, state


def install(monkeypatch, replies=None, raise_exc=None, llm_caller_mod=None):
    """Route every consolidation face through a scripted host-call fake;
    returns (fake, state). Covers BOTH call paths in one call:

      * provider-driven faces (_llm_consolidate / _supersede_confirm_candidates
        / _mine_alias_candidates / _observe_consolidate -> _load_llm_caller,
        which RE-EXCS llm_caller.py fresh): a fresh exec re-binds
        ``_HOST_CALL_LLM`` from ``sys.modules["agent.auxiliary_client"]`` at
        call time, so we seed that module object here (auto-reverted). The
        host tree is not importable under a standalone / py3.8-only run, so
        the seed is always needed in the test env.
      * direct face calls (distill / confirm_supersede / ... imported at the
        test top): those closed over the already-imported
        ``consolidation.llm_caller`` module, so we patch its
        ``_HOST_CALL_LLM`` seam directly.

    Both routes land on the same ``fake``; ``state`` records calls / prompts
    / per-call kwargs. Pass llm_caller_mod to pin the direct-call module."""
    import sys
    import types
    fake, state = make_host_fake(replies=replies, raise_exc=raise_exc)

    # provider-driven path: seed the host module so each fresh re-exec binds
    # the fake call surface (the real host module is absent under py3.8).
    host_mod = types.ModuleType("agent.auxiliary_client")
    host_mod.call_llm = fake
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", host_mod)

    # direct-call path: patch the already-imported module's seam too.
    import consolidation.llm_caller as _lc
    monkeypatch.setattr(llm_caller_mod or _lc, "_HOST_CALL_LLM", fake)
    return fake, state

"""Host main-model LLM face for filemem consolidation (explicit main_runtime).

All six faces call the host `agent.auxiliary_client.call_llm` with the
EXPLICIT main_runtime snapshot the plugin captures at register() time (see
__init__.py: _HOST_MAIN_RUNTIME, epic card t_32980f71), mirroring the
supercompress engine.py pattern:

    call_llm(main_runtime=<dict>,
              messages=[{"role": "user", "content": instructions + schema + text}],
              temperature=0.0, max_tokens=<face budget>)

No task key anywhere (host main-model route — no auxiliary task-slot
pinning), zero hardcoded model names, zero provider config keys, zero
third-party SDK imports: the plugin imports the host call surface only,
and host files are never touched.

The host returns raw text; the plugin parses the JSON reply itself
(stay stdlib-only: the schema constants below shape the prompt, a
fence-stripping json.loads parse keeps the SAME field-reading logic the
old structured face gave each caller).

Fail-soft (不吞脏 / 不静默): a None main_runtime snapshot (standalone /
non-host / capture degrade) or a missing host call surface takes the
existing keyword/skip path — every face returns its verdict-shaped
result (model_used=False, reason=model_unavailable_fallback=...), never
raises into the host, never silently swallows a reply.
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

try:  # host call surface — plugin-side import only. Standalone / older
      # host builds (or an interpreter that cannot load the host tree)
      # degrade to None -> every face keeps its fail-soft keyword/skip
      # path below (zero crash).
    from agent.auxiliary_client import call_llm as _HOST_CALL_LLM
except Exception:  # pragma: no cover - standalone / older-host fallback
    _HOST_CALL_LLM = None

# Test-injection seam (the host tree is absent under a standalone
# interpreter / py3.8-only test run): behavior tests install a fake by
# monkeypatching _HOST_CALL_LLM directly. Production always resolves the
# real host call surface when the host tree is importable.

logger = logging.getLogger("filemem.consolidation.llm_caller")


# ── shared host-call face (module header: outside the per-EP audit segments) ──

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)


def _strip_code_fences(text: str) -> str:
    """The first fenced code block's body, or the stripped text when unfenced."""
    match = _FENCE_RE.search(text or "")
    return match.group(1).strip() if match else (text or "").strip()


def _llm_text(response: Any) -> str:
    """Assistant text of a host LLM response (dict- or namespace-shaped,
    string or list-of-parts content)."""
    try:
        if isinstance(response, dict):
            msg = response["choices"][0]["message"]
        else:
            msg = response.choices[0].message
    except Exception:
        raise ValueError("unexpected host LLM response shape")
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(getattr(part, "text", "") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


def _parse_structured_reply(text: str) -> Optional[Dict[str, Any]]:
    """Plugin-side JSON parse of the host reply (the host helper for this is
    a test-only surface, so the plugin parses its own replies — stdlib
    json only): fence-strip, json.loads, dict-or-None. A non-dict or
    unparseable reply degrades to None -> the face's fail-soft verdict
    (不吞脏: the caller decides, nothing is silently accepted)."""
    if not text:
        return None
    try:
        parsed = json.loads(_strip_code_fences(text))
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _structured_user_message(instructions: str, schema: Dict[str, Any], text: str) -> str:
    """One user message carrying the model-visible content of the old
    structured face: instructions + the JSON schema shape + the JSON-only
    directive + the input text (the host built these as separate messages /
    parts; call_llm takes a flat messages list, so they ride one message)."""
    try:
        schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        schema_text = str(schema)
    return (
        f"{(instructions or '').strip()}\n\n"
        f"JSON schema:\n{schema_text}\n\n"
        "Respond with a single JSON object that matches the requested shape. "
        "Do not include prose or markdown fences.\n\n"
        f"Input:\n{text}"
    )


# ── epic card 3: per-call telemetry seam (consolidation_log llm_call rows) ──
# Every face records ONE result on the module-level _last_telemetry dict:
# the concrete route attribution the host call_llm RETURNS via route_info
# (provider / model of the route actually used) — falling back to the
# main_runtime snapshot's declared provider / model — plus the wall-time of
# the host call (duration_ms), ok / fallback, and a reason string on any
# fallback. The provider call sites in __init__.py read the dict off their
# own (fresh-per-call, re-exec'd) llm_caller module object and append ONE
# face-level row {ts, face, model, provider, duration_ms, ok, fallback,
# reason?} to the EXISTING consolidation_log.jsonl — no new log file, no
# changed existing-row schema (reuse the append + flock protocol). An
# empty / whitespace-only host reply is thereby manifested as a grep-able
# fallback=True reason=empty_response row instead of the old silent 0.5 /
# medium fall-through (the caller's fail-soft verdict value is UNCHANGED —
# only the log row now records that it went through the fallback).
_last_telemetry: Dict[str, Any] = None


def _set_last_telemetry(model, provider, duration_ms: int, ok: bool,
                        fallback: bool, reason: Optional[str] = None) -> None:
    global _last_telemetry
    tel: Dict[str, Any] = {
        "model": model, "provider": provider, "duration_ms": duration_ms,
        "ok": ok, "fallback": fallback,
    }
    if reason:
        tel["reason"] = reason
    _last_telemetry = tel


def _telemetry_route(main_runtime, route_info: Dict[str, str]) -> tuple:
    """(model, provider): the concrete route attribution the host
    call_llm filled into route_info, else the main_runtime snapshot's
    declared values (the snapshot is the explicit card-1 capture)."""
    mr = main_runtime if isinstance(main_runtime, dict) else {}
    ri = route_info if isinstance(route_info, dict) else {}
    model = ri.get("model") or mr.get("model")
    provider = ri.get("provider") or mr.get("provider") or mr.get("requested_provider")
    return model, provider


def _record_face_fallback(main_runtime, exc) -> None:
    """Face-level fail-soft record (each face's except handler calls it):
    no host call surface / no main_runtime snapshot / host transport
    exception / unexpected response shape — record a FRESH ok=False
    fallback=True row-data on the seam so the provider site's llm_call
    row manifests this call's fallback — while the VERDICT returned to
    the caller stays exactly the pre-card-3 fail-soft value. (When
    _structured_call already recorded this call — empty / unparseable
    reply, no raise — the face's try-block succeeds, this never runs,
    and that call's recording stands.)"""
    model, provider = _telemetry_route(main_runtime, {})
    _set_last_telemetry(model, provider, 0, False, True,
                        reason=f"model_unavailable: {exc}")


def _structured_call(
    main_runtime: Optional[Dict[str, Any]],
    instructions: str,
    schema: Dict[str, Any],
    text: str,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Explicit host main-model call (supercompress pattern): the captured
    main_runtime snapshot + one user message, no task key, zero
    provider/model configuration anywhere in the plugin. Raises on a
    missing snapshot / call surface, a host failure, or an unexpected
    response shape — the calling face catches and returns its fail-soft
    verdict (不静默: the verdict carries model_used=False + reason).

    epic card 3: the host route attribution comes back in the route_info
    dict the host fills (provider / model of the concrete route used);
    an empty / whitespace-only reply and an unparseable reply are
    recorded on the _last_telemetry seam (fallback=True + reason) — the
    face's fail-soft verdict value is unchanged, only the log row now
    manifests it."""
    if _HOST_CALL_LLM is None:
        raise RuntimeError("host call surface unavailable (standalone / older host build)")
    if not isinstance(main_runtime, dict) or not main_runtime:
        raise RuntimeError("no host main_runtime snapshot (standalone / non-host)")
    route_info: Dict[str, str] = {}
    t0 = time.monotonic()
    response = _HOST_CALL_LLM(
        main_runtime=main_runtime,
        messages=[{"role": "user", "content": _structured_user_message(instructions, schema, text)}],
        temperature=temperature,
        max_tokens=max_tokens,
        route_info=route_info,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    model, provider = _telemetry_route(main_runtime, route_info)
    reply_text = _llm_text(response)  # unexpected shape -> ValueError -> face fallback
    if not reply_text.strip():
        # epic card 3: EMPTY response manifested (no longer a silent
        # 0.5 / medium fall-through): the face still returns its
        # fail-soft defaults, but the llm_call row says fallback=True.
        _set_last_telemetry(model, provider, duration_ms, False, True,
                            reason="empty_response")
        return None
    parsed = _parse_structured_reply(reply_text)
    if parsed is None:
        _set_last_telemetry(model, provider, duration_ms, False, True,
                            reason="unparseable_response")
    else:
        _set_last_telemetry(model, provider, duration_ms, True, False)
    return parsed


SALIENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "salience": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string"},
    },
    "required": ["salience"],
}

SALIENCE_INSTRUCTIONS = ("Rate the memorability/salience of this content for "
                         "long-term recall. Return a JSON object.")


def score_salience(main_runtime: Optional[Dict[str, Any]], content: str) -> Dict[str, Any]:
    """Return {'salience': float, 'reason': str, 'model_used': bool}.

    Explicit main_runtime host face. ANY failure (no snapshot / host call
    surface missing / host down / timeout / unparseable reply) falls back
    to the keyword-only verdict (salience 0.5, model_used=False, reason
    kept) — see FileMemProvider._salience_fallback in __init__.py.
    """
    try:
        parsed = _structured_call(main_runtime, SALIENCE_INSTRUCTIONS,
                                  SALIENCE_SCHEMA, content) or {}
        return {"salience": float(parsed.get("salience", 0.5)),
                "reason": parsed.get("reason", ""), "model_used": True}
    except Exception as exc:  # host down / no snapshot / parse None — keyword fallback
        _record_face_fallback(main_runtime, exc)  # card 3: llm_call row fallback=True
        return {"salience": 0.5, "reason": f"model_unavailable_fallback={exc}", "model_used": False}


RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["relevance"],
}

RELEVANCE_INSTRUCTIONS = ("Rate how relevant this memory content is to the user query. "
                          "Return a JSON object.")


def relevance_score(main_runtime: Optional[Dict[str, Any]], query: str, text: str) -> Dict[str, Any]:
    """B3: model-assisted relevance re-ranking for search (host main-model
    face, explicit main_runtime).

    Returns {'relevance': 0-1, 'model_used': bool}; falls back to 0.5 on any
    failure so the caller's keyword ranking is unaffected.
    """
    try:
        parsed = _structured_call(main_runtime, RELEVANCE_INSTRUCTIONS,
                                  RELEVANCE_SCHEMA,
                                  f"QUERY: {query}\nMEMORY: {text}") or {}
        return {"relevance": float(parsed.get("relevance", 0.5)), "model_used": True}
    except Exception as exc:
        _record_face_fallback(main_runtime, exc)  # card 3: llm_call row fallback=True
        return {"relevance": 0.5, "model_used": False, "reason": f"model_unavailable_fallback={exc}"}


_DISTILL_FAIL = {"summary": None, "value": None, "confirmed": False}

DISTILL_INSTRUCTIONS = """
You are a memory curator for an AI agent. Extract actionable insights
and judge their long-term value for future sessions.

Context: Conversation between Hermes Agent and the User.

Output JSON:
{
  "summary": "concise summary (target ~300 chars, max 4000 chars hard limit)",
  "value": "high|medium|low",
  "confirmed": boolean
}

Rules:
- Factual content (decisions, preferences, technical details): summarize key points (~300 chars)
- Procedural content (workflows, commands): capture steps concisely
- Conversational filler (greetings, acknowledgments): value="low", confirmed=false
- Never hallucinate — only summarize what's explicitly stated
"""

DISTILL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "精炼摘要（4000 char 封顶）"},
        "value": {"type": "string", "enum": ["high", "medium", "low"]},
        "confirmed": {"type": "boolean"}
    },
    "required": ["summary", "confirmed"]
}


def distill(main_runtime: Optional[Dict[str, Any]], content: str) -> Dict[str, Any]:
    """LLM distillation of one memory item: summary + value + confirmed flag
    (explicit main_runtime host face, temperature 0.0 / max_tokens 512).

    Fail-soft, same shape as score_salience: ANY failure (no snapshot /
    host call surface missing / host down / timeout / JSON parse None /
    self-contradictory output confirmed=true with empty summary) coerces
    to confirmed=False — 宁可漏注, 不吞脏. Callers update the SAME
    memories/{type}.json item by id under the existing _shard_lock;
    raw_pending stays the quarantine pool, untouched.
    """
    try:
        parsed = _structured_call(main_runtime, DISTILL_INSTRUCTIONS, DISTILL_SCHEMA,
                                  content, temperature=0.0, max_tokens=512) or {}
        summary = (parsed.get("summary") or "").strip()
        value = parsed.get("value")
        if value not in ("high", "medium", "low"):
            value = None
        confirmed = bool(parsed.get("confirmed"))
        # @reviewer constraint 3: a self-contradictory LLM output (confirmed
        # true but no summary) must not flow through — force confirmed=False.
        if confirmed and not summary:
            confirmed = False
        return {"summary": summary or None, "value": value, "confirmed": confirmed}
    except Exception as exc:
        # Fail-soft (same shape as score_salience): the caller decides whether to
        # log; the helper only guarantees a safe, confirmed=False result.
        _record_face_fallback(main_runtime, exc)  # card 3: llm_call row fallback=True
        return dict(_DISTILL_FAIL)


# ── t_090d6f3d: type-classification fallback (host main-model face, explicit main_runtime) ──
# A budget-gated LLM classifier for the memory-shard type of a content item the
# deterministic regex pass (filemem._detect_type) left as a `fact`-miss. Same
# posture as distill / confirm_supersede / suggest_aliases: the host call
# surface with the captured main_runtime snapshot (explicit, no task key -> host
# main-model route). Zero hardcoded model names, zero custom api/provider
# config keys, zero third-party SDK imports (stdlib + the host call surface
# only). The caller (filemem._reclassify_type_llm) gates this face behind a
# per-session budget; over-cap / no runtime / ANY failure -> the caller keeps
# the regex verdict (fail-soft, never blocks the save, 不吞脏).
TYPE_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["preference", "fact", "decision", "task", "plan"],
            "description": "the memory shard this content most precisely belongs to",
        },
    },
    "required": ["type"],
}

TYPE_CLASSIFY_INSTRUCTIONS = """You are a memory curator for an AI agent.
Classify the single memory content below into EXACTLY one shard type:
- "preference": a user preference / taste / standing choice
- "decision": a settled choice or decision about what to use / adopt / commit to
- "task": an action to do, a to-do, something that MUST / needs to be completed
- "plan": a structured multi-step execution sequence / PRD / ordered repair
  plan with a clear done-when criterion; distinct from a single atomic task
  (task = one to-do; plan = ordered steps with progress + completion state)
- "fact": a neutral durable statement (default when none of the above fit)
Prefer the MOST SPECIFIC non-fact type the content genuinely supports; use
"fact" only when it is purely a neutral fact. Return a JSON object."""


def classify_type(main_runtime: Optional[Dict[str, Any]], content: str) -> Dict[str, Any]:
    """LLM classification of one memory item's shard type (host main-model face,
    explicit main_runtime, temperature 0.0 / max_tokens 64).

    Returns {'type': one of preference/fact/decision/task | None,
             'model_used': bool, 'reason'?: str}. ANY failure (no snapshot /
    host surface missing / host down / timeout / unparseable reply / type not in
    the enum) -> type=None + model_used=False — the caller keeps the regex
    verdict (fail-soft; 宁可漏判, 不吞脏, 不炸宿主).
    """
    try:
        parsed = _structured_call(main_runtime, TYPE_CLASSIFY_INSTRUCTIONS,
                                  TYPE_CLASSIFY_SCHEMA, content,
                                  temperature=0.0, max_tokens=64) or {}
        verdict = parsed.get("type")
        if verdict not in ("preference", "fact", "decision", "task", "plan"):
            _record_face_fallback(main_runtime, Exception("invalid_type"))  # card 3: llm_call row fallback
            return {"type": None, "model_used": False,
                    "reason": f"invalid_type_fallback={verdict!r}"}
        return {"type": verdict, "model_used": True}
    except Exception as exc:
        _record_face_fallback(main_runtime, exc)  # card 3: llm_call row fallback=True
        return {"type": None, "model_used": False,
                "reason": f"model_unavailable_fallback={exc}"}


# ── EP-2: supersedes-candidate confirmation (host main-model face, explicit main_runtime) ──
# Mirrors distill: the host call surface with the captured main_runtime
# snapshot, no task key -> host main-model route. Zero hardcoded model
# names, zero provider config keys, zero third-party SDK imports
# (stdlib + the host call surface only).
CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["confirmed"],
}

CONFIRM_INSTRUCTIONS = """You are a memory curator. Two memory items are in the
same category. The NEWER item is a candidate to supersede (replace) the OLDER
item only if the newer genuinely obsoletes the older (same fact updated /
corrected / refined). If they merely overlap or are unrelated, do NOT confirm.
Return JSON: {"confirmed": boolean, "reason": short str}"""


def confirm_supersede(main_runtime: Optional[Dict[str, Any]], new_text: str, old_text: str) -> Dict[str, Any]:
    """EP-2 LLM confirmation: does NEWER genuinely supersede OLDER? Explicit
    main_runtime host face, temperature 0.0 / max_tokens 160, fail-soft: ANY
    failure -> confirmed=False (never auto-trust on failure; the candidate
    stays unconfirmed and the caller keeps it pending, 不吞脏)."""
    try:
        parsed = _structured_call(main_runtime, CONFIRM_INSTRUCTIONS, CONFIRM_SCHEMA,
                                  f"OLDER:\n{old_text}\n\nNEWER:\n{new_text}",
                                  temperature=0.0, max_tokens=160) or {}
        return {"confirmed": bool(parsed.get("confirmed", False)),
                "reason": (parsed.get("reason") or "")[:200], "model_used": True}
    except Exception as exc:
        _record_face_fallback(main_runtime, exc)  # card 3: llm_call row fallback=True
        return {"confirmed": False, "reason": f"model_unavailable_fallback={exc}",
                "model_used": False}


# ── EP-3: alias mining (host main-model face, 镜像 distill/confirm_supersede) ─
# Zero hardcoded model names, zero custom api/provider config keys: the call
# goes through the host call surface with the captured main_runtime snapshot
# (explicit, no implicit contextvar fallback — epic card 2). No
# third-party SDK imports (stdlib + the host call surface only).
ALIAS_SCHEMA = {
    "type": "object",
    "properties": {
        "aliases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "5-15 synonyms / related terms including CJK where appropriate",
        },
    },
    "required": ["aliases"],
}

ALIAS_INSTRUCTIONS = """You are a vocabulary expandor for a search system.
Given a topic, produce a flat list of synonyms, related terms, abbreviations,
and CJK equivalents (where the topic is English). 5-15 items, no repetition
of the topic itself, no full phrases — short high-signal tokens only."""


def suggest_aliases(main_runtime: Optional[Dict[str, Any]], topic: str) -> Dict[str, Any]:
    """EP-3: explicit main_runtime host face (temperature 0.0 / max_tokens
    256). Fail-soft: any exception -> aliases=[] + model_used=False (caller
    treats as no-alias, native keyword-search degradation — 不吞脏 不炸宿主)."""
    try:
        parsed = _structured_call(main_runtime, ALIAS_INSTRUCTIONS, ALIAS_SCHEMA,
                                  f"Topic: {topic}", temperature=0.0,
                                  max_tokens=256) or {}
        raw = parsed.get("aliases") or []
        aliases = sorted({str(a).strip().lower() for a in raw if str(a).strip()})
        return {"aliases": aliases, "model_used": True}
    except Exception as exc:
        _record_face_fallback(main_runtime, exc)  # card 3: llm_call row fallback=True
        return {"aliases": [], "model_used": False,
                "reason": f"model_unavailable_fallback={exc}"}


# ── EP-2 T2: observation synthesis (host main-model face, explicit main_runtime) ─
# Mirrors distill / confirm_supersede / suggest_aliases: the host call
# surface with the captured main_runtime snapshot (explicit, same as the
# built-in compressor / supercompress pattern). Zero hardcoded model names,
# zero third-party SDK imports (stdlib + the host call surface only), zero
# custom api/provider config keys.
OBS_SCHEMA = {
    "type": "object",
    "properties": {
        "observation": {
            "type": "string",
            "description": "preference-neutral synthesis of what the facts establish",
        },
        "value": {"type": "string", "enum": ["high", "medium", "low"]},
        "supported": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["id"],
            },
            "description": "each supporting fact, cited",
        },
    },
    "required": ["observation", "value"],
}

OBS_INSTRUCTIONS = """You are a memory curator for an AI agent. You are given a
set of raw fact items that the agent has already recorded. Synthesize them
into ONE preference-neutral observation.

Rules:
- Summarize what the facts ESTABLISH. Do not inject preference,
  recommendation, or evaluation of your own — state only what the facts
  support.
- Cite each supporting fact: for every claim in your observation, add an
  entry to "supported" with that fact's id and a short quote of the part of
  it you relied on.
- If the facts are too thin or mutually contradictory to establish anything
  durable, return an empty observation string.
- Never hallucinate — only synthesize what the given facts state.
"""


def observe(main_runtime: Optional[Dict[str, Any]], topic: str, facts: List[Dict]) -> Dict[str, Any]:
    """EP-2 T2: host main-model synthesis of a topic's raw facts into ONE
    preference-neutral observation (explicit main_runtime, temperature 0.0 /
    max_tokens 512).

    facts = [{"id": <memory_id>, "content": str}, ...] — caller caps the
    list (knowledge.observe_max_facts_per_topic, default 8) so the prompt
    stays bounded. Output shape (always a full dict, fail-soft):
      {"observation": str, "value": "high|medium|low" | "",
       "supported": [{"id", "quote"}], "model_used": bool,
       "reason"?: str}
    ANY exception (no snapshot / host surface missing / host down / timeout /
    trust gate / malformed reply) -> observation="" + model_used=False: the
    caller does NOT write the entry, raw facts stay in raw_pending, and the
    next consolidation tick retries (不吞脏, 不静默). A self-contradictory
    reply (value set but observation empty) is coerced to empty — same
    posture as distill's confirmed-without-summary guard.
    """
    try:
        lines = [f"Topic: {topic}", ""]
        for i, fct in enumerate(facts or [], 1):
            fid = (fct or {}).get("id") or f"fact_{i}"
            ftxt = str((fct or {}).get("content") or "")[:800]
            lines.append(f"[{i}] id={fid}\n{ftxt}")
            lines.append("")
        parsed = _structured_call(main_runtime, OBS_INSTRUCTIONS, OBS_SCHEMA,
                                  "\n".join(lines), temperature=0.0,
                                  max_tokens=512) or {}
        obs = str(parsed.get("observation") or "").strip()
        value = parsed.get("value")
        if value not in ("high", "medium", "low"):
            value = ""
        if not obs:
            value = ""  # no synthesis -> no value verdict (self-contradictory guard)
        raw_supp = parsed.get("supported") or []
        supported = [
            {"id": str(s.get("id")), "quote": str(s.get("quote") or "")[:200]}
            for s in raw_supp if isinstance(s, dict) and s.get("id")
        ]
        return {"observation": obs, "value": value or "medium",
                "supported": supported, "model_used": True}
    except Exception as exc:
        # Fail-soft: caller treats observation=="" as "not synthesized yet"
        # (raw pending pool untouched, retry next tick).
        _record_face_fallback(main_runtime, exc)  # card 3: llm_call row fallback=True
        return {"observation": "", "value": "", "supported": [],
                "model_used": False, "reason": f"model_unavailable_fallback={exc}"}


# ── S3: dream semantic faces (host main-model face, 4 new LLM faces) ───────
# S3 extends the consolidation "dreaming" tick with 4 faces that close the
# blind spot the 4 existing faces leave: "semantically equivalent /
# reworded / cross-shard" rows that the same-shard token-overlap EP-1 gate
# never sees. All 4 follow the EXACT pattern of confirm_supersede /
# suggest_aliases / observe: explicit main_runtime snapshot, _structured_call,
# fail-soft fallback dict (model_used=False on ANY exception), zero hardcoded
# model names, zero third-party SDK, zero custom api/provider config keys.

# ── ① semantic merge: are two rows the SAME fact (reworded / cross-shard)? ──
SEMANTIC_MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "same_fact": {
            "type": "boolean",
            "description": "True only if the two rows state the SAME fact "
                           "(equivalent meaning); reworded / split / "
                           "complementary-but-not-same = False",
        },
        "keep": {"type": "string", "description": "id of the row to KEEP "
                                                   "(the fuller / newer one); "
                                                   "must be one of the two given row ids"},
        "reason": {"type": "string"},
    },
    "required": ["same_fact"],
}

SEMANTIC_MERGE_INSTRUCTIONS = """You are a memory curator. You are given two
memory rows that may or may not state the SAME underlying fact (one could be
a rewrite, split, or cross-category duplicate of the other).

Decide strictly:
- same_fact=True ONLY when both rows assert the same claim and folding them
  into one would LOSE NOTHING. Different facts that merely share a topic are
  NOT the same fact (e.g. "use docker" vs "don't use docker today" differ).
- When same_fact=True, name the row to KEEP (keep) — prefer the one that is
  fuller / more recent — and list the other id(s) in "supersede".
- When same_fact=False, leave keep/supersede empty.

Return a JSON object. Never hallucinate — judge only on the text given."""


def merge_semantic(main_runtime, row_a, row_b) -> Dict[str, Any]:
    """S3 ①: does the pair (row_a, row_b) state the same fact? Explicit
    main_runtime host face (temp 0.0 / 200 tok). row_a/row_b = {"id": str,
    "content": str, "created_at": str}. Fail-soft: ANY failure ->
    same_fact=False + model_used=False (never fold on a broken call — a bad
    verdict is worse than no verdict; the pair simply stays unmerged)."""
    try:
        def _fmt(r):
            r = r or {}
            return (f"id={r.get('id')}\ncreated_at={r.get('created_at', '')}\n"
                    f"{str(r.get('content') or '')[:800]}")
        parsed = _structured_call(
            main_runtime, SEMANTIC_MERGE_INSTRUCTIONS, SEMANTIC_MERGE_SCHEMA,
            f"ROW A:\n{_fmt(row_a)}\n\nROW B:\n{_fmt(row_b)}",
            temperature=0.0, max_tokens=200) or {}
        same = bool(parsed.get("same_fact", False))
        keep = str(parsed.get("keep") or "")
        if same and not keep:
            # A merge verdict must name a survivor; missing -> refuse to fold.
            same = False
        return {"same_fact": same, "keep": keep,
                "reason": (str(parsed.get("reason") or ""))[:200], "model_used": True}
    except Exception as exc:
        _record_face_fallback(main_runtime, exc)
        return {"same_fact": False, "keep": "",
                "reason": f"model_unavailable_fallback={exc}", "model_used": False}


# ── ② contradiction: do two coexisting rows MUTUALLY EXCLUDE each other? ────
CONTRADICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "contradiction": {
            "type": "boolean",
            "description": "True only if the two rows CANNOT both be true "
                           "at the same time (mutually exclusive claims)",
        },
        "stale_id": {
            "type": "string",
            "description": "id of the OLDER / now-wrong row to mark stale",
        },
        "reason": {"type": "string"},
    },
    "required": ["contradiction"],
}

CONTRADICTION_INSTRUCTIONS = """You are a memory curator. You are given two
coexisting memory rows. Decide whether they CONTRADICT each other — i.e. they
assert mutually exclusive claims that cannot both be true now.

- contradiction=True ONLY for a hard conflict (e.g. "deploy uses docker"
  vs "deploy does NOT use docker"). Rows that merely coexist, complement, or
  apply to different scopes are NOT a contradiction.
- When contradiction=True, name the row that should be marked stale in
  "stale_id" (usually the OLDER one, superseded by the newer claim).
- When contradiction=False, leave stale_id empty.

Return a JSON object. Never hallucinate — judge only on the text given."""


def detect_contradiction(main_runtime, row_a, row_b) -> Dict[str, Any]:
    """S3 ②: do two coexisting rows mutually exclude? Explicit main_runtime
    host face (temp 0.0 / 200 tok). Fail-soft: ANY failure ->
    contradiction=False + model_used=False (never mark stale on a broken
    call — a false-positive stale is worse than a missed one)."""
    try:
        def _fmt(r):
            r = r or {}
            return (f"id={r.get('id')}\ncreated_at={r.get('created_at', '')}\n"
                    f"{str(r.get('content') or '')[:800]}")
        parsed = _structured_call(
            main_runtime, CONTRADICTION_INSTRUCTIONS, CONTRADICTION_SCHEMA,
            f"ROW A:\n{_fmt(row_a)}\n\nROW B:\n{_fmt(row_b)}",
            temperature=0.0, max_tokens=200) or {}
        contra = bool(parsed.get("contradiction", False))
        stale = str(parsed.get("stale_id") or "")
        if contra and not stale:
            # A conflict verdict must name a stale row; missing -> no action.
            contra = False
        return {"contradiction": contra, "stale_id": stale,
                "reason": (str(parsed.get("reason") or ""))[:200], "model_used": True}
    except Exception as exc:
        _record_face_fallback(main_runtime, exc)
        return {"contradiction": False, "stale_id": "",
                "reason": f"model_unavailable_fallback={exc}", "model_used": False}


# ── ④ aggregation: fold a topic's fragmentary facts into ONE L1 conclusion ───
AGGREGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "conclusion": {
            "type": "string",
            "description": "ONE durable L1/L2-level conclusion the fragments "
                           "jointly establish; empty if too thin",
        },
        "value": {"type": "string", "enum": ["high", "medium", "low"]},
        "supported": {
            "type": "array",
            "items": {"type": "string"},
            "description": "source fact ids the conclusion is grounded in",
        },
    },
    "required": ["conclusion"],
}

AGGREGATE_INSTRUCTIONS = """You are a memory curator. You are given a set of
fragmentary fact rows that all belong to ONE topic. Synthesize them into a
single durable CONCLUSION worth promoting to a higher memory tier (L1/L2).

Rules:
- The conclusion must be grounded ONLY in the given fragments — never
  add facts of your own.
- Cite the source fragment ids in "supported".
- If the fragments are too thin or too scattered to establish anything
  durable, return an empty "conclusion" string.
- Return a JSON object."""


def aggregate(main_runtime, topic: str, facts: List[Dict]) -> Dict[str, Any]:
    """S3 ④: fold a topic's fragmentary facts into ONE L1/L2 conclusion.
    facts = [{"id": <memory_id>, "content": str}, ...] (caller caps). 
    Fail-soft: ANY failure -> conclusion="" + model_used=False (caller writes
    nothing, fragments stay as-is, retry next tick)."""
    try:
        lines = [f"Topic: {topic}", ""]
        for i, f in enumerate(facts or [], 1):
            fid = (f or {}).get("id") or f"fact_{i}"
            ftxt = str((f or {}).get("content") or "")[:600]
            lines.append(f"[{i}] id={fid}\n{ftxt}")
            lines.append("")
        parsed = _structured_call(main_runtime, AGGREGATE_INSTRUCTIONS,
                                  AGGREGATE_SCHEMA, "\n".join(lines),
                                  temperature=0.0, max_tokens=512) or {}
        concl = str(parsed.get("conclusion") or "").strip()
        value = parsed.get("value")
        if value not in ("high", "medium", "low"):
            value = "medium"
        raw_supp = parsed.get("supported") or []
        supported = [str(s) for s in raw_supp if str(s).strip()]
        if not concl:
            supported = []  # no conclusion -> no grounding (self-consistency)
        return {"conclusion": concl, "value": value, "supported": supported,
                "model_used": True}
    except Exception as exc:
        _record_face_fallback(main_runtime, exc)
        return {"conclusion": "", "value": "", "supported": [],
                "model_used": False, "reason": f"model_unavailable_fallback={exc}"}


# ── S3⑥ recurring-repetition (near-synonym directive) consolidate face ─────
# ⑥ same standing DIRECTIVE reworded N times (red lines / rules the user keeps
# re-speaking). Third directive-specific lane, parallel to S3① (same FACT)
# and S3② (MUTUALLY EXCLUSIVE): it only fires for directive-class clusters
# (rules / red lines / workflow mandates) and only ASKS the user (clarify
# panel) — the dream tick writes nothing; applying "sediment" happens on
# consumption of the panel (provider-side _tool_review_drafts).
RECURRING_SCHEMA = {
    "type": "object",
    "properties": {
        "same_directive": {
            "type": "boolean",
            "description": "True only if all given rows restate the SAME "
                           "standing directive / rule / red line (reworded, "
                           "same intent). Plain coexisting facts or merely "
                           "topical fragments = False",
        },
        "authoritative": {
            "type": "string",
            "description": "id of the FULLEST / most authoritative row among "
                           "the given ones; must be one of the given row ids",
        },
        "reason": {"type": "string"},
    },
    "required": ["same_directive"],
}

RECURRING_INSTRUCTIONS = """You are a memory curator. You are given several
memory rows that share a lot of wording. Decide strictly:

- same_directive=True ONLY when ALL the rows restate the SAME standing
  DIRECTIVE / RULE / RED LINE — i.e. a repeated user mandate (what must /
  must not happen, a workflow rule). Reworded copies of the same mandate
  count; rows that merely share a topic but are independent facts do NOT.
- When same_directive=True, name the most authoritative / fullest row in
  "authoritative" (prefer the most recent complete wording; it must be one of
  the given row ids).
- When same_directive=False, leave authoritative empty.

Return a JSON object. Never hallucinate — judge only on the text given."""


def recurring_directive(main_runtime, rows: List[Dict]) -> Dict[str, Any]:
    """S3⑥: do N rows restate the SAME standing directive? Explicit
    main_runtime host face (temp 0.0 / 200 tok). rows = [{"id": str,
    "content": str}, ...]. Fail-soft: ANY failure -> same_directive=False +
    model_used=False (never panel on a broken call — a false "ask" is worse
    than a missed one; the cluster stays unasked, retry next tick)."""
    try:
        lines: List[str] = []
        for i, r in enumerate(rows or [], 1):
            r = r or {}
            rid = str(r.get("id") or "")
            content = str(r.get("content") or "")[:600]
            lines.append(f"[{i}] id={rid}\n{content}")
        parsed = _structured_call(
            main_runtime, RECURRING_INSTRUCTIONS, RECURRING_SCHEMA,
            "\n".join(lines), temperature=0.0, max_tokens=200) or {}
        same = bool(parsed.get("same_directive", False))
        auth = str(parsed.get("authoritative") or "")
        reason = (str(parsed.get("reason") or ""))[:200]
        # pair-boundary guard (mirrors merge_semantic): the named authoritative
        # row must be IN the given cluster. A hallucinated out-of-cluster id
        # would anchor the sediment row to a non-member (lineage break).
        valid_ids = {str((r or {}).get("id") or "") for r in (rows or [])}
        refused_reason = None
        if same and auth and auth not in valid_ids:
            logger.warning(
                f"FileMem: recurring_directive authoritative={auth!r} outside "
                f"cluster {valid_ids}; verdict refused (fail-soft)")
            refused_auth = auth
            same = False
            auth = ""
            # P2-19 (reviewer 10-09): stamp the refusal so the llm_call
            # telemetry row can distinguish "LLM judged not-same" from
            # "boundary guard refused a hallucinated anchor" (不静默).
            refused_reason = (f"boundary_refused:auth_not_in_cluster:"
                              f"{refused_auth}")
        if same and not auth:
            # a same-directive verdict must name an anchor row; missing ->
            # refuse (never sediment without a known anchor).
            same = False
            refused_reason = "boundary_refused:missing_authoritative"
        if refused_reason:
            reason = refused_reason
            # F-5 (reviewer re-sign 10-09): also stamp the module's
            # _last_telemetry seam so the provider's llm_call AUDIT row
            # (consolidation_log.jsonl) carries the refusal — the verdict
            # dict alone only reaches the face, the audit chain would not
            # record it. ok=True (the host call itself succeeded; the
            # refusal is a plugin-side guard, not a host failure).
            try:
                _model, _provider = _telemetry_route(main_runtime, {})
                _set_last_telemetry(_model, _provider, 0, True, False,
                                    reason=refused_reason)
            except Exception:  # pragma: no cover - telemetry never raises
                pass
        return {"same_directive": same, "authoritative": auth,
                "reason": reason, "model_used": True}
    except Exception as exc:
        _record_face_fallback(main_runtime, exc)
        return {"same_directive": False, "authoritative": "",
                "reason": f"model_unavailable_fallback={exc}",
                "model_used": False}


# ③ salience-decay downweight is DETERMINISTIC (zero LLM) — it lives in the
# provider (_salience_decay_downweight), not here. It only reuses the existing
# reference-count / last-referenced / superseded signals; no host call.

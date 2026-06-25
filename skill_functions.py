"""Shared library — the load-bearing module every skill imports.

Mirrors Section 1 of `kelp_skill_specification.md`. Contents:

  1.1  call_model               — provider-agnostic model adapter
  1.2  search_for_data_fields   — structured research helper for domain skills
  1.3  parse_json_safe / validate_against_schema / merge_partial_results
  1.4  run_skills_in_parallel   — bounded-concurrency, per-skill-timeout runner
  1.5  evaluate_persona_relevance
  1.6  ModelResponse / SkillError / SkillTelemetry / TelemetryCollector

Provider SDKs (anthropic / openai / google-genai) are imported lazily inside
each adapter so this module imports cleanly even when an SDK or API key is
absent. A missing SDK/key only fails the specific call that needs it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field

import config
from schemas import PersonaRelevance

JsonFormat = Literal["text", "json"]


# --------------------------------------------------------------------------- #
# 1.6  Response / error / telemetry models                                    #
# --------------------------------------------------------------------------- #
class ModelResponse(BaseModel):
    """Normalized result shape returned by every provider adapter."""

    text: str = ""
    parsed: Optional[Any] = None          # populated when response_format="json" and parse succeeds
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_used: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: int = 0
    provider: str = ""
    model: str = ""
    sources: List[str] = Field(default_factory=list)
    # url -> real publisher title from grounding metadata (so a source can show a
    # real name instead of the generic "web search result"). Optional; keyed by
    # the same urls that appear in ``sources``.
    source_titles: Dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None           # set when the call failed (after fallback)


class SkillError(BaseModel):
    """Returned (not raised) by run_skills_in_parallel so the pipeline continues."""

    skill_name: str
    error: str
    error_type: str = "error"             # "timeout" | "exception"


class SkillTelemetry(BaseModel):
    """Per-skill telemetry record (Section 1.6)."""

    skill_name: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_cost_usd: float = 0.0
    provider: str = ""
    model_used: str = ""
    success: bool = True


class TelemetryCollector:
    """Aggregates per-skill records into the run-level telemetry object."""

    def __init__(self) -> None:
        self.records: List[SkillTelemetry] = []

    def record(self, rec: SkillTelemetry) -> None:
        self.records.append(rec)

    def record_from_response(
        self, skill_name: str, resp: ModelResponse, started_at: datetime
    ) -> SkillTelemetry:
        finished = datetime.now()
        rec = SkillTelemetry(
            skill_name=skill_name,
            started_at=started_at,
            finished_at=finished,
            duration_ms=int((finished - started_at).total_seconds() * 1000),
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            estimated_cost_usd=resp.estimated_cost_usd,
            provider=resp.provider,
            model_used=resp.model,
            success=resp.error is None,
        )
        self.record(rec)
        return rec

    def finalize(self) -> Dict[str, Any]:
        return {
            "total_cost_usd": round(sum(r.estimated_cost_usd for r in self.records), 6),
            "total_tokens": sum(r.tokens_in + r.tokens_out for r in self.records),
            "total_duration_ms": sum(r.duration_ms for r in self.records),
            "skill_timings": [r.model_dump(mode="json") for r in self.records],
        }


# --------------------------------------------------------------------------- #
# Cost estimation                                                             #
# --------------------------------------------------------------------------- #
# Approximate USD per 1M tokens (input, output). Edit as pricing changes.
_PRICING: Dict[str, Tuple[float, float]] = {
    # Anthropic
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    # OpenAI (placeholders — adjust to the exact model you wire in)
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4": (10.0, 30.0),
    # Gemini (June 2026). NOTE: order matters — "gemini-2.5-flash-lite" contains
    # "gemini-2.5-flash" as a substring, so the more specific lite key must come
    # FIRST for the substring match in _estimate_cost to pick the right rate.
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.0),
    "gemini": (0.30, 2.50),
}


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rate_in, rate_out = 0.30, 2.50  # default to flash-ish if unknown
    for key, (ri, ro) in _PRICING.items():
        if key in model:
            rate_in, rate_out = ri, ro
            break
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out


# --------------------------------------------------------------------------- #
# Cost ledger — per-call logging + process-wide cumulative                     #
# --------------------------------------------------------------------------- #
LOG_COSTS = True  # set False to silence the per-call cost lines

_cumulative_cost_usd = 0.0
_call_count = 0
_grounded_call_count = 0


def _record_cost(resp: "ModelResponse", grounded: bool) -> None:
    """Account one physical API call: add grounding surcharge, update totals, log."""
    import sys

    global _cumulative_cost_usd, _call_count, _grounded_call_count

    # Count EVERY grounded (web-search) call, regardless of how it was routed. Calls go
    # through the LiteLLM gateway now (provider="litellm", model="gemini/…"), so the old
    # `provider == "gemini"` gate silently stopped counting them — and suppressed the
    # grounding-cost caveat. Detect a Gemini-backed call by provider OR model string.
    if grounded:
        _grounded_call_count += 1
        is_gemini = resp.provider == "gemini" or "gemini" in (resp.model or "").lower()
        if is_gemini:
            surcharge = getattr(config, "GEMINI_GROUNDING_COST_PER_CALL", 0.0) or 0.0
            resp.estimated_cost_usd += surcharge

    _cumulative_cost_usd += resp.estimated_cost_usd
    _call_count += 1

    if LOG_COSTS:
        flag = " +search" if grounded else ""
        status = f" ERROR({resp.error})" if resp.error else ""
        print(
            f"💸 [{resp.model or resp.provider}{flag}] "
            f"in={resp.tokens_in:,} out={resp.tokens_out:,} "
            f"call=${resp.estimated_cost_usd:.6f} "
            f"cumulative=${_cumulative_cost_usd:.6f} ({_call_count} calls){status}",
            file=sys.stderr,
        )


def get_cumulative_cost() -> float:
    """Total estimated USD spent across all model calls this process."""
    return round(_cumulative_cost_usd, 6)


def get_call_count() -> int:
    return _call_count


def reset_cost_ledger() -> None:
    """Zero the cumulative cost / call counters (e.g. at the start of a run)."""
    global _cumulative_cost_usd, _call_count, _grounded_call_count
    _cumulative_cost_usd = 0.0
    _call_count = 0
    _grounded_call_count = 0


def cost_summary() -> Dict[str, Any]:
    """Run-level cost rollup with a grounding caveat."""
    caveat = None
    if _grounded_call_count and not (getattr(config, "GEMINI_GROUNDING_COST_PER_CALL", 0.0) or 0.0):
        caveat = (
            f"{_grounded_call_count} grounded (web-search) call(s) — token cost is "
            "exact, but Gemini grounding is billed separately (~$35/1,000 requests "
            "after the free daily tier) and is NOT included above."
        )
    return {
        "total_cost_usd": get_cumulative_cost(),
        "calls": _call_count,
        "grounded_calls": _grounded_call_count,
        "grounding_caveat": caveat,
    }


# --------------------------------------------------------------------------- #
# Persistent cost ledger — lifetime spend across runs/phases (on disk)         #
# --------------------------------------------------------------------------- #
import os as _os

LEDGER_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "cost_ledger.json")


def load_persistent_ledger() -> Dict[str, Any]:
    """Read the lifetime cost ledger from disk (empty default if absent)."""
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"lifetime_cost_usd": 0.0, "lifetime_calls": 0, "lifetime_grounded": 0, "entries": []}


def record_run_to_ledger(label: str) -> Dict[str, Any]:
    """Add the CURRENT in-memory run totals to the persistent ledger and save.

    Call once at the end of a run (after reset_cost_ledger() at its start). Returns
    the updated ledger so callers can print the new lifetime total.
    """
    led = load_persistent_ledger()
    run_cost = get_cumulative_cost()
    led["lifetime_cost_usd"] = round(led.get("lifetime_cost_usd", 0.0) + run_cost, 6)
    led["lifetime_calls"] = led.get("lifetime_calls", 0) + _call_count
    led["lifetime_grounded"] = led.get("lifetime_grounded", 0) + _grounded_call_count
    led.setdefault("entries", []).append({
        "label": label,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "cost_usd": run_cost,
        "calls": _call_count,
        "grounded": _grounded_call_count,
    })
    try:
        with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
            json.dump(led, fh, indent=2)
    except OSError as exc:
        print(f"[ledger] could not persist cost ledger: {exc}", file=__import__("sys").stderr)
    return led


def snapshot() -> tuple:
    """Capture (cost, calls, grounded) now — for measuring one generation's delta."""
    return (get_cumulative_cost(), _call_count, _grounded_call_count)


def append_delta_to_ledger(label: str, before: tuple) -> Dict[str, Any]:
    """Persist the spend since ``before`` as one ledger entry. Server-safe (uses
    deltas, so reusing the process across many generations doesn't double-count)."""
    b_cost, b_calls, b_gr = before
    d_cost = round(get_cumulative_cost() - b_cost, 6)
    d_calls = _call_count - b_calls
    d_gr = _grounded_call_count - b_gr
    led = load_persistent_ledger()
    led["lifetime_cost_usd"] = round(led.get("lifetime_cost_usd", 0.0) + d_cost, 6)
    led["lifetime_calls"] = led.get("lifetime_calls", 0) + d_calls
    led["lifetime_grounded"] = led.get("lifetime_grounded", 0) + d_gr
    led.setdefault("entries", []).append({
        "label": label, "timestamp": datetime.now().isoformat(timespec="seconds"),
        "cost_usd": d_cost, "calls": d_calls, "grounded": d_gr,
    })
    try:
        with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
            json.dump(led, fh, indent=2)
    except OSError as exc:
        print(f"[ledger] could not persist: {exc}", file=__import__("sys").stderr)
    return led


EXPENSE_MD_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "API_EXPENSES.md")


def append_expense_md(label: str, run_cost: float, calls: int = 0,
                      grounded: int = 0, model: str = "gemini-2.5-flash (LiteLLM)") -> float:
    """Append one run to the human-readable API expense log and return the new
    cumulative total. Source of truth is the markdown table itself — the running
    total is recomputed from its rows each time, so it stays correct even if edited.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    label = str(label).replace("|", "/")
    rows: List[str] = []
    if _os.path.exists(EXPENSE_MD_PATH):
        with open(EXPENSE_MD_PATH, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("|") and not s.startswith("| #") and "---" not in s:
                    rows.append(s)
    prior = 0.0
    for r in rows:
        cells = [c.strip() for c in r.strip("|").split("|")]
        if len(cells) >= 6:
            try:
                prior += float(cells[5].replace("$", "").replace(",", ""))
            except ValueError:
                pass
    n = len(rows) + 1
    cumulative = round(prior + run_cost, 6)
    rows.append(f"| {n} | {ts} | {label} | {model} | {calls} | ${run_cost:.6f} | ${cumulative:.6f} |")

    header = (
        "# API Expense Log — LiteLLM gateway (kelpllm)\n\n"
        "Every billed pipeline run on the LiteLLM key. Cost is estimated from token "
        "usage (input + output, per the rates in `skill_functions._PRICING`).\n\n"
        "| # | Timestamp | Run | Model | Calls | Run cost (USD) | Cumulative (USD) |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    body = header + "\n".join(rows) + f"\n\n**Total spent on LiteLLM key: ${cumulative:.6f}**\n"
    try:
        with open(EXPENSE_MD_PATH, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        print(f"[expenses] could not persist: {exc}", file=__import__("sys").stderr)
    return cumulative


def print_lifetime_cost() -> None:
    """Print the persistent lifetime spend (for end-of-run reporting)."""
    led = load_persistent_ledger()
    print(
        f"📊 LIFETIME Gemini spend: ${led['lifetime_cost_usd']:.6f} "
        f"over {led['lifetime_calls']} calls ({led['lifetime_grounded']} grounded) "
        f"across {len(led.get('entries', []))} run(s)",
        file=__import__("sys").stderr,
    )


# --------------------------------------------------------------------------- #
# 1.3  JSON utilities                                                          #
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.search(text)
    return m.group(1) if m else text


def _extract_json_blob(text: str) -> str:
    """Best-effort: return the substring from the first { or [ to its match."""
    cleaned = _strip_fences(text).strip()
    start = min(
        [i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1],
        default=-1,
    )
    if start == -1:
        return cleaned
    opener = cleaned[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == opener:
            depth += 1
        elif cleaned[i] == closer:
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]
    return cleaned[start:]


def parse_json_safe(text: str) -> Optional[Union[dict, list]]:
    """Parse JSON tolerant of markdown fences and surrounding prose."""
    if not text:
        return None
    for candidate in (text, _strip_fences(text), _extract_json_blob(text)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def validate_against_schema(
    data: dict, schema: type[BaseModel]
) -> Tuple[bool, List[str]]:
    """Return (ok, errors). Never raises."""
    try:
        schema.model_validate(data)
        return True, []
    except Exception as exc:  # pydantic.ValidationError or other
        return False, [str(exc)]


def merge_partial_results(existing: dict, new_partial: dict, path: str = "") -> dict:
    """Slot Wave 1 results into the skeleton without overwriting completed sections.

    ``path`` is a dot-delimited location (e.g. "sections.financials"). New data is
    merged in; populated/non-empty values already present are preserved.
    """
    target = existing
    keys = [k for k in path.split(".") if k]
    for k in keys[:-1]:
        target = target.setdefault(k, {})
    if keys:
        leaf_key = keys[-1]
        current = target.get(leaf_key)
        if isinstance(current, dict) and isinstance(new_partial, dict):
            target[leaf_key] = _deep_merge(current, new_partial)
        else:
            target[leaf_key] = new_partial
    else:
        return _deep_merge(existing, new_partial)
    return existing


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        elif k in out and out[k] not in (None, "", [], {}):
            # preserve already-populated values
            continue
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# 1.1  Model adapter                                                           #
# --------------------------------------------------------------------------- #
async def call_model(
    prompt: str,
    system: str,
    model_config: "config.ModelConfig",
    enable_search: bool = False,
    max_tokens: Optional[int] = None,
    response_format: JsonFormat = "json",
    _is_repair: bool = False,
) -> ModelResponse:
    """Provider-agnostic model call.

    Resolves search/token settings from the explicit args OR the model_config
    (whichever is more permissive). On failure, retries once with the configured
    fallback. When ``response_format="json"`` and parsing fails, issues one
    JSON-repair follow-up turn before giving up.
    """
    eff_search = enable_search or model_config.enable_search
    eff_max_tokens = max_tokens if max_tokens is not None else model_config.max_tokens

    dispatch = {
        "anthropic": _call_anthropic,
        "openai": _call_openai,
        "gemini": _call_gemini,
        "litellm": _call_litellm,
    }
    adapter = dispatch.get(model_config.provider)
    if adapter is None:
        return ModelResponse(
            provider=model_config.provider,
            model=model_config.model,
            error=f"unknown provider: {model_config.provider}",
        )

    started = time.perf_counter()
    resp = await _call_with_retry(adapter, prompt, system, model_config, eff_search, eff_max_tokens)
    resp.latency_ms = int((time.perf_counter() - started) * 1000)
    # Account this physical API call (cost + cumulative + per-call log). Recursive
    # fallback / JSON-repair calls each pass through here, so each counts once.
    _record_cost(resp, grounded=eff_search)

    # Fallback on hard failure
    if resp.error and model_config.fallback is not None:
        fb = await call_model(
            prompt, system, model_config.fallback,
            enable_search=enable_search, max_tokens=max_tokens,
            response_format=response_format, _is_repair=_is_repair,
        )
        if fb.error is None:
            return fb
        resp.error = f"primary: {resp.error} | fallback: {fb.error}"
        return resp

    if resp.error:
        return resp

    # JSON post-processing + one repair attempt
    if response_format == "json":
        parsed = parse_json_safe(resp.text)
        if parsed is None and not _is_repair:
            repair = await call_model(
                prompt=(
                    "The following text was supposed to be valid JSON but failed "
                    "to parse. Return ONLY the corrected, valid JSON — no prose, "
                    f"no markdown fences:\n\n{resp.text}"
                ),
                system="You are a strict JSON repair tool. Output only valid JSON.",
                model_config=model_config,
                enable_search=False,
                max_tokens=eff_max_tokens,
                response_format="json",
                _is_repair=True,
            )
            if repair.parsed is not None:
                # keep repaired content but credit both calls' token cost
                repair.tokens_in += resp.tokens_in
                repair.tokens_out += resp.tokens_out
                repair.estimated_cost_usd += resp.estimated_cost_usd
                return repair
        resp.parsed = parsed

    return resp


_TRANSIENT_MARKERS = (
    "503", "unavailable", "429", "overloaded", "resource_exhausted",
    "rate limit", "rate_limit", "try again", "timeout", "temporarily",
)


def _is_transient(msg: str) -> bool:
    m = (msg or "").lower()
    return any(t in m for t in _TRANSIENT_MARKERS)


async def _call_with_retry(adapter, prompt, system, mc, enable_search, max_tokens) -> "ModelResponse":
    """Call an adapter, retrying transient errors (503/429/overloaded) with backoff."""
    max_retries = getattr(config, "MAX_TRANSIENT_RETRIES", 3)
    base_delay = getattr(config, "RETRY_BASE_DELAY_SECONDS", 1.0)
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            return await adapter(prompt, system, mc, enable_search, max_tokens)
        except Exception as exc:  # provider/network/SDK error
            last_err = str(exc)
            if _is_transient(last_err) and attempt < max_retries:
                await asyncio.sleep(base_delay * (2 ** attempt))
                continue
            return ModelResponse(provider=mc.provider, model=mc.model, error=last_err)
    return ModelResponse(provider=mc.provider, model=mc.model, error=last_err)


async def _call_anthropic(
    prompt: str, system: str, mc: "config.ModelConfig",
    enable_search: bool, max_tokens: int,
) -> ModelResponse:
    import anthropic

    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    kwargs: Dict[str, Any] = {
        "model": mc.model,
        "max_tokens": max_tokens,
        "temperature": mc.temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if enable_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    msg = await client.messages.create(**kwargs)

    text_parts: List[str] = []
    sources: List[str] = []
    for block in msg.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
            for cite in getattr(block, "citations", None) or []:
                url = getattr(cite, "url", None)
                if url:
                    sources.append(url)
        elif btype == "web_search_tool_result":
            for item in getattr(block, "content", None) or []:
                url = getattr(item, "url", None)
                if url:
                    sources.append(url)

    tokens_in = msg.usage.input_tokens
    tokens_out = msg.usage.output_tokens
    return ModelResponse(
        text="".join(text_parts),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_used=tokens_in + tokens_out,
        estimated_cost_usd=_estimate_cost(mc.model, tokens_in, tokens_out),
        provider="anthropic",
        model=mc.model,
        sources=list(dict.fromkeys(sources)),
    )


async def _call_openai(
    prompt: str, system: str, mc: "config.ModelConfig",
    enable_search: bool, max_tokens: int,
) -> ModelResponse:
    from openai import AsyncOpenAI

    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    # Note: browsing/tool wiring is provider-version-specific; basic chat path here.
    resp = await client.chat.completions.create(
        model=mc.model,
        temperature=mc.temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    text = resp.choices[0].message.content or ""
    usage = resp.usage
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    return ModelResponse(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_used=tokens_in + tokens_out,
        estimated_cost_usd=_estimate_cost(mc.model, tokens_in, tokens_out),
        provider="openai",
        model=mc.model,
    )


async def _call_litellm(
    prompt: str, system: str, mc: "config.ModelConfig",
    enable_search: bool, max_tokens: int,
) -> ModelResponse:
    """LiteLLM gateway (OpenAI-compatible). Grounding is requested via the Gemini
    googleSearch tool; grounded source URLs come back as message.annotations."""
    from openai import AsyncOpenAI

    if not config.LITELLM_BASE_URL or not config.LITELLM_API_KEY:
        raise RuntimeError("LITELLM_BASE_URL / LITELLM_API_KEY not set")

    client = AsyncOpenAI(base_url=config.LITELLM_BASE_URL, api_key=config.LITELLM_API_KEY)
    kwargs: Dict[str, Any] = {
        "model": mc.model,
        "temperature": mc.temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if enable_search:
        # Best-effort grounding: the gateway forwards this to Gemini's web search.
        kwargs["extra_body"] = {"tools": [{"googleSearch": {}}]}

    resp = await client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    text = msg.content or ""

    sources: List[str] = []
    source_titles: Dict[str, str] = {}
    for ann in getattr(msg, "annotations", None) or []:
        uc = getattr(ann, "url_citation", None)
        url = getattr(uc, "url", None) if uc else None
        if url:
            sources.append(url)
            title = getattr(uc, "title", None) if uc else None
            if title and url not in source_titles:
                source_titles[url] = str(title)

    usage = resp.usage
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    return ModelResponse(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_used=tokens_in + tokens_out,
        estimated_cost_usd=_estimate_cost(mc.model, tokens_in, tokens_out),
        provider="litellm",
        model=mc.model,
        sources=list(dict.fromkeys(sources)),
        source_titles=source_titles,
    )


async def _call_gemini(
    prompt: str, system: str, mc: "config.ModelConfig",
    enable_search: bool, max_tokens: int,
) -> ModelResponse:
    from google import genai
    from google.genai import types as genai_types

    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    cfg_kwargs: Dict[str, Any] = {
        "system_instruction": system,
        "temperature": mc.temperature,
        "max_output_tokens": max_tokens,
    }
    if enable_search:
        cfg_kwargs["tools"] = [genai_types.Tool(google_search=genai_types.GoogleSearch())]

    resp = await client.aio.models.generate_content(
        model=mc.model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(**cfg_kwargs),
    )
    text = resp.text or ""
    usage = getattr(resp, "usage_metadata", None)
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0

    sources: List[str] = []
    source_titles: Dict[str, str] = {}
    for cand in getattr(resp, "candidates", None) or []:
        gm = getattr(cand, "grounding_metadata", None)
        for chunk in getattr(gm, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None)
            if uri:
                sources.append(uri)
                title = getattr(web, "title", None)
                if title and uri not in source_titles:
                    source_titles[uri] = str(title)

    return ModelResponse(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_used=tokens_in + tokens_out,
        estimated_cost_usd=_estimate_cost(mc.model, tokens_in, tokens_out),
        provider="gemini",
        model=mc.model,
        sources=list(dict.fromkeys(sources)),
        source_titles=source_titles,
    )


# --------------------------------------------------------------------------- #
# 1.2  Structured search helper                                               #
# --------------------------------------------------------------------------- #
_RESEARCH_SYSTEM = (
    "You are a meticulous financial research analyst. Use web search to find "
    "REAL, verifiable, recent data — prefer primary/named sources. For every "
    "item, report the value WITH units and the period/date, plus the source "
    "(name and the real retrieved URL).\n"
    "ANTI-LAZINESS: only conclude 'Not found' AFTER a genuine multi-source attempt "
    "(try several queries and the fallback sources named in the task). Do not stop "
    "at the first miss.\n"
    "RECENCY: a TODAY date is given — for time-sensitive items (news, ratings, "
    "deals, prices) run dated trailing-12-month queries and report the most recent.\n"
    "SCREENING FOCUS: this feeds a top-of-funnel SOURCING one-pager an analyst skims to "
    "decide pursue-vs-pass. For each item report the MOST RECENT and MOST MATERIAL data "
    "point — include earlier periods ONLY when the trend itself is the signal (e.g. "
    "revenue, margins, credit ratings). Do NOT compile exhaustive multi-year histories, "
    "scattered minor data points, definitions, or background narrative.\n"
    "PERIOD DISCIPLINE: report ONLY periods/dates explicitly stated in a real source. "
    "Never infer, interpolate, annualise or extrapolate a period (e.g. do not invent a "
    "'full-year' figure from a quarter). If only one period is sourced, give only that one.\n"
    "ENTITY SCOPE: attribute every figure to the SPECIFIC entity named in the task. If a "
    "figure belongs to a subsidiary, bottler, franchisee, JV or affiliate rather than the "
    "named parent, say so explicitly in the value — never present a subsidiary/affiliate "
    "number as the parent company's.\n"
    "URL DISCIPLINE: every URL you cite must be one you ACTUALLY retrieved during "
    "search and that resolves to the cited page. Never guess, construct, shorten or "
    "reuse a look-alike URL — if you don't have the real URL, write the source name "
    "and 'Not Available' for the URL.\n"
    "Never invent or approximate numbers."
)

_EXTRACT_SYSTEM = (
    "You convert a research report into strict JSON. Use ONLY facts present in "
    "the report. If the report lacks a field (or says 'Not found'), use "
    '"Not Available". Never invent data. Every URL you put in a "sources" entry '
    "MUST be copied verbatim from the SOURCE URLS list provided in the prompt — "
    "never fabricate, construct, guess or shorten a URL. Attribute each fact to "
    "the specific url(s) that actually support it; if none in the list applies, "
    "give the source name with url null.\n"
    "ACCURACY (non-negotiable — wrong data is worse than missing data):\n"
    "- NO MISLABELING: if the field asks for metric X but the report only contains a "
    "different though related metric Y, you MAY include Y ONLY IF you RELABEL the value to "
    "name what it actually is (e.g. value \"organic revenue +8% (proxy — price/mix not "
    "disclosed)\"). NEVER present Y as if it were X. If Y is not even a reasonable proxy, "
    "set \"Not Available\".\n"
    "- NO INVENTED PERIODS: use ONLY periods explicitly present in the report. Never infer, "
    "interpolate, annualise or extrapolate a period that the report does not state.\n"
    "- ENTITY SCOPE: attribute a figure to the named parent company ONLY. If the report's "
    "figure belongs to a subsidiary/bottler/affiliate, either label it as such in the value "
    "or set \"Not Available\" — never present it as the parent's.\n"
    "CRISPNESS (this JSON renders directly into a SOURCING one-pager — include only "
    "what changes a pursue-vs-pass call):\n"
    "- LATEST-BIASED: give the most recent value per metric, plus the few most recent stated "
    "earlier periods when the report gives them and a trend is useful (revenue, margins, "
    "ratings) — never a long history.\n"
    "- STRUCTURE over prose: when a value covers multiple items/periods/regions, make it "
    "a JSON ARRAY of FLAT objects with SHORT, CONSISTENT keys across every row (e.g. "
    "{period, value} or {region, value}). Never return a paragraph, a definition, a "
    "caveat, or a '*'-bulleted blob as a value.\n"
    "- NO HOLLOW RECORDS: every object in a list MUST carry real information — a value OR a "
    "short description. If the exact metric for an item/period is Not Available but the source "
    "says something qualitative about it, capture that in a 'description'/'note' field rather "
    "than emitting a record that is just a period/label with an empty or 'Not Available' value. "
    "Drop a record entirely ONLY when nothing at all is known about that item. Never lose a "
    "real fact just because the number is missing.\n"
    "- CONCISE notes: any note/detail field is a short clause (≤ ~25 words) that adds real "
    "context (driver, caveat, mix) — not a full sentence of narrative.\n"
    "- OMIT junk: if a field is missing, immaterial, or only an explanation of why data is "
    "absent, set value to \"Not Available\" (it is dropped from the one-pager). Do not pad "
    "with absence-narrative."
)


# --------------------------------------------------------------------------- #
# 1.2b  Local ground-truth document loader                                    #
# --------------------------------------------------------------------------- #
_DOC_EXTS = {".pdf", ".docx", ".txt", ".md", ".markdown"}


def _read_one_document(path: str) -> str:
    """Extract plain text from one PDF / .docx / .txt / .md file."""
    ext = _os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import pdfplumber

        parts: List[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)
    if ext == ".docx":
        import docx

        document = docx.Document(path)
        return "\n".join(p.text for p in document.paragraphs)
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:  # .txt / .md
        return fh.read()


def load_input_documents(
    input_dir: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> Tuple[str, List[str]]:
    """Load all local ground-truth documents from ``input_dir``.

    Returns ``(concatenated_text, filenames)``. Each file is prefixed with a
    ``=== <filename> ===`` header. The combined text is capped at ``max_chars``
    (truncated with a marker) to bound per-call cost. Returns ``("", [])`` when
    the folder is missing/empty so the pipeline behaves exactly as before when no
    documents are supplied. Unparseable files are skipped, not fatal.
    """
    base = input_dir or config.INPUT_DIR
    cap = max_chars if max_chars is not None else config.MAX_DOC_CONTEXT_CHARS
    if not base or not _os.path.isdir(base):
        return "", []
    names: List[str] = []
    chunks: List[str] = []
    for fname in sorted(_os.listdir(base)):
        path = _os.path.join(base, fname)
        if not _os.path.isfile(path):
            continue
        if _os.path.splitext(fname)[1].lower() not in _DOC_EXTS:
            continue
        try:
            text = _read_one_document(path).strip()
        except Exception as exc:  # never let a bad file kill the run
            print(f"  [doc] skipped {fname}: {type(exc).__name__} {exc}")
            continue
        if not text:
            continue
        names.append(fname)
        chunks.append(f"=== {fname} ===\n{text}")
    if not chunks:
        return "", names
    combined = "\n\n".join(chunks)
    if len(combined) > cap:
        combined = combined[:cap] + "\n\n[... documents truncated to fit context ...]"
        print(f"  [doc] {len(names)} doc(s) loaded, truncated to {cap:,} chars")
    else:
        print(f"  [doc] {len(names)} doc(s) loaded ({len(combined):,} chars)")
    return combined, names


def _documents_block(documents: str) -> str:
    """Authoritative-ground-truth block prepended to research/synthesis prompts."""
    if not documents or not documents.strip():
        return ""
    return (
        "\n\nAUTHORITATIVE COMPANY DOCUMENTS (GROUND TRUTH). Official documents "
        "supplied for this company follow. Treat them as the authoritative source: "
        "when they CONFLICT with web-search results, USE THE DOCUMENT VALUE and set "
        'that field\'s source to "Company document". Still use web search for '
        "anything the documents do not cover.\n"
        f"{documents}\n"
    )


def _norm_tokens(s: Any) -> set:
    return set(re.findall(r"[a-z0-9]+", str(s).lower()))


_EMPTY_VALUE_TOKENS = {"", "not available", "n/a", "na", "none", "unknown", "-"}


def _normalize_sources(entry: Dict[str, Any], valid_urls: Optional[set]) -> List[Dict[str, Any]]:
    """Coerce a field entry's source attribution into a clean
    [{"name": str, "url": str|None}] list.

    Accepts the new ``sources`` array, the legacy ``source``/``source_url`` pair,
    or a bare ``source`` string. Any url NOT in ``valid_urls`` (the set actually
    retrieved during grounded research) is dropped to None — this is the guard
    against the model fabricating plausible-looking links.
    """
    raw = entry.get("sources")
    items: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for s in raw:
            if isinstance(s, dict):
                items.append({"name": s.get("name") or s.get("source"), "url": s.get("url")})
            elif isinstance(s, str) and s.strip():
                items.append({"name": s, "url": None})
    # Legacy single-source shape.
    if not items and (entry.get("source") or entry.get("source_url")):
        items.append({"name": entry.get("source"), "url": entry.get("source_url")})

    clean: List[Dict[str, Any]] = []
    seen = set()
    for it in items:
        name = it.get("name")
        url = it.get("url")
        if isinstance(url, str):
            url = url.strip() or None
        else:
            url = None
        # Whitelist urls against what was actually grounded.
        if url is not None and valid_urls is not None and url not in valid_urls:
            url = None
        if not name and not url:
            continue
        key = f"{name}|{url or ''}"
        if key in seen:
            continue
        seen.add(key)
        clean.append({"name": name, "url": url})
    return clean


def _shape_entry(value: Any, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Uniform per-field entry the whole pipeline relies on. Keeps legacy
    ``source``/``source_url`` keys (primary source) for back-compat readers."""
    primary = sources[0] if sources else {"name": None, "url": None}
    is_empty = value is None or str(value).strip().lower() in _EMPTY_VALUE_TOKENS
    return {
        "value": value,
        "sources": [] if is_empty else sources,
        "source": None if is_empty else primary.get("name"),
        "source_url": None if is_empty else primary.get("url"),
    }


def _reconcile_fields(
    requested: List[str],
    extracted: Dict[str, Any],
    valid_urls: Optional[set] = None,
) -> Dict[str, Any]:
    """Map a requested field list onto an extraction dict tolerant of key drift.

    Models rarely reproduce long, punctuation-heavy field names verbatim as JSON
    keys, so exact lookups silently lose data. Match each requested field to the
    best extracted key by token containment (intersection / shorter-key length).
    Every returned entry is normalized to {value, sources[], source, source_url}.
    """
    items = [(k, _norm_tokens(k), v) for k, v in extracted.items() if not str(k).startswith("_")]
    out: Dict[str, Any] = {}
    for f in requested:
        fn = _norm_tokens(f)
        best_v, best_score = None, 0.0
        for _k, kn, v in items:
            inter = len(fn & kn)
            if not inter:
                continue
            score = inter / min(len(fn), len(kn))
            if score > best_score:
                best_score, best_v = score, v
        if best_v is None or best_score < 0.6:
            out[f] = _shape_entry("Not Available", [])
        elif isinstance(best_v, dict) and "value" in best_v:
            out[f] = _shape_entry(best_v["value"], _normalize_sources(best_v, valid_urls))
        else:
            # Model ignored the {value, sources} contract and emitted a bare
            # list/string/dict for this field — normalize so every entry has the
            # same shape the rest of the pipeline relies on.
            out[f] = _shape_entry(best_v, [])
    return out


def _extract_prompt(
    fields_block: str,
    findings: str,
    documents: str = "",
    grounded_urls: Optional[List[str]] = None,
    source_titles: Optional[Dict[str, str]] = None,
) -> str:
    doc_block = ""
    if documents and documents.strip():
        doc_block = (
            "\n\nAUTHORITATIVE COMPANY DOCUMENTS (GROUND TRUTH — when a value here "
            "conflicts with the web research report above, USE THE DOCUMENT VALUE and "
            'set that field\'s source name to "Company document" with url null):\n'
            f"{documents}\n"
        )

    # The grounded URLs actually visited during the research pass. The model maps
    # each fact to the SPECIFIC url(s) here that support it — this is what makes a
    # citation CORRECT instead of an arbitrarily-assigned link.
    urls = list(grounded_urls or [])
    titles = source_titles or {}
    if urls:
        # Show the real publisher title next to each url (when grounding gave one)
        # so the model attributes facts to the ACTUAL source name, not a guess.
        def _line(i: int, u: str) -> str:
            t = titles.get(u)
            return f"  [{i}] {t} — {u}" if t else f"  [{i}] {u}"
        url_lines = "\n".join(_line(i, u) for i, u in enumerate(urls))
        url_block = (
            "\n\nSOURCE URLS actually retrieved during research, with the publisher "
            "name where known (use these EXACT url strings — never invent a url; "
            'prefer the shown publisher name for that url\'s "name"):\n' + url_lines + "\n"
        )
        sources_rule = (
            'For "sources", list EVERY source that supports that specific fact, as an '
            'array of {"name": <publication/source name>, "url": <one of the exact '
            "urls listed above, or null>}. If a fact rests on 3 sources, include all 3. "
            'Use the SPECIFIC url that backs THAT fact — do not reuse one url for '
            "unrelated facts. If a fact has no url among the list, give its name with "
            "url null. MANDATORY: the research report came from the SOURCE URLS above, so "
            "every fact you extract WAS found in one of them — attribute each found value to "
            "at least one of those urls (pick the most relevant). NEVER return an empty "
            'sources array for a value you extracted; use [] ONLY for a "Not Available" field.'
        )
    else:
        url_block = ""
        sources_rule = (
            'For "sources", list every source the report names for that fact as an '
            'array of {"name": <publication/source name>, "url": <url if the report '
            "gives one, else null>}. Empty array [] only if no source is given."
        )

    return (
        "Extract these fields from the research report below into a JSON object "
        "keyed by the EXACT field names. Each value must be an object: "
        '{"value": <data or "Not Available">, "sources": [{"name": str, "url": '
        "str|null}, ...]}. When a field asks for multiple items (news, rounds, "
        "deals, milestones), set value to a JSON ARRAY of objects with the requested "
        "keys (e.g. date, headline) — and still attach the supporting sources to "
        'that field\'s "sources" array.\n'
        "ACCURACY: if the field asks for X but the report only has a related metric Y, you "
        "MAY include Y ONLY IF you RELABEL the value to say what it really is (e.g. "
        "\"organic revenue +8% (proxy — price/mix not disclosed)\") — never present Y as X; "
        "use ONLY periods the report states (never infer/annualise/extrapolate a period); "
        "attribute figures to the named parent company only (label or drop subsidiary/"
        "bottler/affiliate figures).\n"
        "KEEP IT CRISP (this renders straight into a sourcing one-pager): lead with the "
        "LATEST stated value per metric and add the few most recent earlier periods the "
        "report states when a trend helps; make multi-item values ARRAYS of FLAT objects "
        "with SHORT keys CONSISTENT across rows; keep any note to a short clause (≤ ~25 "
        "words) that adds real context; never output a paragraph, definition, caveat or "
        '\'*\'-bulleted blob as a value; set immaterial or unfound fields to "Not Available" '
        "rather than explaining their absence.\n"
        "NO HOLLOW LIST RECORDS: every object in a list value must carry a value OR a short "
        "description — if the exact number for an item is missing but something qualitative is "
        "known, put it in a 'description'/'note' instead of an empty value; drop a record only "
        "when nothing is known about it. Never lose a real fact because the number is missing.\n"
        f"{sources_rule}\n"
        f"{url_block}\n"
        f"Fields:\n{fields_block}\n\n"
        f"Research report:\n{findings if findings.strip() else '(no findings)'}"
        f"{doc_block}"
    )


async def search_for_data_fields(
    company_name: str,
    data_fields: List[str],
    recommended_sources: List[str],
    model_config: "config.ModelConfig",
    max_searches: int = 5,
    focus: str = "",
    documents: str = "",
) -> Dict[str, Any]:
    """Research a set of named data fields for a company.

    Returns a dict keyed by field name. Each value is
    ``{"value": <data or "Not Available">, "source": <source name or None>}``.
    Missing data is marked "Not Available" — never fabricated. ``focus`` optionally
    narrows the grounded search to one sub-topic. ``documents`` is authoritative
    local-document text that overrides web data on conflict (ground truth).
    """
    fields_block = "\n".join(f"- {f}" for f in data_fields)
    sources_block = ", ".join(recommended_sources) if recommended_sources else "any credible source"

    # --- Pass 1: grounded research, FREE-FORM (no JSON constraint) ------------
    # Forcing strict JSON while web-search is active suppresses the model's
    # research effort and trips the JSON-repair path. Let it research and write
    # naturally first; structure it in pass 2.
    focus_line = f"FOCUS of this search: {focus}.\n" if focus else ""
    today = datetime.now().strftime("%d %b %Y")
    research_prompt = (
        f"Research the company: {company_name}.\n"
        f"TODAY is {today} — prefer the most recent data and run dated "
        f"trailing-12-month queries for time-sensitive items.\n"
        f"{focus_line}"
        f"Prefer these sources first: {sources_block}. "
        f"If a hard field isn't in those, try additional credible sources before "
        f"concluding 'Not found'. You may run up to {max_searches} web searches.\n\n"
        "Find each of the following and report value + date/period + source "
        "(list EVERY item you find, most recent first):\n"
        f"{fields_block}"
        f"{_documents_block(documents)}"
    )
    research = await call_model(
        prompt=research_prompt,
        system=_RESEARCH_SYSTEM,
        model_config=model_config,
        enable_search=True,
        max_tokens=max(model_config.max_tokens, 4000),
        response_format="text",
    )
    findings = research.text or ""
    grounded_urls = list(research.sources or [])
    source_titles = dict(research.source_titles or {})

    # --- Pass 2: structured extraction, JSON only, NO search ------------------
    # The grounded URLs (with real publisher titles) are handed to the extractor so
    # it attributes each fact to the SPECIFIC, correctly-NAMED url(s) that support
    # it (correct citations with real source names, not guessed ones).
    extraction = await call_model(
        prompt=_extract_prompt(fields_block, findings, documents, grounded_urls, source_titles),
        system=_EXTRACT_SYSTEM,
        model_config=model_config,
        enable_search=False,
        # Generous token budget so extraction over many fields (data-rich
        # companies) doesn't truncate the JSON and silently drop fields.
        max_tokens=max(model_config.max_tokens, 6000),
        response_format="json",
    )

    extracted = extraction.parsed if isinstance(extraction.parsed, dict) else {}
    result = _reconcile_fields(data_fields, extracted, valid_urls=set(grounded_urls))
    result["_sources"] = grounded_urls
    result["_source_titles"] = source_titles
    result["_meta"] = {
        "tokens_used": research.tokens_used + extraction.tokens_used,
        "estimated_cost_usd": research.estimated_cost_usd + extraction.estimated_cost_usd,
        "error": research.error or extraction.error,
    }
    return result


async def search_grouped(
    company_name: str,
    groups: List[Dict[str, Any]],
    recommended_sources: List[str],
    model_config: "config.ModelConfig",
    documents: str = "",
) -> Dict[str, Any]:
    """Multi-search research for breadth-heavy domains.

    Runs ONE focused grounded research call per group (concurrently) so each
    sub-topic — e.g. recent news vs funding rounds vs milestones — gets its own
    web search, then a SINGLE extraction over the merged findings. Each group is
    ``{"label": str, "focus": str, "fields": List[str]}``. Returns the same shape
    as ``search_for_data_fields`` (field -> {value, source}, plus _sources/_meta).
    ``documents`` is authoritative local-document ground truth passed to each group.
    """
    # Each group is an independent focused research+extraction (its own
    # search_for_data_fields), scoping each extraction to its sub-topic's findings
    # so a big news/funding array can't truncate the later milestone/catalyst
    # fields. Groups run concurrently; results are merged.
    per_group = await asyncio.gather(*[
        search_for_data_fields(
            company_name=company_name, data_fields=g["fields"],
            recommended_sources=recommended_sources, model_config=model_config,
            focus=g.get("focus", ""), documents=documents,
        )
        for g in groups
    ])

    merged: Dict[str, Any] = {}
    srcs: List[str] = []
    titles: Dict[str, str] = {}
    tokens, cost = 0, 0.0
    err = None
    for r in per_group:
        for k, v in r.items():
            if k == "_sources":
                srcs.extend(v)
            elif k == "_source_titles":
                for u, t in (v or {}).items():
                    titles.setdefault(u, t)
            elif k == "_meta":
                tokens += v.get("tokens_used", 0) or 0
                cost += v.get("estimated_cost_usd", 0.0) or 0.0
                err = err or v.get("error")
            else:
                merged[k] = v
    merged["_sources"] = list(dict.fromkeys(srcs))
    merged["_source_titles"] = titles
    merged["_meta"] = {"tokens_used": tokens, "estimated_cost_usd": cost, "error": err}
    return merged


# --------------------------------------------------------------------------- #
# 1.4  Concurrency utility                                                     #
# --------------------------------------------------------------------------- #
async def run_skills_in_parallel(
    skills: List[Callable[[dict], Awaitable[Any]]],
    inputs: List[dict],
    max_concurrent: int = config.MAX_PARALLEL_SKILLS,
    per_skill_timeout: int = config.PER_SKILL_TIMEOUT_SECONDS,
    skill_names: Optional[List[str]] = None,
) -> List[Union[Any, SkillError]]:
    """Run skill callables with bounded concurrency + per-skill timeout.

    Results are returned in the same order as ``inputs``. Failures and timeouts
    are returned as ``SkillError`` objects (never raised) so the pipeline can
    mark the affected section "Not Available" and continue.
    """
    if len(skills) != len(inputs):
        raise ValueError("skills and inputs must be the same length")
    names = skill_names or [getattr(s, "__name__", f"skill_{i}") for i, s in enumerate(skills)]
    sem = asyncio.Semaphore(max_concurrent)

    async def _run_one(idx: int) -> Union[Any, SkillError]:
        async with sem:
            try:
                return await asyncio.wait_for(skills[idx](inputs[idx]), timeout=per_skill_timeout)
            except asyncio.TimeoutError:
                return SkillError(
                    skill_name=names[idx],
                    error=f"timed out after {per_skill_timeout}s",
                    error_type="timeout",
                )
            except Exception as exc:
                return SkillError(skill_name=names[idx], error=str(exc), error_type="exception")

    return await asyncio.gather(*[_run_one(i) for i in range(len(skills))])


# --------------------------------------------------------------------------- #
# 1.5  Persona relevance evaluator                                            #
# --------------------------------------------------------------------------- #
async def evaluate_persona_relevance(
    item_description: str,
    item_data_summary: str,
    model_config: Optional["config.ModelConfig"] = None,
) -> PersonaRelevance:
    """Score how each of the four personas is affected by an item.

    Implemented as a model call (per spec 1.5) using a fast model. Each persona
    field is a specific decision-impact statement or None; overall_score 0-100.
    """
    mc = model_config or config.IMPORTANCE_SCORING_MODEL
    system = (
        "You apply the persona-relevance test for a SOURCING one-pager — a screening "
        "doc an analyst reads to decide whether a company is worth DEEPER research "
        "(pursue vs pass), NOT to make a final decision. The four personas each make "
        "that screen-in / screen-out call: PE analyst (worth diligencing as an "
        "investment?), investment banker (worth pitching / a live deal angle?), credit "
        "analyst (worth a closer underwriting look / any obvious red flags?), consultant "
        "(an operational angle worth pursuing?). For each, state the SPECIFIC way this "
        "item would change that pursue-or-pass decision, or null if it is irrelevant to "
        "that persona. Be sharp, not vague — 'good context' fails the test."
    )
    prompt = (
        f"Item: {item_description}\n"
        f"Data summary: {item_data_summary}\n\n"
        "Return JSON with keys: pe_analyst, banker, credit_analyst, consultant "
        "(each a one-sentence pursue-vs-pass impact string or null), and overall_score "
        "(integer 0-100 reflecting how many personas care and how strongly it moves "
        "their decision to research the company deeper)."
    )
    resp = await call_model(prompt, system, mc, response_format="json")
    # A failed scoring CALL (timeout / 503 / API error) must not look like
    # "the model judged this irrelevant" — default to a keep-worthy score so the
    # caller doesn't silently drop a domain over a transient error.
    if resp.error:
        return PersonaRelevance(overall_score=50)
    data = resp.parsed if isinstance(resp.parsed, dict) else {}
    try:
        return PersonaRelevance.model_validate(data)
    except Exception:
        return PersonaRelevance(overall_score=int(data.get("overall_score", 0)) if isinstance(data, dict) else 0)

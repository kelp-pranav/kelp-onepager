"""Phase 4.5 — Section Presentation.

Domain-fed sections arrive from json_population as a RAW ``{field: value}`` dump
(with "Not Available" strings for every missing field). This pass turns each
data-bearing section's FETCHED fields into render-ready content matching its
declared ``content_type`` (stat_grid / table / list / chart / prose) plus a
one-line analytical takeaway — mirroring how the synthesis skills already produce
presentable output, but for the generic + sector domains.

Hard rules:
  * NEVER invent data — only reformat / lightly analyze the values provided.
  * Drop "Not Available" fields entirely (no NA noise in the JSON).
  * Preserve source citations: keep section ``citations`` and the per-field
    ``field_citations`` (filtered to the surviving fields).
  * Degrade gracefully — on any model error, build a deterministic presented form
    from the found fields and continue. Never block.

One batched model call per feeding domain; domains run concurrently.

Public entry point:
    async def run(final_json, section_plan, model_config) -> (final_json, warnings)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Tuple

import config
import skill_functions as sf
from schemas import FinalJSON, SectionPlan

# Tokens meaning "no data" (same set the rest of the pipeline uses).
_EMPTY = {"", "not available", "n/a", "na", "none", "unknown", "-"}

# Sections NOT fed by a Wave 1 domain — already presentable / handled elsewhere.
_SKIP_DOMAINS = {"Investment Thesis", "SWOT", "Future Plan", "Risk Flags", "Sector Research"}

_MAX_CONCURRENT = 4


def _found(value: Any) -> bool:
    return value is not None and str(value).strip().lower() not in _EMPTY


def _found_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """The subset of a section's data that was actually found (NA dropped)."""
    return {f: v for f, v in data.items() if _found(v)}


def _deterministic_present(content_type: str, found: Dict[str, Any]) -> Any:
    """Fallback render-ready form built without a model (used on model failure)."""
    if content_type == "prose":
        return {"text": "; ".join(f"{f}: {v}" for f, v in found.items())}
    if content_type == "table":
        return {"columns": ["Field", "Value"],
                "rows": [[f, v if isinstance(v, str) else json.dumps(v)] for f, v in found.items()]}
    if content_type == "stat_grid":
        return [{"value": v if isinstance(v, str) else json.dumps(v), "label": f}
                for f, v in list(found.items())[:6]]
    # list / chart / anything else → simple labelled bullets
    return [f"{f}: {v if isinstance(v, str) else json.dumps(v)}" for f, v in found.items()]


_SYSTEM = (
    "You format ALREADY-FETCHED company data into presentable one-pager content. "
    "ABSOLUTE RULE: use ONLY the field values provided — never add, infer, "
    "extrapolate or invent any fact, number, date or item that is not explicitly "
    "present. You reformat and lightly analyze; you do NOT research.\n"
    "For each section, output content matching its content_type:\n"
    '  stat_grid -> [{"value": str, "label": str, "sub": str|null, "direction": '
    '"up"|"down"|"flat"|null}]   (max 6 cards, headline numbers)\n'
    '  table     -> {"columns": [str,...], "rows": [[cell,...], ...]}\n'
    '  chart     -> {"series": [{"name": str, "points": [{"x": str, "y": number}]}]}\n'
    '  list      -> [str, ...]  OR  [{...}, ...]  (e.g. news/peers/leadership items)\n'
    '  prose     -> {"text": str}   (1-3 tight sentences)\n'
    'Also give "analysis": ONE neutral sentence summarizing what the data shows '
    "(empty string if there is too little to say). Never editorialize beyond the data.\n"
    'Return STRICT JSON: {"sections": [{"section_name": str (verbatim), '
    '"presented": <per content_type>, "analysis": str}]}.'
)


def _build_prompt(company: str, sections: List[Dict[str, Any]]) -> str:
    blocks = []
    for s in sections:
        blocks.append(
            f"### Section: {s['section_name']}\n"
            f"content_type: {s['content_type']}\n"
            f"fetched fields (use ONLY these):\n{json.dumps(s['found'], ensure_ascii=False, indent=1)}"
        )
    return (
        f"Company: {company}\n\n"
        "Format each of the following sections. Use only the fetched fields shown "
        "for each — do not borrow data across sections, do not add anything.\n\n"
        + "\n\n".join(blocks)
    )


async def _present_domain(
    company: str,
    sections: List[Dict[str, Any]],
    model_config: "config.ModelConfig",
) -> Dict[str, Dict[str, Any]]:
    """One model call for all of a domain's sections. Returns
    {section_name: {"presented":..., "analysis":...}}. Falls back deterministically
    (per section) on any error so a section is never left as a raw NA dump."""
    fallback = {
        s["section_name"]: {
            "presented": _deterministic_present(s["content_type"], s["found"]),
            "analysis": "",
            "_degraded": True,
        }
        for s in sections
    }
    try:
        resp = await sf.call_model(
            prompt=_build_prompt(company, sections), system=_SYSTEM,
            model_config=model_config, enable_search=False,
            response_format="json", max_tokens=max(model_config.max_tokens, 4000),
        )
    except Exception:
        return fallback
    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    items = parsed.get("sections")
    if not isinstance(items, list):
        return fallback
    by_name: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if isinstance(it, dict) and it.get("section_name") and it.get("presented") is not None:
            by_name[it["section_name"]] = {
                "presented": it["presented"],
                "analysis": it.get("analysis") or "",
            }
    # Any section the model dropped keeps its deterministic fallback.
    for s in sections:
        by_name.setdefault(s["section_name"], fallback[s["section_name"]])
    return by_name


async def run(
    final_json: FinalJSON,
    section_plan: SectionPlan,
    model_config: "config.ModelConfig" = config.PRESENTATION_MODEL,
) -> Tuple[FinalJSON, List[str]]:
    company = (final_json.metadata or {}).get("company_name") or "the company"
    warnings: List[str] = []

    # section_name -> feeding domain (from the planned skeleton slots).
    fed_by: Dict[str, str] = {}
    for slot in (section_plan.skeleton or {}).get("sections", []):
        fed_by[slot.get("section_name")] = slot.get("fed_by_domain", "")

    # Group data-bearing domain-fed sections by their feeding domain.
    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    targets = {}  # section_name -> (CompletedSection, found)
    for sec in final_json.sections:
        data = sec.content.get("data")
        if not isinstance(data, dict):
            continue  # synthesis / profile sections have no raw data dict
        if sec.content.get("status") in ("unavailable", "not_applicable"):
            continue
        domain = fed_by.get(sec.section_name, "")
        if domain in _SKIP_DOMAINS:
            continue
        found = _found_fields(data)
        if not found:
            continue
        by_domain.setdefault(domain or "_ungrouped", []).append({
            "section_name": sec.section_name,
            "content_type": sec.content_type,
            "found": found,
        })
        targets[sec.section_name] = (sec, found)

    if not by_domain:
        return final_json, warnings

    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _guarded(domain: str, secs: List[Dict[str, Any]]):
        async with sem:
            return domain, await _present_domain(company, secs, model_config)

    results = await asyncio.gather(*[_guarded(d, s) for d, s in by_domain.items()])

    degraded = 0
    for _domain, by_name in results:
        for name, payload in by_name.items():
            tgt = targets.get(name)
            if tgt is None:
                continue
            sec, found = tgt
            if payload.get("_degraded"):
                degraded += 1
            # Rewrite content: render-ready + analysis, NA-free data, preserved meta.
            sec.content["presented"] = payload["presented"]
            sec.content["analysis"] = payload.get("analysis", "")
            sec.content["data"] = found  # NA fields dropped for good
            # Preserve citations; filter per-field map to the surviving fields.
            if sec.field_citations:
                sec.field_citations = {
                    f: idxs for f, idxs in sec.field_citations.items() if f in found
                }

    if degraded:
        warnings.append(f"{degraded} section(s) used deterministic presentation fallback")
    return final_json, warnings

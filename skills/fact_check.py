"""Accuracy verification pass (sector-only).

After a domain is researched, re-confirm each extracted value against current primary
sources with ONE grounded Flash-Lite call, and annotate every field with a verdict:

    confirmed       — a credible source supports the value as stated
    corrected       — the value is wrong; `note` carries the right figure
    unsupported     — no credible source supports it (likely fabricated)
    scope_mismatch  — the figure belongs to a subsidiary/bottler/affiliate, not the parent

This ANNOTATES (it does not silently rewrite values) so the UI can badge wrong/unverified
data and the analyst can discount it. Best-effort: on any failure every field is left
verdict "unchecked" and nothing is changed.

Public entry:  async def verify_fields(company, domain_name, field_pairs, model_config) -> dict
    field_pairs: List[(label, value, sources)]   (the found, non-empty fields)
    returns:     {label: {"verdict": str, "note": str}}
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import config
import skill_functions as sf

_VERDICTS = {"confirmed", "corrected", "unsupported", "scope_mismatch"}

_SYSTEM = (
    "You are a meticulous fact-checker for a financial sourcing one-pager. You are given "
    "data points already extracted about ONE company, each with the value and the source it "
    "was attributed to. Use web search to confirm each value against current PRIMARY sources "
    "(company filings/earnings, regulator databases, rating agencies, reputable press).\n"
    "For EACH field return one verdict:\n"
    "  'confirmed'      — a credible source supports the value as stated.\n"
    "  'corrected'      — the value is materially wrong; put the correct figure in note.\n"
    "  'unsupported'    — no credible source supports it (treat as likely fabricated).\n"
    "  'scope_mismatch' — the figure actually belongs to a subsidiary / bottler / franchisee "
    "/ affiliate, NOT the named parent company.\n"
    "Be strict: if you cannot find support, say 'unsupported' — do NOT default to 'confirmed'. "
    "Keep each note to a short clause (≤ 14 words). Output ONLY the JSON object specified."
)


def _compact(value: Any, cap: int = 320) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= cap else s[:cap] + "…"


def _prompt(company: str, domain_name: str, field_pairs: List[Tuple]) -> str:
    lines = []
    for i, (label, value, sources) in enumerate(field_pairs):
        names = ", ".join(s.get("name") for s in (sources or []) if s.get("name")) or "(none)"
        lines.append(f'  {i}. "{label}" = {_compact(value)}   [cited: {names}]')
    body = "\n".join(lines)
    return (
        f"Company (parent entity to verify against): {company}\n"
        f"Research domain: {domain_name}\n\n"
        f"Fields to verify:\n{body}\n\n"
        'Return ONLY a JSON object keyed by the field INDEX (as a string), e.g. '
        '{"0": {"verdict": "confirmed", "note": ""}, '
        '"1": {"verdict": "corrected", "note": "actual FY26 figure is X"}}. '
        "Every index 0.." + str(len(field_pairs) - 1) + " must appear."
    )


async def verify_fields(
    company: str,
    domain_name: str,
    field_pairs: List[Tuple[str, Any, List[Dict[str, Any]]]],
    model_config: "config.ModelConfig" = None,
) -> Dict[str, Dict[str, str]]:
    """Verify one domain's found fields. Returns {label: {verdict, note}} — verdict is one of
    _VERDICTS or 'unchecked' if verification could not run."""
    model_config = model_config or config.VERIFY_MODEL
    out: Dict[str, Dict[str, str]] = {
        label: {"verdict": "unchecked", "note": ""} for label, _v, _s in field_pairs
    }
    if not field_pairs:
        return out
    try:
        resp = await sf.call_model(
            prompt=_prompt(company, domain_name, field_pairs),
            system=_SYSTEM,
            model_config=model_config,
            enable_search=True,
            response_format="json",
        )
        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    except Exception as exc:  # never let verification break the run
        for label in out:
            out[label]["note"] = f"verify error: {type(exc).__name__}"
        return out

    for i, (label, _v, _s) in enumerate(field_pairs):
        rec = parsed.get(str(i)) or parsed.get(i) or {}
        if not isinstance(rec, dict):
            continue
        verdict = str(rec.get("verdict", "")).strip().lower()
        if verdict in _VERDICTS:
            out[label] = {"verdict": verdict, "note": str(rec.get("note", "")).strip()[:140]}
    return out

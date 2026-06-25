"""Skill — Coverage Gap-Gate (Phase 4.5).

The opposite of fact-checking: instead of verifying what's present, it looks at
what's MISSING or FORCED relative to the company's business, so the one-pager is
honestly scoped rather than padded. One flash-lite call that:

  - marks genuinely irrelevant sections "Not Applicable" (vs just "missing data"),
  - lists real gaps (important sections that came back empty) with why-it-matters.

Public entry point:
    async def run(final_json, subsector, model_config) -> (final_json, report)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import config
import skill_functions as sf
from schemas import FinalJSON

_SYSTEM = (
    "You assess COVERAGE of a company one-pager against the company's business "
    "type. You are given each section and whether it is populated / partial / "
    "unavailable. Do TWO things:\n"
    "1) not_applicable: list sections that are genuinely IRRELEVANT to this kind of "
    "business (e.g. 'Working capital analysis' / inventory days for a bank; 'Credit "
    "ratings' for an early unlisted startup with none; 'Revenue by product segment' "
    "for a single-product firm). Only sections that do not apply — NOT ones that are "
    "merely missing data.\n"
    "2) gaps: list sections that are unavailable/partial but SHOULD have data for "
    "this business, each with a short 'why_it_matters'.\n"
    'Return JSON: {"not_applicable": [section names], '
    '"gaps": [{"section": str, "why_it_matters": str}], "notes": str}.'
)


def _digest(final_json: FinalJSON) -> str:
    rows = [f"- {s.section_name} [{s.section_tag_label}]: {s.content.get('status', 'unknown')}"
            for s in final_json.sections]
    return "\n".join(rows)


async def run(
    final_json: FinalJSON,
    subsector: str,
    model_config: "config.ModelConfig" = None,
) -> Tuple[FinalJSON, Dict[str, Any]]:
    mc = model_config or getattr(config, "COVERAGE_MODEL", config.VALIDATION_MODEL)
    prompt = (
        f"Company business type / subsector: {subsector}\n\n"
        f"Sections and their status:\n{_digest(final_json)}\n\n"
        "Assess coverage as specified."
    )
    resp = await sf.call_model(prompt, _SYSTEM, mc, response_format="json")
    data = resp.parsed if isinstance(resp.parsed, dict) else {}
    report = {
        "not_applicable": data.get("not_applicable", []) if isinstance(data, dict) else [],
        "gaps": data.get("gaps", []) if isinstance(data, dict) else [],
        "notes": data.get("notes", "") if isinstance(data, dict) else "",
    }

    # Mark genuinely-irrelevant sections "not_applicable" (only if not already
    # populated — never override real data).
    na = {str(n).strip().lower() for n in report["not_applicable"]}
    for s in final_json.sections:
        if s.section_name.strip().lower() in na and s.content.get("status") != "populated":
            s.content["status"] = "not_applicable"

    final_json.metadata["coverage"] = report
    final_json.metadata["sections_not_applicable"] = sum(
        1 for s in final_json.sections if s.content.get("status") == "not_applicable")
    return final_json, report

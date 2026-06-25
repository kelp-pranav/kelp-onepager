"""Skill 8 — SWOT synthesis (Wave 2).

Reads ALL Wave 1 outputs. Produces 4 quadrants × 4-5 data-backed bullets.
Strengths from Financials + Market Position; Weaknesses from Credit & Risk +
Track Record gaps; Opportunities from Market Position + sector-specific domains;
Threats from Credit & Risk + Geography (FX, regulatory). This is what a banker
uses to build the pitch narrative.

Input:  {"company_profile": {...}, "wave_1_results": {domain_name: DomainResearchOutput}}
Output: {"strengths": [...], "weaknesses": [...], "opportunities": [...], "threats": [...]}
"""

from __future__ import annotations

from typing import Any, Dict

import config
from skills import synthesis_base

_SYSTEM = (
    "You are a strategy analyst producing a SWOT for a company one-pager used by an "
    "investment banker to build the pitch narrative. Each quadrant must have 4-5 bullets, "
    "and EVERY bullet must be traceable to a specific data point in the Wave 1 research "
    "(a number, rating, fact). Do NOT invent claims — if the data shows best-in-class "
    "margins, you may claim it; otherwise don't. Guidance: Strengths from Financials + "
    "Market Position; Weaknesses from Credit & Risk + gaps; Opportunities from Market "
    "Position + sector-specific domains; Threats from Credit & Risk + Geography "
    "(FX, regulatory).\n\n"
    'Return JSON: {"strengths": [str,...], "weaknesses": [str,...], '
    '"opportunities": [str,...], "threats": [str,...]}.'
)


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.SWOT_MODEL,
) -> Dict[str, Any]:
    wave_1 = input_data.get("wave_1_results", {})
    profile = input_data.get("company_profile", {})
    data = synthesis_base.digest(wave_1)  # all domains
    prompt = (
        f"Company: {profile.get('company_name', 'the company')} "
        f"({profile.get('resolved_sector', '')}).\n\n"
        f"Wave 1 research data:\n{data}\n\n"
        "Produce the data-backed SWOT as specified."
    )
    result = await synthesis_base.synthesize(_SYSTEM, prompt, model_config)
    if not any(result.get(q) for q in ("strengths", "weaknesses", "opportunities", "threats")):
        result = {"strengths": [], "weaknesses": [], "opportunities": [], "threats": [],
                  "note": "SWOT could not be synthesized from available data."}
    result["_reads"] = list(wave_1.keys())  # SWOT reads all domains
    return result

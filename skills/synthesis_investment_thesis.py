"""Skill 7 — Investment Thesis synthesis (Wave 2).

Reads Financials, Market Position, Track Record, Credit & Risk. Produces 3
data-backed thesis bullets — competitive moat / current story / forward
opportunity. This is what a PE analyst reads first.

Input:  {"company_profile": {...}, "wave_1_results": {domain_name: DomainResearchOutput}}
Output: {"thesis": [{"heading", "text"}], "confidence"}
"""

from __future__ import annotations

from typing import Any, Dict

import config
from skills import synthesis_base

_READS = ["Financials & Ratios", "Market Position", "Track Record", "Credit & Risk"]

_SYSTEM = (
    "You are an investment analyst writing the thesis for a company one-pager, read "
    "first by a PE analyst deciding whether to invest and at what price. Write EXACTLY "
    "three bullets: (1) competitive moat — why defensible; (2) current story — why now, "
    "citing a recent metric/event; (3) forward opportunity — why ahead, citing catalysts. "
    "Every claim MUST be backed by a specific number/fact from the data provided. No "
    "fluff, no unsupported superlatives. If data is too sparse, say so honestly.\n\n"
    'Return JSON: {"thesis": [{"heading": str, "text": str}, ...], "confidence": '
    '"high"|"medium"|"low"}.'
)


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.INVESTMENT_THESIS_MODEL,
) -> Dict[str, Any]:
    wave_1 = input_data.get("wave_1_results", {})
    profile = input_data.get("company_profile", {})
    data = synthesis_base.digest(wave_1, _READS)
    prompt = (
        f"Company: {profile.get('company_name', 'the company')} "
        f"({profile.get('resolved_sector', '')}).\n\n"
        f"Wave 1 research data:\n{data}\n\n"
        "Write the three-bullet investment thesis as specified."
    )
    result = await synthesis_base.synthesize(_SYSTEM, prompt, model_config)
    if not result.get("thesis"):
        result = {"thesis": [], "confidence": "low",
                  "note": "Investment thesis could not be synthesized from available data."}
    result["_reads"] = _READS  # evidence-base domains, for source attribution
    return result

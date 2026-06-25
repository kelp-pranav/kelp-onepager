"""Skill 9 — Future Plan synthesis (Wave 2).

Reads Track Record (catalysts), Financials (capacity for growth), and
sector-specific domains (pipeline, capex plans). Produces 5-7 quantified
strategic targets with timelines. This is what a consultant evaluates for
management credibility.

Input:  {"company_profile": {...}, "wave_1_results": {domain_name: DomainResearchOutput}}
Output: {"targets": [{"target", "timeline"}], "confidence"}
"""

from __future__ import annotations

from typing import Any, Dict

import config
from schemas import DomainResearchOutput
from skills import synthesis_base

# Always read these; plus any sector-specific domains (is_sector_specific=True).
_CORE_READS = ["Track Record", "Financials & Ratios"]

_SYSTEM = (
    "You are an analyst extracting a company's forward plan for a one-pager, evaluated "
    "by a consultant for management credibility. Produce 5-7 QUANTIFIED strategic targets "
    "with timelines (e.g. 'Revenue ₹300 Cr by FY26', 'Commission Dahej plant by Q3 FY26'). "
    "Each target must be specific and grounded in the data (announced catalysts, capex, "
    "capacity plans) — not vague aspirations. If the data lacks forward guidance, return "
    "fewer targets rather than inventing them.\n\n"
    'Return JSON: {"targets": [{"target": str, "timeline": str}, ...], '
    '"confidence": "high"|"medium"|"low"}.'
)


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.FUTURE_PLAN_MODEL,
) -> Dict[str, Any]:
    wave_1 = input_data.get("wave_1_results", {})
    profile = input_data.get("company_profile", {})

    reads = list(_CORE_READS)
    for name, out in wave_1.items():
        if isinstance(out, DomainResearchOutput) and name not in reads:
            # include sector-specific domains (anything beyond the 6 generics)
            if name not in (
                "Corporate Structure", "Market Position", "Credit & Risk", "Geography",
            ):
                reads.append(name)

    data = synthesis_base.digest(wave_1, reads)
    prompt = (
        f"Company: {profile.get('company_name', 'the company')} "
        f"({profile.get('resolved_sector', '')}).\n\n"
        f"Wave 1 research data:\n{data}\n\n"
        "Extract the 5-7 quantified forward targets as specified."
    )
    result = await synthesis_base.synthesize(_SYSTEM, prompt, model_config)
    if not result.get("targets"):
        result = {"targets": [], "confidence": "low",
                  "note": "Future plan could not be synthesized from available data."}
    result["_reads"] = reads  # evidence-base domains, for source attribution
    return result

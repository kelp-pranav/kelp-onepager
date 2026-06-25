"""Skill — Risk Flags synthesis (Wave 2, interpretive).

Risk flags are not a lookup — they are JUDGED. This reads the relevant Wave 1
research (credit ratings & triggers, recent news/litigation, governance/RPT/audit,
leverage & working-capital stress) AND runs its own fresh web search to surface
red/amber/green flags with severity. This is the panel a credit analyst scans
first. Replaces the old factual "risk flags" field on the Credit & Risk domain.

Input:  {"company_profile": {...}, "wave_1_results": {domain_name: DomainResearchOutput},
         "documents": str}
Output: {"data": {"flags": [{"severity", "title", "description"}, ...]}, "confidence"}
        (nested under "data" for the Risk flags section; falls back
        to status "unavailable" when nothing can be synthesized.)
"""

from __future__ import annotations

from typing import Any, Dict

import config
from skills import synthesis_base

# Risk-relevant generic domains. (Sector-specific domains still inform via the
# digest if they carry risk language, but these are the reliable anchors.)
_READS = ["Credit & Risk", "Track Record", "Corporate Structure", "Financials & Ratios"]

_SYSTEM = (
    "You are a credit/risk analyst producing the RISK FLAGS panel of a company "
    "one-pager, read first by a credit analyst sizing default probability. Identify "
    "4-7 concrete risk flags, each rated severity: red (serious / near-term threat), "
    "amber (watch item), or green (notable strength / mitigant). Base each flag on the "
    "supplied Wave 1 research (credit ratings & triggers, recent news/litigation, "
    "governance / related-party / audit issues, leverage & working-capital stress) AND "
    "your own fresh web search for anything recent or material the data misses. Every "
    "flag must be SPECIFIC and evidence-backed — name the number, fact or event in the "
    "description; no vague boilerplate. If authoritative company documents are supplied, "
    "treat them as ground truth.\n\n"
    'Return JSON: {"flags": [{"severity": "red"|"amber"|"green", "title": str, '
    '"description": str}, ...], "confidence": "high"|"medium"|"low"}.'
)


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.RISK_FLAGS_MODEL,
) -> Dict[str, Any]:
    wave_1 = input_data.get("wave_1_results", {})
    profile = input_data.get("company_profile", {})
    documents = input_data.get("documents", "")

    data = synthesis_base.digest(wave_1, _READS)
    prompt = (
        f"Company: {profile.get('company_name', 'the company')} "
        f"({profile.get('resolved_sector', '')}).\n\n"
        f"Wave 1 research data:\n{data}\n\n"
        "Identify the risk flags as specified."
    )
    result = await synthesis_base.synthesize(
        _SYSTEM, prompt, model_config, enable_search=True, documents=documents,
    )
    flags = result.get("flags") if isinstance(result, dict) else None
    if not flags:
        return {"data": {}, "confidence": "low", "status": "unavailable",
                "note": "Risk flags could not be synthesized from available data."}
    return {"data": {"flags": flags}, "confidence": result.get("confidence", "medium"),
            "_reads": _READS}  # evidence-base domains, for source attribution

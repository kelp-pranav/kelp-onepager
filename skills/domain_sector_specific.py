"""Skill 5 — Sector-Specific Domain Research (templated).

One flexible skill instantiated once per sector-specific domain identified by
Sector Research. Not a hardcoded "Pharma Pipeline skill" — it takes any Domain
spec (fields + sources already chosen by Sector Research) and researches it,
reusing the same engine as the generic domains.

Input: {"company_name"/"company_profile", "domain": Domain, "model_config"?}
Output: DomainResearchOutput (same schema as generic domains).
"""

from __future__ import annotations

from typing import Any, Dict

import config
from schemas import Domain, DomainResearchOutput
from skills import domain_base


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.SECTOR_SPECIFIC_MODEL,
) -> DomainResearchOutput:
    domain = input_data.get("domain")
    if domain is None:
        return DomainResearchOutput(
            domain_name="(unspecified sector domain)",
            warnings=["no Domain spec provided to sector-specific skill"],
        )
    if isinstance(domain, dict):
        domain = Domain.model_validate(domain)

    payload = {
        "company_name": input_data.get("company_name"),
        "company_profile": input_data.get("company_profile", {}),
        "data_fields_needed": domain.data_fields_needed,
        "recommended_sources": domain.recommended_sources,
    }
    out = await domain_base.run_domain(domain.domain_name, payload, model_config)

    # Vague-source warning (spec failure mode).
    if any(s.lower() in ("various", "unknown") for s in domain.recommended_sources):
        out.warnings.append("domain spec had vague/unknown sources — best-effort search")
    return out

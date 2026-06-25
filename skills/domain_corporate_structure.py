"""Wave 1 generic domain — Corporate Structure.

Feeds: Ownership, Shareholders, Leadership, Board, Compliance/ESG.
Persona anchor: PE/banker read ownership + leadership for deal structuring;
consultant reads governance/ESG for management credibility.
"""

from __future__ import annotations

from typing import Any, Dict

import config
from schemas import DomainResearchOutput
from skills import domain_base

DOMAIN_NAME = "Corporate Structure"


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.CORPORATE_STRUCTURE_DOMAIN_MODEL,
) -> DomainResearchOutput:
    return await domain_base.run_domain(
        DOMAIN_NAME, domain_base.with_defaults(input_data, DOMAIN_NAME), model_config
    )

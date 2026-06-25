"""Wave 1 generic domain — Geography.

Feeds: Revenue by Geography, Global Presence.
Persona anchor: credit analyst reads FX/region concentration as risk; PE/banker
read geographic diversification for growth optionality.
"""

from __future__ import annotations

from typing import Any, Dict

import config
from schemas import DomainResearchOutput
from skills import domain_base

DOMAIN_NAME = "Geography"


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.GEOGRAPHY_DOMAIN_MODEL,
) -> DomainResearchOutput:
    return await domain_base.run_domain(
        DOMAIN_NAME, domain_base.with_defaults(input_data, DOMAIN_NAME), model_config
    )

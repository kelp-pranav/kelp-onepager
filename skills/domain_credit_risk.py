"""Wave 1 generic domain — Credit & Risk.

Feeds: Credit Ratings, Risk Flags.
Persona anchor: credit analyst's core inputs — ratings + triggers set default
probability; PE/banker read risk flags for deal red lines.
"""

from __future__ import annotations

from typing import Any, Dict

import config
from schemas import DomainResearchOutput
from skills import domain_base

DOMAIN_NAME = "Credit & Risk"


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.CREDIT_RISK_DOMAIN_MODEL,
) -> DomainResearchOutput:
    return await domain_base.run_domain(
        DOMAIN_NAME, domain_base.with_defaults(input_data, DOMAIN_NAME), model_config
    )

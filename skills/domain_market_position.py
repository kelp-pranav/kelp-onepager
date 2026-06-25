"""Wave 1 generic domain — Market Position.

Feeds: Peers, Market Size, Products & Services, Channel Mix.
Persona anchor: PE/banker size the opportunity and competitive moat; consultant
reads channel mix + customer concentration for operational risk.

Market Position spans four distinct research questions (peer set vs market/TAM
sizing vs product catalogue vs go-to-market channels) that one grounded search
can't all cover — so, like Track Record, it uses field_groups to issue a focused
search per sub-topic (see skill_functions.search_grouped via domain_base).
Field strings are kept IDENTICAL to the canonical Market Position spec so
sections_catalog.select_fields still maps them to the right sections.
"""

from __future__ import annotations

from typing import Any, Dict

import config
from schemas import DomainResearchOutput
from skills import domain_base

DOMAIN_NAME = "Market Position"

FIELD_GROUPS = [
    {
        "label": "Peers",
        "focus": ("the competitive peer set — list 10-15 comparable companies spanning "
                  "incumbents, scale-comparables and newer entrants, each with revenue, "
                  "EBITDA margin and focus; do NOT stop at the first 5 obvious ones"),
        "fields": ["10-15 peers spanning incumbents, scale-comparables and new entrants — each with name, revenue, EBITDA margin, focus (do NOT stop at the first 5 obvious ones)"],
    },
    {
        "label": "Market size",
        "focus": "the addressable market(s) the company serves — 3-5 TAM/market-size figures each with CAGR and year",
        "fields": ["3-5 market sizes (TAM with CAGR)"],
    },
    {
        "label": "Products & services",
        "focus": "the company's full product / service portfolio across all business segments",
        "fields": ["full product list"],
    },
    {
        "label": "Channels & customers",
        "focus": "the company's go-to-market channels and any disclosed key customers / customer concentration",
        "fields": [
            "channel breakdown (B2B/B2C, direct/distributor)",
            "key customers if disclosed",
        ],
    },
]

_FIELDS = [f for g in FIELD_GROUPS for f in g["fields"]]


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.MARKET_POSITION_DOMAIN_MODEL,
) -> DomainResearchOutput:
    merged = domain_base.with_defaults(input_data, DOMAIN_NAME)
    payload = {
        **merged,
        "field_groups": FIELD_GROUPS,
        "data_fields_needed": _FIELDS,
    }
    return await domain_base.run_domain(DOMAIN_NAME, payload, model_config)

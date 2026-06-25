"""Wave 1 generic domain — Track Record.

Feeds: Deals & Transactions, Key Milestones, Recent News, Upcoming Catalysts.
Persona anchor: banker sources deals from M&A/funding history; PE analyst reads
catalysts for the timing of the thesis.

Track Record spans several distinct research questions (recent news vs funding
rounds vs M&A vs milestones vs catalysts) that one grounded search can't all
cover — so it uses field_groups to issue a focused search per sub-topic
(see skill_functions.search_grouped via domain_base.run_domain).
"""

from __future__ import annotations

from typing import Any, Dict

import config
from schemas import DomainResearchOutput
from skills import domain_base

DOMAIN_NAME = "Track Record"

# Each group = one focused grounded search. Field names keep the keywords that
# sections_catalog.select_fields maps to sections (news / funding / m&a /
# milestone / catalyst|upcoming).
FIELD_GROUPS = [
    {
        "label": "Recent news",
        "focus": "the 8-10 MOST RECENT dated news items / announcements (prefer the last 12-18 months, most recent first)",
        "fields": ["8-10 most recent news items — each with date, headline, source"],
    },
    {
        "label": "Funding & M&A",
        "focus": "the company's complete funding/investment round history AND any M&A / acquisitions, with dates, investors and amounts",
        "fields": [
            "funding/investment rounds — each with date, round/stage, lead investors, amount raised",
            "M&A and acquisitions — each with date, target, amount, strategic rationale",
        ],
    },
    {
        "label": "Milestones & catalysts",
        "focus": "corporate milestones from founding to present, plus upcoming catalysts / events to watch",
        "fields": [
            "corporate milestones — each with date and event, from founding to present",
            "upcoming catalysts — each with date/quarter, event, materiality",
        ],
    },
]

_FIELDS = [f for g in FIELD_GROUPS for f in g["fields"]]
_SOURCES = ["company press releases", "Google News", "VCCircle", "Tracxn", "Crunchbase",
            "Mint", "BloombergQuint", "BSE/NSE corporate announcements"]


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.TRACK_RECORD_DOMAIN_MODEL,
) -> DomainResearchOutput:
    payload = {
        **input_data,
        "field_groups": FIELD_GROUPS,
        "data_fields_needed": _FIELDS,
        "recommended_sources": _SOURCES,
    }
    return await domain_base.run_domain(DOMAIN_NAME, payload, model_config)

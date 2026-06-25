"""Wave 1 generic domain — Financials & Ratios.

Feeds: Key Stats, Financial Performance, Margin/Key Metrics, Working Capital.
Persona anchor: PE analyst values the multiple off these numbers; credit analyst
reads leverage/coverage for default probability.

Uses field_groups so the working-capital cycle (debtor/inventory/payable days,
operating cycle) gets its OWN focused grounded search instead of being buried in
an 18-field single pass — previously it consistently came back "Not Available".
Field strings are kept IDENTICAL to the canonical spec so sections_catalog still
maps them to Key Stats / Financial Performance / Margin / Working Capital.
"""

from __future__ import annotations

from typing import Any, Dict

import config
from schemas import DomainResearchOutput
from skills import domain_base

DOMAIN_NAME = "Financials & Ratios"

FIELD_GROUPS = [
    {
        "label": "P&L & scale",
        "focus": "5-year revenue / EBITDA / PAT trend and margins, with currency and units",
        "fields": ["5-year revenue", "EBITDA", "PAT", "EBITDA margin", "PAT margin",
                   "currency", "units"],
    },
    {
        "label": "Returns & leverage",
        "focus": "profitability/return ratios and the debt/leverage profile",
        "fields": ["ROE", "ROCE", "debt/equity", "interest coverage", "debt/EBITDA",
                   "debt breakdown (secured/unsecured, short/long term) — try annual-report notes / rating rationale before NA"],
    },
    {
        "label": "Working capital cycle",
        "focus": ("the working-capital cycle — current ratio and debtor/inventory/payable "
                  "days and the resulting operating cycle (from the balance sheet / ratio "
                  "analysis in the annual report or Screener.in)"),
        "fields": ["current ratio", "debtor days", "inventory days", "payable days",
                   "operating cycle"],
    },
]

_FIELDS = [f for g in FIELD_GROUPS for f in g["fields"]]


async def run(
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig" = config.FINANCIALS_DOMAIN_MODEL,
) -> DomainResearchOutput:
    merged = domain_base.with_defaults(input_data, DOMAIN_NAME)
    payload = {
        **merged,
        "field_groups": FIELD_GROUPS,
        "data_fields_needed": _FIELDS,
    }
    return await domain_base.run_domain(DOMAIN_NAME, payload, model_config)

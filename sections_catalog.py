"""Canonical catalog of generic one-pager sections + layout metadata.

Single source of truth shared by Importance Scoring (needs the section list) and
Layout Planning (needs column / content_type / feeding-domain per section). Maps
each generic section to the domain that produces its data, its default column,
and its render content_type. Sector-specific sections are NOT listed here — they
come dynamically from SectorResearchOutput.sector_domains and default to the
left column with a blue sector tag.

Derived from references/Generic_Sections_Reference.csv and the domain→section
feed mapping in CLAUDE.md / spec Skill 4.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# content_type ∈ {"table", "stat_grid", "swot_grid", "list", "chart", "prose"}
# column      ∈ {"left", "right"}
# configurable: default column may be overridden by importance in future v2.
GENERIC_SECTIONS: List[Dict] = [
    # --- Left column ---
    {"name": "Business description", "column": "left", "content_type": "prose", "fed_by_domain": "Sector Research", "configurable": False},
    {"name": "Investment thesis", "column": "left", "content_type": "prose", "fed_by_domain": "Investment Thesis", "configurable": False},
    {"name": "Key stats bar", "column": "left", "content_type": "stat_grid", "fed_by_domain": "Financials & Ratios", "configurable": False},
    {"name": "Financial performance chart", "column": "left", "content_type": "chart", "fed_by_domain": "Financials & Ratios", "configurable": False},
    {"name": "Margin trend & key metrics", "column": "left", "content_type": "chart", "fed_by_domain": "Financials & Ratios", "configurable": False},
    {"name": "Working capital analysis", "column": "left", "content_type": "chart", "fed_by_domain": "Financials & Ratios", "configurable": False},
    {"name": "Revenue by geography", "column": "left", "content_type": "chart", "fed_by_domain": "Geography", "configurable": True},
    {"name": "Revenue by product segment", "column": "left", "content_type": "chart", "fed_by_domain": "Market Position", "configurable": True},
    {"name": "Shareholders & promoters", "column": "right", "content_type": "chart", "fed_by_domain": "Corporate Structure", "configurable": False},
    {"name": "Products & services", "column": "left", "content_type": "list", "fed_by_domain": "Market Position", "configurable": True},
    {"name": "Channel mix & distribution", "column": "left", "content_type": "list", "fed_by_domain": "Market Position", "configurable": True},
    {"name": "SWOT", "column": "left", "content_type": "swot_grid", "fed_by_domain": "SWOT", "configurable": False},
    {"name": "Market size", "column": "left", "content_type": "table", "fed_by_domain": "Market Position", "configurable": False},
    {"name": "Recent news", "column": "left", "content_type": "list", "fed_by_domain": "Track Record", "configurable": False},
    # --- Right column ---
    {"name": "Details", "column": "right", "content_type": "list", "fed_by_domain": "Corporate Structure", "configurable": False},
    {"name": "Ownership", "column": "right", "content_type": "list", "fed_by_domain": "Corporate Structure", "configurable": False},
    {"name": "Deals & transactions", "column": "right", "content_type": "list", "fed_by_domain": "Track Record", "configurable": False},
    {"name": "Leadership", "column": "right", "content_type": "list", "fed_by_domain": "Corporate Structure", "configurable": False},
    {"name": "Board members", "column": "right", "content_type": "list", "fed_by_domain": "Corporate Structure", "configurable": False},
    {"name": "Credit ratings", "column": "right", "content_type": "table", "fed_by_domain": "Credit & Risk", "configurable": False},
    {"name": "Compliance & ESG", "column": "right", "content_type": "list", "fed_by_domain": "Corporate Structure", "configurable": False},
    {"name": "Risk flags", "column": "left", "content_type": "list", "fed_by_domain": "Risk Flags", "configurable": False},
    {"name": "Key catalysts", "column": "right", "content_type": "list", "fed_by_domain": "Track Record", "configurable": False},
    {"name": "Awards & certifications", "column": "right", "content_type": "list", "fed_by_domain": "Track Record", "configurable": False},
    {"name": "Key milestones", "column": "right", "content_type": "list", "fed_by_domain": "Track Record", "configurable": False},
    {"name": "Future plan", "column": "right", "content_type": "list", "fed_by_domain": "Future Plan", "configurable": False},
    {"name": "Peers comparison", "column": "left", "content_type": "table", "fed_by_domain": "Market Position", "configurable": False},
    {"name": "Global presence", "column": "right", "content_type": "list", "fed_by_domain": "Geography", "configurable": False},
]

# Canonical generic layout — the EXACT order + column for generic sections,
# followed deterministically every run (no importance reordering). Source of truth
# for generic placement. Sector-specific sections are interleaved into the LEFT
# column by importance (see layout_planning); the RIGHT sidebar is generics only.
GENERIC_LEFT_ORDER = [
    "Key stats bar",
    "Business description",
    "Products & services",
    "Investment thesis",
    "Risk flags",
    "Financial performance chart",
    "Margin trend & key metrics",
    "Working capital analysis",
    "Revenue by geography",
    "Revenue by product segment",
    "Peers comparison",
    "Channel mix & distribution",
    "SWOT",
    "Market size",
    "Recent news",
]
GENERIC_RIGHT_ORDER = [
    "Details",
    "Ownership",
    "Shareholders & promoters",
    "Leadership",
    "Board members",
    "Compliance & ESG",
    "Credit ratings",
    "Deals & transactions",
    "Key catalysts",
    "Key milestones",
    "Awards & certifications",
    "Global presence",
    "Future plan",
]

# The page-header block: no interleaved sector section may rise above these.
LEFT_TOP_ORDER = ["Key stats bar", "Business description"]
RIGHT_TOP_ORDER = []  # right column is fully fixed by GENERIC_RIGHT_ORDER

# Per-section field selection: a domain feeds several sections, so each section
# pulls only the slice of the domain's data relevant to it (the spec's "extract
# relevant subset" step). Matched by case-insensitive substring against the data
# field names. A section absent here (e.g. a sector-specific section) gets the
# whole domain payload; a mapped section with no matches falls back to the whole
# payload rather than rendering empty.
SECTION_FIELD_KEYWORDS: Dict[str, list] = {
    # Financials & Ratios → 4 sections, each a distinct slice
    "Key stats bar": ["revenue", "ebitda", "pat", "interest coverage", "rating"],
    "Financial performance chart": ["revenue", "ebitda", "pat"],
    "Margin trend & key metrics": ["margin", "roe", "roce", "debt/equity", "debt/ebitda",
                                     "interest coverage", "current ratio", "gearing"],
    "Working capital analysis": ["debtor", "inventory", "payable", "operating cycle",
                                  "working capital", "current ratio"],
    # Corporate Structure → 5 sections
    "Ownership": ["promoter", "institutional", "retail", "pledge", "paid-up",
                  "authorized", "company type", "capital"],
    "Shareholders & promoters": ["promoter", "institutional", "retail", "shareholder"],
    "Leadership": ["executive", "leadership", "ceo", "managing director", "management"],
    "Board members": ["board"],
    "Compliance & ESG": ["auditor", "litigation", "rpt", "related party", "contingent",
                          "esg", "compliance"],
    "Details": ["company type", "auditor", "paid-up", "authorized"],
    # Market Position → 4 sections
    "Peers comparison": ["peer"],
    "Market size": ["market size", "tam", "cagr", "market"],
    "Products & services": ["product"],
    "Channel mix & distribution": ["channel", "customer", "distribut", "b2b", "b2c"],
    # Track Record → 4 sections
    "Deals & transactions": ["m&a", "funding", "deal", "transaction", "acquisition"],
    "Key milestones": ["milestone", "incorporation", "restructuring"],
    "Recent news": ["news"],
    "Key catalysts": ["catalyst", "upcoming"],
    # Credit & Risk → 2 sections
    "Credit ratings": ["rating", "trigger"],
    "Risk flags": ["risk"],
    # Geography → 2 sections
    "Revenue by geography": ["region", "geograph", "export", "revenue split", "revenue by"],
    "Global presence": ["country", "global", "presence", "approval", "certif"],
}


def select_fields(section_name: str, data: Dict) -> Dict:
    """Return the slice of a domain's data dict relevant to one section.

    Substring match on field names. An UNMAPPED section (e.g. a sector-specific
    section) gets the whole payload. A MAPPED section with no matching fields
    returns {} — the data genuinely isn't there for that section, so it should be
    marked unavailable rather than re-dumping the whole domain.
    """
    keywords = SECTION_FIELD_KEYWORDS.get(section_name)
    if not keywords:
        return dict(data)
    return {f: v for f, v in data.items()
            if any(k in f.lower() for k in keywords)}

_BY_NAME: Dict[str, Dict] = {s["name"]: s for s in GENERIC_SECTIONS}


def generic_section_names() -> List[str]:
    return [s["name"] for s in GENERIC_SECTIONS]


def is_generic(section_name: str) -> bool:
    return section_name in _BY_NAME


def get_meta(section_name: str) -> Optional[Dict]:
    return _BY_NAME.get(section_name)


def sector_label(resolved_sector: Optional[str]) -> str:
    """Pretty blue-pill label for a sector tag, e.g. 'pharma' -> 'Pharma'."""
    if not resolved_sector or resolved_sector == "other":
        return "Sector"
    return resolved_sector.replace("_", " ").title()

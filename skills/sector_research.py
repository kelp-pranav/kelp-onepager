"""Skill 1 — Sector Research (Phase 1 of the pipeline).

The only skill that determines pipeline shape. Given a company name, sector
hint, and business description it: resolves a precise subsector, loads the
matching sector taxonomy as a baseline, customizes the sector-specific domains
against the company, runs a persona-relevance pass to cut weak domains, and
attaches the 6 canonical generic domains.

Get this wrong and every downstream skill researches the wrong things — hence
the highest model tier and the persona discipline baked into the prompt.

Public entry point:  async def run(input_data, model_config) -> SectorResearchOutput
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import sections_catalog as catalog
import skill_functions as sf
from schemas import Domain, PersonaRelevance, PipelineInput, SectorResearchOutput

# The 6 generic domains are described to the model verbatim so it never proposes
# a sector-specific domain that duplicates them (spec Skill 1, step 4).
GENERIC_DOMAINS_ANTIDUP = (
    "Do NOT propose any domain already covered by the 6 GENERIC domains:\n"
    "1. Financials & Ratios — revenue, EBITDA, PAT, margins, ratios, working capital\n"
    "2. Corporate Structure — ownership, leadership, board, compliance, ESG\n"
    "3. Market Position — peers, market size, products, channel mix\n"
    "4. Track Record — deals, milestones, news, catalysts\n"
    "5. Credit & Risk — ratings, risk flags\n"
    "6. Geography — revenue by region, global presence\n"
    "Only propose domains that capture SECTOR-SPECIFIC data none of the above hold."
)


def _antidup_block() -> str:
    """Anti-duplication instruction + the FULL list of generic SECTION names the
    one-pager already produces, so the model can avoid restating any of them
    (by name OR by content). The generic sections are the non-negotiable cause of
    the overlap problem — listing them is what lets the model steer clear."""
    generic_sections = ", ".join(f'"{n}"' for n in catalog.generic_section_names())
    return (
        GENERIC_DOMAINS_ANTIDUP
        + "\n\nThe one-pager ALREADY renders these GENERIC sections for EVERY "
        "company — your sector sections must NOT duplicate any of them, by name "
        "or by content:\n"
        f"{generic_sections}\n\n"
        "ALWAYS-GENERIC THEMES — the generics own the COMPANY-LEVEL / CONSOLIDATED "
        "view of each. NEVER propose a sector section that merely restates the "
        "consolidated view. BUT a SEGMENT-LEVEL breakdown or an ALTERNATIVE headline "
        "metric the consolidated generic does NOT track IS a valid, distinct sector "
        "section (see CARVE-OUT below):\n"
        "  • CONSOLIDATED revenue/volume by product, segment, category or 'product "
        "mix' (a simple A-vs-B revenue split) -> generic Revenue by product segment "
        "/ Products & services. [Carve-out: per-segment NOV/GTV, per-segment EBITDA / "
        "contribution margin / profitability inflection, or an alternative gross "
        "metric like NOV/GTV vs recognized revenue or take-rate are NOT shown there.]\n"
        "  • revenue by geography/region, regulated-vs-emerging split, exports, "
        "global footprint -> generic Revenue by geography / Global presence\n"
        "  • customer concentration, top-customer %, channel or distribution mix "
        "-> generic Channel mix & distribution\n"
        "  • ESG, environmental, sustainability, green chemistry, safety, "
        "emissions, water/waste, litigation, RPT, auditor, compliance -> generic "
        "Compliance & ESG\n"
        "  • ownership, cap-table, shareholding, promoter %, pledge, funding "
        "rounds -> generic Ownership / Shareholders & promoters\n"
        "  • leadership, management, board -> generic Leadership / Board members\n"
        "  • CONSOLIDATED margins, ratios, working capital, cost structure, capex "
        "funding/debt-equity -> generic Financials sections. [Carve-out: SEGMENT-LEVEL "
        "unit economics / per-vertical profitability are NOT in the consolidated "
        "Financials and ARE valid.]\n"
        "  • credit ratings -> Credit ratings; peers -> Peers comparison; market "
        "size/TAM -> Market size; M&A/deals -> Deals & transactions; news -> "
        "Recent news; milestones -> Key milestones; expansion plans / future "
        "targets / upcoming catalysts -> Key catalysts / Future plan; awards -> "
        "Awards & certifications\n\n"
        "CARVE-OUT (do NOT drop these): the generics are COMPANY-LEVEL / CONSOLIDATED. "
        "A sector section is genuinely distinct — KEEP it — when its core data is one of:\n"
        "  (a) a SEGMENT-LEVEL cut the consolidated generic can't show (per-vertical "
        "NOV/GTV, per-segment EBITDA / contribution margin / profitability inflection);\n"
        "  (b) an ALTERNATIVE headline metric the generics don't track (NOV/GTV vs "
        "recognized revenue, take-rate, MTU / transacting users, gig-partner scale);\n"
        "  (c) a STRUCTURAL INSIGHT with no generic home (an accounting/revenue-"
        "recognition change that breaks YoY comparability; idiosyncratic seasonality "
        "/ lumpiness; operational-depth metrics like network/store density or "
        "SKU-assortment depth by city tier).\n\n"
        "SELF-CHECK before including ANY sector section: ask 'could one of the generic "
        "sections already display this for THIS company AT THE COMPANY LEVEL?' If it "
        "would merely restate the consolidated generic — DROP it. If it is a "
        "segment-level cut, an alternative metric, or a structural insight per the "
        "CARVE-OUT above — KEEP it. Examples of genuinely-distinct cores: pharma "
        "(regulatory inspection/483 history, controlled-substance licences, DMF/CEP "
        "filings, fermentation capacity); bank (asset quality/NPA, capital adequacy/"
        "CRAR, NIM); platform (segment NOV/GTV, per-segment profitability, "
        "recognition-model shift). When unsure about a TRUE consolidated overlap, drop "
        "it; but never drop a real segment-level / alternative-metric / structural-"
        "insight section just because it mentions revenue, margin or segment."
    )

# Canonical definitions of the 6 generic domains (spec Skill 4, 4a-4f). These are
# the same for every company; downstream Wave 1 generic domain skills reuse them.
GENERIC_DOMAINS: List[Dict[str, Any]] = [
    {
        "domain_name": "Financials & Ratios",
        "sections_covered": ["Key stats bar", "Financial performance chart", "Margin trend & key metrics", "Working capital analysis"],
        "data_fields_needed": ["5-year revenue", "EBITDA", "PAT", "EBITDA margin", "PAT margin", "ROE", "ROCE", "debt/equity", "interest coverage", "debt/EBITDA", "current ratio", "debtor days", "inventory days", "payable days", "operating cycle", "debt breakdown (secured/unsecured, short/long term) — try annual-report notes / rating rationale before NA", "currency", "units"],
        "recommended_sources": ["CARE/CRISIL/ICRA reports", "BSE/NSE filings", "MCA annual returns", "company investor presentations", "Screener.in", "annual report notes to accounts"],
    },
    {
        "domain_name": "Corporate Structure",
        "sections_covered": ["Ownership", "Shareholders & promoters", "Leadership", "Board members", "Compliance & ESG"],
        "data_fields_needed": ["company type (listed/unlisted/PE-backed)", "promoter %", "institutional %", "retail %", "pledge %", "paid-up capital", "authorized capital", "individual promoters", "board members + roles", "executives + backgrounds", "auditor name", "litigation status", "RPT flags", "contingent liabilities", "ESG initiatives"],
        "recommended_sources": ["MCA filings", "Tofler/Tracxn", "BSE/NSE shareholding patterns", "Whalesbook"],
    },
    {
        "domain_name": "Market Position",
        "sections_covered": ["Peers comparison", "Market size", "Products & services", "Channel mix & distribution"],
        "data_fields_needed": ["10-15 peers spanning incumbents, scale-comparables and new entrants — each with name, revenue, EBITDA margin, focus (do NOT stop at the first 5 obvious ones)", "3-5 market sizes (TAM with CAGR)", "full product list", "channel breakdown (B2B/B2C, direct/distributor)", "key customers if disclosed"],
        "recommended_sources": ["industry reports (IBEF, Nielsen, IQVIA)", "company website", "broker reports", "Screener.in / exchange filings (for peer financials)", "Tracxn (for new entrants)"],
    },
    {
        "domain_name": "Track Record",
        "sections_covered": ["Deals & transactions", "Key milestones", "Recent news", "Key catalysts"],
        # NOTE: domain_track_record.py runs this domain with field_groups (one
        # focused grounded search per sub-topic). These granular fields mirror that.
        "data_fields_needed": ["8-10 most recent news items — each with date, headline, source",
                               "funding/investment rounds — each with date, round/stage, lead investors, amount raised",
                               "M&A and acquisitions — each with date, target, amount, strategic rationale",
                               "corporate milestones — each with date and event, from founding to present",
                               "upcoming catalysts — each with date/quarter, event, materiality"],
        "recommended_sources": ["company press releases", "Google News", "VCCircle", "Tracxn",
                                "Crunchbase", "Mint", "BloombergQuint", "BSE/NSE corporate announcements"],
    },
    {
        # NOTE: "Risk flags" is no longer a factual field here — it is an
        # interpretive section synthesized from ALL Wave 1 data + a fresh web
        # search by skills/synthesis_risk_flags.py. Credit & Risk still supplies
        # the rating facts + triggers that feed that synthesis.
        "domain_name": "Credit & Risk",
        "sections_covered": ["Credit ratings"],
        "data_fields_needed": ["current LT and ST credit ratings + agency + outlook (check ALL of CARE, CRISIL, ICRA, India Ratings before NA)", "4-year rating history", "key positive/negative triggers"],
        "recommended_sources": ["CARE Ratings", "CRISIL", "ICRA", "India Ratings", "Acuité", "rating rationale PDFs"],
    },
    {
        "domain_name": "Geography",
        "sections_covered": ["Revenue by geography", "Global presence"],
        "data_fields_needed": ["revenue split by region with % (and growth if available)", "country count", "key regional approvals/certifications", "regional revenue trend"],
        "recommended_sources": ["company annual reports", "investor presentations", "segment disclosures"],
    },
]

_TAXONOMY_PATH = os.path.join(config.REFERENCE_DIR, "sector_taxonomies.json")


def _load_taxonomies() -> Dict[str, Any]:
    try:
        with open(_TAXONOMY_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _match_taxonomy(sector_hint: Optional[str], taxonomies: Dict[str, Any]) -> Optional[str]:
    """Return the taxonomy key matching a sector hint, or None.

    Two passes so a short hint can't be hijacked by another sector's alias:
      1. EXACT key/alias match across ALL taxonomies first — so 'tech' resolves to
         the tech taxonomy via its exact alias, not to pharma via the substring of
         'biotech'.
      2. WORD-BOUNDARY containment fallback (either direction) — so 'tech' never
         matches 'biotech', but 'specialty chemicals' still matches manufacturing
         via the whole word 'chemicals'.
    """
    if not sector_hint:
        return None
    hint = sector_hint.strip().lower()

    # Pass 1 — exact key or alias match (substring can't hijack a short hint).
    for key, entry in taxonomies.items():
        if key.startswith("_"):
            continue
        if key == hint or hint in [a.lower() for a in entry.get("aliases", [])]:
            return key

    # Pass 2 — whole-word containment either direction (no arbitrary substrings).
    for key, entry in taxonomies.items():
        if key.startswith("_"):
            continue
        for a in entry.get("aliases", []):
            al = a.lower()
            if (re.search(rf"\b{re.escape(hint)}\b", al)
                    or re.search(rf"\b{re.escape(al)}\b", hint)):
                return key
    return None


def _build_generic_domains() -> List[Domain]:
    domains: List[Domain] = []
    for d in GENERIC_DOMAINS:
        domains.append(
            Domain(
                domain_name=d["domain_name"],
                priority_hint="high",
                sections_covered=list(d["sections_covered"]),
                data_fields_needed=list(d["data_fields_needed"]),
                recommended_sources=list(d["recommended_sources"]),
                is_sector_specific=False,
                persona_relevance=PersonaRelevance(overall_score=80),
            )
        )
    return domains


def _system_prompt() -> str:
    return (
        "You are a senior equity/credit research lead building the research plan for a "
        "professional company SOURCING one-pager — a top-of-funnel screening doc an "
        "analyst reads to decide whether this company is worth DEEPER research (pursue "
        "vs pass), NOT to make a final investment/lending/deal decision. Your four "
        "readers each make that screen-in / screen-out call: PE analyst (worth "
        "diligencing as an investment?), investment banker (worth pitching / a live "
        "deal angle?), credit analyst (worth a closer underwriting look / any obvious "
        "red flags?), and consultant (an operational angle worth pursuing?). Favour the "
        "signals that drive that triage — scale, momentum, distinctiveness, obvious "
        "dealbreakers — over exhaustive diligence detail.\n\n"
        "Your job: (1) resolve the company's PRECISE subsector — 'pharma' is too coarse; "
        "say e.g. 'controlled-substance API manufacturer with biofermentation platform' vs "
        "'Indian branded formulation player'. (2) Capture a brief company_profile. "
        "(3) Propose 3-5 SECTOR-SPECIFIC research domains tailored to THIS company, yielding "
        "roughly 5-8 sector SECTIONS IN TOTAL — sharp and genuinely distinctive, NOT 15. "
        "Be ruthless: a great one-pager has a handful of high-signal sector sections, not a "
        "long thin list. Do NOT split one theme into several near-identical sections (e.g. an "
        "expansion is ONE section — 'Capacity Expansion' — not separate 'status' + 'funding' + "
        "'impact' sections; compliance is ONE section, not 'environmental' + 'safety' + 'green "
        "chemistry'). Every section must pass the anti-duplication SELF-CHECK below. For EACH "
        "domain list 5-10 SPECIFIC, QUANTITATIVE data_fields an analyst would actually pull "
        "(e.g. store-level EBITDA, capex per store, AR/try-on adoption %, tele-consult volumes, "
        "franchise payback months, backward-integration %, cap-table/funding-round detail, "
        "DRHP/IPO status) — NOT generic placeholders. Give each NAMED recommended_sources (e.g. "
        "'USFDA Warning Letter database', 'CARE Ratings', 'RBI filings', 'Euromonitor' — never "
        "'various reports'); if you cannot name a real source use [\"unknown\"].\n"
        "JURISDICTION: name fields, regulators and sources for the company's ACTUAL country "
        "(stated in 'Resolved jurisdiction' below). For an Indian company use CDSCO / Narcotics "
        "Commissioner / Schedule-1&2 / CRISIL-ICRA-CARE / SEBI / MCA — NOT US 'DEA'/'USFDA' or "
        "EU bodies unless the company actually files there. Mismatched-jurisdiction labels make "
        "real data look 'Not Available'.\n"
        "PUBLIC-DISCLOSURE TEST: choose fields companies in this sector ACTUALLY disclose "
        "(filings, earnings, rating reports, regulator databases, the prospectus). AVOID "
        "internal/confidential metrics no public source reports — production quotas, plant "
        "utilisation %, single-source-dependency counts, per-customer economics, R&D-as-%-of-"
        "revenue for small firms — they come back empty or invite fabrication. A field with no "
        "plausible public source is worse than omitting it.\n"
        "DISCLOSED GRANULARITY: ask for the cut the company REPORTS, not a finer one it doesn't. "
        "If a precise breakdown isn't public — per-segment/per-market growth, an exact 'number of "
        "X' count, rate-by-region — request the AGGREGATE it does disclose instead (company-level "
        "growth, the revenue split it publishes, a described list of named wins) rather than a "
        "per-cell number that returns 'Not Available'. The data still gets captured — just at the "
        "granularity that actually exists.\n\n"
        "For EACH section in sections_covered, also classify its expected data SHAPE as one of: "
        "stat_grid (a handful of headline numbers), table (multi-row comparable data — multiple "
        "items sharing the same columns), chart (a trend over time), list (qualitative bullets, "
        "not strictly tabular). Do NOT use stat_grid for anything but 3-6 headline numbers. Do NOT "
        "propose swot_grid (reserved for the generic SWOT section) or prerendered_html (reserved "
        "for pre-built modules) — those are not valid choices here. Default to 'table' only when "
        "the data is genuinely multi-row; a handful of single-value facts is 'list', not 'table'.\n\n"
        "COVERAGE CHECKLIST — consider these themes ONLY WHERE they yield data the generic "
        "sections (listed below) do NOT already hold, and skip any that the generics already "
        "cover for this company: technology/digital/D2C; operations/supply-chain/manufacturing; "
        "value-added or tech-enabled services; brand/IP/licensing; customer acquisition & "
        "retention economics. (Product mix, distribution/channel, ownership/cap-table, ESG, and "
        "expansion plans / future targets / capex are ALREADY generic — do not re-propose them "
        "as sector sections unless you have genuinely sector-distinctive data the generics "
        "can't show.)\n\n"
        "ANALYST-INSIGHT SECTIONS (do NOT stop at standard operational buckets): the buckets "
        "above are table stakes. Beyond them, proactively identify this company's 2-3 "
        "IDIOSYNCRATIC, decision-relevant structural or economic stories and propose a section "
        "for each. These are what separate an analyst's one-pager from a data dump, and they are "
        "exactly what a generic template can never surface. Hunt specifically for:\n"
        "  • an accounting / revenue-recognition change that makes YoY numbers NON-comparable "
        "(e.g. marketplace -> inventory-led, gross vs net booking) — flag it as its own section;\n"
        "  • SEGMENT-LEVEL economics where the company runs multiple businesses (per-segment "
        "NOV/GTV, per-segment EBITDA / contribution margin, a profitability inflection in one "
        "segment) — the consolidated generics hide these;\n"
        "  • an ALTERNATIVE headline metric the company itself leads with that the generics "
        "don't track (NOV/GTV, take-rate, MTU / transacting users, gig-partner scale);\n"
        "  • a STRUCTURAL pattern unique to the business model (idiosyncratic seasonality / "
        "lumpiness; network or store density; SKU-assortment depth by geography/tier).\n"
        "If the company genuinely has none of these, do not invent them — but for any "
        "multi-segment, platform, or recently-restructured company, at least one almost always "
        "applies, and missing it is the #1 way this plan underperforms a human analyst.\n\n"
        + _antidup_block()
        + "\n\nA sharper, more specific SECTOR view of a theme is allowed ONLY when it adds "
        "data the generic sections cannot show; if it would mostly restate a generic section, "
        "drop it. Never merely restate the generic domains.\n\n"
        "Apply the persona-relevance test ruthlessly: every domain must change at least one "
        "persona's pursue-vs-pass (deep-research-or-not) decision. Vague 'good context' "
        "domains are cut.\n\n"
        "WORKED EXAMPLE (pharma — a controlled-substance API maker):\n"
        '  resolved_subsector: "controlled-substance API & intermediates manufacturer with '
        'biofermentation platform"\n'
        "  KEEP — sector sections whose CORE data has NO generic home:\n"
        "   - Regulatory Inspection History (export-market inspections it actually faces — e.g. "
        "USFDA Form 483s / Warning Letters / Import Alerts for a US-exporting API maker, plus its "
        "home regulator; sources: the relevant regulator databases) — credit analyst: one Import "
        "Alert can erase 30% of revenue.\n"
        "   - Controlled-Substance Licences & DMF/CEP Filings (the company's OWN-jurisdiction "
        "controlled-substance manufacturing licences — e.g. India's Narcotics Commissioner "
        "Schedule-1/2 for an Indian maker, or US DEA for a US maker — plus US/EU/JP DMF & CEP "
        "counts; sources: company disclosures, EDQM CEP database, the home narcotics regulator) — "
        "PE analyst: regulatory moat + market access.\n"
        "   - Fermentation & Biotransformation Capacity (fermenter capacity KL, platform types, "
        "backward-integration %; sources: company facility disclosures) — consultant: operational "
        "scalability. [note: omit plant-utilisation % — not publicly disclosed.]\n"
        "  REJECT — the generic sections already own these, so do NOT propose them (this is the "
        "self-check in action):\n"
        '   - "Product-wise revenue mix / controlled-vs-commodity split" -> generic Revenue by '
        "product segment.\n"
        '   - "Regulated vs emerging geographic revenue split" -> generic Revenue by geography.\n'
        '   - "Top-customer concentration" -> generic Channel mix & distribution.\n'
        '   - "Capacity expansion / capex plan" -> generic Future plan / Key catalysts.\n\n'
        "WORKED EXAMPLE (multi-segment consumer-internet platform — e.g. food delivery + "
        "quick-commerce + going-out):\n"
        '  resolved_subsector: "multi-segment consumer-internet platform (food delivery + '
        'quick-commerce + going-out)"\n'
        "  KEEP — analyst-insight / segment-level / alternative-metric sections with NO generic "
        "home (this is the CARVE-OUT in action):\n"
        "   - Segment-wise NOV/GTV (per-segment gross order value + growth; sources: quarterly "
        "results/investor deck) — NOV is gross transaction value, NOT recognized revenue, so the "
        "generic Revenue sections never show it; PE analyst: the real demand signal per business.\n"
        "   - Per-Segment Profitability & Inflection (segment-level adjusted EBITDA, e.g. a "
        "quick-commerce arm turning EBITDA-positive for the first time) — consolidated Financials "
        "hides this; credit/PE: the path-to-profit per vertical.\n"
        "   - Revenue-Recognition Model Shift (marketplace -> inventory-led; why YoY revenue is "
        "NOT like-for-like) — a structural/accounting insight no generic chart can surface; all "
        "personas: stops misreading the headline growth rate.\n"
        "   - Quick-Commerce Network Density & Assortment Depth (dark-store count/density, "
        "SKU count by city tier) — operational-depth metric; consultant: scalability + moat.\n"
        "   - Structural Seasonality of the Going-Out Segment (calendar-driven lumpiness: "
        "events/IPL/movie-release dependence) — a structural pattern, not a risk-flag footnote.\n"
        "  REJECT — true CONSOLIDATED restatements the generics already own:\n"
        '   - "Total revenue / EBITDA / PAT trend" -> generic Financials sections.\n'
        '   - "Company-level revenue by segment (simple split)" -> generic Revenue by product '
        "segment (BUT per-segment NOV/GTV or per-segment profitability is KEPT above).\n"
        '   - "Promoter/institutional shareholding" -> generic Ownership.\n\n'
        "Return STRICT JSON only."
    )


# --------------------------------------------------------------------------- #
# Pass 1 — grounded CURRENT-STATE research (text, search ON). Planning (pass 2)  #
# reads this instead of the model's training memory, so it stops proposing      #
# metrics from a company's HISTORICAL identity (e.g. Meesho's defunct reseller   #
# KPIs) and reflects what the company actually is/reports today.                #
# --------------------------------------------------------------------------- #
_RESEARCH_SYSTEM = (
    "You are a research analyst scoping a company before building its one-pager. "
    "Use web search to establish what the company is RIGHT NOW — not its historical "
    "reputation. Prefer the most recent primary sources (latest annual report / "
    "DRHP-RHP / investor deck / recent filings & news). Report only what you can "
    "verify; never invent. Be concrete and current."
)


def _research_prompt(inp: PipelineInput) -> str:
    today = datetime.now().strftime("%d %b %Y")
    hint = inp.sector or inp.sector_override or ""
    return (
        f"Company: {inp.company_name}"
        + (f" (sector hint: {hint})" if hint else "")
        + f"\nTODAY is {today}. Research this company's CURRENT state and report:\n"
        "1. Precise subsector and what the company actually does TODAY + how it makes "
        "money now (current revenue model).\n"
        "2. Any MAJOR PIVOT from what it was historically known for — state the old "
        "model, the new model, and roughly when it changed (e.g. 'shifted from "
        "reseller/social-commerce to direct D2C marketplace ~2020'). If no pivot, say so.\n"
        "3. The MOST RECENT operating metrics/KPIs the company itself reports today "
        "(from the latest FY / DRHP / recent disclosures) that an analyst would track "
        "for THIS subsector — with the period/date. Flag any once-headline metric that "
        "it has STOPPED reporting.\n"
        "4. The data sources that actually disclose these (named).\n"
        "Write a tight factual briefing — no need for JSON."
    )


def _user_prompt(inp: PipelineInput, taxonomy_key: Optional[str],
                 baseline: Optional[Dict[str, Any]], findings: str = "") -> str:
    parts = [
        f"Company: {inp.company_name}",
        f"Sector hint: {inp.sector or inp.sector_override or '(infer it)'}",
        f"Business description HINT (enrich into a researched, specific 2-3 sentence "
        f"description from web search — do NOT copy this verbatim): "
        f"{inp.business_description or '(none given — research it)'}",
    ]
    if findings and findings.strip():
        parts.append(
            "\nCURRENT-STATE RESEARCH (authoritative — base your plan on THIS, not on "
            "the company's historical reputation). If it shows the company has pivoted "
            "away from a model it was once known for, do NOT propose sections/metrics "
            "from the OLD model unless they are STILL reported today:\n"
            f"{findings.strip()}"
        )
    parts.append(
        "\nResolved jurisdiction: FIRST infer the company's home country/jurisdiction from the "
        "research above, then apply the JURISDICTION rule to EVERY field, regulator and source "
        "label you generate (use the company's own regulators, not US/EU bodies it doesn't file "
        "with). Echo the country you inferred in company_profile.country."
    )
    if baseline:
        parts.append(
            f"\nBaseline sector taxonomy matched ('{taxonomy_key}') — use as a STARTING POINT, "
            "then add/remove/reweight for this specific company:\n"
            + json.dumps(baseline.get("baseline_domains", []), indent=1)
        )
    parts.append(
        "\nReturn JSON with this exact shape:\n"
        "{\n"
        '  "resolved_subsector": str,\n'
        '  "resolved_sector": str,   // one of: pharma, banking, tech, consumer, manufacturing, real_estate, energy, or other\n'
        '  "company_profile": {"founded": str|null, "listed_status": str, "country": str, "ticker": str|null, "hq": str|null, "employees": str|null, "cin": str|null, '
        '"business_description": "RESEARCHED, web-grounded 2-3 sentence description — what the company '
        'does + revenue model + key differentiator, with specifics (founding, geography, scale). '
        'Must NOT be a verbatim echo of the input hint."},\n'
        '  "sector_domains": [\n'
        '    {"domain_name": str, "priority_hint": "high"|"medium"|"low", "sections_covered": [str], '
        '"section_content_types": {sectionName: "stat_grid"|"table"|"chart"|"list"|"prose"}, '
        '"data_fields_needed": [str], "recommended_sources": [str]}\n'
        "  ]\n"
        "}"
    )
    return "\n".join(parts)


async def run_with_rejected(
    input_data: PipelineInput,
    model_config: "config.ModelConfig" = config.SECTOR_RESEARCH_MODEL,
    documents: str = "",
) -> Tuple[SectorResearchOutput, List[Dict[str, Any]]]:
    """Execute Sector Research and ALSO return the rejected sector-domain
    candidates (persona-relevance < 30) with their reasoning.

    Returns ``(SectorResearchOutput, rejected)`` where ``rejected`` is a list of
    ``{"domain_name", "sections_covered", "persona_relevance", "rejected_reason"}``.
    ``run()`` wraps this and drops the second value, so there is one source of truth.

    ``documents`` is authoritative local-document text (ground truth) used to
    resolve the subsector / company profile when available.
    """
    taxonomies = _load_taxonomies()
    sector_hint = input_data.sector_override or input_data.sector
    taxonomy_key = _match_taxonomy(sector_hint, taxonomies)
    baseline = taxonomies.get(taxonomy_key) if taxonomy_key else None

    # PASS 1 — grounded CURRENT-STATE research (search ON, free-form text). Forcing
    # JSON + search in one call suppresses real web search, so we research first,
    # then plan. This is what stops the planner proposing a company's HISTORICAL
    # metrics (e.g. Meesho's defunct reseller KPIs) instead of its current ones.
    findings = ""
    if not documents:  # local ground-truth docs already give the current state
        research = await sf.call_model(
            prompt=_research_prompt(input_data),
            system=_RESEARCH_SYSTEM,
            model_config=model_config,
            enable_search=True,
            response_format="text",
        )
        findings = research.text or ""

    # PASS 2 — JSON planning, search OFF (the research above is the grounding),
    # fed the current-state findings so the plan reflects TODAY's company.
    planning_config = dataclasses.replace(model_config, enable_search=False)
    resp = await sf.call_model(
        prompt=_user_prompt(input_data, taxonomy_key, baseline, findings)
        + sf._documents_block(documents),
        system=_system_prompt(),
        model_config=planning_config,
        enable_search=False,
        response_format="json",
    )

    data = resp.parsed if isinstance(resp.parsed, dict) else {}
    resolved_subsector = data.get("resolved_subsector") or (sector_hint or "unknown subsector")
    resolved_sector = data.get("resolved_sector") or taxonomy_key or "other"
    company_profile = data.get("company_profile") if isinstance(data.get("company_profile"), dict) else {}
    company_profile.setdefault("resolved_sector", resolved_sector)

    raw_sector_domains = data.get("sector_domains") or []
    if not isinstance(raw_sector_domains, list):
        raw_sector_domains = []

    # Build candidate Domain objects. Pool is wider than the primary cap so the
    # lower-ranked-but-distinct proposals survive as a reserve pool (researched
    # only on demand by the post-data swap phase) rather than being discarded.
    candidates: List[Domain] = []
    pool_cap = max(config.SECTOR_PRIMARY_MAX + config.SECTOR_RESERVE_MAX, 8)
    for d in raw_sector_domains[:pool_cap]:
        if not isinstance(d, dict) or not d.get("domain_name"):
            continue
        sources = d.get("recommended_sources") or ["unknown"]
        raw_ctypes = d.get("section_content_types") if isinstance(d.get("section_content_types"), dict) else {}
        valid = {"table", "stat_grid", "list", "chart", "prose"}
        section_content_types = {k: v for k, v in raw_ctypes.items() if v in valid}
        candidates.append(
            Domain(
                domain_name=str(d["domain_name"]),
                priority_hint=d.get("priority_hint") if d.get("priority_hint") in ("high", "medium", "low") else "medium",
                sections_covered=list(d.get("sections_covered") or []),
                section_content_types=section_content_types,
                data_fields_needed=list(d.get("data_fields_needed") or []),
                recommended_sources=list(sources),
                is_sector_specific=True,
            )
        )

    # Persona-relevance pass (spec step 5) — run concurrently, cut domains < 30
    async def _score(domain: Domain) -> PersonaRelevance:
        return await sf.evaluate_persona_relevance(
            item_description=f"Research domain '{domain.domain_name}' for {input_data.company_name} "
            f"({resolved_subsector}). Sections fed: {', '.join(domain.sections_covered) or 'n/a'}.",
            item_data_summary="Data fields: " + (", ".join(domain.data_fields_needed) or "n/a"),
        )

    rejected: List[Dict[str, Any]] = []
    if candidates:
        relevances = await asyncio.gather(*[_score(c) for c in candidates], return_exceptions=True)
        kept: List[Domain] = []
        for cand, rel in zip(candidates, relevances):
            if isinstance(rel, PersonaRelevance):
                cand.persona_relevance = rel
                if rel.overall_score >= 30:
                    kept.append(cand)
                else:
                    # Below the persona-relevance bar — record it (with reasoning) so
                    # callers can show "considered but not chosen".
                    rejected.append({
                        "domain_name": cand.domain_name,
                        "sections_covered": list(cand.sections_covered),
                        "persona_relevance": rel,
                        "rejected_reason": (
                            f"Persona-relevance score {rel.overall_score}/100 — "
                            "no persona's decision changes on this."
                        ),
                    })
            else:
                # scoring failed — keep the domain rather than silently dropping it
                cand.persona_relevance = PersonaRelevance(overall_score=50)
                kept.append(cand)
    else:
        kept = []

    # Rank by persona score (desc), then priority hint, then original order, and
    # split: the top SECTOR_PRIMARY_MAX are PRIMARY (researched in Wave 1); the
    # rest become a RESERVE pool (researched only on demand by the swap phase).
    _prio = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(
        enumerate(kept),
        key=lambda iv: (-iv[1].persona_relevance.overall_score,
                        _prio.get(iv[1].priority_hint, 1), iv[0]),
    )
    ranked_domains = [d for _, d in ranked]
    sector_domains = ranked_domains[:config.SECTOR_PRIMARY_MAX]

    # Reserves must add genuinely new sections — drop any whose sections are
    # wholly already covered by the promoted primary set (no near-clones).
    primary_sections = {s for d in sector_domains for s in d.sections_covered}
    reserve_domains: List[Domain] = []
    for d in ranked_domains[config.SECTOR_PRIMARY_MAX:]:
        if len(reserve_domains) >= config.SECTOR_RESERVE_MAX:
            break
        if set(d.sections_covered) - primary_sections:
            reserve_domains.append(d)

    generic_domains = _build_generic_domains()
    total_sections = sum(len(d.sections_covered) for d in generic_domains + sector_domains)

    return (
        SectorResearchOutput(
            resolved_subsector=resolved_subsector,
            company_profile=company_profile,
            generic_domains=generic_domains,
            sector_domains=sector_domains,
            reserve_domains=reserve_domains,
            total_estimated_sections=total_sections,
        ),
        rejected,
    )


async def run(
    input_data: PipelineInput,
    model_config: "config.ModelConfig" = config.SECTOR_RESEARCH_MODEL,
    documents: str = "",
) -> SectorResearchOutput:
    """Execute Sector Research. Returns a validated SectorResearchOutput.

    Thin wrapper over ``run_with_rejected`` (the single source of truth) that
    drops the rejected-candidates list, preserving the existing pipeline contract.

    ``documents`` is authoritative local-document text (ground truth) used to
    resolve the subsector / company profile when available.
    """
    result, _rejected = await run_with_rejected(input_data, model_config, documents)
    return result

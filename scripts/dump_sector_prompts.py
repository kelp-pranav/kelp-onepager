"""Print every prompt involved in (A) choosing the sector sections and (B)
researching the data for them, in execution order, with the dynamic parts
rendered. No API calls — it only builds the prompt strings.

    python scripts/dump_sector_prompts.py            # defaults to Zomato / tech
    python scripts/dump_sector_prompts.py "Embio Limited" pharma

Order shown:
  --- A. SECTOR SELECTION (Phase 1: which sector sections exist) ---
  PASS 1  grounded current-state research  (system + user)
  PASS 2  JSON planning                    (system + user)
  PASS 3  persona-relevance scoring        (system + user, one per candidate domain)
  --- B. DATA RESEARCH (Wave 1: the data for each chosen sector section) ---
  PASS 4  grounded data research           (system + user)
  PASS 5  JSON extraction                  (system + user)
  PASS 6  gap-fill re-research             (user variant, when fields are missing)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skill_functions as sf  # noqa: E402
from schemas import PipelineInput  # noqa: E402
from skills import sector_research as sr  # noqa: E402


def _rule(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def main() -> None:
    args = sys.argv[1:]
    company = args[0] if args else "Zomato"
    sector = args[1] if len(args) > 1 else "tech"
    inp = PipelineInput(company_name=company, sector=sector)

    # Reproduce what run() computes before building the planning prompt.
    taxonomies = sr._load_taxonomies()
    sector_hint = inp.sector_override or inp.sector
    taxonomy_key = sr._match_taxonomy(sector_hint, taxonomies)
    baseline = taxonomies.get(taxonomy_key) if taxonomy_key else None
    documents = ""  # no input/ ground-truth docs in this demo

    # A representative Pass-1 output, so you can see WHERE it gets injected into
    # the Pass-2 user prompt (the real text is whatever the grounded call returns).
    sample_findings = (
        "[PASS-1 GROUNDED FINDINGS TEXT GOES HERE — e.g. current subsector, any "
        "pivot from the historical model, the latest reported KPIs with periods, "
        "and the named sources that disclose them.]"
    )

    _rule(f"SECTOR RESEARCH PROMPTS — {company} (sector hint: {sector})")

    _rule("PASS 1 · SYSTEM  (_RESEARCH_SYSTEM)")
    print(sr._RESEARCH_SYSTEM)

    _rule("PASS 1 · USER  (_research_prompt)")
    print(sr._research_prompt(inp))

    _rule("PASS 2 · SYSTEM  (_system_prompt — the planning prompt)")
    print(sr._system_prompt())

    _rule("PASS 2 · USER  (_user_prompt + documents block)")
    print(sr._user_prompt(inp, taxonomy_key, baseline, sample_findings)
          + sf._documents_block(documents))

    # Persona-relevance scoring (one call per candidate domain). The system+user
    # strings live in skill_functions.evaluate_persona_relevance; reproduced here
    # verbatim with a sample domain so the full set is visible.
    _rule("PASS 3 · SYSTEM  (persona-relevance scoring — evaluate_persona_relevance)")
    print(
        "You apply the persona-relevance test. The four personas are: "
        "PE analyst (invest? at what price?), investment banker (pitch/deal), "
        "credit analyst (lending risk/default probability), consultant "
        "(operational health). For each, state the SPECIFIC decision that changes "
        "because of this item, or null if it is irrelevant to that persona. "
        "Be sharp, not vague — 'good context' fails the test."
    )

    _rule("PASS 3 · USER  (persona-relevance scoring — sample domain)")
    item_description = (
        "Research domain 'Platform Segment Performance & Unit Economics' for "
        f"{company} (multi-segment consumer-internet platform). Sections fed: "
        "Segment-wise Gross Order Value (GOV) & Take Rate, Segment-wise Adjusted "
        "EBITDA & Contribution Margin."
    )
    item_data_summary = (
        "Data fields: Food Delivery GOV, Blinkit GOV, Hyperpure Revenue, "
        "Food Delivery Take Rate, Blinkit Adj. EBITDA, Contribution Margin %"
    )
    print(
        f"Item: {item_description}\n"
        f"Data summary: {item_data_summary}\n\n"
        "Return JSON with keys: pe_analyst, banker, credit_analyst, consultant "
        "(each a one-sentence decision-impact string or null), and overall_score "
        "(integer 0-100 reflecting how many personas care and how strongly)."
    )

    # ----------------------------------------------------------------------- #
    # B. DATA RESEARCH — how the data for ONE chosen sector section is fetched. #
    # Sector domains run skill_functions.search_for_data_fields (two passes +   #
    # an optional gap-fill). Below is rendered for a sample chosen domain.      #
    # ----------------------------------------------------------------------- #
    _rule("B. DATA RESEARCH (Wave 1) — for one chosen sector domain/section")

    # A representative chosen sector domain (as Sector Research would emit it).
    domain_name = "Platform Segment Performance & Unit Economics"
    data_fields = [
        "Food Delivery GOV (₹ Cr)", "Blinkit GOV (₹ Cr)", "Food Delivery Take Rate (%)",
        "Blinkit Adj. EBITDA (₹ Cr)", "Blinkit Contribution Margin (%)",
    ]
    recommended_sources = [
        "Company Investor Presentations", "Quarterly Earnings Transcripts",
        "Company Annual Reports",
    ]
    fields_block = "\n".join(f"- {f}" for f in data_fields)
    sources_block = ", ".join(recommended_sources)
    today = datetime.now().strftime("%d %b %Y")

    _rule("PASS 4 · SYSTEM  (data research — skill_functions._RESEARCH_SYSTEM)")
    print(sf._RESEARCH_SYSTEM)

    _rule("PASS 4 · USER  (data research — built in search_for_data_fields)")
    print(
        f"Research the company: {company}.\n"
        f"TODAY is {today} — prefer the most recent data and run dated "
        f"trailing-12-month queries for time-sensitive items.\n"
        f"Prefer these sources first: {sources_block}. "
        f"If a hard field isn't in those, try additional credible sources before "
        f"concluding 'Not found'. You may run up to 5 web searches.\n\n"
        "Find each of the following and report value + date/period + source "
        "(list EVERY item you find, most recent first):\n"
        f"{fields_block}"
        f"{sf._documents_block('')}"
    )

    _rule("PASS 5 · SYSTEM  (JSON extraction — skill_functions._EXTRACT_SYSTEM)")
    print(sf._EXTRACT_SYSTEM)

    _rule("PASS 5 · USER  (JSON extraction — _extract_prompt, sample findings + URLs)")
    sample_findings = (
        "[PASS-4 GROUNDED RESEARCH TEXT goes here — the value + period + source the "
        "model found for each field, written free-form.]"
    )
    sample_urls = [
        "https://www.zomato.com/investor-relations/q4fy26-results.pdf",
        "https://www.bseindia.com/...eternal-q4fy26.pdf",
    ]
    sample_titles = {sample_urls[0]: "Eternal Q4 FY26 Investor Presentation"}
    print(sf._extract_prompt(fields_block, sample_findings, "", sample_urls, sample_titles))

    _rule("PASS 6 · USER  (gap-fill re-research — same system as PASS 4, with a FOCUS line)")
    gap_focus = ("ONLY these specific items, which a broad first pass missed — dig "
                 "deeper and try alternate credible sources before concluding Not Available")
    print(
        f"Research the company: {company}.\n"
        f"TODAY is {today} — prefer the most recent data and run dated "
        f"trailing-12-month queries for time-sensitive items.\n"
        f"FOCUS of this search: {gap_focus}.\n"
        f"Prefer these sources first: {sources_block}. "
        f"If a hard field isn't in those, try additional credible sources before "
        f"concluding 'Not found'. You may run up to 5 web searches.\n\n"
        "Find each of the following and report value + date/period + source "
        "(list EVERY item you find, most recent first):\n"
        "- Blinkit Contribution Margin (%)   [example: the still-missing field]"
    )

    print("\n" + "=" * 80)
    print("END — every prompt for (A) choosing sector sections + (B) researching their data.")
    print("Note: generic domains use the SAME data-research prompts (PASS 4-6); breadth-heavy")
    print("generic domains (Financials/Market/Track) run one PASS-4/5 per sub-topic group.")
    print("Phase 1.5 dedup / synthesis / presentation / validation have their own prompts.")
    print("=" * 80)


if __name__ == "__main__":
    main()

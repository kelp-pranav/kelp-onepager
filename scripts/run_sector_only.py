"""Run the SECTOR slice of the pipeline end-to-end — skipping the 6 generic
domains, synthesis, presentation, coverage and validation. The cheap way to
validate the post-data sector swap (Phase 3.5) live without paying for a full run.

Pipeline run here:
    Phase 1   sector_research        (select primary + reserve sector domains)
    Phase 1.5 sector_dedup           (drop generic-duplicate sector sections, clean reserves)
    research  domain_sector_specific (ONLY the sector domains — the generics are skipped)
    Phase 3   json_population        (apply the substance bar to sector sections)
    Phase 3.5 sector_swap            (drop empty sections, swap in data-rich reserves)

    python scripts/run_sector_only.py "Meesho"
    python scripts/run_sector_only.py "Embio Limited" --sector pharma

Prints, for every sector section, how many fields were found and its status
(populated / dropped), the swap log, and the final surviving sector sections.
Logs cost to API_EXPENSES.md.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import sections_catalog as catalog  # noqa: E402
import skill_functions as sf  # noqa: E402
from schemas import DomainResearchOutput, PipelineInput  # noqa: E402
from skills import (  # noqa: E402
    domain_sector_specific, json_population, layout_planning, sector_dedup,
    sector_research, sector_swap,
)
from skills.json_population import _found


def _profile(sector_result, inp: PipelineInput) -> dict:
    return {
        **sector_result.company_profile,
        "company_name": inp.company_name,
        "business_description": (sector_result.company_profile.get("business_description")
                                 or inp.business_description),
    }


def _distinct_sector_sections(sector_result) -> list:
    seen, out = set(), []
    for d in sector_result.sector_domains:
        for s in d.sections_covered:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


async def main() -> None:
    args = list(sys.argv[1:])
    sector = None
    if "--sector" in args:
        i = args.index("--sector")
        sector = args[i + 1]
        del args[i:i + 2]
    company = args[0] if args else "Meesho"

    before = sf.snapshot()
    inp = PipelineInput(company_name=company, sector=sector)

    # Phase 0 — load input/ ground-truth docs exactly like the full pipeline, so
    # sector research + sector-domain research behave identically to a real run.
    documents, doc_names = sf.load_input_documents()
    if doc_names:
        print(f"[ground-truth docs loaded: {', '.join(doc_names)}]")

    # Phase 1 — sector research.
    sector_result = await sector_research.run(inp, documents=documents)
    # Phase 1.5 — dedup + reserve cleaning.
    sector_result, removed = await sector_dedup.run(sector_result)
    profile = _profile(sector_result, inp)

    print("\n" + "=" * 74)
    print(f"SECTOR-ONLY RUN — {company}")
    print("=" * 74)
    print(f"resolved_subsector : {sector_result.resolved_subsector}")
    print(f"primary sector domains : {[d.domain_name for d in sector_result.sector_domains]}")
    print(f"reserve sector domains : {[d.domain_name for d in sector_result.reserve_domains]}")
    if removed:
        print(f"deduped (generic-dup)  : {[r['section'] for r in removed]}")

    # Research ONLY the sector domains (generics skipped). This is the EXACT call
    # the orchestrator's _branch_b makes for sector domains — same skill, model,
    # input dict, concurrency cap and timeout — so each section is researched
    # identically to a full pipeline run.
    base = {"company_name": profile["company_name"], "company_profile": profile,
            "documents": documents}
    skills = [domain_sector_specific.run for _ in sector_result.sector_domains]
    inputs = [{**base, "domain": d} for d in sector_result.sector_domains]
    names = [d.domain_name for d in sector_result.sector_domains]
    raw = await sf.run_skills_in_parallel(
        skills=skills, inputs=inputs,
        max_concurrent=config.WAVE1_MAX_CONCURRENT,
        per_skill_timeout=config.WAVE1_PER_SKILL_TIMEOUT_SECONDS,
        skill_names=names,
    )
    domain_results = [r for r in raw if isinstance(r, DomainResearchOutput)]

    # Layout + population (uniform sector scores — ordering is irrelevant here).
    scores = {s: 60 for s in _distinct_sector_sections(sector_result)}
    plan = layout_planning.run(scores, sector_result)
    final = json_population.run(plan.skeleton, plan, domain_results, profile)

    print("\nSECTOR SECTIONS BEFORE SWAP (substance bar applied):")
    for s in final.sections:
        if s.section_tag != "Sector":
            continue
        data = s.content.get("data") or {}
        found = sum(1 for v in data.values() if _found(v))
        comp = s.content.get("completeness")
        mark = "KEEP " if s.content.get("status") != "unavailable" else "DROP "
        comp_s = f"{comp:.2f}" if isinstance(comp, (int, float)) else "n/a"
        print(f"  [{mark}] {s.section_name:<42} found={found:<2} completeness={comp_s} "
              f"status={s.content.get('status')}")

    # Phase 3.5 — the swap.
    final, plan, domain_results, swap_log = await sector_swap.run(
        final, plan, domain_results, sector_result, profile,
        documents=documents, doc_names=doc_names)

    print("\nSWAP LOG:")
    for e in swap_log:
        print(f"  {e}")

    print("\nFINAL SURVIVING SECTOR SECTIONS:")
    survivors = [s.section_name for s in final.sections
                 if s.section_tag == "Sector" and s.content.get("status") != "unavailable"]
    for s in survivors:
        print(f"  • {s}")
    print(f"  ({len(survivors)} sector section(s); floor = {sector_dedup.MIN_SECTOR_SECTIONS})")

    after = sf.snapshot()
    run_cost = round(after[0] - before[0], 6)
    calls = after[1] - before[1]
    grounded = after[2] - before[2]
    total = sf.append_expense_md(f"{company} (sector-only swap test)", run_cost,
                                 calls=calls, grounded=grounded)
    print(f"\ncost: ${run_cost:.6f} over {calls} call(s) ({grounded} grounded) | "
          f"logged. lifetime on key: ${total:.6f}")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())

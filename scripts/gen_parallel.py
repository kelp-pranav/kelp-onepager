"""Generic PARALLEL one-pager JSON generator (no HTML compile).

Usage:
    python3 gen_parallel.py "<Company Name>" ["<business description hint>"]

Mirrors orchestrator.generate_one_pager up to validation, then dumps the
FinalJSON. Wave 1 domains run concurrently (bounded by WAVE1_MAX_CONCURRENT).
Auto-loads any ground-truth documents in input/ (authoritative, override web on
conflict) and synthesizes interpretive sections (Risk flags) with web search.
Output: output/<slug>.json
"""

import asyncio
import os
import re
import sys
import time

# Run from the project root: `python3 scripts/gen_parallel.py ...`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import skill_functions as sf
from schemas import DomainResearchOutput, PipelineInput
from orchestrator import _branch_a, _branch_b, _build_profile
from skills import (
    coverage_assessment, data_validation, json_population, sector_research,
    synthesis_future_plan, synthesis_investment_thesis, synthesis_risk_flags,
    synthesis_swot,
)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "company"


async def main(company: str, description: str) -> None:
    before = sf.snapshot()
    wall0 = time.perf_counter()
    inp = PipelineInput(company_name=company, business_description=description or None)
    out_file = f"{_slug(company)}.json"

    print(f"Running {company} pipeline (PARALLEL, Phases 1-5, no compile)…\n")

    # PHASE 0 — load local ground-truth documents from input/
    documents, doc_names = sf.load_input_documents()
    if doc_names:
        print(f"  ground-truth docs: {', '.join(doc_names)}")

    # PHASE 1 — Sector Research (sequential, blocking)
    t = time.perf_counter()
    sector = await sector_research.run(inp, documents=documents)
    profile = _build_profile(sector, inp)
    print(f"  Phase1 sector research: {time.perf_counter()-t:.1f}s | "
          f"subsector: {sector.resolved_subsector} | sector domains: {len(sector.sector_domains)}")

    # PHASE 2 — Branch A (scoring+layout) ∥ Branch B (Wave 1, concurrent)
    t = time.perf_counter()
    plan, wave1_raw = await asyncio.gather(
        _branch_a(sector), _branch_b(sector, profile, documents)
    )
    domain_results = [r for r in wave1_raw if isinstance(r, DomainResearchOutput)]
    print(f"  Phase2 Wave1+layout (parallel): {time.perf_counter()-t:.1f}s | "
          f"domains succeeded: {len(domain_results)}/{len(wave1_raw)}")
    for r in wave1_raw:
        if not isinstance(r, DomainResearchOutput):
            print(f"    [!] {getattr(r,'skill_name','domain')}: {getattr(r,'error','failed')}")

    # PHASE 3 — JSON Population
    final = json_population.run(plan.skeleton, plan, domain_results, profile,
                               documents_used=doc_names)

    # PHASE 4 — Wave 2 synthesis (parallel; Risk flags is interpretive + grounded)
    t = time.perf_counter()
    wave1_map = {d.domain_name: d for d in domain_results}
    syn_in = {"company_profile": profile, "wave_1_results": wave1_map,
              "documents": documents}
    thesis, swot, future, risk_flags = await asyncio.gather(
        synthesis_investment_thesis.run(syn_in),
        synthesis_swot.run(syn_in),
        synthesis_future_plan.run(syn_in),
        synthesis_risk_flags.run(syn_in),
    )
    json_population.attach_synthesis(final, thesis, swot, future, risk_flags)
    print(f"  Phase4 synthesis (parallel): {time.perf_counter()-t:.1f}s")

    # PHASE 4.5 — Coverage gap-gate
    final, coverage = await coverage_assessment.run(final, sector.resolved_subsector)

    # PHASE 5 — Validation
    final, report = await data_validation.run(final, wave1_map)
    # Final layout pass: drop unavailable sections, close order gaps.
    final = json_population.prune_and_renumber(final)

    # Dump FinalJSON (no compile)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(config.OUTPUT_DIR, out_file)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(final.model_dump_json(indent=2))

    wall = time.perf_counter() - wall0
    cur = sf.snapshot()
    run_cost = cur[0] - before[0]
    run_calls = cur[1] - before[1]
    led = sf.append_delta_to_ledger(f"{company} JSON (PARALLEL, Phases 1-5, no compile)", before)
    # Human-readable expense log for the LiteLLM key (per-run + cumulative).
    total_spent = sf.append_expense_md(f"{company} (one-pager JSON)", run_cost, calls=run_calls)

    m = final.metadata
    print(f"\n=== TOTAL: {wall:.1f}s | run cost ${run_cost:.6f} ===")
    print(f"sections: populated={m['sections_populated']} partial={m['sections_partial']} "
          f"unavailable={m['sections_unavailable']} "
          f"not_applicable={m.get('sections_not_applicable',0)} | sources={len(m['sources'])}")
    print(f"documents_used: {m.get('documents_used') or '(none)'}")
    print(f"domains succeeded: {len(domain_results)}/{len(wave1_raw)}")
    print(f"output JSON: {out_path} ({os.path.getsize(out_path):,} bytes)")
    print(f"LIFETIME cost: ${led['lifetime_cost_usd']:.6f} | LiteLLM key total: ${total_spent:.6f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python3 gen_parallel.py "<Company Name>" ["<description>"]')
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))

"""Top-level pipeline coordinator (Section 4 of the spec).

Wires the skills into the two-wave parallel pipeline:

  Phase 1  Sector Research (sequential, blocking)
  Phase 2  Branch A: Importance Scoring + Layout Planning   ┐ in parallel
           Branch B: Wave 1 domain research (bounded fan-out)┘
  Phase 3  JSON Population (skeleton ← Wave 1 results)
  Phase 4  Wave 2 synthesis (Investment Thesis / SWOT / Future Plan), parallel
  Phase 5  Data Validation & dedup

Degrades gracefully: a Wave 1 skill failure marks its sections unavailable and
the pipeline continues; only Phase 1 failure is fatal.

Public entry point:  async def generate_one_pager(input) -> (final_json, telemetry)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
import skill_functions as sf
from schemas import DomainResearchOutput, PipelineInput, SectorResearchOutput
from skills import (
    coverage_assessment, data_validation, domain_corporate_structure,
    domain_credit_risk, domain_financials, domain_geography,
    domain_market_position, domain_sector_specific, domain_track_record,
    importance_scoring, json_population, layout_planning,
    section_presentation, sector_dedup, sector_research, sector_swap,
    synthesis_future_plan, synthesis_investment_thesis, synthesis_risk_flags,
    synthesis_swot,
)

_GENERIC_DOMAIN_SKILLS = [
    ("Financials & Ratios", domain_financials.run),
    ("Corporate Structure", domain_corporate_structure.run),
    ("Market Position", domain_market_position.run),
    ("Track Record", domain_track_record.run),
    ("Credit & Risk", domain_credit_risk.run),
    ("Geography", domain_geography.run),
]


def _build_profile(sector_result: SectorResearchOutput, inp: PipelineInput) -> Dict[str, Any]:
    return {
        **sector_result.company_profile,
        "company_name": inp.company_name,
        # Prefer the RESEARCHED description from Sector Research; the caller's
        # input is only a hint and is used merely as a fallback if research
        # produced nothing.
        "business_description": (sector_result.company_profile.get("business_description")
                                 or inp.business_description),
    }


async def _branch_a(sector_result: SectorResearchOutput):
    scores = await importance_scoring.run(sector_result)
    return layout_planning.run(scores, sector_result)


async def _branch_b(sector_result: SectorResearchOutput, profile: Dict[str, Any],
                    documents: str = ""):
    base = {"company_name": profile["company_name"], "company_profile": profile,
            "documents": documents}
    skills: List = []
    inputs: List[dict] = []
    names: List[str] = []
    for dname, fn in _GENERIC_DOMAIN_SKILLS:
        skills.append(fn)
        inputs.append(base)
        names.append(dname)
    for d in sector_result.sector_domains:
        skills.append(domain_sector_specific.run)
        inputs.append({**base, "domain": d})
        names.append(d.domain_name)
    return await sf.run_skills_in_parallel(
        skills=skills, inputs=inputs,
        max_concurrent=config.WAVE1_MAX_CONCURRENT,
        per_skill_timeout=config.WAVE1_PER_SKILL_TIMEOUT_SECONDS,
        skill_names=names,
    )


async def generate_one_pager(
    inp: PipelineInput,
    progress: Optional[Callable[[str], None]] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Run the full pipeline.

    ``progress`` (optional) is called with a phase key the moment that phase
    begins — used by the web UI to show live status. Safe to omit.
    """
    before = sf.snapshot()
    t0 = time.perf_counter()
    timings: Dict[str, int] = {}
    phase_costs: Dict[str, float] = {}
    warnings: List[str] = []

    def emit(phase: str) -> None:
        if progress is not None:
            try:
                progress(phase)
            except Exception:  # progress reporting must never break a run
                pass

    def phase_start():
        return (time.perf_counter(), sf.get_cumulative_cost())

    def mark(phase: str, start) -> None:
        t_start, c_start = start
        timings[phase] = int((time.perf_counter() - t_start) * 1000)
        phase_costs[phase] = round(sf.get_cumulative_cost() - c_start, 6)

    # PHASE 0 — load local ground-truth documents (authoritative source; empty
    # input/ folder => behaves exactly as before).
    documents, doc_names = sf.load_input_documents()
    if doc_names:
        warnings.append(f"{len(doc_names)} ground-truth document(s) loaded: {', '.join(doc_names)}")

    # PHASE 1 — Sector Research (fatal if it fails)
    emit("sector_research")
    t = phase_start()
    sector_result = await sector_research.run(inp, documents=documents)
    mark("sector_research", t)
    if not sector_result.generic_domains and not sector_result.sector_domains:
        raise RuntimeError("Sector Research produced no domains — cannot proceed")
    profile = _build_profile(sector_result, inp)

    # PHASE 1.5 — drop sector sections that duplicate generic ones (generic wins),
    # then backfill so >=5 sector sections survive. Keeps Phase 2 input clean.
    emit("sector_dedup")
    t = phase_start()
    sector_result, removed_dups = await sector_dedup.run(sector_result)
    mark("sector_dedup", t)
    if removed_dups:
        names = ", ".join(f"{r['section']}→{r['matched_generic']}" for r in removed_dups[:6])
        warnings.append(f"{len(removed_dups)} duplicate sector section(s) dropped: {names}")

    # PHASE 2 — Branch A (scoring+layout) ∥ Branch B (Wave 1)
    emit("wave_1_and_layout")
    t = phase_start()
    plan, wave_1_raw = await asyncio.gather(
        _branch_a(sector_result), _branch_b(sector_result, profile, documents)
    )
    mark("wave_1_and_layout", t)

    domain_results: List[DomainResearchOutput] = []
    for r in wave_1_raw:
        if isinstance(r, DomainResearchOutput):
            domain_results.append(r)
            if r.confidence == "low":
                warnings.append(f"{r.domain_name}: low confidence ({r.completeness:.0%} complete)")
        else:  # SkillError
            warnings.append(f"{getattr(r, 'skill_name', 'domain')}: {getattr(r, 'error', 'failed')}")

    # PHASE 3 — JSON Population
    emit("json_population")
    t = phase_start()
    final = json_population.run(plan.skeleton, plan, domain_results, profile,
                               documents_used=doc_names)
    mark("json_population", t)

    # PHASE 3.5 — Post-data sector swap: chosen sector sections that came back
    # empty/thin (failed the substance bar) are dropped and refilled from the
    # reserve pool with data-rich runner-ups, so the >=5 sector floor holds AFTER
    # data — not just at selection time. On-demand: data-rich companies skip it.
    emit("sector_swap")
    t = phase_start()
    final, plan, domain_results, swap_log = await sector_swap.run(
        final, plan, domain_results, sector_result, profile, documents, doc_names)
    mark("sector_swap", t)
    _dropped = [e for e in swap_log if e["action"] == "drop"]
    _swapped = [s for e in swap_log if e["action"] == "swap_in" for s in e["sections"]]
    if _dropped or _swapped:
        warnings.append(
            f"sector swap: dropped {len(_dropped)} empty section(s)"
            + (f" ({', '.join(e['section'] for e in _dropped[:6])})" if _dropped else "")
            + f"; swapped in {len(_swapped)} reserve section(s)"
            + (f" ({', '.join(_swapped[:6])})" if _swapped else ""))
    if any(e["action"] == "floor_unmet" for e in swap_log):
        fu = next(e for e in swap_log if e["action"] == "floor_unmet")
        warnings.append(
            f"sector floor not met: {fu['have']}/{fu['need']} sector sections have "
            f"data after {fu['attempts']} reserve attempt(s) — shipping fewer")

    # PHASE 4 — Wave 2 synthesis (parallel; Risk flags is interpretive + grounded)
    emit("synthesis")
    t = phase_start()
    wave_1_map = {d.domain_name: d for d in domain_results}
    syn_in = {"company_profile": profile, "wave_1_results": wave_1_map,
              "documents": documents}
    thesis, swot, future, risk_flags = await asyncio.gather(
        synthesis_investment_thesis.run(syn_in),
        synthesis_swot.run(syn_in),
        synthesis_future_plan.run(syn_in),
        synthesis_risk_flags.run(syn_in),
    )
    json_population.attach_synthesis(final, thesis, swot, future, risk_flags)
    mark("synthesis", t)

    # PHASE 4.5 — Presentation (reformat fetched fields into render-ready content
    # + analysis, drop "Not Available" noise). Touches only domain-fed sections.
    emit("presentation")
    t = phase_start()
    final, present_warnings = await section_presentation.run(final, plan)
    warnings.extend(present_warnings)
    mark("presentation", t)

    # PHASE 4.6 — Coverage gap-gate (honest scoping: Not Applicable vs gaps)
    emit("coverage")
    t = phase_start()
    final, coverage = await coverage_assessment.run(final, sector_result.resolved_subsector)
    mark("coverage", t)
    if coverage.get("gaps"):
        warnings.append(f"{len(coverage['gaps'])} coverage gap(s) flagged")

    # PHASE 5 — Validation
    emit("validation")
    t = phase_start()
    final, report = await data_validation.run(final, wave_1_map)
    if report.get("missing_citations"):
        miss = report["missing_citations"]
        warnings.append(f"{len(miss)} section(s) without a real source link: {', '.join(miss[:6])}")
    # Final layout pass: drop unavailable sections, close order gaps.
    final = json_population.prune_and_renumber(final)
    mark("validation", t)

    led = sf.append_delta_to_ledger(f"one-pager: {inp.company_name}", before)
    cur = sf.snapshot()
    telemetry = {
        "company": inp.company_name,
        "resolved_subsector": sector_result.resolved_subsector,
        "total_duration_ms": int((time.perf_counter() - t0) * 1000),
        "phase_timings": timings,
        "phase_costs": phase_costs,
        "run_cost_usd": round(cur[0] - before[0], 6),
        "run_calls": cur[1] - before[1],
        "run_grounded_calls": cur[2] - before[2],
        "lifetime_cost_usd": led["lifetime_cost_usd"],
        "sections_populated": final.metadata.get("sections_populated"),
        "sections_partial": final.metadata.get("sections_partial"),
        "sections_unavailable": final.metadata.get("sections_unavailable"),
        "sections_not_applicable": final.metadata.get("sections_not_applicable"),
        "coverage_gaps": [g.get("section") for g in (coverage.get("gaps") or [])],
        "sector_swaps": swap_log,
        "sector_sections_dropped": sum(1 for e in swap_log if e["action"] == "drop"),
        "sector_sections_swapped_in": sum(
            len(e["sections"]) for e in swap_log if e["action"] == "swap_in"),
        "sector_floor_met": not any(e["action"] == "floor_unmet" for e in swap_log),
        "domains_succeeded": len(domain_results),
        "validation_report": report,
        "warnings": warnings,
    }
    emit("done")
    return final, telemetry

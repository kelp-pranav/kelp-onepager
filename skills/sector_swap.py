"""Phase 3.5 — Post-data sector-section swap.

Selection (Phase 1) and the pre-data floor (Phase 1.5) are blind to whether the
chosen sector sections' data actually exists on the public web. Private companies
in particular disclose few of the KPIs an analyst would want, so a section that
looked great at selection time can come back empty or too thin at the data stage.

This phase runs AFTER Wave 1 + JSON population, once each chosen sector section's
real data depth is known. It:
  1. counts surviving sector sections (those that cleared the substance bar in
     json_population — sector sections that failed it are marked "unavailable");
  2. if fewer than MIN_SECTOR_SECTIONS survive, researches RESERVE sector domains
     (runner-ups proposed in Phase 1 but not promoted to Wave 1) one at a time,
     bounded, and keeps the ones whose sections clear the same bar;
  3. injects the accepted reserve sections into the layout + domain results and
     rebuilds the FinalJSON so swapped-in sections flow through synthesis,
     presentation, coverage, and validation exactly like primary sections.

Reserves are researched on demand only — a data-rich listed company triggers no
swaps and pays nothing extra. Every drop/swap is logged (no silent caps); if the
floor still can't be met, the one-pager ships with fewer sector sections honestly.

Public entry point:
    async def run(final, plan, domain_results, sector_result, profile, documents,
                  doc_names, ...) -> (final, plan, domain_results, swap_log)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import config
import sections_catalog as catalog
from schemas import (
    DomainResearchOutput,
    FinalJSON,
    PlannedSection,
    SectionPlan,
    SectorResearchOutput,
)
from skills import domain_sector_specific, json_population
from skills.json_population import _found, _sector_status
from skills.sector_dedup import MIN_SECTOR_SECTIONS


def _survivors_and_drops(final: FinalJSON) -> Tuple[set, List[str]]:
    """Distinct sector sections that cleared the bar, and those that didn't."""
    survivors: set = set()
    dropped: List[str] = []
    for s in final.sections:
        if s.section_tag != "Sector":
            continue
        if s.content.get("status") == "unavailable":
            dropped.append(s.section_name)
        else:
            survivors.add(s.section_name)
    return survivors, dropped


def _passing_sections(
    reserve_domain, out: DomainResearchOutput, used: set
) -> List[str]:
    """Sections this reserve would feed that clear the substance bar and aren't
    already on the page."""
    full = {f: (e.get("value") if isinstance(e, dict) else e)
            for f, e in out.data.items()}
    passing: List[str] = []
    for s in reserve_domain.sections_covered:
        if s in used:
            continue
        sliced = catalog.select_fields(s, full)
        found = sum(1 for v in sliced.values() if _found(v))
        if _sector_status(found, out.completeness) == "populated":
            passing.append(s)
    return passing


def _inject(
    plan: SectionPlan,
    accepted: List[Tuple[Any, DomainResearchOutput, List[str]]],
    profile: Dict[str, Any],
) -> None:
    """Append accepted reserve sections to plan.skeleton + plan.sections as
    left-column Sector slots (in place). Order is appended below existing left
    sections; prune_and_renumber finalizes contiguous ordering later."""
    skeleton_sections: List[dict] = plan.skeleton.setdefault("sections", [])
    next_order = max(
        (slot.get("order_in_column", 0) for slot in skeleton_sections
         if slot.get("column") == "left"),
        default=-1,
    ) + 1
    label = catalog.sector_label((profile or {}).get("resolved_sector"))
    for rd, _out, passing in accepted:
        score = rd.persona_relevance.overall_score or 40
        for name in passing:
            ctype = rd.section_content_types.get(name, "table")
            skeleton_sections.append({
                "section_name": name,
                "column": "left",
                "order_in_column": next_order,
                "section_tag": "Sector",
                "section_tag_label": label,
                "content_type": ctype,
                "fed_by_domain": rd.domain_name,
                "importance_score": score,
                "populated": False,
                "content": {},
            })
            plan.sections.append(PlannedSection(
                section_name=name,
                fed_by_domain=rd.domain_name,
                importance_score=score,
                column="left",
                order_in_column=next_order,
                persona_relevance=rd.persona_relevance,
            ))
            next_order += 1


async def run(
    final: FinalJSON,
    plan: SectionPlan,
    domain_results: List[Any],
    sector_result: SectorResearchOutput,
    profile: Dict[str, Any],
    documents: str = "",
    doc_names: List[str] | None = None,
    min_sector: int = MIN_SECTOR_SECTIONS,
    max_attempts: int = config.SECTOR_SWAP_MAX_ATTEMPTS,
) -> Tuple[FinalJSON, SectionPlan, List[Any], List[Dict[str, Any]]]:
    """Enforce the post-data sector floor by swapping data-rich reserves in for
    chosen sections that came back empty/thin. Returns the (possibly rebuilt)
    FinalJSON, the (possibly extended) plan, the (possibly extended) domain
    results, and a structured swap log."""
    swap_log: List[Dict[str, Any]] = []

    survivors, dropped = _survivors_and_drops(final)
    for name in dropped:
        swap_log.append({"action": "drop", "section": name, "reason": "below_substance_bar"})

    needed = max(0, min_sector - len(survivors))
    if needed == 0 or not sector_result.reserve_domains:
        if needed > 0:
            swap_log.append({"action": "floor_unmet", "have": len(survivors),
                             "need": min_sector, "attempts": 0})
        return final, plan, domain_results, swap_log

    used = set(survivors)
    accepted: List[Tuple[Any, DomainResearchOutput, List[str]]] = []
    attempts = 0
    for rd in sector_result.reserve_domains:
        if needed <= 0 or attempts >= max_attempts:
            break
        attempts += 1
        try:
            out = await domain_sector_specific.run({
                "company_name": profile.get("company_name"),
                "company_profile": profile,
                "documents": documents,
                "domain": rd,
            })
        except Exception as exc:  # never fatal — log and try the next reserve
            swap_log.append({"action": "reserve_failed", "domain": rd.domain_name,
                             "error": str(exc)})
            continue
        if not isinstance(out, DomainResearchOutput):
            swap_log.append({"action": "reserve_failed", "domain": rd.domain_name,
                             "error": "no DomainResearchOutput"})
            continue

        passing = _passing_sections(rd, out, used)
        if passing:
            accepted.append((rd, out, passing))
            used.update(passing)
            needed -= len(passing)
            swap_log.append({"action": "swap_in", "domain": rd.domain_name,
                             "sections": passing, "completeness": out.completeness})
        else:
            swap_log.append({"action": "reserve_rejected", "domain": rd.domain_name,
                             "reason": "below_substance_bar",
                             "completeness": out.completeness})

    if accepted:
        # Add the reserves to the layout + domain results, then rebuild the JSON
        # so sources/citations get correct global indices (no surgical merge).
        _inject(plan, accepted, profile)
        domain_results = list(domain_results) + [out for _rd, out, _p in accepted]
        final = json_population.run(
            plan.skeleton, plan, domain_results, profile,
            documents_used=list(doc_names or []),
        )

    if needed > 0:
        swap_log.append({"action": "floor_unmet", "have": len(used),
                         "need": min_sector, "attempts": attempts})

    return final, plan, domain_results, swap_log

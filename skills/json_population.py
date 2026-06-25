"""Skill 6 — JSON Population (Phase 3).

Takes the empty skeleton from Layout Planning and fills it with Wave 1 domain
results, producing a FinalJSON whose sections carry data + citation indices +
status. Pure Python (no model call). Wave 2 synthesis sections (Investment
thesis / SWOT / Future plan) are left pending here and filled later by
attach_synthesis().

Public entry points:
    run(skeleton, section_plan, domain_results, company_profile=None) -> FinalJSON
    attach_synthesis(final_json, thesis, swot, future) -> FinalJSON
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import sections_catalog as catalog
from schemas import (
    CompletedSection,
    DomainResearchOutput,
    FinalJSON,
    SectionPlan,
    Source,
)

# Sections produced by Wave 2 synthesis — not fed by a Wave 1 domain result.
_SYNTHESIS_FED = {"Investment Thesis", "SWOT", "Future Plan", "Risk Flags"}
_PROFILE_FED = {"Sector Research"}

# Tokens meaning "no data".
_EMPTY = {"", "not available", "n/a", "na", "none", "unknown", "-"}


def _found(value: Any) -> bool:
    return value is not None and str(value).strip().lower() not in _EMPTY


def _sector_status(found: int, completeness: float) -> str:
    """Substance bar for SECTOR-specific sections: keep only if the section shows
    real depth (>=3 fields) OR the feeding domain is reasonably complete
    (completeness >= 0.3). Below the bar -> "unavailable", so prune drops it and
    the swap phase can refill the sector floor with a data-rich reserve.

    Single source of truth — the swap phase reuses this predicate to decide
    whether a freshly-researched reserve clears the bar before injecting it.
    """
    return "populated" if (found >= 3 or completeness >= 0.3) else "unavailable"


def _domain_map(
    domain_results: List[Union[DomainResearchOutput, Any]]
) -> Dict[str, DomainResearchOutput]:
    out: Dict[str, DomainResearchOutput] = {}
    for r in domain_results:
        if isinstance(r, DomainResearchOutput):
            out[r.domain_name] = r
    return out


def _build_source_index(
    domains: Dict[str, DomainResearchOutput]
) -> tuple[List[Dict[str, Any]], Dict[str, List[int]], Dict[str, Dict[str, List[int]]]]:
    """Flat metadata.sources list + per-domain citation indices into it +
    per-domain-per-field citation indices (the inline map).

    Returns ``(sources, per_domain, per_domain_field)`` where
    ``per_domain_field[domain][field]`` is the list of source indices backing
    that specific field — derived from each Source's ``field_attributions`` so a
    datum links only to the sources that actually support it.
    """
    sources: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    per_domain: Dict[str, List[int]] = {}
    per_domain_field: Dict[str, Dict[str, List[int]]] = {}
    for dname, out in domains.items():
        idxs: List[int] = []
        field_map: Dict[str, List[int]] = {}
        for s in out.sources_used:
            key = f"{s.name}|{s.url or ''}"
            if key not in seen:
                seen[key] = len(sources)
                sources.append({
                    "name": s.name,
                    "url": s.url,
                    "accessed_at": s.accessed_at.isoformat() if s.accessed_at else None,
                    "field_attributions": s.field_attributions,
                })
            gidx = seen[key]
            idxs.append(gidx)
            for field in s.field_attributions:
                field_map.setdefault(field, [])
                if gidx not in field_map[field]:
                    field_map[field].append(gidx)
        per_domain[dname] = idxs
        per_domain_field[dname] = field_map
    return sources, per_domain, per_domain_field


def _populate_domain_slot(
    slot: Dict[str, Any],
    name: str,
    out: DomainResearchOutput,
    per_domain_cites: Dict[str, List[int]],
    per_domain_field_cites: Dict[str, Dict[str, List[int]]],
) -> tuple[Dict[str, Any], List[int], Dict[str, List[int]]]:
    """Fill one domain-fed section slot: slice the domain's data to this section,
    assign status (sector sections clear the substance bar; generic sections keep
    the honest-partial ladder), and wire inline + section citations.
    Returns ``(content, citations, field_citations)``."""
    fed_by = slot.get("fed_by_domain", "")
    full = {f: (e.get("value") if isinstance(e, dict) else e)
            for f, e in out.data.items()}
    # Slice to only this section's relevant fields so sections fed by the
    # same domain don't all render identical data.
    data = catalog.select_fields(name, full)
    found = sum(1 for v in data.values() if _found(v))

    if slot.get("section_tag") == "Sector":
        # Substance bar — a thin sector section is treated as empty so it can be
        # pruned and swapped for a data-rich reserve. Never lingers as "partial".
        status = _sector_status(found, out.completeness)
    else:
        # Generic sections keep the honest-partial behaviour.
        status = "populated"
        if found == 0:
            status = "unavailable"
        elif out.completeness < 0.3:
            status = "partial"

    content = {
        "data": data,
        "confidence": out.confidence,
        "completeness": out.completeness,
        "status": status,
    }
    # Inline citations: link each SHOWN, found field to the exact sources
    # backing it. Section citations are the union of those — so a section
    # cites only the sources behind the facts it actually displays.
    field_citations: Dict[str, List[int]] = {}
    field_map = per_domain_field_cites.get(fed_by, {})
    union: List[int] = []
    for field, value in data.items():
        if not _found(value):
            continue
        idxs = field_map.get(field) or []
        if idxs:
            field_citations[field] = list(idxs)
            for i in idxs:
                if i not in union:
                    union.append(i)
    citations = sorted(union)
    # Safety net for the hard rule: if no per-field attribution matched but
    # the section shows data and the domain has sources, fall back to the
    # domain's sources so the section still carries real links (inline
    # citations stay empty — we can't map a specific datum in that case).
    if not citations and found:
        citations = sorted(set(per_domain_cites.get(fed_by, [])))
    return content, citations, field_citations


def run(
    skeleton: Dict[str, Any],
    section_plan: SectionPlan,
    domain_results: List[Union[DomainResearchOutput, Any]],
    company_profile: Optional[Dict[str, Any]] = None,
    documents_used: Optional[List[str]] = None,
) -> FinalJSON:
    domains = _domain_map(domain_results)
    sources, per_domain_cites, per_domain_field_cites = _build_source_index(domains)
    profile = company_profile or {}

    completed: List[CompletedSection] = []
    for slot in skeleton.get("sections", []):
        name = slot["section_name"]
        fed_by = slot.get("fed_by_domain", "")
        content: Dict[str, Any] = {}
        citations: List[int] = []
        field_citations: Dict[str, List[int]] = {}

        if fed_by in _SYNTHESIS_FED:
            content = {"status": "pending_synthesis"}
        elif fed_by in _PROFILE_FED:
            # Business description comes from the profile / description.
            desc = profile.get("business_description") or profile.get("description")
            content = {"text": desc, "status": "populated" if desc else "unavailable"}
        elif fed_by in domains:
            content, citations, field_citations = _populate_domain_slot(
                slot, name, domains[fed_by], per_domain_cites, per_domain_field_cites)
        else:
            content = {"status": "unavailable"}  # domain timed out / failed

        completed.append(CompletedSection(
            section_name=name,
            column=slot.get("column", "left"),
            order_in_column=slot.get("order_in_column", 0),
            section_tag=slot.get("section_tag", "Generic"),
            section_tag_label=slot.get("section_tag_label", "Generic"),
            content_type=slot.get("content_type", "prose"),
            content=content,
            citations=citations,
            field_citations=field_citations,
        ))

    company_header = {
        "name": profile.get("company_name"),
        "sector": profile.get("resolved_sector"),
        "ticker": profile.get("ticker"),
        "cin": profile.get("cin"),
        "founded": profile.get("founded"),
        "listed_status": profile.get("listed_status"),
        "hq": profile.get("hq"),
    }
    metadata = {
        "company_name": profile.get("company_name"),
        "sources": sources,
        # domain name -> source indices, used by attach_synthesis to cite the
        # evidence base behind each Wave 2 synthesis section.
        "domain_source_index": per_domain_cites,
        "documents_used": list(documents_used or []),
        "sections_total": len(completed),
        "sections_populated": sum(1 for c in completed if c.content.get("status") == "populated"),
        "sections_partial": sum(1 for c in completed if c.content.get("status") == "partial"),
        "sections_unavailable": sum(1 for c in completed if c.content.get("status") == "unavailable"),
    }
    return FinalJSON(metadata=metadata, company_header=company_header, sections=completed)


def attach_synthesis(
    final_json: FinalJSON,
    thesis: Optional[Dict[str, Any]] = None,
    swot: Optional[Dict[str, Any]] = None,
    future: Optional[Dict[str, Any]] = None,
    risk_flags: Optional[Dict[str, Any]] = None,
) -> FinalJSON:
    """Fill the Wave 2 synthesis sections in-place and return the FinalJSON."""
    payloads = {
        "Investment thesis": thesis,
        "SWOT": swot,
        "Future plan": future,
        "Risk flags": risk_flags,
    }
    domain_index: Dict[str, List[int]] = final_json.metadata.get("domain_source_index", {}) or {}
    for section in final_json.sections:
        payload = payloads.get(section.section_name)
        if payload is not None:
            payload = dict(payload)
            # Cite the evidence base: the sources of the Wave 1 domains this
            # synthesis read. Synthesis does no research of its own, so its claims
            # are traceable to those domains' sources rather than fabricated links.
            reads = payload.pop("_reads", []) or []
            union: List[int] = []
            for dname in reads:
                for i in domain_index.get(dname, []):
                    if i not in union:
                        union.append(i)
            if union:
                section.citations = sorted(union)
            # Respect a status the skill set (e.g. an interpretive skill that
            # found nothing returns status "unavailable"); default to populated.
            status = payload.get("status", "populated")
            section.content = {**payload, "status": status}
    return final_json


def prune_and_renumber(final_json: FinalJSON) -> FinalJSON:
    """Final layout pass: drop sections with no data and close the gaps.

    Removes every section whose ``content.status == "unavailable"`` (kept:
    populated / partial / not_applicable), then renumbers ``order_in_column``
    contiguously within each column so the fixed layout has no holes. Must run
    LAST — statuses are only final after synthesis + coverage + validation.
    """
    kept = [s for s in final_json.sections if s.content.get("status") != "unavailable"]
    for col in ("left", "right"):
        for idx, s in enumerate(sorted((s for s in kept if s.column == col),
                                       key=lambda s: s.order_in_column)):
            s.order_in_column = idx
    final_json.sections = kept

    m = final_json.metadata
    m["sections_total"] = len(kept)
    m["sections_populated"] = sum(1 for s in kept if s.content.get("status") == "populated")
    m["sections_partial"] = sum(1 for s in kept if s.content.get("status") == "partial")
    m["sections_unavailable"] = 0
    m["sections_not_applicable"] = sum(
        1 for s in kept if s.content.get("status") == "not_applicable")
    return final_json

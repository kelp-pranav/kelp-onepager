"""Skill 3 — Layout Planning (Phase 2, Branch A).

Pure-Python (no model call). Turns importance scores into the JSON skeleton:
which section goes in left/right column, in what order, with empty content slots
and the correct generic/sector tag. The rules encode persona attention patterns
(PE scans top-left first; credit scans right column for ratings).

Public entry point:  def run(scores, sector_result) -> SectionPlan
"""

from __future__ import annotations

from typing import Dict, List

import sections_catalog as catalog
from schemas import PlannedSection, SectionPlan, SectorResearchOutput


def _feed_map(sector_result: SectorResearchOutput) -> Dict[str, str]:
    """section_name -> feeding domain (generic from catalog, sector from domains)."""
    feed: Dict[str, str] = {}
    for s in catalog.GENERIC_SECTIONS:
        feed[s["name"]] = s["fed_by_domain"]
    for d in sector_result.sector_domains:
        for s in d.sections_covered:
            feed.setdefault(s, d.domain_name)
    return feed


def _sector_content_type_map(sector_result: SectorResearchOutput) -> Dict[str, str]:
    """section_name -> declared content_type, merged across all sector domains."""
    ctypes: Dict[str, str] = {}
    for d in sector_result.sector_domains:
        ctypes.update(d.section_content_types)
    return ctypes


def _content_type_for(section_name: str, sector_ctypes: Dict[str, str]) -> str:
    meta = catalog.get_meta(section_name)
    if meta:
        return meta["content_type"]          # generic sections: unchanged, always fixed
    return sector_ctypes.get(section_name, "table")   # sector sections: real value, "table" only as last resort


def _interleave_sector_left(
    left_generics: List[str],
    sector_sections: List[str],
    scores: Dict[str, int],
) -> List[str]:
    """Fixed generic order with sector-specific sections inserted by importance.

    Generic relative order is exactly ``left_generics`` and never changes. Sector
    sections (sorted by importance desc) are slotted between generics: each
    non-pinned generic at fixed position ``i`` has a strictly-decreasing anchor,
    and any remaining sector section scoring at/above that anchor is inserted just
    before it. Pinned top generics (catalog.LEFT_TOP_ORDER) keep a sector section
    from ever rising above the page header. Deterministic (ties broken by name).
    """
    n = len(left_generics)
    pinned = set(catalog.LEFT_TOP_ORDER)
    ranked = sorted(sector_sections, key=lambda s: (-scores.get(s, 40), s))
    out: List[str] = []
    si = 0
    for i, g in enumerate(left_generics):
        if g not in pinned and n:
            anchor = 100.0 * (n - i) / n
            while si < len(ranked) and scores.get(ranked[si], 40) >= anchor:
                out.append(ranked[si])
                si += 1
        out.append(g)
    out.extend(ranked[si:])  # anything below the last anchor goes at the bottom
    return out


def run(
    scores: Dict[str, int],
    sector_result: SectorResearchOutput,
) -> SectionPlan:
    feed = _feed_map(sector_result)
    sector_ctypes = _sector_content_type_map(sector_result)

    # Generic columns follow the fixed canonical layout (always present, never
    # reordered). Sector-specific sections (scored but not generic) interleave into
    # the LEFT column by importance; the RIGHT sidebar is generics only.
    sector_sections = [n for n in scores if not catalog.is_generic(n)]
    left = _interleave_sector_left(list(catalog.GENERIC_LEFT_ORDER), sector_sections, scores)
    right = list(catalog.GENERIC_RIGHT_ORDER)

    sections: List[PlannedSection] = []
    skeleton_sections: List[dict] = []

    # Pretty blue-pill label for sector sections, e.g. "consumer" -> "Consumer".
    sector_pill = catalog.sector_label(
        (sector_result.company_profile or {}).get("resolved_sector"))
    for column, ordered in (("left", left), ("right", right)):
        for i, name in enumerate(ordered):
            is_generic = catalog.is_generic(name)
            # Sector-specific sections carry the actual sector name as their pill
            # label (e.g. "Consumer", "Pharma"); generic sections carry no pill.
            tag = "Generic" if is_generic else "Sector"
            tag_label = "" if is_generic else sector_pill
            fed_by = feed.get(name, "Sector Research")

            sections.append(
                PlannedSection(
                    section_name=name,
                    fed_by_domain=fed_by,
                    importance_score=scores.get(name, 0),
                    column=column,
                    order_in_column=i,
                )
            )
            skeleton_sections.append({
                "section_name": name,
                "column": column,
                "order_in_column": i,
                "section_tag": tag,
                "section_tag_label": tag_label,
                "content_type": _content_type_for(name, sector_ctypes),
                "fed_by_domain": fed_by,
                "importance_score": scores.get(name, 0),
                "populated": False,
                "content": {},
            })

    skeleton = {"sections": skeleton_sections}
    return SectionPlan(sections=sections, skeleton=skeleton)

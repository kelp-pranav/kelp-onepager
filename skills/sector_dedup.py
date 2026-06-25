"""Phase 1.5 — Sector/Generic de-duplication.

Sector Research occasionally proposes "sector-specific" sections that merely
restate a generic section (e.g. "Product/Category Mix" ≈ generic "Revenue by
product segment", "Ownership/Funding/Cap-Table" ≈ "Ownership"). The exact-name
dedup downstream only catches identical strings, so semantic/near duplicates slip
through and waste one-pager space.

This step removes them BEFORE importance scoring / layout planning, so the rest
of the pipeline receives already-clean input. Policy: GENERIC WINS — the canonical
generic section stays and the overlapping sector section is dropped + logged.

Two detection layers:
  1. Lexical gate (pure Python, free) — conservative: exact-normalized match,
     token-subset, or high Jaccard. High precision, catches the obvious dupes.
  2. LLM semantic judge (one cheap call) — catches conceptual duplicates with no
     shared keywords (e.g. "Network/Footprint" ≈ "Channel mix & distribution").
     Degrades gracefully: any model error → keep the lexical result only.

Backfill: dropping duplicates can leave too few sector sections. If fewer than
MIN_SECTOR_SECTIONS (5) survive, one model call requests additional, genuinely
sector-distinctive sections (non-overlapping with the generics OR the survivors)
to top the count back up — so the one-pager keeps its sector depth.

Public entry point:
    async def run(sector_result, model_config, ...) -> (SectorResearchOutput, removed)
where ``removed`` is a list of {"section", "matched_generic", "via"}.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

import config
import sections_catalog as catalog
import skill_functions as sf
from schemas import Domain, PersonaRelevance, SectorResearchOutput

MIN_SECTOR_SECTIONS = 5

# Connective / filler tokens that shouldn't drive a name match.
_STOPWORDS = {
    "and", "or", "the", "of", "by", "a", "an", "to", "for", "in", "on", "with",
    "amp", "mix", "status", "analysis", "overview", "summary", "key", "other",
    "details", "data", "info", "information", "section",
}


def _tokens(name: str) -> set:
    """Significant, lightly-stemmed token set for a section name."""
    raw = re.findall(r"[a-z0-9]+", str(name).lower())
    out = set()
    for t in raw:
        if t in _STOPWORDS or len(t) <= 1:
            continue
        # crude singular stem so "products" == "product"
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        out.add(t)
    return out


def _lexical_match(sector_name: str, generic_names: List[str]) -> str | None:
    """Return the generic section a sector name duplicates lexically, or None.

    Conservative (high precision): exact normalized-token match, OR one name's
    significant tokens are a subset of the other's, OR Jaccard >= 0.5.
    """
    st = _tokens(sector_name)
    if not st:
        return None
    best, best_score = None, 0.0
    for g in generic_names:
        gt = _tokens(g)
        if not gt:
            continue
        if st == gt:
            return g
        inter = st & gt
        if not inter:
            continue
        # subset either way (e.g. {ownership} ⊆ {ownership, funding, cap, table})
        if gt <= st or st <= gt:
            return g
        jaccard = len(inter) / len(st | gt)
        if jaccard > best_score:
            best, best_score = g, jaccard
    return best if best_score >= 0.5 else None


_JUDGE_SYSTEM = (
    "You remove duplicate sections from a company one-pager. The one-pager already "
    "has a fixed set of GENERIC sections. The generic sections show the COMPANY-LEVEL "
    "/ CONSOLIDATED view (e.g. consolidated revenue/EBITDA/PAT, company-level segment "
    "split, ownership). You are given candidate SECTOR-SPECIFIC sections. Flag a "
    "sector section ONLY if it would mostly RESTATE the CONSOLIDATED generic — i.e. a "
    "reader would see largely the same company-level data twice.\n"
    "CARVE-OUT — these are NOT duplicates, do NOT flag them even though they touch a "
    "revenue/margin/segment theme, because the consolidated generic does not show them:\n"
    "  • a SEGMENT-LEVEL cut (per-vertical NOV/GTV, per-segment EBITDA / contribution "
    "margin / profitability inflection);\n"
    "  • an ALTERNATIVE headline metric the consolidated generic doesn't track "
    "(NOV/GTV vs recognized revenue, take-rate, MTU / transacting users, gig-partner scale);\n"
    "  • a STRUCTURAL insight with no generic home (an accounting / revenue-recognition "
    "model shift, idiosyncratic seasonality / lumpiness, network/store density, "
    "SKU-assortment depth).\n"
    "Be conservative: flag ONLY a section that restates the company-level generic itself; "
    "when unsure, do NOT flag.\n"
    'Return STRICT JSON: {"duplicates": [{"section": <sector section name '
    'verbatim>, "matches_generic": <the generic section it restates>}]}.'
)


async def _semantic_judge(
    sector_names: List[str],
    generic_names: List[str],
    model_config: "config.ModelConfig",
) -> List[Dict[str, str]]:
    """One cheap model call → conceptual duplicates the lexical gate missed.
    Returns [] on any error (never blocks the pipeline)."""
    if not sector_names:
        return []
    prompt = (
        "GENERIC sections already on the one-pager:\n"
        + "\n".join(f"- {g}" for g in generic_names)
        + "\n\nCandidate SECTOR-SPECIFIC sections to check:\n"
        + "\n".join(f"- {s}" for s in sector_names)
        + "\n\nWhich candidates mostly restate a generic section?"
    )
    try:
        resp = await sf.call_model(
            prompt=prompt, system=_JUDGE_SYSTEM, model_config=model_config,
            enable_search=False, response_format="json", max_tokens=600,
        )
    except Exception:
        return []
    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    dups = parsed.get("duplicates") or []
    allowed = set(sector_names)
    out: List[Dict[str, str]] = []
    for d in dups:
        if not isinstance(d, dict):
            continue
        name = d.get("section")
        if name in allowed:  # only honour names we actually asked about
            out.append({"section": name, "matches_generic": d.get("matches_generic") or "generic section"})
    return out


def _distinct_sector_sections(sector_result: SectorResearchOutput) -> List[str]:
    out: List[str] = []
    seen = set()
    for d in sector_result.sector_domains:
        for s in d.sections_covered:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


_BACKFILL_SYSTEM = (
    "You extend a company one-pager's research plan with ADDITIONAL sector-specific "
    "sections. Each must be genuinely sector-distinctive and must NOT overlap (by "
    "name or content) the generic sections already on the one-pager, nor the "
    "sector sections already chosen. For each new section give a render shape and "
    "5-10 specific, quantitative data fields an analyst would actually pull.\n"
    'Return STRICT JSON: {"sections": [{"section_name": str, "content_type": '
    '"stat_grid"|"table"|"chart"|"list", "data_fields_needed": [str], '
    '"recommended_sources": [str]}]}.'
)


async def _backfill(
    sector_result: SectorResearchOutput,
    generic_names: List[str],
    need: int,
    model_config: "config.ModelConfig",
) -> List[str]:
    """Ask for ``need`` more sector-distinctive sections; append them as one
    'Additional Sector Insights' domain. Returns the names actually added (after a
    lexical re-check so a re-proposed duplicate can't sneak back in). [] on error."""
    profile = sector_result.company_profile or {}
    existing = _distinct_sector_sections(sector_result)
    prompt = (
        f"Company: {profile.get('company_name', 'the company')}\n"
        f"Subsector: {sector_result.resolved_subsector}\n"
        f"Business: {profile.get('business_description', '')}\n\n"
        "GENERIC sections already present (do NOT duplicate):\n"
        + "\n".join(f"- {g}" for g in generic_names)
        + "\n\nSector sections already chosen (do NOT duplicate):\n"
        + ("\n".join(f"- {s}" for s in existing) or "- (none)")
        + f"\n\nPropose {need} ADDITIONAL, non-overlapping, sector-distinctive sections."
    )
    try:
        resp = await sf.call_model(
            prompt=prompt, system=_BACKFILL_SYSTEM, model_config=model_config,
            enable_search=False, response_format="json",
            max_tokens=max(getattr(model_config, "max_tokens", 0) or 0, 1500),
        )
    except Exception:
        return []

    parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
    proposed = parsed.get("sections") or []
    taken = {s.lower() for s in existing} | {g.lower() for g in generic_names}
    new_sections: List[str] = []
    content_types: Dict[str, str] = {}
    fields: List[str] = []
    sources: List[str] = []
    for item in proposed:
        if not isinstance(item, dict):
            continue
        name = (item.get("section_name") or "").strip()
        if not name or name.lower() in taken:
            continue
        # Re-check against the generics lexically — never re-admit a duplicate.
        if name in set(generic_names) or _lexical_match(name, generic_names):
            continue
        taken.add(name.lower())
        new_sections.append(name)
        ctype = item.get("content_type")
        if ctype in ("stat_grid", "table", "chart", "list", "prose"):
            content_types[name] = ctype
        for f in item.get("data_fields_needed") or []:
            if isinstance(f, str) and f.strip():
                fields.append(f.strip())
        for src in item.get("recommended_sources") or []:
            if isinstance(src, str) and src.strip():
                sources.append(src.strip())
        if len(new_sections) >= need:
            break

    if not new_sections:
        return []
    sector_result.sector_domains.append(Domain(
        domain_name="Additional Sector Insights",
        priority_hint="medium",
        sections_covered=new_sections,
        section_content_types=content_types,
        data_fields_needed=fields or [f"key data for {n}" for n in new_sections],
        recommended_sources=sources or ["company filings", "industry reports"],
        is_sector_specific=True,
        persona_relevance=PersonaRelevance(overall_score=60),
    ))
    return new_sections


def _apply(sector_result: SectorResearchOutput, drop: Dict[str, str]) -> None:
    """Remove dropped section names from every sector domain in place; drop a
    domain that ends up with no sections."""
    kept_domains = []
    for d in sector_result.sector_domains:
        d.sections_covered = [s for s in d.sections_covered if s not in drop]
        if d.section_content_types:
            d.section_content_types = {
                k: v for k, v in d.section_content_types.items() if k not in drop
            }
        if d.sections_covered:
            kept_domains.append(d)
    sector_result.sector_domains = kept_domains


def _clean_reserves(
    sector_result: SectorResearchOutput, generic_names: List[str]
) -> None:
    """Lexically clean the reserve pool in place so a later swap can't inject a
    section that duplicates a generic OR a surviving primary sector section.

    Lexical gate only (no LLM judge) — reserves are a fallback pool and the swap
    phase re-applies the data substance bar anyway. Drops an emptied reserve."""
    primary_sections = {s for d in sector_result.sector_domains for s in d.sections_covered}
    generic_set = set(generic_names)
    kept_reserves: List[Domain] = []
    for d in sector_result.reserve_domains:
        keep_sections = []
        for s in d.sections_covered:
            if s in generic_set or s in primary_sections:
                continue
            if _lexical_match(s, generic_names):
                continue
            keep_sections.append(s)
        if not keep_sections:
            continue
        d.sections_covered = keep_sections
        if d.section_content_types:
            d.section_content_types = {
                k: v for k, v in d.section_content_types.items() if k in keep_sections
            }
        kept_reserves.append(d)
    sector_result.reserve_domains = kept_reserves


async def run(
    sector_result: SectorResearchOutput,
    model_config: "config.ModelConfig" = config.VALIDATION_MODEL,
    backfill_model_config: "config.ModelConfig" = config.SECTOR_RESEARCH_MODEL,
    min_sector_sections: int = MIN_SECTOR_SECTIONS,
) -> Tuple[SectorResearchOutput, List[Dict[str, str]]]:
    """De-duplicate sector sections against the generic catalog (generic wins),
    then backfill so at least ``min_sector_sections`` sector sections survive."""
    generic_names = catalog.generic_section_names()
    generic_set = set(generic_names)

    # Every distinct sector section currently proposed.
    sector_names: List[str] = []
    seen = set()
    for d in sector_result.sector_domains:
        for s in d.sections_covered:
            if s and s not in seen:
                seen.add(s)
                sector_names.append(s)

    removed: List[Dict[str, str]] = []
    drop: Dict[str, str] = {}

    # Layer 1 — lexical gate.
    survivors: List[str] = []
    for s in sector_names:
        if s in generic_set:
            match = s  # exact collision with a generic name
        else:
            match = _lexical_match(s, generic_names)
        if match:
            drop[s] = match
            removed.append({"section": s, "matched_generic": match, "via": "lexical"})
        else:
            survivors.append(s)

    # Layer 2 — LLM semantic judge over what survived the lexical gate.
    for d in await _semantic_judge(survivors, generic_names, model_config):
        s = d["section"]
        if s not in drop:
            drop[s] = d["matches_generic"]
            removed.append({"section": s, "matched_generic": d["matches_generic"], "via": "semantic"})

    if drop:
        _apply(sector_result, drop)

    # Backfill so the one-pager keeps its sector depth after drops. This is the
    # PRE-DATA floor (ensures the layout starts with >=5 sector slots); the
    # POST-DATA substance floor is enforced later by the swap phase (Phase 3.5).
    if min_sector_sections > 0:
        current = len(_distinct_sector_sections(sector_result))
        if current < min_sector_sections:
            await _backfill(
                sector_result, generic_names,
                need=min_sector_sections - current,
                model_config=backfill_model_config,
            )

    # Clean the reserve pool against the generics + final primary sections so a
    # swap can never inject a section that duplicates something already on the page.
    _clean_reserves(sector_result, generic_names)

    return sector_result, removed

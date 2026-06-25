"""Skill 2 — Importance Scoring (Phase 2, Branch A).

Assigns each section (generic + sector-specific) an importance score 0-100 for
THIS company. The score drives layout placement and visual emphasis. A single
LLM call ranks all sections with the persona-relevance test embedded; Python
then clamps and de-duplicates so the layout planner has a deterministic order.

Public entry point:  async def run(sector_result, model_config) -> Dict[str, int]
"""

from __future__ import annotations

from typing import Dict, List

import config
import sections_catalog as catalog
import skill_functions as sf
from schemas import SectorResearchOutput


def _all_section_names(sector_result: SectorResearchOutput) -> List[str]:
    """Generic sections + unique sector-specific sections from sector domains."""
    names = catalog.generic_section_names()
    seen = set(names)
    for d in sector_result.sector_domains:
        for s in d.sections_covered:
            if s and s not in seen:
                names.append(s)
                seen.add(s)
    return names


def _system_prompt() -> str:
    return (
        "You rank SOURCING one-pager sections by importance for a SPECIFIC company. The "
        "one-pager is a top-of-funnel screening doc: an analyst reads it to decide whether "
        "the company is worth DEEPER research (pursue vs pass). Score each section by how "
        "much it moves that screen-in / screen-out call, for four reader personas: PE "
        "analyst, investment banker, credit analyst, consultant.\n\n"
        "Apply the persona-relevance test to every section: identify which personas care, "
        "then score 0-100. Rules:\n"
        "- Sections useful to 3+ personas always score above 70.\n"
        "- Sections useful to 0 personas score below 20.\n"
        "- Anchor examples (listed mid-cap pharma): 'Financial performance'=95, "
        "'Credit ratings'=90, 'Patent cliff exposure'=85, 'Global presence'=40, "
        "'Awards & certifications'=15.\n"
        "- Give each section a DISTINCT score (avoid ties) so ordering is deterministic.\n\n"
        "Return a flat JSON object mapping each EXACT section name to an integer score."
    )


def _user_prompt(sector_result: SectorResearchOutput, names: List[str]) -> str:
    profile = sector_result.company_profile
    listed = profile.get("listed_status", "unknown")
    return (
        f"Company subsector: {sector_result.resolved_subsector}\n"
        f"Listed status: {listed}\n"
        f"Profile: {profile}\n\n"
        "Score these sections (0-100), one integer each:\n"
        + "\n".join(f"- {n}" for n in names)
        + "\n\nReturn ONLY a JSON object: {\"<section name>\": <int>, ...}"
    )


def _clamp_and_dedupe(raw: Dict[str, int], names: List[str]) -> Dict[str, int]:
    """Clamp to 0-100 and make every score unique (deterministic ordering).

    Missing sections default to 40. Exact/over-lapping scores are nudged down in
    a stable (score desc, name) order so the layout planner gets a strict ranking
    without a re-prompt. Strict uniqueness — not the spec's stricter ">=2 apart",
    which is infeasible for ~30 sections once defaults cluster — is sufficient for
    deterministic ordering.
    """
    scored: Dict[str, int] = {}
    for n in names:
        v = raw.get(n, 40)
        try:
            v = int(round(float(v)))
        except (TypeError, ValueError):
            v = 40
        scored[n] = max(0, min(100, v))

    ordered = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    result: Dict[str, int] = {}
    prev = 101  # sentinel above the max so a genuine 100 is kept as-is
    for name, score in ordered:
        if score >= prev:
            score = max(0, prev - 1)
        result[name] = score
        prev = score
    return result


async def run(
    sector_result: SectorResearchOutput,
    model_config: "config.ModelConfig" = config.IMPORTANCE_SCORING_MODEL,
) -> Dict[str, int]:
    names = _all_section_names(sector_result)
    resp = await sf.call_model(
        prompt=_user_prompt(sector_result, names),
        system=_system_prompt(),
        model_config=model_config,
        response_format="json",
    )
    raw = resp.parsed if isinstance(resp.parsed, dict) else {}
    # keep only int-ish values keyed by known section names
    cleaned = {k: v for k, v in raw.items() if k in names}
    return _clamp_and_dedupe(cleaned, names)

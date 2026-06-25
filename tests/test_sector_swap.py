"""Offline tests for the post-data sector swap (Phase 3.5) + the substance bar.

No model calls — `domain_sector_specific.run` is monkeypatched. Covers:
  * `_sector_status` bar boundaries + the sector-vs-generic branch in json_population
  * swap injection: a passing reserve lands in final.sections + plan.skeleton, its
    domain in domain_results, its sources in metadata with valid citation indices,
    and the swap_log records the drop + swap_in
  * bounding: all-failing reserves -> <= max_attempts research calls, floor_unmet
  * no-op when the floor is already met (reserves never researched)
  * sector_dedup cleans a reserve that lexically duplicates a generic

Run:  python3 tests/test_sector_swap.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import (  # noqa: E402
    Domain, DomainResearchOutput, PersonaRelevance, SectorResearchOutput, Source,
)
from skills import importance_scoring, json_population, layout_planning  # noqa: E402
from skills import sector_swap, sector_dedup  # noqa: E402
from skills.json_population import _sector_status  # noqa: E402


def _dro(domain_name, fields, completeness, *, url="https://ex.com/x"):
    """A DomainResearchOutput with `fields` (name->value) all attributed to one source."""
    src = Source(name="Example Filing", url=url, field_attributions=list(fields.keys()))
    return DomainResearchOutput(
        domain_name=domain_name, completeness=completeness, confidence="high",
        sources_used=[src],
        data={f: {"value": v} for f, v in fields.items()},
    )


def test_sector_status_bar() -> None:
    assert _sector_status(0, 0.5) == "populated"     # complete domain, no slice fields
    assert _sector_status(0, 0.0) == "unavailable"
    assert _sector_status(2, 0.2) == "unavailable"   # thin + incomplete -> fails bar
    assert _sector_status(3, 0.0) == "populated"     # >=3 fields clears it
    assert _sector_status(1, 0.3) == "populated"     # completeness clears it
    print("  [ok] _sector_status boundaries")


def test_json_population_branch() -> None:
    """Same thin data: a Sector section fails the bar (unavailable); a Generic
    section keeps the honest-partial behaviour."""
    # Sector section fed by a thin domain (2 fields, completeness 0.1).
    sr = SectorResearchOutput(
        resolved_subsector="x",
        company_profile={"resolved_sector": "consumer", "company_name": "Co"},
        sector_domains=[Domain(domain_name="Thin Sector Domain",
                               sections_covered=["Thin Sector Sec"],
                               is_sector_specific=True,
                               persona_relevance=PersonaRelevance(overall_score=80))],
    )
    names = importance_scoring._all_section_names(sr)
    scores = importance_scoring._clamp_and_dedupe({"Thin Sector Sec": 80}, names)
    plan = layout_planning.run(scores, sr)
    thin = _dro("Thin Sector Domain", {"a": "1", "b": "2"}, 0.1)
    final = json_population.run(plan.skeleton, plan, [thin], sr.company_profile)
    sec = next(s for s in final.sections if s.section_name == "Thin Sector Sec")
    assert sec.content["status"] == "unavailable", sec.content["status"]

    # A Generic section fed by the same thin shape (completeness 0.1) -> partial,
    # NOT dropped (generics keep the honest-partial behaviour).
    wc = _dro("Financials & Ratios", {"working capital days": "45"}, 0.1)
    final2 = json_population.run(plan.skeleton, plan, [wc], sr.company_profile)
    wcs = [s for s in final2.sections if s.section_tag == "Generic"
           and s.content.get("completeness") == 0.1 and
           sum(1 for v in (s.content.get("data") or {}).values()
               if json_population._found(v)) > 0]
    assert wcs and all(s.content["status"] == "partial" for s in wcs), \
        "generic thin section should be partial, not unavailable"
    print("  [ok] json_population: sector fails bar, generic stays partial")


def _swap_fixture(min_reserve_completeness=0.5, n_reserves=1, reserve_found=3):
    """sector_result whose single primary sector section has NO data (unavailable),
    plus `n_reserves` reserves the mock will fill."""
    profile = {"resolved_sector": "consumer", "company_name": "TestCo"}
    reserves = []
    for i in range(n_reserves):
        reserves.append(Domain(
            domain_name=f"Reserve Domain {i}",
            sections_covered=[f"Reserve Section {i}"],
            section_content_types={f"Reserve Section {i}": "stat_grid"},
            data_fields_needed=[f"r{i}_f{j}" for j in range(reserve_found)],
            is_sector_specific=True,
            persona_relevance=PersonaRelevance(overall_score=45),
        ))
    sr = SectorResearchOutput(
        resolved_subsector="x", company_profile=profile,
        sector_domains=[Domain(domain_name="Primary Domain",
                               sections_covered=["Primary Sector Sec"],
                               is_sector_specific=True,
                               persona_relevance=PersonaRelevance(overall_score=90))],
        reserve_domains=reserves,
    )
    names = importance_scoring._all_section_names(sr)
    scores = importance_scoring._clamp_and_dedupe({"Primary Sector Sec": 90}, names)
    plan = layout_planning.run(scores, sr)
    # No domain result for "Primary Domain" -> the sector section is unavailable.
    final = json_population.run(plan.skeleton, plan, [], profile)
    return sr, plan, final, profile


async def test_swap_injection() -> None:
    sr, plan, final, profile = _swap_fixture()
    calls = {"n": 0}

    async def fake_run(input_data, *a, **k):
        calls["n"] += 1
        rd = input_data["domain"]
        fields = {f: f"val{i}" for i, f in enumerate(rd.data_fields_needed)}
        return _dro(rd.domain_name, fields, 0.5)

    orig = sector_swap.domain_sector_specific.run
    sector_swap.domain_sector_specific.run = fake_run
    try:
        final2, plan2, domain_results2, swap_log = await sector_swap.run(
            final, plan, [], sr, profile, min_sector=1)
    finally:
        sector_swap.domain_sector_specific.run = orig

    # The reserve section is on the page, populated, Sector-tagged with the label.
    rsec = next((s for s in final2.sections if s.section_name == "Reserve Section 0"), None)
    assert rsec is not None, "reserve section not injected into final.sections"
    assert rsec.section_tag == "Sector" and rsec.section_tag_label == "Consumer"
    assert rsec.content["status"] == "populated"
    assert rsec.citations and all(0 <= i < len(final2.metadata["sources"]) for i in rsec.citations)
    # Injected into the skeleton (so presentation would pick it up) + domain_results.
    assert any(sl["section_name"] == "Reserve Section 0"
               for sl in plan2.skeleton["sections"]), "not added to skeleton"
    assert any(d.domain_name == "Reserve Domain 0" for d in domain_results2)
    # Swap log records the drop + swap_in.
    assert any(e["action"] == "drop" and e["section"] == "Primary Sector Sec" for e in swap_log)
    assert any(e["action"] == "swap_in" and "Reserve Section 0" in e["sections"] for e in swap_log)
    assert not any(e["action"] == "floor_unmet" for e in swap_log)  # min_sector=1 met
    print("  [ok] swap injection (section + skeleton + domain_results + citations + log)")


async def test_swap_bounding() -> None:
    """All reserves fail the bar -> <= max_attempts research calls, floor_unmet, no raise."""
    sr, plan, final, profile = _swap_fixture(n_reserves=6)
    calls = {"n": 0}

    async def fake_fail(input_data, *a, **k):
        calls["n"] += 1
        return _dro(input_data["domain"].domain_name, {}, 0.0)  # empty -> fails bar

    orig = sector_swap.domain_sector_specific.run
    sector_swap.domain_sector_specific.run = fake_fail
    try:
        final2, _plan2, _dr2, swap_log = await sector_swap.run(
            final, plan, [], sr, profile, min_sector=5, max_attempts=4)
    finally:
        sector_swap.domain_sector_specific.run = orig

    assert calls["n"] <= 4, f"researched {calls['n']} reserves, exceeds max_attempts"
    assert any(e["action"] == "floor_unmet" for e in swap_log)
    assert not any(e["action"] == "swap_in" for e in swap_log)
    print(f"  [ok] bounding ({calls['n']} <= 4 attempts, floor_unmet logged)")


async def test_swap_noop_when_floor_met() -> None:
    sr, plan, final, profile = _swap_fixture()
    calls = {"n": 0}

    async def fake_run(input_data, *a, **k):
        calls["n"] += 1
        return _dro("x", {}, 0.0)

    orig = sector_swap.domain_sector_specific.run
    sector_swap.domain_sector_specific.run = fake_run
    try:
        # min_sector=0 -> needed=0 -> reserves never researched.
        _f, _p, _d, swap_log = await sector_swap.run(
            final, plan, [], sr, profile, min_sector=0)
    finally:
        sector_swap.domain_sector_specific.run = orig
    assert calls["n"] == 0, "reserves researched even though floor was already met"
    print("  [ok] no-op when floor already met (0 research calls)")


def test_clean_reserves() -> None:
    """_clean_reserves (pure Python, no LLM) drops reserve sections that duplicate
    a generic OR a surviving primary section, and removes emptied reserve domains."""
    import sections_catalog as catalog
    generics = catalog.generic_section_names()
    sr = SectorResearchOutput(
        resolved_subsector="x",
        company_profile={"resolved_sector": "consumer", "company_name": "Co"},
        sector_domains=[Domain(domain_name="Primary",
                               sections_covered=["Primary Distinct KPI"],
                               is_sector_specific=True)],
        reserve_domains=[
            Domain(domain_name="Dup Generic", sections_covered=[generics[0]],
                   is_sector_specific=True),
            Domain(domain_name="Dup Primary", sections_covered=["Primary Distinct KPI"],
                   is_sector_specific=True),
            Domain(domain_name="Good Reserve",
                   sections_covered=["A Truly Novel Sector Metric"],
                   is_sector_specific=True),
        ],
    )
    sector_dedup._clean_reserves(sr, generics)
    surviving = [d.domain_name for d in sr.reserve_domains]
    assert surviving == ["Good Reserve"], surviving
    print("  [ok] _clean_reserves drops generic/primary-duplicate reserves")


def main() -> None:
    print("Offline sector-swap checks:")
    test_sector_status_bar()
    test_json_population_branch()
    asyncio.run(test_swap_injection())
    asyncio.run(test_swap_bounding())
    asyncio.run(test_swap_noop_when_floor_met())
    test_clean_reserves()
    print("\nAll sector-swap offline tests passed.")


if __name__ == "__main__":
    main()

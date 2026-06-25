"""Phase E test — JSON Population + Wave 2 synthesis.

Offline (no key): population fills/partials/unavailables correctly, citations
index into metadata.sources, synthesis sections stay pending then attach.

Live (needs GEMINI_API_KEY): Sector Research → Importance → Layout on Embio, a
reduced Wave 1 (4 generic + 1 sector domain), JSON Population, then the 3
synthesis skills, attach, and print the assembled result. Records spend to the
persistent lifetime ledger.

Run: python test_phase_e.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import skill_functions as sf
from schemas import (
    Domain, DomainResearchOutput, PersonaRelevance, PipelineInput,
    SectorResearchOutput, Source,
)
from skills import (
    domain_credit_risk, domain_financials, domain_market_position,
    domain_sector_specific, domain_track_record, importance_scoring,
    json_population, layout_planning, sector_research as sr,
    synthesis_future_plan, synthesis_investment_thesis, synthesis_swot,
)


def _synthetic_sector_result() -> SectorResearchOutput:
    return SectorResearchOutput(
        resolved_subsector="controlled-substance API manufacturer",
        company_profile={"resolved_sector": "pharma", "company_name": "Embio Limited",
                         "business_description": "Controlled-substance API maker."},
        sector_domains=[Domain(domain_name="Regulatory & Manufacturing",
                               sections_covered=["Regulatory inspection history"],
                               is_sector_specific=True,
                               persona_relevance=PersonaRelevance(overall_score=90))],
    )


def test_offline() -> None:
    sr_out = _synthetic_sector_result()
    names = importance_scoring._all_section_names(sr_out)
    scores = importance_scoring._clamp_and_dedupe({}, names)
    plan = layout_planning.run(scores, sr_out)

    # Synthetic Wave 1: Credit complete, Financials sparse, Market Position missing.
    domain_results = [
        DomainResearchOutput(
            domain_name="Credit & Risk", completeness=1.0, confidence="high",
            data={"current LT and ST credit ratings + agency + outlook":
                  {"value": "CARE BBB+; Stable", "source": "CARE Ratings"}},
            sources_used=[Source(name="CARE Ratings", url="http://care.example",
                                 field_attributions=["ratings"])],
        ),
        DomainResearchOutput(
            domain_name="Financials & Ratios", completeness=0.1, confidence="low",
            data={"5-year revenue": {"value": "Not Available", "source": None}},
        ),
    ]

    final = json_population.run(plan.skeleton, plan, domain_results, sr_out.company_profile)
    by_name = {s.section_name: s for s in final.sections}

    assert by_name["Credit ratings"].content["status"] == "populated"
    assert by_name["Credit ratings"].citations == [0], by_name["Credit ratings"].citations
    assert by_name["Key stats bar"].content["status"] in ("partial", "unavailable")
    assert by_name["Peers comparison"].content["status"] == "unavailable"  # domain absent
    assert by_name["Investment thesis"].content["status"] == "pending_synthesis"
    assert final.metadata["sources"][0]["name"] == "CARE Ratings"
    assert final.company_header["name"] == "Embio Limited"
    print("  [ok] population (populated/partial/unavailable, citations index sources, "
          "synthesis pending)")

    final = json_population.attach_synthesis(
        final,
        thesis={"thesis": [{"heading": "Moat", "text": "x"}]},
        swot={"strengths": ["s"], "weaknesses": [], "opportunities": [], "threats": []},
        future={"targets": [{"target": "₹300 Cr", "timeline": "FY26"}]},
    )
    by_name = {s.section_name: s for s in final.sections}
    assert by_name["Investment thesis"].content["status"] == "populated"
    assert by_name["SWOT"].content["strengths"] == ["s"]
    assert by_name["Future plan"].content["targets"][0]["timeline"] == "FY26"
    print("  [ok] attach_synthesis (thesis / SWOT / future plan filled)")

    digest = synthesis_base_digest(domain_results)
    assert "CARE BBB+" in digest and "Not Available" not in digest
    print("  [ok] synthesis digest (includes found data, omits 'Not Available')")


def synthesis_base_digest(domain_results):
    from skills import synthesis_base
    wave = {d.domain_name: d for d in domain_results}
    return synthesis_base.digest(wave)


async def test_live() -> None:
    if not config.GEMINI_API_KEY:
        print("  [skip] no GEMINI_API_KEY — live Phase E run not executed")
        return
    sf.reset_cost_ledger()

    inp = PipelineInput(company_name="Embio Limited", sector="pharma",
                        business_description="Controlled-substance API and intermediates manufacturer in India")
    print("\n  Sector Research → Importance → Layout (Embio)…")
    sector_result = await sr.run(inp)
    profile = {**sector_result.company_profile, "company_name": "Embio Limited",
               "business_description": inp.business_description}
    scores = await importance_scoring.run(sector_result)
    plan = layout_planning.run(scores, sector_result)

    print("  Wave 1 (reduced: 4 generic + 1 sector domain)…")
    base = {"company_name": "Embio Limited", "company_profile": profile}
    generic = await asyncio.gather(
        domain_financials.run(base), domain_credit_risk.run(base),
        domain_market_position.run(base), domain_track_record.run(base),
    )
    wave_1 = list(generic)
    if sector_result.sector_domains:
        ss = await domain_sector_specific.run({**base, "domain": sector_result.sector_domains[0]})
        wave_1.append(ss)
    for d in wave_1:
        print(f"    • {d.domain_name}: completeness={d.completeness} {d.confidence}")

    print("  JSON Population…")
    final = json_population.run(plan.skeleton, plan, wave_1, profile)
    m = final.metadata
    print(f"    populated={m['sections_populated']} partial={m['sections_partial']} "
          f"unavailable={m['sections_unavailable']} sources={len(m['sources'])}")

    print("  Wave 2 synthesis (thesis / SWOT / future)…")
    wave_1_map = {d.domain_name: d for d in wave_1}
    syn_in = {"company_profile": profile, "wave_1_results": wave_1_map}
    thesis, swot, future = await asyncio.gather(
        synthesis_investment_thesis.run(syn_in),
        synthesis_swot.run(syn_in),
        synthesis_future_plan.run(syn_in),
    )
    final = json_population.attach_synthesis(final, thesis, swot, future)

    print("\n  --- Investment thesis ---")
    for b in thesis.get("thesis", []):
        print(f"    • {b.get('heading')}: {b.get('text', '')[:110]}")
    print("  --- SWOT (first bullet each) ---")
    for q in ("strengths", "weaknesses", "opportunities", "threats"):
        bullets = swot.get(q, [])
        print(f"    {q}: {bullets[0][:90] if bullets else '—'}")
    print("  --- Future plan ---")
    for t in future.get("targets", [])[:7]:
        print(f"    • {t.get('target')} [{t.get('timeline')}]")

    led = sf.record_run_to_ledger("Phase E live (Embio: research + synthesis)")
    print(f"\n  this run: ${sf.get_cumulative_cost():.6f} | "
          f"LIFETIME: ${led['lifetime_cost_usd']:.6f} over {led['lifetime_calls']} calls")


def main() -> None:
    print("Offline logic checks:")
    test_offline()
    print("\nLive Phase E run:")
    asyncio.run(test_live())
    print("\nPhase E test complete.")


if __name__ == "__main__":
    main()

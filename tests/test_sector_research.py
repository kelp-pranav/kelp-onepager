"""Phase C test — Sector Research.

Offline part (no key): taxonomy loads, alias matching works, the 6 generic
domains build and validate, and the output schema is well-formed.

Live part (needs ANTHROPIC_API_KEY): runs the skill against the 3 spec test
companies and prints the resolved subsector + proposed sector domains so the
output can be eyeballed for quality. Skipped cleanly if no key.

Run: python test_sector_research.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import skill_functions as sf
from schemas import PipelineInput, SectorResearchOutput
from skills import sector_research as sr


def test_offline() -> None:
    tax = sr._load_taxonomies()
    assert "pharma" in tax and "banking" in tax, "taxonomy failed to load"
    assert len(tax.get("energy", {}).get("baseline_domains", [])) >= 3
    print(f"  [ok] taxonomy loaded ({len([k for k in tax if not k.startswith('_')])} sectors)")

    assert sr._match_taxonomy("Pharmaceuticals", tax) == "pharma"
    assert sr._match_taxonomy("IT Services", tax) == "tech"
    assert sr._match_taxonomy("NBFC", tax) == "banking"
    assert sr._match_taxonomy("nonsense-sector", tax) is None
    print("  [ok] alias matching (Pharmaceuticals→pharma, IT Services→tech, NBFC→banking)")

    generics = sr._build_generic_domains()
    assert len(generics) == 6, generics
    assert all(not g.is_sector_specific for g in generics)
    names = {g.domain_name for g in generics}
    assert "Financials & Ratios" in names and "Credit & Risk" in names
    print(f"  [ok] 6 generic domains build ({', '.join(g.domain_name for g in generics)})")

    # full output object validates
    out = SectorResearchOutput(resolved_subsector="test", generic_domains=generics)
    assert out.total_estimated_sections == 0  # not computed until run()
    print("  [ok] SectorResearchOutput validates")


def _summarize(label: str, out: SectorResearchOutput) -> None:
    print(f"\n  === {label} ===")
    print(f"  subsector: {out.resolved_subsector}")
    print(f"  profile:   {out.company_profile}")
    print(f"  generic domains: {len(out.generic_domains)} | sector domains: {len(out.sector_domains)}")
    for d in out.sector_domains:
        print(f"    • {d.domain_name} [{d.priority_hint}] score={d.persona_relevance.overall_score}")
        print(f"        fields:  {', '.join(d.data_fields_needed[:4])}{'...' if len(d.data_fields_needed) > 4 else ''}")
        print(f"        sources: {', '.join(d.recommended_sources[:3])}")
    print(f"  total_estimated_sections: {out.total_estimated_sections}")


async def test_live() -> None:
    if not config.GEMINI_API_KEY:
        print("  [skip] no GEMINI_API_KEY — live iteration against Embio/Cipla/HDFC not run")
        return
    sf.reset_cost_ledger()
    cases = [
        ("Embio Limited", PipelineInput(company_name="Embio Limited", sector="pharma",
                                        business_description="Controlled-substance API and intermediates manufacturer in India")),
        ("Cipla", PipelineInput(company_name="Cipla", sector="pharma")),
        ("HDFC Bank", PipelineInput(company_name="HDFC Bank", sector="banking")),
    ]
    for label, inp in cases:
        before = sf.get_cumulative_cost()
        try:
            out = await sr.run(inp)
            _summarize(label, out)
            print(f"  cost for {label}: ${sf.get_cumulative_cost() - before:.6f}")
        except Exception as exc:
            print(f"  [FAIL] {label}: {exc}")

    summary = sf.cost_summary()
    print("\n  === cumulative cost ===")
    print(f"  total: ${summary['total_cost_usd']:.6f} over {summary['calls']} calls "
          f"({summary['grounded_calls']} grounded)")
    if summary["grounding_caveat"]:
        print(f"  note: {summary['grounding_caveat']}")


def main() -> None:
    print("Offline structural checks:")
    test_offline()
    print("\nLive sector-research runs:")
    asyncio.run(test_live())
    print("\nPhase C test complete.")


if __name__ == "__main__":
    main()

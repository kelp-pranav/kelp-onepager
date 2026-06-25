"""Phase F test — Data Validation.

Builds a realistic FinalJSON for Embio (real figures), runs validation, and
writes the validated one-pager JSON to output/. Validation's SWOT-alignment is
the only model call (skipped gracefully without a key).

Run: python test_phase_f.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import skill_functions as sf
from schemas import (
    Domain, DomainResearchOutput, PersonaRelevance, SectorResearchOutput, Source,
)
from skills import (
    data_validation, importance_scoring, json_population,
    layout_planning,
)


def _embio_inputs():
    sr_out = SectorResearchOutput(
        resolved_subsector="controlled-substance API & intermediates manufacturer",
        company_profile={
            "resolved_sector": "pharma", "company_name": "Embio Limited",
            "listed_status": "Unlisted Public (PE-backed)", "founded": "1986",
            "hq": "Mumbai, India", "cin": "U24110MH1986PLC038680",
            "business_description": ("Embio Limited, founded 1986 in Mumbai, manufactures "
                "controlled-substance APIs, chiral intermediates and specialty chemicals via "
                "fermentation and chiral chemistry at USFDA/WHO-GMP facilities, exporting to ~80 "
                "countries. It leads in licensed Schedule-1/2 controlled-substance APIs."),
        },
        sector_domains=[Domain(domain_name="Regulatory & Manufacturing",
                               sections_covered=["Regulatory inspection history", "Manufacturing & facilities"],
                               is_sector_specific=True,
                               persona_relevance=PersonaRelevance(overall_score=95))],
    )
    care = Source(name="CARE Ratings, Oct 2025", url="https://www.careratings.com/embio",
                  field_attributions=["credit ratings"])
    web = Source(name="Embio Annual Report FY25", url="https://embio.co.in/ar25",
                 field_attributions=["revenue", "EBITDA"])

    domain_results = [
        DomainResearchOutput(
            domain_name="Financials & Ratios", completeness=0.9, confidence="high",
            sources_used=[web],
            data={
                "5-year revenue": {"value": {"FY25": "₹272.85 Cr", "FY24": "₹180.84 Cr", "FY23": "₹207.88 Cr"}},
                "EBITDA": {"value": {"FY25": "₹69.16 Cr", "FY24": "₹17.76 Cr"}},
                "PAT": {"value": {"FY25": "₹39.05 Cr", "FY24": "₹4.47 Cr"}},
                "EBITDA margin": {"value": "25.35% (FY25)"},
                "interest coverage": {"value": "28.46x (FY25)"},
            },
        ),
        DomainResearchOutput(
            domain_name="Credit & Risk", completeness=1.0, confidence="high",
            sources_used=[care],
            data={
                "current LT and ST credit ratings + agency + outlook": {"value": "CARE BBB+ / Stable (LT), CARE A2 (ST)"},
                "4-year rating history": {"value": [
                    {"date": "Oct 2025", "rating": "BBB+/Stable"},
                    {"date": "FY2023", "rating": "A-/Negative"}]},
                "risk flags": {"value": ["Controlled-substance diversion compliance (amber)",
                                          "Customer concentration in top-10 pharma (amber)"]},
            },
        ),
        DomainResearchOutput(
            domain_name="Market Position", completeness=0.8, confidence="high",
            sources_used=[web],
            data={
                "top 5-8 peers with revenue + EBITDA margin + focus": {"value": [
                    {"name": "Divi's Laboratories", "focus": "APIs/intermediates"},
                    {"name": "Laurus Labs", "focus": "APIs/CDMO"}]},
                "full product list": {"value": ["Ephedrine", "Pseudoephedrine", "Chiral intermediates"]},
            },
        ),
    ]
    return sr_out, domain_results


async def main() -> None:
    sf.reset_cost_ledger()
    sr_out, domain_results = _embio_inputs()
    profile = sr_out.company_profile

    names = importance_scoring._all_section_names(sr_out)
    scores = importance_scoring._clamp_and_dedupe({
        "Business description": 99, "Investment thesis": 97, "Key stats bar": 96,
        "Financial performance chart": 94, "Credit ratings": 92, "SWOT": 90,
        "Risk flags": 85, "Peers comparison": 80, "Products & services": 75,
        "Regulatory inspection history": 93, "Future plan": 70, "Details": 88,
    }, names)
    plan = layout_planning.run(scores, sr_out)

    final = json_population.run(plan.skeleton, plan, domain_results, profile)
    final = json_population.attach_synthesis(
        final,
        thesis={"thesis": [
            {"heading": "Competitive moat", "text": "Leadership in licensed Schedule-1/2 controlled-substance APIs at USFDA/WHO-GMP facilities — a high regulatory barrier."},
            {"heading": "Current story", "text": "FY25 revenue +50.5% to ₹272.85 Cr with EBITDA margin expanding to 25.35%; CARE upgraded to BBB+/Stable."},
            {"heading": "Forward opportunity", "text": "Dahej capacity expansion plus True North growth capital position the company for export-led scale-up."}]},
        swot={"strengths": ["FY25 EBITDA margin 25.35%, up from ~10%", "CARE BBB+/Stable, upgraded from A-/Negative"],
              "weaknesses": ["Customer concentration in top-10 pharma majors"],
              "opportunities": ["Dahej expansion; ~80-country export base"],
              "threats": ["Controlled-substance regulatory/diversion risk"]},
        future={"targets": [
            {"target": "Commission Dahej facility", "timeline": "FY26"},
            {"target": "Scale controlled-substance API exports", "timeline": "FY27"}]},
        risk_flags={"data": {"flags": [
            {"severity": "amber", "flag": "Customer concentration in top-10 pharma majors"},
            {"severity": "red", "flag": "Controlled-substance diversion / regulatory risk"}]},
            "confidence": "medium"},
    )

    wave_1_map = {d.domain_name: d for d in domain_results}
    final, report = await data_validation.run(final, wave_1_map)

    print("Validation report:")
    print(f"  unit_issues: {report['unit_issues']}")
    print(f"  date_issues: {report['date_issues']}")
    print(f"  missing_citations: {report['missing_citations']}")
    print(f"  completeness_actions: {report['completeness_actions']}")
    print(f"  swot_alignment: {report['swot_alignment']}")

    payload = final.model_dump_json(indent=2)

    # Assertions on the validated JSON
    assert "pending_synthesis" not in payload, "synthesis placeholder leaked into JSON"
    assert "CARE BBB+" in payload, "credit rating missing from JSON"
    assert final.metadata["sections_populated"] > 0, "no sections populated"

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(config.OUTPUT_DIR, "Embio_Limited_sample.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(payload)

    m = final.metadata
    print(f"\n  [ok] JSON written: {len(payload):,} bytes -> {out_path}")
    print(f"  sections populated={m['sections_populated']} partial={m['sections_partial']} "
          f"unavailable={m['sections_unavailable']} | sources={len(m['sources'])}")

    led = sf.record_run_to_ledger("Phase F (validation + compile, Embio sample)")
    print(f"\n  this run: ${sf.get_cumulative_cost():.6f} | "
          f"LIFETIME: ${led['lifetime_cost_usd']:.6f} over {led['lifetime_calls']} calls")


if __name__ == "__main__":
    asyncio.run(main())

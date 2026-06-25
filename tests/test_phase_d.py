"""Phase D test — Importance Scoring, Layout Planning, domain skills.

Offline (no key): score clamping/dedupe, section-list assembly, and the
pure-Python layout rules (column split, hard top-ordering, sector tagging,
skeleton shape).

Live (needs GEMINI_API_KEY): runs Sector Research → Importance Scoring →
Layout Planning on Embio, then exercises 3 domain skills (Financials,
Credit & Risk, and the first sector-specific domain) in isolation. Prints
per-step + cumulative cost.

Run: python test_phase_d.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import sections_catalog as catalog
import skill_functions as sf
from schemas import Domain, PersonaRelevance, PipelineInput, SectorResearchOutput
from skills import (
    domain_credit_risk,
    domain_financials,
    domain_sector_specific,
    importance_scoring,
    layout_planning,
    sector_research as sr,
)


def _synthetic_sector_result() -> SectorResearchOutput:
    return SectorResearchOutput(
        resolved_subsector="controlled-substance API manufacturer",
        company_profile={"resolved_sector": "pharma", "listed_status": "Unlisted"},
        generic_domains=[],
        sector_domains=[
            Domain(
                domain_name="Regulatory & Manufacturing",
                sections_covered=["Regulatory inspection history", "Manufacturing & facilities"],
                is_sector_specific=True,
                persona_relevance=PersonaRelevance(overall_score=90),
            )
        ],
    )


def test_offline() -> None:
    sr_out = _synthetic_sector_result()

    names = importance_scoring._all_section_names(sr_out)
    assert "Key stats bar" in names and "Credit ratings" in names
    assert "Regulatory inspection history" in names  # sector section appended
    assert "Manufacturing & facilities" in names
    print(f"  [ok] section list assembled ({len(names)} sections: generics + sector)")

    # clamp + dedupe: out-of-range clamped, missing defaulted, no two within 2
    raw = {"Key stats bar": 150, "SWOT": -5, "Recent news": 80, "Market size": 80}
    scored = importance_scoring._clamp_and_dedupe(raw, names)
    assert scored["Key stats bar"] == 100, scored["Key stats bar"]  # 150 clamped, kept as top
    assert scored["SWOT"] == 0, scored["SWOT"]                      # -5 clamped
    vals = list(scored.values())
    assert len(set(vals)) == len(vals), "scores not unique"          # deterministic ordering
    assert all(0 <= v <= 100 for v in vals)
    print("  [ok] clamp+dedupe (clamps range, defaults missing, unique scores)")

    # layout rules
    plan = layout_planning.run(scored, sr_out)
    left = [s for s in plan.sections if s.column == "left"]
    right = [s for s in plan.sections if s.column == "right"]
    left_names = [s.section_name for s in sorted(left, key=lambda s: s.order_in_column)]
    right_names = [s.section_name for s in sorted(right, key=lambda s: s.order_in_column)]
    assert left_names[:3] == ["Key stats bar", "Business description", "Products & services"], left_names[:3]
    assert right_names[0] == "Details", right_names[0]
    print(f"  [ok] layout hard-ordering (left top-3 fixed, right starts with Details)")

    # sector tagging in skeleton
    sk = {s["section_name"]: s for s in plan.skeleton["sections"]}
    assert sk["Regulatory inspection history"]["section_tag"] == "Sector"
    assert sk["Regulatory inspection history"]["section_tag_label"] == "Pharma"
    assert sk["Key stats bar"]["section_tag"] == "Generic"
    assert sk["Key stats bar"]["populated"] is False
    assert sk["Key stats bar"]["content"] == {}
    print("  [ok] skeleton (sector→blue 'Pharma' tag, generic→'Generic', slots empty)")


async def test_live() -> None:
    if not config.GEMINI_API_KEY:
        print("  [skip] no GEMINI_API_KEY — live Phase D run not executed")
        return
    sf.reset_cost_ledger()

    inp = PipelineInput(company_name="Embio Limited", sector="pharma",
                        business_description="Controlled-substance API and intermediates manufacturer in India")
    print("\n  Sector Research (Embio)…")
    sector_result = await sr.run(inp)
    print(f"    subsector: {sector_result.resolved_subsector}")
    print(f"    sector domains: {[d.domain_name for d in sector_result.sector_domains]}")

    print("\n  Importance Scoring…")
    scores = await importance_scoring.run(sector_result)
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:8]
    print("    top 8: " + ", ".join(f"{n}={s}" for n, s in top))

    print("\n  Layout Planning (pure Python)…")
    plan = layout_planning.run(scores, sector_result)
    left = sorted([s for s in plan.sections if s.column == "left"], key=lambda s: s.order_in_column)
    right = sorted([s for s in plan.sections if s.column == "right"], key=lambda s: s.order_in_column)
    print(f"    LEFT  ({len(left)}): " + " > ".join(s.section_name for s in left[:6]) + " …")
    print(f"    RIGHT ({len(right)}): " + " > ".join(s.section_name for s in right[:6]) + " …")
    sector_tagged = [s["section_name"] for s in plan.skeleton["sections"] if s["section_tag"] == "Sector"]
    print(f"    sector-tagged sections: {sector_tagged}")

    profile = {**sector_result.company_profile, "company_name": "Embio Limited"}
    base_in = {"company_name": "Embio Limited", "company_profile": profile}

    print("\n  Domain skills in isolation…")
    fin = await domain_financials.run(base_in)
    _show_domain(fin)
    cr = await domain_credit_risk.run(base_in)
    _show_domain(cr)
    if sector_result.sector_domains:
        ss = await domain_sector_specific.run({**base_in, "domain": sector_result.sector_domains[0]})
        _show_domain(ss)

    summary = sf.cost_summary()
    print("\n  === cumulative cost (Phase D live) ===")
    print(f"  total: ${summary['total_cost_usd']:.6f} over {summary['calls']} calls "
          f"({summary['grounded_calls']} grounded)")
    if summary["grounding_caveat"]:
        print(f"  note: {summary['grounding_caveat']}")


def _show_domain(out) -> None:
    print(f"    • {out.domain_name}: completeness={out.completeness} confidence={out.confidence} "
          f"sources={len(out.sources_used)}")
    sample = list(out.data.items())[:3]
    for field, entry in sample:
        val = entry.get("value") if isinstance(entry, dict) else entry
        val_str = str(val)
        print(f"        {field}: {val_str[:80]}{'…' if len(val_str) > 80 else ''}")
    if out.warnings:
        print(f"        warnings: {out.warnings}")


def main() -> None:
    print("Offline logic checks:")
    test_offline()
    print("\nLive Phase D run:")
    asyncio.run(test_live())
    print("\nPhase D test complete.")


if __name__ == "__main__":
    main()

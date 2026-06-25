"""Run ONLY the sector-research skill (Phase 1) for a company and print the
sector-specific sections it proposes. For iterating on selection quality.

    python scripts/run_sector_research.py "Embio Limited"
    python scripts/run_sector_research.py "Embio Limited" --sector pharma

Does NOT run the full workflow, dedup, or data fetching — just the skill that
decides WHICH sector sections get researched. Logs cost to API_EXPENSES.md.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skill_functions as sf  # noqa: E402
from schemas import PipelineInput  # noqa: E402
from skills import sector_research  # noqa: E402


async def main() -> None:
    args = [a for a in sys.argv[1:]]
    sector = None
    if "--sector" in args:
        i = args.index("--sector")
        sector = args[i + 1]
        del args[i:i + 2]
    company = args[0] if args else "Embio Limited"

    before = sf.snapshot()
    inp = PipelineInput(company_name=company, sector=sector)
    # documents="" on purpose — don't contaminate with whatever is in input/.
    out = await sector_research.run(inp, documents="")

    print("\n" + "=" * 72)
    print(f"SECTOR RESEARCH — {company}")
    print("=" * 72)
    print(f"resolved_sector    : {out.company_profile.get('resolved_sector')}")
    print(f"resolved_subsector : {out.resolved_subsector}")
    desc = out.company_profile.get("business_description") or ""
    print(f"business_description: {desc[:300]}")

    print(f"\nSECTOR-SPECIFIC DOMAINS proposed: {len(out.sector_domains)}")
    for d in out.sector_domains:
        rel = d.persona_relevance
        personas = [p for p, v in (
            ("PE", rel.pe_analyst), ("Banker", rel.banker),
            ("Credit", rel.credit_analyst), ("Consultant", rel.consultant),
        ) if v]
        print("\n  " + "-" * 68)
        print(f"  DOMAIN: {d.domain_name}   [priority={d.priority_hint}, "
              f"overall_score={rel.overall_score}]")
        print(f"    personas served : {', '.join(personas) or '(none stated)'}")
        print(f"    sections_covered: {d.sections_covered}")
        print(f"    data_fields ({len(d.data_fields_needed)}): {d.data_fields_needed}")
        print(f"    sources         : {d.recommended_sources}")

    # Flat list of every distinct sector section (what the user asked for).
    seen, sections = set(), []
    for d in out.sector_domains:
        for s in d.sections_covered:
            if s and s not in seen:
                seen.add(s)
                sections.append(s)
    print("\n" + "=" * 72)
    print(f"ALL SECTOR-SPECIFIC SECTIONS ({len(sections)}):")
    for s in sections:
        print(f"  • {s}")
    print("=" * 72)

    after = sf.snapshot()
    run_cost = round(after[0] - before[0], 6)
    calls = after[1] - before[1]
    grounded = after[2] - before[2]
    total = sf.append_expense_md(f"{company} (sector_research only)", run_cost,
                                 calls=calls, grounded=grounded)
    print(f"\ncost: ${run_cost:.6f} over {calls} call(s) ({grounded} grounded) | "
          f"logged. lifetime on key: ${total:.6f}")


if __name__ == "__main__":
    asyncio.run(main())

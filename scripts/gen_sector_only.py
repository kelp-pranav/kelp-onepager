"""Lightweight SECTOR-ONLY one-pager generator.

Researches ONLY the sector-specific sections — skips all 6 generic domains
(Financials & Ratios, Corporate Structure, Market Position, Track Record,
Credit & Risk, Geography) and skips Wave 2 synthesis (Investment Thesis, SWOT,
Future Plan, Risk Flags) entirely, since those are generic-only outputs.

Usage:
    python3 gen_sector_only.py "<Company Name>" ["<business description hint>"]

Pipeline run here:
    Phase 1   sector_research.run_with_rejected  (select sector domains + reasoning)
    research  domain_sector_specific             (ONLY the kept sector domains, parallel)

Output: output/<slug>_sector_only.json — a plain dict with the chosen sections,
the domains considered-but-not-chosen (with reasoning), and an actual + estimated
cost breakdown. Auto-logs cost to the ledger + API_EXPENSES.md.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict

# Run from the project root: `python3 scripts/gen_sector_only.py ...`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import skill_functions as sf  # noqa: E402
from orchestrator import _build_profile  # noqa: E402
from schemas import DomainResearchOutput, PipelineInput  # noqa: E402
from skills import domain_sector_specific, fact_check, sector_research  # noqa: E402

_EMPTY_VALUES = {None, "", "not available", "n/a", "na", "none", "-"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "company"


def _fmt_value(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _entry_sources(entry) -> list:
    """Per-field source list as [{name, url}] — so every datum can be verified."""
    out = []
    if isinstance(entry, dict):
        raw = entry.get("sources")
        if isinstance(raw, list):
            for s in raw:
                if isinstance(s, dict) and (s.get("name") or s.get("url")):
                    out.append({"name": s.get("name"), "url": s.get("url")})
        if not out and (entry.get("source") or entry.get("source_url")):
            out.append({"name": entry.get("source"), "url": entry.get("source_url")})
    return out


def _found_fields(out) -> list:
    """Found (non-empty) fields as [(label, value, sources)] — value kept STRUCTURED
    (dict/list/scalar) and sources = [{name, url}] backing that specific field."""
    if not isinstance(out, DomainResearchOutput):
        return []
    pairs = []
    for field, entry in out.data.items():
        value = entry.get("value") if isinstance(entry, dict) else entry
        if value is None or (isinstance(value, str) and value.strip().lower() in _EMPTY_VALUES):
            continue
        pairs.append((field, value, _entry_sources(entry)))
    return pairs


def _reasoning(pr) -> dict:
    return {
        "overall_score": pr.overall_score,
        "pe_analyst": pr.pe_analyst,
        "banker": pr.banker,
        "credit_analyst": pr.credit_analyst,
        "consultant": pr.consultant,
    }


async def main(company: str, description: str) -> None:
    before = sf.snapshot()                       # t0 — cost/call/grounded snapshot
    wall0 = time.perf_counter()
    inp = PipelineInput(company_name=company, business_description=description or None)

    print(f"Running {company} (SECTOR-ONLY — no generic domains, no synthesis)…\n")

    # PHASE 0 — load local ground-truth documents from input/
    documents, doc_names = sf.load_input_documents()
    if doc_names:
        print(f"  ground-truth docs: {', '.join(doc_names)}")

    # PHASE 1 — Sector Research (also returns the rejected candidates + reasoning)
    t = time.perf_counter()
    sector_result, rejected = await sector_research.run_with_rejected(inp, documents=documents)
    profile = _build_profile(sector_result, inp)
    snap1 = sf.snapshot()
    phase1_cost = snap1[0] - before[0]
    print(f"  Phase1 sector research: {time.perf_counter()-t:.1f}s | "
          f"subsector: {sector_result.resolved_subsector} | "
          f"kept sector domains: {len(sector_result.sector_domains)} | "
          f"rejected: {len(rejected)} | cost ${phase1_cost:.6f}")

    # RESEARCH — ONLY the kept sector domains (no generic domains), in parallel.
    t = time.perf_counter()
    base = {"company_name": profile["company_name"], "company_profile": profile,
            "documents": documents}
    skills = [domain_sector_specific.run for _ in sector_result.sector_domains]
    inputs = [{**base, "domain": d} for d in sector_result.sector_domains]
    names = [d.domain_name for d in sector_result.sector_domains]
    raw = await sf.run_skills_in_parallel(
        skills=skills, inputs=inputs,
        max_concurrent=config.WAVE1_MAX_CONCURRENT,
        per_skill_timeout=config.WAVE1_PER_SKILL_TIMEOUT_SECONDS,
        skill_names=names,
    )
    snap2 = sf.snapshot()
    domain_research_cost = snap2[0] - snap1[0]
    n_ok = sum(1 for r in raw if isinstance(r, DomainResearchOutput))
    print(f"  Domain research (parallel): {time.perf_counter()-t:.1f}s | "
          f"domains succeeded: {n_ok}/{len(raw)} | cost ${domain_research_cost:.6f}")
    for r in raw:
        if not isinstance(r, DomainResearchOutput):
            print(f"    [!] {getattr(r, 'skill_name', 'domain')}: {getattr(r, 'error', 'failed')}")

    # ACCURACY VERIFICATION — re-confirm each found field against primary sources (grounded
    # Flash-Lite, one batched call per domain, bounded concurrency). Annotates, never rewrites.
    verdicts_by_domain: Dict[int, Dict[str, Dict[str, str]]] = {}
    verify_cost = 0.0
    if config.ACCURACY_VERIFY:
        t = time.perf_counter()
        sem = asyncio.Semaphore(config.WAVE1_MAX_CONCURRENT)

        async def _verify(i, domain, out):
            research = out if isinstance(out, DomainResearchOutput) else None
            fp = _found_fields(research)
            if not fp:
                return i, {}
            async with sem:
                v = await fact_check.verify_fields(
                    profile["company_name"], domain.domain_name, fp)
            return i, v

        pairs = await asyncio.gather(*[
            _verify(i, d, o)
            for i, (d, o) in enumerate(zip(sector_result.sector_domains, raw))
        ])
        verdicts_by_domain = {i: v for i, v in pairs}
        snap_v = sf.snapshot()
        verify_cost = snap_v[0] - snap2[0]
        nflag = sum(1 for v in verdicts_by_domain.values() for rec in v.values()
                    if rec.get("verdict") not in ("confirmed", "unchecked"))
        print(f"  Accuracy verify: {time.perf_counter()-t:.1f}s | "
              f"flagged {nflag} field(s) | cost ${verify_cost:.6f}")

    # Build the lightweight sections list — one entry per (domain, section_name) pair.
    sections = []
    for di, (domain, out) in enumerate(zip(sector_result.sector_domains, raw)):
        research = out if isinstance(out, DomainResearchOutput) else None
        field_pairs = _found_fields(research)
        verdicts = verdicts_by_domain.get(di, {})
        # Structured fields (with per-field sources, plus accuracy verdict) + bullets.
        fields = []
        for label, value, sources in field_pairs:
            rec = verdicts.get(label) or {}
            verdict = rec.get("verdict", "unchecked")
            fields.append({
                "label": label, "value": value, "sources": sources,
                "verified": (verdict == "confirmed") if verdict != "unchecked" else None,
                "verdict": verdict,           # confirmed|corrected|unsupported|scope_mismatch|unchecked
                "verify_note": rec.get("note", ""),
            })
        bullets = [f"{label}: {_fmt_value(value)}" for label, value, _ in field_pairs]
        # Data points we planned to fetch but the research could NOT find — surfaced so
        # the analyst sees the gaps (and knows what to chase in deeper diligence).
        found_labels = {label for label, _, _ in field_pairs}
        planned = list(domain.data_fields_needed)
        missing_fields = [f for f in planned if f not in found_labels]
        # Deduped section-level reference list (name + url), from the domain's sources.
        references, seen = [], set()
        for s in (research.sources_used if research else []):
            key = (s.name, s.url)
            if key in seen:
                continue
            seen.add(key)
            references.append({"name": s.name, "url": s.url,
                               "fields": list(s.field_attributions)})
        for section_name in domain.sections_covered:
            sections.append({
                "heading": section_name,
                "content_type": domain.section_content_types.get(section_name, "list"),
                "fields": fields,
                "bullets": bullets,
                "references": references,
                "domain_name": domain.domain_name,
                "confidence": research.confidence if research else "low",
                "completeness": research.completeness if research else 0.0,
                "planned_field_count": len(planned),
                "missing_fields": missing_fields,
                "reasoning": _reasoning(domain.persona_relevance),
            })

    considered_not_chosen = [
        {
            "domain_name": r["domain_name"],
            "would_have_covered": r["sections_covered"],
            "reasoning": _reasoning(r["persona_relevance"]),
            "rejected_reason": r["rejected_reason"],
        }
        for r in rejected
    ]

    # Accuracy-verification summary across all fields.
    _vcounts: Dict[str, int] = {}
    for s in sections:
        for f in s.get("fields", []):
            _vcounts[f.get("verdict", "unchecked")] = _vcounts.get(f.get("verdict", "unchecked"), 0) + 1
    verification = {"enabled": config.ACCURACY_VERIFY, "verdict_counts": _vcounts,
                    "cost_usd": round(verify_cost, 6)}

    # Cost: actual (this run) + an explicit ESTIMATE for the full workflow.
    elapsed = time.perf_counter() - wall0
    snap_end = sf.snapshot()
    actual_total = snap_end[0] - before[0]
    actual_calls = snap_end[1] - before[1]
    actual_grounded = snap_end[2] - before[2]
    # Grounding (Google-Search step) is billed per request and is ALREADY folded into
    # actual_total via the per-call surcharge; break it out so token vs grounding is visible.
    grounding_rate = getattr(config, "GEMINI_GROUNDING_COST_PER_CALL", 0.0) or 0.0
    grounding_usd = round(actual_grounded * grounding_rate, 6)
    token_usd = round(actual_total - grounding_usd, 6)
    n_dom = len(sector_result.sector_domains)
    avg_cost_per_domain = domain_research_cost / max(1, n_dom)
    cost = {
        "phase1_sector_research_usd": round(phase1_cost, 6),
        "domain_research_usd": round(domain_research_cost, 6),
        "accuracy_verify_usd": round(verify_cost, 6),
        "token_usd": token_usd,
        "grounding_usd": grounding_usd,
        "grounded_calls": actual_grounded,
        "grounding_rate_per_call": grounding_rate,
        "grounding_note": ("grounding billed past Google's free daily tier; $0 while under it"
                           if grounding_rate else "grounding not attributed (rate 0)"),
        "actual_total_usd": round(actual_total, 6),
        "actual_calls": actual_calls,
        "elapsed_seconds": round(elapsed, 1),
        "estimated_full_workflow_usd": {
            "method": ("avg cost per sector domain this run, extrapolated to 6 generic "
                       "domains + synthesis (same model tier per config.py)"),
            "avg_cost_per_domain_this_run": round(avg_cost_per_domain, 6),
            "estimated_generic_domains_usd": round(avg_cost_per_domain * 6, 6),
            "estimated_synthesis_usd": round(avg_cost_per_domain * 4, 6),
            "estimated_total_usd": round(phase1_cost + domain_research_cost
                                         + avg_cost_per_domain * 10, 6),
        },
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = {
        "company_name": company,
        "generated_at": ts,
        "resolved_subsector": sector_result.resolved_subsector,
        "sections": sections,
        "considered_not_chosen": considered_not_chosen,
        "verification": verification,
        "cost": cost,
    }

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    # Canonical "latest" file (what the Streamlit app reads).
    out_path = os.path.join(config.OUTPUT_DIR, f"{_slug(company)}_sector_only.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    # Immutable per-run archive — every run is kept, never overwritten.
    runs_dir = os.path.join(config.OUTPUT_DIR, "runs")
    os.makedirs(runs_dir, exist_ok=True)
    archive_path = os.path.join(runs_dir, f"{_slug(company)}_{ts}_sector_only.json")
    with open(archive_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    # Log the billed run (ledger + human-readable expense md).
    led = sf.append_delta_to_ledger(f"{company} (sector-only)", before)
    total_spent = sf.append_expense_md(f"{company} (sector-only)", actual_total,
                                       calls=actual_calls)

    print(f"\n=== TOTAL: {elapsed:.1f}s | actual cost ${actual_total:.6f} "
          f"({actual_calls} calls) ===")
    print(f"sections: {len(sections)} across {n_dom} kept domain(s) | "
          f"considered-not-chosen: {len(considered_not_chosen)}")
    print(f"  cost split: tokens ${token_usd:.6f} + grounding ${grounding_usd:.6f} "
          f"({actual_grounded} grounded calls × ${grounding_rate:.3f}; $0 under free tier)")
    print(f"  phase1 ${phase1_cost:.6f} + domain research ${domain_research_cost:.6f}")
    print(f"  estimated FULL workflow (generic+synthesis): "
          f"~${cost['estimated_full_workflow_usd']['estimated_total_usd']:.6f}")
    print(f"output JSON: {out_path} ({os.path.getsize(out_path):,} bytes)")
    print(f"archived run: {archive_path}")
    print(f"LIFETIME cost: ${led['lifetime_cost_usd']:.6f} | "
          f"LiteLLM key total: ${total_spent:.6f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python3 gen_sector_only.py "<Company Name>" ["<description>"]')
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))

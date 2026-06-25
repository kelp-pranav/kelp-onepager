"""Skill 10 — Data Validation & Deduplication (Phase 5).

Final pass over the populated FinalJSON before compilation. Mostly Python
rule-checking; one model call for SWOT-data alignment (semantic comparison).
Cleans where safe, flags everything in a validation report, and never blocks —
prefers a canonical source of truth on conflict.

Public entry point:  async def run(final_json, wave_1_results=None, model_config) -> (FinalJSON, report)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import config
import skill_functions as sf
from schemas import DomainResearchOutput, FinalJSON

# Currency/unit tokens we don't want mixed within one one-pager.
_UNIT_PATTERNS = {
    "INR Crore": re.compile(r"₹|INR|\bCr\b|crore", re.I),
    "INR Lakh": re.compile(r"\blakh\b|\bLac\b", re.I),
    "USD": re.compile(r"\$|\bUSD\b|US\$", re.I),
}
_DATE_FORMS = {
    "FYxx": re.compile(r"\bFY\d{2}\b"),
    "FYxxxx": re.compile(r"\bFY\d{4}\b"),
    "Month YYYY": re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b"),
}


def _section_text(content: Dict[str, Any]) -> str:
    return str(content.get("data", content))


def _check_units(sections) -> List[str]:
    issues: List[str] = []
    for s in sections:
        text = _section_text(s.content)
        forms = [name for name, pat in _UNIT_PATTERNS.items() if pat.search(text)]
        if len(forms) > 1 and "INR Crore" in forms and "INR Lakh" in forms:
            issues.append(f"{s.section_name}: mixes {', '.join(forms)}")
    return issues


def _check_dates(sections) -> List[str]:
    issues: List[str] = []
    for s in sections:
        text = _section_text(s.content)
        forms = [name for name, pat in _DATE_FORMS.items() if pat.search(text)]
        if "FYxx" in forms and "FYxxxx" in forms:
            issues.append(f"{s.section_name}: mixes FYxx and FYxxxx date forms")
    return issues


def _iter_values(content: Dict[str, Any]):
    """Yield (field, scalar-value-as-str) pairs from a section's data."""
    data = content.get("data")
    if not isinstance(data, dict):
        return
    for field, v in data.items():
        if isinstance(v, dict):
            for _k, vv in v.items():
                yield field, str(vv)
        elif isinstance(v, list):
            for item in v:
                yield field, str(item)
        else:
            yield field, str(v)


_PCT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")


def _check_implausible(sections) -> List[str]:
    """Flag obviously-wrong numbers: margins/percentages outside a sane band."""
    issues: List[str] = []
    for s in sections:
        fname = s.section_name
        for field, val in _iter_values(s.content):
            fl = field.lower()
            if "margin" in fl or "%" in fl or "rate" in fl:
                for m in _PCT_RE.findall(val):
                    try:
                        pct = float(m)
                    except ValueError:
                        continue
                    if pct > 150 or pct < -100:
                        issues.append(f"{fname}: implausible '{field}' = {pct}%")
                        break
    return issues


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _check_fabricated_urls(sections, metadata: Dict[str, Any]) -> List[str]:
    """Flag http URLs that appear inline in section data but are NOT among the
    grounding-derived sources in metadata (possible model-fabricated links)."""
    known = set()
    for src in metadata.get("sources", []):
        if src.get("url"):
            known.add(src["url"])
    flagged: List[str] = []
    for s in sections:
        for _field, val in _iter_values(s.content):
            for url in _URL_RE.findall(val):
                if url not in known:
                    flagged.append(f"{s.section_name}: uncorroborated URL {url[:60]}")
    return flagged


def _norm_name(name: str) -> str:
    """Significant-token signature of a section name for duplicate detection."""
    stop = {"and", "or", "the", "of", "by", "a", "an", "to", "for", "amp", "mix",
            "status", "analysis", "overview", "key", "section"}
    toks = []
    for t in re.findall(r"[a-z0-9]+", str(name).lower()):
        if t in stop or len(t) <= 1:
            continue
        toks.append(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return " ".join(sorted(toks))


def _check_duplicate_section_names(sections) -> List[str]:
    """Flag (non-blocking) any two final sections that collapse to the same
    normalized name — a residual duplicate that escaped Phase 1.5 dedup."""
    by_sig: Dict[str, List[str]] = {}
    for s in sections:
        if s.content.get("status") == "unavailable":
            continue
        by_sig.setdefault(_norm_name(s.section_name), []).append(s.section_name)
    dups: List[str] = []
    for sig, names in by_sig.items():
        if len(names) > 1:
            dups.append(" / ".join(names))
    return dups


def _check_citations(sections, metadata: Dict[str, Any]) -> List[str]:
    """Enforce the hard rule (flag, don't block): every data-bearing section must
    carry at least one REAL source link.

    A section passes only if one of its cited sources has a non-empty url. Sections
    that fail get a ``no_source_link`` quality flag (non-blocking) and are listed
    in the report. Citations with names but no url also fail — a citation without a
    link isn't a link.
    """
    src_list = metadata.get("sources", []) or []

    def _has_real_link(section) -> bool:
        for i in section.citations:
            if 0 <= i < len(src_list) and (src_list[i] or {}).get("url"):
                return True
        return False

    missing: List[str] = []
    for s in sections:
        if s.content.get("status") not in ("populated", "partial"):
            continue
        # strip any stale flag from a prior pass, then re-evaluate
        s.quality_flags = [f for f in s.quality_flags if f != "no_source_link"]
        if not _has_real_link(s):
            s.quality_flags.append("no_source_link")
            missing.append(s.section_name)
    return missing


def _apply_completeness_gates(sections) -> List[str]:
    """Sections with populated data but nothing found get downgraded to unavailable."""
    actions: List[str] = []
    for s in sections:
        data = s.content.get("data")
        if isinstance(data, dict) and data:
            found = sum(1 for v in data.values()
                        if v is not None and str(v).strip().lower()
                        not in ("", "not available", "n/a", "na", "none", "unknown", "-"))
            if found == 0 and s.content.get("status") != "unavailable":
                s.content["status"] = "unavailable"
                actions.append(f"{s.section_name}: 0 fields found -> marked unavailable")
    return actions


async def _swot_alignment(
    sections, wave_1_results: Dict[str, Any], model_config
) -> Dict[str, Any]:
    """One model call: are the SWOT bullets traceable to Wave 1 data?"""
    swot = next((s for s in sections if s.section_name == "SWOT"), None)
    if swot is None or swot.content.get("status") != "populated":
        return {"checked": False, "reason": "no populated SWOT"}

    from skills import synthesis_base
    digest = synthesis_base.digest(wave_1_results) if wave_1_results else "(no Wave 1 data)"
    bullets = []
    for q in ("strengths", "weaknesses", "opportunities", "threats"):
        bullets += [f"[{q}] {b}" for b in (swot.content.get(q) or [])]
    if not bullets:
        return {"checked": False, "reason": "empty SWOT"}

    system = (
        "You verify that SWOT bullets are supported by research data. For each bullet, "
        "answer if it is SUPPORTED by a specific fact in the data or UNSUPPORTED "
        "(invented / not in data). Be strict.\n"
        'Return JSON: {"unsupported": [<bullet text>, ...]}.'
    )
    prompt = f"Research data:\n{digest}\n\nSWOT bullets:\n" + "\n".join(bullets)
    resp = await sf.call_model(prompt, system, model_config, response_format="json")
    data = resp.parsed if isinstance(resp.parsed, dict) else {}
    return {"checked": True, "unsupported": data.get("unsupported", [])}


async def run(
    final_json: FinalJSON,
    wave_1_results: Optional[Dict[str, DomainResearchOutput]] = None,
    model_config: "config.ModelConfig" = config.VALIDATION_MODEL,
) -> Tuple[FinalJSON, Dict[str, Any]]:
    sections = final_json.sections

    report: Dict[str, Any] = {
        "unit_issues": _check_units(sections),
        "date_issues": _check_dates(sections),
        "missing_citations": _check_citations(sections, final_json.metadata),
        "implausible_numbers": _check_implausible(sections),
        "fabricated_urls": _check_fabricated_urls(sections, final_json.metadata),
        "duplicate_sections": _check_duplicate_section_names(sections),
        "completeness_actions": _apply_completeness_gates(sections),
    }
    try:
        report["swot_alignment"] = await _swot_alignment(sections, wave_1_results or {}, model_config)
    except Exception as exc:
        report["swot_alignment"] = {"checked": False, "reason": str(exc)}

    # Recompute metadata status tallies after gating.
    final_json.metadata["sections_populated"] = sum(
        1 for s in sections if s.content.get("status") == "populated")
    final_json.metadata["sections_partial"] = sum(
        1 for s in sections if s.content.get("status") == "partial")
    final_json.metadata["sections_unavailable"] = sum(
        1 for s in sections if s.content.get("status") == "unavailable")
    final_json.metadata["validation_report"] = report
    return final_json, report

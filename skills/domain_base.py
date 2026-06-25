"""Shared engine for all Wave 1 domain research skills (Skill 4 generic + Skill 5
templated sector-specific).

Every domain skill is structurally identical: take a field list + recommended
sources, run one coherent research pass via skill_functions.search_for_data_fields,
then compute completeness and confidence. The six generic domain modules and the
templated sector-specific module are thin wrappers around run_domain().
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

import config
import skill_functions as sf
from schemas import DomainResearchOutput, Source
from skills.sector_research import GENERIC_DOMAINS

# Tokens that mean "no data" when checking completeness.
_EMPTY_TOKENS = {"", "not available", "n/a", "na", "none", "unknown", "-"}

# Canonical generic-domain field/source specs, keyed by domain name. Lets each
# domain skill run standalone (using its spec defaults) when the caller doesn't
# pass an explicit field list.
_CANONICAL: Dict[str, Dict[str, Any]] = {d["domain_name"]: d for d in GENERIC_DOMAINS}


def with_defaults(input_data: Dict[str, Any], domain_name: str) -> Dict[str, Any]:
    """Fill data_fields_needed / recommended_sources from the canonical spec if absent."""
    canon = _CANONICAL.get(domain_name, {})
    merged = dict(input_data)
    if not merged.get("data_fields_needed"):
        merged["data_fields_needed"] = list(canon.get("data_fields_needed", []))
    if not merged.get("recommended_sources"):
        merged["recommended_sources"] = list(canon.get("recommended_sources", []))
    return merged


def _is_found(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in _EMPTY_TOKENS


def _company_name(input_data: Dict[str, Any]) -> str:
    return (
        input_data.get("company_name")
        or (input_data.get("company_profile") or {}).get("company_name")
        or "the company"
    )


def _entry_sources(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-field source list as [{name, url}], tolerant of the legacy single
    source/source_url shape."""
    raw = entry.get("sources")
    out: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for s in raw:
            if isinstance(s, dict) and (s.get("name") or s.get("url")):
                out.append({"name": s.get("name"), "url": s.get("url")})
    if not out and (entry.get("source") or entry.get("source_url")):
        out.append({"name": entry.get("source"), "url": entry.get("source_url")})
    return out


def _build_sources(
    field_results: Dict[str, Dict[str, Any]],
    fields: List[str],
    grounding_urls: List[str],
    source_titles: Optional[Dict[str, str]] = None,
) -> List[Source]:
    """Build Source objects from each field's OWN attributed sources.

    The correctness guarantee lives here: a field is attributed to the exact
    (name, url) pairs the extractor mapped to it — never to a url assigned by
    arbitrary order. One distinct (name, url) becomes one Source, accumulating
    every field it backs, so "5 sources behind a fact" yields 5 real links.

    ``source_titles`` (url -> real publisher name from grounding) is the
    deterministic fallback for naming: when the extractor left a source name
    blank but we know the url's publisher, use that real name instead of the
    generic "web search result".
    """
    titles = source_titles or {}

    def _name_for(name: Optional[str], url: Optional[str]) -> str:
        if name:
            return name
        if url and titles.get(url):
            return titles[url]
        return "web search result"

    order: List[str] = []                      # preserves first-seen ordering
    by_key: Dict[str, Dict[str, Any]] = {}     # key -> {name, url, fields[]}
    for f in fields:
        entry = field_results.get(f) or {}
        if not _is_found(entry.get("value")):
            continue
        for s in _entry_sources(entry):
            name = s.get("name")
            url = s.get("url")
            if not name and not url:
                continue
            key = f"{name or ''}|{url or ''}"
            if key not in by_key:
                by_key[key] = {"name": _name_for(name, url), "url": url, "fields": []}
                order.append(key)
            by_key[key]["fields"].append(f)

    sources: List[Source] = []
    attributed_urls = {by_key[k]["url"] for k in order if by_key[k]["url"]}
    for key in order:
        rec = by_key[key]
        sources.append(Source(name=rec["name"], url=rec["url"], field_attributions=rec["fields"]))
    # Keep any grounded URL we visited but didn't attribute to a specific field,
    # so the References list stays complete. These carry NO field attribution, so
    # they are never falsely cited against a datum. Name them by their real
    # publisher title when known.
    for url in grounding_urls:
        if url and url not in attributed_urls:
            attributed_urls.add(url)
            sources.append(Source(name=_name_for(None, url), url=url, field_attributions=[]))
    return sources


def _confidence(completeness: float, sources: List[Source]) -> str:
    # A source backed by a URL or a specific publication name is a real citation;
    # only a bare unnamed "web search result" with no URL doesn't count.
    has_real = any(s.url or s.name != "web search result" for s in sources)
    if completeness >= 0.6 and has_real:
        return "high"
    if completeness >= 0.3:
        return "medium"
    return "low"


async def run_domain(
    domain_name: str,
    input_data: Dict[str, Any],
    model_config: "config.ModelConfig",
) -> DomainResearchOutput:
    """Research one domain. ``input_data`` carries company_name/company_profile,
    data_fields_needed and recommended_sources."""
    fields: List[str] = list(input_data.get("data_fields_needed") or [])
    recommended: List[str] = list(input_data.get("recommended_sources") or [])
    company = _company_name(input_data)
    documents: str = input_data.get("documents") or ""

    warnings: List[str] = []
    if not fields:
        return DomainResearchOutput(
            domain_name=domain_name, data={}, completeness=0.0,
            confidence="low", warnings=["no data_fields_needed provided"],
        )

    groups = input_data.get("field_groups")

    async def _do_search():
        # Breadth-heavy domains (with field_groups) do one focused grounded
        # search per sub-topic; the rest do a single research pass.
        if groups:
            return await sf.search_grouped(
                company_name=company, groups=groups,
                recommended_sources=recommended, model_config=model_config,
                documents=documents,
            )
        return await sf.search_for_data_fields(
            company_name=company, data_fields=fields,
            recommended_sources=recommended, model_config=model_config,
            documents=documents,
        )

    result = await _do_search()
    data = {f: result.get(f, {"value": "Not Available", "source": None}) for f in fields}
    found = sum(1 for f in fields if _is_found(data[f].get("value")))

    # Retry-on-empty: a 0-field result usually means the grounded call was
    # throttled (503/high-demand) under parallel load rather than the data being
    # absent. Re-run once before accepting an empty domain.
    if found == 0:
        warnings.append("first pass returned no data — retrying once")
        result = await _do_search()
        data = {f: result.get(f, {"value": "Not Available", "source": None}) for f in fields}
        found = sum(1 for f in fields if _is_found(data[f].get("value")))

    grounding_urls = list(result.get("_sources") or [])
    source_titles: Dict[str, str] = dict(result.get("_source_titles") or {})

    # Gap-fill backstop: when the first pass found SOME but not all fields,
    # re-research ONLY the still-missing fields once, with a sharper "dig deeper"
    # focus. This is the completeness guarantee (budget headroom is reserved for
    # it) — it rescues fields a broad pass skips (working-capital days, niche
    # peers, a specific catalyst) without paying to re-fetch what we already have.
    missing = [f for f in fields if not _is_found(data[f].get("value"))]
    gapfill_max = getattr(config, "GAPFILL_COMPLETENESS_THRESHOLD", 0.5)
    if missing and 0 < found < len(fields) and (found / len(fields)) < gapfill_max:
        warnings.append(f"gap-fill: re-researching {len(missing)} missing field(s)")
        fill = await sf.search_for_data_fields(
            company_name=company, data_fields=missing,
            recommended_sources=recommended, model_config=model_config,
            focus=("ONLY these specific items, which a broad first pass missed — dig "
                   "deeper and try alternate credible sources before concluding Not Available"),
            documents=documents,
        )
        for f in missing:
            v = fill.get(f)
            if isinstance(v, dict) and _is_found(v.get("value")):
                data[f] = v
        grounding_urls += fill.get("_sources") or []
        for _u, _t in (fill.get("_source_titles") or {}).items():
            source_titles.setdefault(_u, _t)
        found = sum(1 for f in fields if _is_found(data[f].get("value")))

    meta = result.get("_meta") or {}
    if meta.get("error"):
        warnings.append(f"search error: {meta['error']}")
    completeness = found / len(fields)
    sources = _build_sources(data, fields, grounding_urls, source_titles)
    confidence = _confidence(completeness, sources)
    if completeness < 0.3:
        warnings.append(f"low completeness: only {found}/{len(fields)} fields found")

    return DomainResearchOutput(
        domain_name=domain_name,
        data=data,
        completeness=round(completeness, 3),
        sources_used=sources,
        confidence=confidence,
        warnings=warnings,
    )

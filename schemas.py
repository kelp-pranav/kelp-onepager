"""Pydantic data contracts for every inter-skill boundary in the pipeline.

These mirror Section 2 of `kelp_skill_specification.md`. Every skill validates
its input on entry and its output on exit against these models so that parallel
skills fail loudly on wrong-shape data rather than silently corrupting the JSON
assembly step downstream.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Persona relevance — the discipline baked into the whole system               #
# --------------------------------------------------------------------------- #
class PersonaRelevance(BaseModel):
    """Which of the four personas a domain/section/datum changes a decision for.

    Each persona field is a specific decision-impact statement, or None if the
    item is irrelevant to that persona. ``overall_score`` (0-100) summarizes how
    many personas care and how strongly.
    """

    pe_analyst: Optional[str] = None
    banker: Optional[str] = None
    credit_analyst: Optional[str] = None
    consultant: Optional[str] = None
    overall_score: int = 0


# --------------------------------------------------------------------------- #
# Pipeline entry                                                              #
# --------------------------------------------------------------------------- #
class PipelineInput(BaseModel):
    """Top-level input to the orchestrator."""

    company_name: str
    sector: Optional[str] = None              # Inferred if missing
    business_description: Optional[str] = None  # Researched if missing
    sector_override: Optional[str] = None     # User can force a sector


# --------------------------------------------------------------------------- #
# Sector Research output (Phase 1)                                            #
# --------------------------------------------------------------------------- #
class Domain(BaseModel):
    """One research domain — generic or sector-specific."""

    domain_name: str                          # e.g. "Pharma Pipeline"
    priority_hint: Literal["high", "medium", "low"] = "medium"
    sections_covered: List[str] = Field(default_factory=list)
    # Maps each name in sections_covered to its render shape:
    # one of "table" | "stat_grid" | "list" | "chart" | "prose".
    # Additive/optional — empty dict is valid; nothing existing reads it yet.
    section_content_types: Dict[str, str] = Field(default_factory=dict)
    data_fields_needed: List[str] = Field(default_factory=list)
    recommended_sources: List[str] = Field(default_factory=list)
    persona_relevance: PersonaRelevance = Field(default_factory=PersonaRelevance)
    is_sector_specific: bool = False          # False for the 6 generic domains


class SectorResearchOutput(BaseModel):
    """Determines the shape of the whole pipeline."""

    resolved_subsector: str                   # e.g. "controlled-substance API manufacturer"
    company_profile: Dict[str, Any] = Field(default_factory=dict)
    generic_domains: List[Domain] = Field(default_factory=list)   # the 6 standard
    sector_domains: List[Domain] = Field(default_factory=list)    # 3-5 sector-specific
    # Ranked runner-up sector domains — NOT researched in Wave 1. The post-data
    # swap phase (Phase 3.5) researches these on demand to refill the sector floor
    # when chosen sections fail the data substance bar.
    reserve_domains: List[Domain] = Field(default_factory=list)
    total_estimated_sections: int = 0


# --------------------------------------------------------------------------- #
# Layout planning (Phase 2, Branch A)                                         #
# --------------------------------------------------------------------------- #
class PlannedSection(BaseModel):
    section_name: str
    fed_by_domain: str                        # which domain skill produces this section's data
    importance_score: int = 0                 # 0-100
    column: Literal["left", "right"] = "left"
    order_in_column: int = 0                   # position from top
    persona_relevance: PersonaRelevance = Field(default_factory=PersonaRelevance)


class SectionPlan(BaseModel):
    sections: List[PlannedSection] = Field(default_factory=list)
    skeleton: Dict[str, Any] = Field(default_factory=dict)  # empty JSON skeleton with all slots


# --------------------------------------------------------------------------- #
# Domain research output (Wave 1)                                             #
# --------------------------------------------------------------------------- #
class Source(BaseModel):
    name: str                                 # e.g. "CARE Ratings Oct 2025"
    url: Optional[str] = None
    accessed_at: datetime = Field(default_factory=datetime.now)
    field_attributions: List[str] = Field(default_factory=list)  # which fields came from this source


class DomainResearchOutput(BaseModel):
    domain_name: str
    data: Dict[str, Any] = Field(default_factory=dict)  # keyed by data_field_name
    completeness: float = 0.0                  # 0.0-1.0, fraction of fields actually found
    sources_used: List[Source] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    warnings: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Final assembled JSON (Phase 3-5)                                            #
# --------------------------------------------------------------------------- #
class CompletedSection(BaseModel):
    section_name: str
    column: Literal["left", "right"] = "left"
    order_in_column: int = 0
    section_tag: Literal["Generic", "Sector"] = "Generic"
    section_tag_label: str = "Generic"        # e.g. "Pharma", "Banking"
    content_type: str = "prose"               # table | stat_grid | swot_grid | list | chart | prose
    content: Dict[str, Any] = Field(default_factory=dict)  # schema varies by content_type
    # Section-level citations: union of the sources backing everything shown in
    # this section. Indices into FinalJSON.metadata["sources"].
    citations: List[int] = Field(default_factory=list)
    # Inline citations: per-field source indices, so each datum can render its own
    # superscript link(s). Keys are field names within content["data"]; values are
    # lists of indices into FinalJSON.metadata["sources"].
    field_citations: Dict[str, List[int]] = Field(default_factory=dict)
    # Quality flags raised by validation (e.g. "no_source_link"). Non-blocking.
    quality_flags: List[str] = Field(default_factory=list)


class FinalJSON(BaseModel):
    metadata: Dict[str, Any] = Field(default_factory=dict)        # generated_at, company_name, telemetry, sources
    company_header: Dict[str, Any] = Field(default_factory=dict)  # name, CIN/ticker, sector, founded, type
    sections: List[CompletedSection] = Field(default_factory=list)  # ordered, validated, deduplicated

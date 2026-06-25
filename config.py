"""Central configuration for the Kelp One-Pager Agent.

This is the single file you edit to swap a skill's model, tune concurrency, or
change paths — no skill code changes required. Mirrors Section 5 of
`kelp_skill_specification.md`.

NOTE: every per-skill model is currently initialized to ``claude-sonnet-4-6``.
Assign the real per-skill tiers (Opus for Sector Research / synthesis, Haiku for
simple lookups / validation, etc.) by editing the assignments below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv optional at import time; .env still readable via env
    pass


# --------------------------------------------------------------------------- #
# Model configuration                                                          #
# --------------------------------------------------------------------------- #
@dataclass
class ModelConfig:
    """Provider-agnostic model spec consumed by ``skill_functions.call_model``."""

    provider: Literal["anthropic", "openai", "gemini"]
    model: str
    temperature: float = 0.2
    enable_search: bool = False
    max_tokens: int = 2000
    fallback: Optional["ModelConfig"] = None


# Current Gemini model ids (June 2026). Gemini 2.0 Flash was shut down 2026-06-01.
GEMINI_FLASH = "gemini-2.5-flash"            # $0.30 in / $2.50 out per 1M tokens
GEMINI_FLASH_LITE = "gemini-2.5-flash-lite"  # $0.10 in / $0.40 out per 1M tokens

# LiteLLM gateway (OpenAI-compatible) — all skills route through this. The direct
# Gemini path (provider="gemini") is kept intact for rollback.
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY")
LITELLM_FLASH_MODEL = os.getenv("LITELLM_FLASH_MODEL", "gemini/gemini-2.5-flash")
LITELLM_FLASH_LITE_MODEL = os.getenv("LITELLM_FLASH_LITE_MODEL", "gemini/gemini-2.5-flash-lite")


def _flash(
    *,
    temperature: float = 0.2,
    enable_search: bool = False,
    max_tokens: int = 2000,
    fallback: Optional[ModelConfig] = None,
) -> ModelConfig:
    """Research / synthesis tier — Gemini 2.5 Flash via LiteLLM. Falls back to Flash-Lite."""

    return ModelConfig(
        provider="litellm",
        model=LITELLM_FLASH_MODEL,
        temperature=temperature,
        enable_search=enable_search,
        max_tokens=max_tokens,
        fallback=fallback if fallback is not None else _flash_lite(),
    )


def _flash_lite(
    *,
    temperature: float = 0.2,
    enable_search: bool = False,
    max_tokens: int = 2000,
) -> ModelConfig:
    """Mechanical tier — Gemini 2.5 Flash-Lite (cheapest) via LiteLLM. No fallback."""

    return ModelConfig(
        provider="litellm",
        model=LITELLM_FLASH_LITE_MODEL,
        temperature=temperature,
        enable_search=enable_search,
        max_tokens=max_tokens,
        fallback=None,
    )


# --------------------------------------------------------------------------- #
# Per-skill model assignments (Gemini mixed tiers)                            #
# --------------------------------------------------------------------------- #
# Phase 1 — Flash + search (critical, errors propagate everywhere)
# max_tokens raised for the enriched prompt (5-8 domains x 5-10 fields each can
# exceed 3000 output tokens and truncate the JSON).
SECTOR_RESEARCH_MODEL = _flash(enable_search=True, max_tokens=8000)

# Phase 2, Branch A — Flash-Lite (mechanical ranking)
IMPORTANCE_SCORING_MODEL = _flash_lite(max_tokens=1500)
# Layout Planning is pure Python — no model.

# Wave 1 — generic domains — Flash + search (real research)
FINANCIALS_DOMAIN_MODEL = _flash(temperature=0.1, enable_search=True)
CORPORATE_STRUCTURE_DOMAIN_MODEL = _flash(enable_search=True)
MARKET_POSITION_DOMAIN_MODEL = _flash(enable_search=True)
TRACK_RECORD_DOMAIN_MODEL = _flash(enable_search=True)
CREDIT_RISK_DOMAIN_MODEL = _flash(temperature=0.1, enable_search=True)
GEOGRAPHY_DOMAIN_MODEL = _flash(enable_search=True)

# Wave 1 — templated sector-specific domain — Flash + search
SECTOR_SPECIFIC_MODEL = _flash(enable_search=True)

# Interpretive sections — grounded synthesis (reads ALL Wave 1 data + own web
# search). Risk flags is derived/judged, not looked up, so it gets search + a
# generous token budget like the other synthesis skills.
RISK_FLAGS_MODEL = _flash(enable_search=True, temperature=0.3, max_tokens=6000)

# Wave 2 — synthesis — Flash (reads Wave 1 output; no own search)
# max_tokens raised to 6000: Gemini 2.5 Flash is a THINKING model, and reasoning
# tokens are billed against max_output_tokens. At the old 2000 default the JSON
# got cut off after the first object/quadrant (e.g. SWOT returned only
# "strengths"). 6000 leaves ample room for thinking + the full structured output.
INVESTMENT_THESIS_MODEL = _flash(temperature=0.3, max_tokens=6000)
SWOT_MODEL = _flash(temperature=0.3, max_tokens=6000)
FUTURE_PLAN_MODEL = _flash(temperature=0.3, max_tokens=6000)

# Phase 4.5 — presentation — Flash (reformats each domain's FETCHED fields into
# render-ready content + a one-line analysis; no own search, must not invent data).
# One batched call per domain; generous tokens so multi-section JSON isn't cut off.
PRESENTATION_MODEL = _flash(temperature=0.2, max_tokens=6000)

# Phase 4.5 — coverage gap-gate — Flash-Lite (assess what's missing/forced)
COVERAGE_MODEL = _flash_lite(temperature=0.2, max_tokens=1500)
# Phase 5 — validation — Flash-Lite (mechanical checking)
VALIDATION_MODEL = _flash_lite(temperature=0.1, max_tokens=1000)

# Accuracy verification (sector-only) — a grounded Flash-Lite pass that re-confirms each
# extracted value against current primary sources and flags mislabels / scope errors /
# unsupported claims. One batched call per domain. Toggle with ACCURACY_VERIFY.
# Off: the per-section accuracy flag UI was removed, so the verdicts have no consumer —
# running the pass would be pure wasted cost. Flip back to True to re-enable both.
ACCURACY_VERIFY = False
VERIFY_MODEL = _flash_lite(temperature=0.0, enable_search=True, max_tokens=2000)


# --------------------------------------------------------------------------- #
# Concurrency limits                                                           #
# --------------------------------------------------------------------------- #
MAX_PARALLEL_SKILLS = 8
PER_SKILL_TIMEOUT_SECONDS = 30
TOTAL_PIPELINE_TIMEOUT_SECONDS = 120

# Wave 1 runs each domain as two sequential grounded calls; firing too many at
# once gets Gemini throttled (503 "high demand"), degrading research quality.
# 2 -> 4: with ~4 sector domains, 2-way parallel meant two sequential waves and
# ~200-250s wall-clock. 4-way runs them all at once (~half the time); call_model's
# transient-503 retry absorbs the extra throttling risk. Lower if you see many 503s.
WAVE1_MAX_CONCURRENT = 4
# 180 -> 120: with the gap-fill backstop now gated (only on sparse domains, see
# GAPFILL_COMPLETENESS_THRESHOLD) the heaviest domains finish well inside 120s, so a
# stuck domain fails fast instead of dragging the whole run to 180s.
WAVE1_PER_SKILL_TIMEOUT_SECONDS = 120

# Gap-fill backstop (re-research the still-missing fields) only fires when the first
# pass found LESS than this fraction of fields. Domains already at/above this skip the
# extra grounded round — it's the main time sink and rarely changes a sourcing screen.
GAPFILL_COMPLETENESS_THRESHOLD = 0.5

# Transient-error retry (503 / 429 / overloaded) inside call_model, before fallback.
MAX_TRANSIENT_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 1.0

# --------------------------------------------------------------------------- #
# Sector-section selection & post-data swap (Phase 1 + Phase 3.5)             #
# --------------------------------------------------------------------------- #
# Sector Research promotes the top SECTOR_PRIMARY_MAX scored sector domains to
# Wave 1 research (keeps selection tight) and retains up to SECTOR_RESERVE_MAX
# ranked runner-ups as a reserve pool (NOT researched unless needed). When chosen
# sections fail the data substance bar and the sector floor drops below
# MIN_SECTOR_SECTIONS, the swap phase researches reserves one at a time, up to
# SECTOR_SWAP_MAX_ATTEMPTS, to refill the floor.
SECTOR_PRIMARY_MAX = 6
SECTOR_RESERVE_MAX = 6
SECTOR_SWAP_MAX_ATTEMPTS = 4

# Gemini Google-Search grounding surcharge per grounded request, in USD.
# Token cost is tracked exactly; grounding (the Google Search step on every
# enable_search call) is billed separately by Google at ~$35 / 1,000 requests
# = $0.035 each, AFTER a free daily allowance. Now INCLUDED in the reported cost
# and broken out separately in the run's cost block. NOTE: while you stay under
# Google's free daily tier the real grounding cost is $0, so the reported figure
# is an upper bound — set this to 0.0 to report token-cost only.
GEMINI_GROUNDING_COST_PER_CALL = 0.035


# --------------------------------------------------------------------------- #
# API keys (loaded from .env)                                                  #
# --------------------------------------------------------------------------- #
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


# --------------------------------------------------------------------------- #
# Output paths                                                                 #
# --------------------------------------------------------------------------- #
OUTPUT_DIR = "./output"
REFERENCE_DIR = "./references"
# Local ground-truth documents: any PDF/.docx/.txt/.md dropped here is used as an
# authoritative research source that overrides web data on conflict.
INPUT_DIR = "./input"
# Cap on total document text injected per research call (~15k tokens). Bounds
# per-call cost; larger document sets are truncated with a warning.
MAX_DOC_CONTEXT_CHARS = 60000

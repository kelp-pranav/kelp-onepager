# Kelp One-Pager Agent — Skill System Specification

This document specifies the complete skill-based architecture for generating professional financial one-pagers. Hand this to Claude Code along with the project's existing CLAUDE.md, Embio_OnePager.html (design reference), and supporting CSVs to implement the system.

---

## Section 0 — Foundational principles

### 0.1 Who this is for

Every skill in this system serves four users. They are the only audience that matters:

1. **PE analysts** deciding whether to invest and at what price
2. **Investment bankers** building pitch books, sourcing deals, structuring transactions
3. **Credit analysts** assessing lending risk
4. **Consultants** diagnosing operational health for management advice

**The persona-relevance test.** Every domain, every section, every field of data must answer: "Which of the four personas changes their decision, price, or risk assessment because of this — and what specifically do they do differently?"

If the answer is vague ("it's good context"), it doesn't earn a place. If the answer is sharp ("a credit analyst sees FDA Import Alert history because one alert can erase 30% of revenue overnight, directly changing default probability"), it earns its place.

This test gets baked into the Sector Research skill, Importance Scoring skill, and the final compilation step. It is the single biggest discipline preventing scope creep.

### 0.2 Architecture in one paragraph

A two-wave parallel pipeline. Phase 1 (Sector Research) runs alone — it's the hard sequential bottleneck because it determines what gets researched. Phase 2 fires Importance Scoring + Layout Planning in one branch (fast) alongside Wave 1 domain research skills (slow, parallel). JSON Assembly is a continuous listener, not a discrete step — it populates the skeleton as Wave 1 results arrive. Phase 3 runs Wave 2 synthesis skills (Investment Thesis, SWOT, Future Plan) which read from Wave 1's output rather than re-researching anything. Phase 4 validates and dedupes. Phase 5 compiles JSON to HTML.

Total wall-clock time = Phase1 + max(Wave1 skills) + Wave2 + validation + compile. Roughly 30-60 seconds for a typical company.

### 0.3 Module structure

```
kelp_agent/
├── orchestrator.py         # Top-level pipeline coordinator
├── skill_functions.py      # Shared library — model adapter, search, parsing, validation
├── skills/
│   ├── sector_research.py
│   ├── importance_scoring.py
│   ├── layout_planning.py
│   ├── domain_financials.py
│   ├── domain_corporate_structure.py
│   ├── domain_market_position.py
│   ├── domain_track_record.py
│   ├── domain_credit_risk.py
│   ├── domain_geography.py
│   ├── domain_sector_specific.py    # Templated, instantiated per sector domain
│   ├── synthesis_investment_thesis.py
│   ├── synthesis_swot.py
│   ├── synthesis_future_plan.py
│   ├── json_population.py
│   ├── data_validation.py
│   └── html_compiler.py
├── schemas.py              # Pydantic models for all JSON contracts
├── config.py               # Model config per skill, API keys, timeouts
└── references/             # Embio_OnePager.html, kelp.css, sector_metadata.csv
```

Every skill in `skills/` is a thin module exposing one async function: `run(input_data, model_config) -> output_data`. All actual work — API calls, search, JSON parsing — happens in `skill_functions.py` and gets imported by each skill.

---

## Section 1 — Shared library: skill_functions.py

This is the load-bearing module. Build it first.

### 1.1 Model adapter

```python
async def call_model(
    prompt: str,
    system: str,
    model_config: ModelConfig,
    enable_search: bool = False,
    max_tokens: int = 2000,
    response_format: Literal["text", "json"] = "json"
) -> ModelResponse
```

ModelConfig fields: `provider` (anthropic | openai | gemini), `model` (model string), `temperature`, `fallback` (optional ModelConfig).

Internally dispatches to provider-specific adapter (`_call_anthropic`, `_call_openai`, `_call_gemini`). Each adapter handles that provider's API format but returns the same `ModelResponse` shape: `{text, tokens_used, estimated_cost_usd, latency_ms, provider, model, sources: List[str]}`.

If `enable_search=True`:
- Anthropic: adds `tools=[{"type": "web_search_20250305", "name": "web_search"}]`
- OpenAI: uses their browsing tool config
- Gemini: enables Google Search grounding

If `response_format="json"`: post-processing strips markdown fences, extracts the JSON object, attempts parse, and if parse fails sends a "fix this JSON, return only valid JSON" follow-up turn before giving up. This logic is the same regardless of provider.

If model call fails (timeout, API error) and `fallback` is set: retry once with the fallback config. Log both attempts.

### 1.2 Structured search helper

```python
async def search_for_data_fields(
    company_name: str,
    data_fields: List[str],
    recommended_sources: List[str],
    model_config: ModelConfig,
    max_searches: int = 5
) -> Dict[str, Any]
```

Used by all domain research skills. Takes a list of specific data fields needed and recommended sources, builds a targeted search prompt, and returns a dictionary keyed by field name with values and per-field source attribution.

Internally handles: query construction, source preference (try recommended sources first), missing-data handling ("Not Available" rather than fabrication), and per-field source tracking for citations.

### 1.3 JSON utilities

```python
def parse_json_safe(text: str) -> Optional[dict]
def validate_against_schema(data: dict, schema: BaseModel) -> Tuple[bool, List[str]]
def merge_partial_results(existing: dict, new_partial: dict, path: str) -> dict
```

`merge_partial_results` is the function JSON Population uses to slot Wave 1 results into the layout skeleton without overwriting completed sections.

### 1.4 Concurrency utilities

```python
async def run_skills_in_parallel(
    skills: List[Callable],
    inputs: List[dict],
    max_concurrent: int = 8,
    per_skill_timeout: int = 30
) -> List[Union[dict, SkillError]]
```

Critical infrastructure. Runs N skills with a concurrency cap (so you don't hit rate limits), per-skill timeout (so one slow skill doesn't block everything), and returns results in the same order as inputs. Failures don't raise — they return a `SkillError` object so the rest of the pipeline can mark that section "Not Available" and continue.

### 1.5 Persona relevance evaluator

```python
def evaluate_persona_relevance(
    item_description: str,
    item_data_summary: str
) -> PersonaRelevance
```

PersonaRelevance: `{pe_analyst: str|None, banker: str|None, credit_analyst: str|None, consultant: str|None, overall_score: int}`. Each persona field is either a specific decision-impact statement or None if irrelevant. Used by Sector Research and Importance Scoring. This is a model call internally (uses a fast model from config), not a heuristic.

### 1.6 Cost and telemetry

Every skill records: `skill_name, started_at, finished_at, duration_ms, tokens_in, tokens_out, estimated_cost_usd, provider, model_used, success`. Aggregated into a single telemetry object the orchestrator returns alongside the final HTML. Lets you see "this one-pager cost $0.43 and took 22 seconds, here's the per-skill breakdown."

---

## Section 2 — Shared schemas

All defined as Pydantic models in `schemas.py`. Every skill validates input and output against these. Wrong-shape data fails loudly rather than silently corrupting downstream skills.

### 2.1 PipelineInput

```python
class PipelineInput:
    company_name: str
    sector: Optional[str]           # Inferred if missing
    business_description: Optional[str]  # Researched if missing
    sector_override: Optional[str]   # User can force a sector
```

### 2.2 SectorResearchOutput

```python
class Domain:
    domain_name: str                 # e.g. "Pharma Pipeline"
    priority_hint: Literal["high", "medium", "low"]
    sections_covered: List[str]      # Final one-pager sections this domain feeds
    data_fields_needed: List[str]    # Specific data points to research
    recommended_sources: List[str]   # Named sources, not "various reports"
    persona_relevance: PersonaRelevance
    is_sector_specific: bool         # False for the 6 generic domains

class SectorResearchOutput:
    resolved_subsector: str          # E.g. "controlled-substance API manufacturer"
    company_profile: dict            # Founded, listed status, country, ticker, etc.
    generic_domains: List[Domain]    # The 6 standard ones
    sector_domains: List[Domain]     # 3-5 sector-specific ones
    total_estimated_sections: int
```

### 2.3 SectionPlan

```python
class PlannedSection:
    section_name: str
    fed_by_domain: str               # Which domain skill produces this section's data
    importance_score: int            # 0-100
    column: Literal["left", "right"]
    order_in_column: int             # Position from top
    persona_relevance: PersonaRelevance

class SectionPlan:
    sections: List[PlannedSection]
    skeleton: dict                   # Empty JSON skeleton with all section slots
```

### 2.4 DomainResearchOutput

```python
class DomainResearchOutput:
    domain_name: str
    data: Dict[str, Any]             # Keyed by data_field_name
    completeness: float              # 0.0-1.0, fraction of fields actually found
    sources_used: List[Source]       # For citations
    confidence: Literal["high", "medium", "low"]
    warnings: List[str]              # E.g. "Patent expiry data only available up to 2027"

class Source:
    name: str                        # E.g. "CARE Ratings Oct 2025"
    url: Optional[str]
    accessed_at: datetime
    field_attributions: List[str]    # Which data fields came from this source
```

### 2.5 FinalJSON

```python
class FinalJSON:
    metadata: dict                   # Generated_at, company_name, telemetry summary
    company_header: dict             # Name, CIN/ticker, sector, founded, type
    sections: List[CompletedSection] # Ordered, validated, deduplicated

class CompletedSection:
    section_name: str
    column: str
    order_in_column: int
    section_tag: Literal["Generic", "Sector"]
    section_tag_label: str           # E.g. "Pharma", "Banking"
    content_type: str                # "table" | "stat_grid" | "swot_grid" | "list" | "chart" | "prose"
    content: dict                    # Schema varies by content_type
    citations: List[int]             # Indices into FinalJSON.metadata.sources
```

---

## Section 3 — Skill specifications

For each skill: purpose, persona anchor, input/output, internal steps, prompt design, model config, failure modes, dependencies.

---

### Skill 1: Sector Research

**Module:** `skills/sector_research.py`

**Purpose.** Given a company name, sector hint, and business description, identify exactly which domain skills should run and what they should research. This is the only skill that determines pipeline shape — get it wrong, every downstream skill researches the wrong things.

**Persona anchor.** Each domain proposed must include a specific persona-relevance justification: which of PE / banker / credit / consultant cares, and what decision changes because of it. Domains failing the persona test are cut, not softened.

**Input:** `PipelineInput`
**Output:** `SectorResearchOutput`

**Internal steps:**

1. **Resolve subsector.** "Pharma" is too coarse. From business description, narrow to e.g. "controlled-substance API manufacturer with biofermentation platform" vs "Indian branded formulation player" vs "biosimilar developer." This is an explicit LLM call. Output stored in `resolved_subsector`.

2. **Check sector taxonomy library.** Load `references/sector_taxonomies.json` (pharma, banking, tech, consumer, manufacturing, real_estate, energy). If `sector` (or inferred sector from step 1) matches one, load its baseline domain template.

3. **Customize against business description.** Even with a matching taxonomy, prompt the model to add/remove/reweight domains based on the specific company. Embio (controlled-substance API) needs Pipeline domain de-emphasized but Regulatory & Manufacturing emphasized; Sun Pharma needs Pipeline as the top domain.

4. **Generic domain anti-duplication check.** Explicitly instruct the model: "Do NOT propose domains covered by the 6 generic domains. Those are: Financials & Ratios (revenue, EBITDA, PAT, margins, ratios, working capital), Corporate Structure (ownership, leadership, board, compliance, ESG), Market Position (peers, market size, products, channel mix), Track Record (deals, milestones, news, catalysts), Credit & Risk (ratings, risk flags), Geography (revenue by region, global presence)." This list is embedded verbatim in the prompt.

5. **Persona relevance pass.** For each proposed domain, call `evaluate_persona_relevance`. If overall_score < 30, the domain is cut. Returns 3-5 sector-specific domains, never more than 7 total.

6. **Source naming.** For each domain, require specific named sources (CARE/CRISIL/ICRA for Indian credit; USFDA Orange Book and Warning Letter database for pharma regulatory; RBI reports for banking; RERA portals for real estate; etc.). If the model can't name a real source for a domain, mark `recommended_sources: ["unknown"]` and `confidence: low` rather than inventing source names.

**Prompt design notes:**
- System prompt includes the 6 generic domains list verbatim (anti-duplication).
- System prompt includes 1-2 fully worked examples — pharma (Embio) and one other sector — showing the depth and specificity expected. Few-shot anchoring matters here.
- User prompt is concise: company name + sector + business description.
- Response format: strict JSON matching `SectorResearchOutput` schema.

**Model config (recommended default):**
```python
ModelConfig(
    provider="anthropic",
    model="claude-opus-4-5",
    temperature=0.2,
    enable_search=True,
    max_tokens=3000,
    fallback=ModelConfig(provider="anthropic", model="claude-sonnet-4-6")
)
```

Highest-reasoning tier is justified here because errors propagate everywhere downstream. Low temperature for determinism — same company should produce stable domain breakdowns across runs.

**Failure modes to handle:**
- Sector doesn't match any known taxonomy → fall back to fresh reasoning + search, log warning
- Multi-sector company (e.g., conglomerate) → return domains from multiple taxonomies, flag in `warnings`
- Web search returns nothing useful for subsector resolution → return best-effort subsector + low confidence
- Model returns malformed JSON → automatic retry via `call_model`'s built-in JSON repair

**Bounded scope:**
- Max 5 web searches
- Hard latency budget: 20 seconds
- This skill is sequential and blocks everything else. Anything slower defeats the parallel architecture.

**Dependencies:** None upstream. Everything downstream depends on this.

---

### Skill 2: Importance Scoring

**Module:** `skills/importance_scoring.py`

**Purpose.** Given the full list of sections (generic + sector-specific), assign each an importance score (0-100) for THIS specific company. The score drives layout placement and final visual emphasis.

**Persona anchor.** Score reflects how many of the four personas care about this section, and how strongly. A section that materially changes a credit analyst's risk assessment AND a PE analyst's valuation scores higher than one only useful to a consultant.

**Input:** `SectorResearchOutput` + the canonical list of 23 generic sections.
**Output:** `Dict[str, int]` — section name → importance score.

**Internal steps:**

1. Build full section list: all 23 generic sections + sections from each sector domain.
2. Single LLM call ranking all sections against this specific company's profile (subsector + revenue range + listed status + business description).
3. The prompt embeds the persona-relevance test: "For each section, identify which of [PE, banker, credit, consultant] cares, then score 0-100. Sections useful to 3+ personas always score above 70. Sections useful to 0 personas score below 20."
4. Validation: scores must be unique (no ties) within ±2 points so layout planner has deterministic ordering.

**Prompt design notes:**
- Embeds the persona-relevance test directly.
- Provides anchor examples: "For a listed mid-cap pharma: 'Financial performance' = 95, 'Credit ratings' = 90, 'Patent cliff exposure' = 85, 'Global presence' = 40, 'Awards & certifications' = 15."
- Output: flat JSON object `{section_name: score}`.

**Model config:**
```python
ModelConfig(provider="anthropic", model="claude-sonnet-4-6", temperature=0.2, max_tokens=1500)
```

Sonnet is fine — this is structured ranking, not deep reasoning. Single call, no search needed.

**Failure modes:**
- Duplicate scores → automatic re-prompt asking model to break ties
- Score outside 0-100 → clamp + log warning

**Dependencies:** Sector Research must complete first.

---

### Skill 3: Layout Planning

**Module:** `skills/layout_planning.py`

**Purpose.** Take importance scores and produce the JSON skeleton — which section goes in left/right column, in what order, with empty content slots ready to be filled.

**Persona anchor.** Not directly persona-anchored; this skill is rule-based. But the rules themselves were designed with persona attention patterns in mind (PE analysts scan top-left first, credit analysts scan right column for ratings, etc.).

**Input:** Section list with importance scores + section type metadata.
**Output:** `SectionPlan` with JSON skeleton.

**Internal logic (rule-based, no LLM call):**

1. **Column assignment by section type:**
   - Always left: Business description, Investment thesis, Key stats bar, Financial performance, Key metrics, SWOT, Market size, Recent news, sector-specific data-heavy sections
   - Always right: Details, Ownership, Leadership, Board members, Credit ratings, Risk flags, Catalysts, Milestones, Peers, Global presence, Future plan
   - Configurable: Geography, Channel mix, Products & services (default left, can be moved if importance dictates)

2. **Ordering within column:**
   - Left column: highest-importance sections first, with hard rule that Business description + Investment thesis always come first, Key stats bar second
   - Right column: highest-importance first, with hard rule that Details always come first

3. **Sector tag assignment:** Each section gets a tag — "Generic" (gray pill) for the 23 standard sections, "[Sector name]" (blue pill) for sector-specific sections.

4. **Skeleton generation:** Build the empty JSON structure with all section slots, each marked with `content_type` and `populated: false`.

**Model config:** No model call — pure Python logic. (Optional: a future v2 could ask a model to override default placement for unusual companies.)

**Dependencies:** Importance Scoring must complete first.

---

### Skill 4: Generic Domain Research (6 instances)

**Modules:**
- `skills/domain_financials.py`
- `skills/domain_corporate_structure.py`
- `skills/domain_market_position.py`
- `skills/domain_track_record.py`
- `skills/domain_credit_risk.py`
- `skills/domain_geography.py`

These are six structurally identical skills with different data field lists. Each runs in parallel as part of Wave 1.

**Purpose.** Fetch all data for one domain in a single coherent research pass, feeding multiple final sections.

**Persona anchor.** Each domain's data fields were chosen because they materially affect at least one persona's decision. Encoded in each skill's docstring with examples.

**Input:**
```python
{
  "company_profile": {...},        # From Sector Research
  "data_fields_needed": [...],      # Domain-specific field list
  "recommended_sources": [...],     # Domain-specific source list
  "model_config": ModelConfig
}
```

**Output:** `DomainResearchOutput` (see schema 2.4).

**Internal steps (same for all six):**

1. Call `search_for_data_fields` from skill_functions.py with the domain's fields and sources.
2. Validate result against domain's expected schema.
3. Compute `completeness` (fraction of fields with real data vs "Not Available").
4. Tag `confidence` based on source quality (named regulatory/credit sources = high; only general web = low).
5. Return.

**The six domains and their fields:**

**4a. Financials & Ratios** (feeds: Key stats, Financial performance, Key metrics, Working capital)
- Fields: 5-year revenue, EBITDA, PAT, EBITDA margin, PAT margin, ROE, ROCE, debt/equity, interest coverage, debt/EBITDA, current ratio, debtor days, inventory days, payable days, operating cycle, currency, units
- Sources: CARE/CRISIL/ICRA reports, BSE/NSE filings, MCA annual returns, company investor presentations

**4b. Corporate Structure** (feeds: Ownership, Shareholders, Leadership, Board members, Compliance/ESG)
- Fields: company type (listed/unlisted/PE-backed), promoter %, institutional %, retail %, pledge %, paid-up capital, authorized capital, individual promoters, board members + roles, executives + backgrounds, auditor name, litigation status, RPT flags, contingent liabilities, ESG initiatives
- Sources: MCA filings, Tofler/Tracxn, BSE/NSE shareholding patterns (if listed), Whalesbook for PE deals

**4c. Market Position** (feeds: Peers, Market size, Products & services, Channel mix)
- Fields: top 5-8 peers with revenue + EBITDA margin + focus, 3-5 relevant market sizes (TAM in $ or ₹ with CAGR), full product list, channel breakdown (B2B/B2C, direct/distributor, key customers if disclosed)
- Sources: industry reports (IBEF, IBEF, Nielsen, IQVIA), company website, broker reports

**4d. Track Record** (feeds: Deals & transactions, Key milestones, Recent news, Upcoming catalysts)
- Fields: M&A and funding history (date, description, amount), corporate milestones (incorporation, restructuring, key launches), 5 most recent news items (date, headline, source URL), 3-6 upcoming catalysts (date, event, materiality)
- Sources: Whalesbook, VCCircle, Mint, BloombergQuint, company press releases, BSE/NSE corporate announcements

**4e. Credit & Risk** (feeds: Credit ratings, Risk flags)
- Fields: current LT and ST credit ratings + agency + outlook, 4-year rating history, key positive/negative triggers, 4-7 risk flags with severity (red/amber/green) and one-line description
- Sources: CARE/CRISIL/ICRA/India Ratings rating reports

**4f. Geography** (feeds: Revenue by geography, Global presence)
- Fields: revenue split by region with % (and growth if available), country count, key regional approvals/certifications, regional revenue trend
- Sources: company annual reports, investor presentations, segment disclosures

**Model config (recommended per domain):**
- Financials, Credit & Risk: `sonnet-4-6` with search — needs precision on numbers
- Corporate Structure, Track Record: `sonnet-4-6` with search — broad data gathering
- Market Position, Geography: `haiku-4-5` with search — simpler retrieval, cheaper model fine
- All have `fallback=sonnet-4-6` if their primary fails.

**Failure modes:**
- Source paywalled / not accessible → mark fields "Not Available", note in warnings
- Conflicting data across sources → prefer most recent, log discrepancy in warnings
- Web search rate limited → fallback model retries with reduced search count

**Dependencies:** Sector Research must complete (for company_profile).

---

### Skill 5: Sector-Specific Domain Research (templated, multiple instances)

**Module:** `skills/domain_sector_specific.py`

**Purpose.** Generic templated skill that gets instantiated once per sector-specific domain identified by Sector Research. Not a hardcoded "Pharma Pipeline skill" — one flexible skill that takes any domain spec and researches it.

**Persona anchor.** Each instance inherits the persona-relevance metadata from the originating Domain object (set by Sector Research).

**Input:**
```python
{
  "company_profile": {...},
  "domain": Domain,  # The Domain object from Sector Research, with fields/sources already specified
  "model_config": ModelConfig
}
```

**Output:** `DomainResearchOutput` — same schema as generic domains.

**Internal logic (same template, parameterized):**

1. Take `domain.data_fields_needed` and `domain.recommended_sources` as research targets.
2. Call `search_for_data_fields` with domain-specific config.
3. Validate against the expected output shape (defined by `domain.sections_covered`).
4. Return.

This template is what lets you add new sectors without writing new skills. Sector Research outputs domain specs, this skill researches them.

**Model config:** Configurable per sector. Pharma domains might warrant Opus (complex regulatory data); banking domains do fine on Sonnet.

**Failure modes:** Same as generic domains, plus:
- Domain spec is too vague (e.g., recommended_sources says "various") → log warning, do best-effort search, mark confidence low.

**Dependencies:** Sector Research must complete.

---

### Skill 6: JSON Population

**Module:** `skills/json_population.py`

**Purpose.** Take the empty skeleton from Layout Planning and incrementally fill it with results from Wave 1 domain skills as they arrive. Not a one-shot operation — it's a continuous listener.

**Persona anchor.** Not directly anchored; this is plumbing. But it preserves the persona-relevance metadata from each section so it flows to the final HTML.

**Input:**
```python
{
  "skeleton": dict,                # From Layout Planning
  "section_plan": SectionPlan,
  "domain_results": List[DomainResearchOutput],  # Streamed as they complete
}
```

**Output:** Partially populated FinalJSON (sections marked `populated: true` as filled).

**Internal logic:**

1. Maintain mapping `{section_name → fed_by_domain}` from SectionPlan.
2. For each incoming DomainResearchOutput:
   - Identify which sections it feeds.
   - For each fed section: extract relevant subset of `domain_results.data` and slot it into the skeleton.
   - Mark section `populated: true`.
   - Attach sources for citation tracking.
3. Track completion: when all expected domains have either succeeded or timed out, mark JSON as Wave 1 complete.

**No model call.** Pure Python logic.

**Failure modes:**
- Domain returned with `confidence: low` and `completeness < 0.3` → section marked `partial: true`, content shows "Limited data available" with what little was found
- Domain timed out → section marked `populated: false, status: timeout` → final HTML shows "Not Available"

**Dependencies:** Layout Planning must complete; runs continuously alongside Wave 1.

---

### Skill 7-9: Wave 2 Synthesis (3 skills)

**Modules:**
- `skills/synthesis_investment_thesis.py`
- `skills/synthesis_swot.py`
- `skills/synthesis_future_plan.py`

**Purpose.** Synthesize cross-domain insights from Wave 1's output. These don't do their own research — they read what's already been gathered.

**Persona anchor.** These are the most persona-relevant sections in the entire one-pager. The Investment Thesis is what a PE analyst reads first. SWOT is what a banker uses to build the pitch narrative. Future Plan is what a consultant evaluates for management credibility.

**Input:**
```python
{
  "company_profile": {...},
  "wave_1_results": Dict[str, DomainResearchOutput],  # All 6+N domain outputs keyed by domain_name
  "model_config": ModelConfig
}
```

**Output:** Domain-specific synthesis JSON.

**7. Investment Thesis synthesis:**
- Reads: Financials, Market Position, Track Record, Credit & Risk
- Outputs: 3 thesis bullets — competitive moat / current story / forward opportunity
- Each bullet is data-backed (cites specific numbers from Wave 1 results)

**8. SWOT synthesis:**
- Reads: All Wave 1 outputs
- Outputs: 4 quadrants × 4-5 bullets each, all data-backed
- Strengths from Financials + Market Position; Weaknesses from Credit & Risk + gaps in Track Record; Opportunities from Market Position + sector-specific domains; Threats from Credit & Risk + Geography (FX, regulatory)

**9. Future Plan synthesis:**
- Reads: Track Record (catalysts), Financials (capacity for growth), sector-specific (pipeline, capex plans)
- Outputs: 5-7 quantified strategic targets with timelines

**Model config:**
```python
# All three:
ModelConfig(provider="anthropic", model="claude-opus-4-5", temperature=0.3, max_tokens=2000)
```

These are synthesis-heavy. Opus pays off — these are the sections that get directly read by the four personas to form their first impression.

**Failure modes:**
- Wave 1 data too sparse for synthesis → produce shorter synthesis, flag low confidence
- Synthesis produces unsupported claims → validation skill (next) catches these

**Dependencies:** All Wave 1 domain skills must complete first.

**Parallelism:** All three synthesis skills run in parallel with each other.

---

### Skill 10: Data Validation & Deduplication

**Module:** `skills/data_validation.py`

**Purpose.** Final pass over the populated JSON before compilation. Catches duplicates, inconsistencies, and unsupported claims.

**Persona anchor.** Implicit — the goal is no persona reads the one-pager and sees obviously wrong, duplicated, or unsupported data.

**Input:** Fully populated FinalJSON.
**Output:** Cleaned FinalJSON + validation report.

**Internal checks:**

1. **Numeric deduplication.** Revenue appears in Key Stats AND Financial Performance AND Key Metrics — must be the same number in all three. If different, flag and use Financial Performance as source of truth.
2. **Unit consistency.** Catch ₹ Cr vs ₹ Lakh vs $ Million mixing.
3. **Date consistency.** Catch FY25 in one place, FY2025 in another, "March 2025" in a third.
4. **Citation completeness.** Every numerical claim must have a citation. Synthesis claims must reference Wave 1 source data.
5. **SWOT-data alignment.** Each SWOT bullet must be traceable to a real data point in Wave 1 results. If a "Strength" claims best-in-class margins, the Financials data should show this — validation catches inventions.
6. **Completeness gates.** If a section has < 30% of expected data, mark it "Not Available" rather than showing a half-filled section.

**Internal logic:** Mostly Python rule-checking. One model call only for SWOT-data alignment (which requires semantic comparison).

**Model config (for SWOT alignment check):**
```python
ModelConfig(provider="anthropic", model="claude-haiku-4-5", temperature=0.1, max_tokens=1000)
```

Haiku is fine — this is mechanical checking, not creative synthesis.

**Failure modes:**
- Unfixable duplication → log, prefer source of truth, continue
- Major data inconsistency → fail loudly, return partial result with warning

**Dependencies:** All Wave 2 synthesis must complete first.

---

### Skill 11: HTML Compiler

**Module:** `skills/html_compiler.py`

**Purpose.** Final step. Take validated JSON, produce complete standalone HTML matching the Kelp design exactly.

**Persona anchor.** Final output is what every persona actually sees. The visual fidelity to Embio_OnePager.html determines whether the result feels professional or template-y.

**Input:** Validated FinalJSON + reference to `references/Embio_OnePager.html` and `references/kelp.css`.
**Output:** Complete HTML string, ready to save and serve.

**Internal steps:**

1. Build header section (company name, CIN/ticker, sector, founded).
2. For each section in left column (in order): render based on `content_type`:
   - `stat_grid` → 6-card stats bar
   - `table` → table with appropriate styling
   - `chart` → Canvas-based chart with embedded data
   - `swot_grid` → 2×2 colored grid
   - `prose` → paragraph with optional thesis box
   - `list` → bulleted list with sub-grouping
3. Same for right column.
4. Render citations as inline superscripts + references section at bottom.
5. Render footer with sources note + KELP GLOBAL branding.

**Prompt design notes:**
- System prompt embeds the full Kelp CSS (loaded from `references/kelp.css`).
- System prompt embeds the Embio HTML as a worked example showing exact structure.
- System prompt embeds the design rules from CLAUDE.md verbatim.
- User prompt is just the FinalJSON.
- Response must start with `<!DOCTYPE html>` and be parseable as valid HTML.

**Model config:**
```python
ModelConfig(provider="anthropic", model="claude-sonnet-4-6", temperature=0.2, max_tokens=12000)
```

Sonnet handles long, structured output well. Opus would also work but is overkill — at this stage the JSON is structured enough that the model just needs good instruction-following, not deep reasoning.

**Failure modes:**
- Incomplete HTML (no closing tags, truncation) → automatic re-prompt with "complete the HTML from where it left off"
- Hallucinated data not in JSON → validation skill should have caught upstream; if it leaks here, log warning

**Dependencies:** Validation must complete.

---

## Section 4 — Orchestrator

**Module:** `orchestrator.py`

**Purpose.** Top-level coordinator. Wires the skills together with the wave structure described in Section 0.2.

**Pseudocode:**

```python
async def generate_one_pager(input: PipelineInput) -> Tuple[str, Telemetry]:
    telemetry = TelemetryCollector()

    # PHASE 1: Sector Research (sequential, blocking)
    sector_result = await sector_research.run(input, config.sector_research_model)
    telemetry.record("sector_research", sector_result)

    # PHASE 2: Parallel — Branch A and Branch B
    branch_a_task = asyncio.create_task(_run_branch_a(sector_result))
    branch_b_task = asyncio.create_task(_run_branch_b(sector_result))

    section_plan, wave_1_results = await asyncio.gather(branch_a_task, branch_b_task)
    telemetry.merge(section_plan.telemetry, wave_1_results.telemetry)

    # PHASE 3: JSON Population (continuous, but here completes since Wave 1 done)
    populated_json = json_population.run(section_plan.skeleton, section_plan, wave_1_results)

    # PHASE 4: Wave 2 Synthesis (3 skills in parallel)
    thesis, swot, future = await asyncio.gather(
        synthesis_investment_thesis.run(populated_json, config.thesis_model),
        synthesis_swot.run(populated_json, config.swot_model),
        synthesis_future_plan.run(populated_json, config.future_model)
    )
    populated_json.attach_synthesis(thesis, swot, future)

    # PHASE 5: Validation
    validated_json = await data_validation.run(populated_json, config.validation_model)

    # PHASE 6: Compile
    html = await html_compiler.run(validated_json, config.compiler_model)

    return html, telemetry.finalize()


async def _run_branch_a(sector_result):
    scores = await importance_scoring.run(sector_result, config.scoring_model)
    plan = layout_planning.run(scores)  # No async, pure Python
    return plan

async def _run_branch_b(sector_result):
    # Build skill invocations from sector_result
    generic_tasks = [
        domain_financials.run(...),
        domain_corporate_structure.run(...),
        domain_market_position.run(...),
        domain_track_record.run(...),
        domain_credit_risk.run(...),
        domain_geography.run(...),
    ]
    sector_tasks = [
        domain_sector_specific.run(domain=d, ...) for d in sector_result.sector_domains
    ]
    results = await skill_functions.run_skills_in_parallel(
        skills=generic_tasks + sector_tasks,
        max_concurrent=8,
        per_skill_timeout=30
    )
    return aggregate_domain_results(results)
```

**Error handling:**
- Phase 1 failure → fatal, return error to caller (can't proceed)
- Any single Wave 1 skill failure → log, mark affected sections "Not Available," continue
- Wave 2 synthesis failure → use template fallback ("Investment thesis could not be synthesized from available data"), continue
- Validation failure → return partial result with warning header
- Compile failure → retry once with shorter input (truncate to most important sections)

**Telemetry output:**
```python
{
  "total_duration_ms": 38421,
  "phase_timings": {...},
  "skill_timings": [...],
  "total_cost_usd": 0.34,
  "total_tokens": 87234,
  "sections_populated": 31,
  "sections_partial": 2,
  "sections_unavailable": 1,
  "warnings": [...]
}
```

---

## Section 5 — Configuration

**Module:** `config.py`

Central config. Every skill imports from here.

```python
@dataclass
class ModelConfig:
    provider: Literal["anthropic", "openai", "gemini"]
    model: str
    temperature: float = 0.2
    fallback: Optional["ModelConfig"] = None

# Per-skill model assignments
SECTOR_RESEARCH_MODEL = ModelConfig("anthropic", "claude-opus-4-5", 0.2, fallback=ModelConfig("anthropic", "claude-sonnet-4-6"))
IMPORTANCE_SCORING_MODEL = ModelConfig("anthropic", "claude-sonnet-4-6", 0.2)
FINANCIALS_DOMAIN_MODEL = ModelConfig("anthropic", "claude-sonnet-4-6", 0.1)
# ... etc per skill

# Concurrency limits
MAX_PARALLEL_SKILLS = 8
PER_SKILL_TIMEOUT_SECONDS = 30
TOTAL_PIPELINE_TIMEOUT_SECONDS = 120

# API keys (loaded from .env)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Output paths
OUTPUT_DIR = "./output"
REFERENCE_DIR = "./references"
```

This is the file you edit to swap a skill's model without touching skill code.

---

## Section 6 — Implementation order for Claude Code

When handing this to Claude Code, build in this order. Each phase produces something testable before moving on.

**Phase A (foundation, ~1-2 prompts):**
1. Project scaffolding (folders, requirements.txt, .env.example)
2. `schemas.py` — all Pydantic models
3. `config.py` — model configs and constants

**Phase B (shared library, ~2 prompts):**
4. `skill_functions.py` — model adapter, parsing, validation utilities
5. Test the adapter against all three providers with a simple "hello" call

**Phase C (Phase 1 of pipeline, ~1 prompt):**
6. `skills/sector_research.py` — the foundational skill
7. Test it with 2-3 example companies (Embio, Cipla, HDFC Bank)
8. Iterate on prompt until domain output looks right

**Phase D (Phase 2 of pipeline, ~2 prompts):**
9. `skills/importance_scoring.py`
10. `skills/layout_planning.py`
11. All 6 generic domain skills (one prompt, since they share structure)
12. `skills/domain_sector_specific.py` (templated)
13. Test each domain skill in isolation against the Embio data we already have

**Phase E (Phase 3-4 of pipeline, ~1 prompt):**
14. `skills/json_population.py`
15. Three synthesis skills

**Phase F (final phases, ~1 prompt):**
16. `skills/data_validation.py`
17. `skills/html_compiler.py` — uses Embio_OnePager.html as reference

**Phase G (orchestration, ~1 prompt):**
18. `orchestrator.py`
19. CLI entry point + web UI

**Phase H (testing, ongoing):**
20. End-to-end test with Embio (validate output matches what we already have)
21. End-to-end test with one company per sector (Cipla pharma, HDFC banking, Infosys tech, HUL consumer, Tata Steel manufacturing, DLF real estate, NTPC energy)
22. Iterate on prompts based on output quality

---

## Section 7 — What ships with the system

Files Claude Code should expect to find in the project directory at start:

1. `CLAUDE.md` — project blueprint (already exists)
2. `Embio_OnePager.html` — design reference, the "this is what good output looks like" anchor
3. `references/Generic_Sections_Reference.csv` — what each generic section contains
4. `references/Kelp_CSS_Classes_Reference.md` — CSS class definitions
5. `references/Sector_Specific_Sections_Metadata.csv` — sector section metadata (already partially built)
6. This specification document (kelp_skill_specification.md)
7. `references/sector_taxonomies.json` — to be built; the 7 baseline sector domain templates

---

## Section 8 — Open questions for future iterations

Things deliberately left out of v1 to keep scope manageable. Worth revisiting after the system works end-to-end:

- **Caching layer.** Sector research output is highly cacheable per (subsector, revenue_range) bucket. Could cut cost and latency 50%+ at scale. v2 work.
- **Multi-language output.** Currently English-only. Adding Hindi/regional language summaries is a single-skill addition.
- **Excel export.** The data underlying each one-pager is itself valuable; a parallel skill exporting to xlsx would extend the product without changing the pipeline.
- **Quality grading.** A meta-skill that grades the final one-pager (data completeness, citation density, persona-relevance score) and either accepts or asks the pipeline to re-research weak sections. v2 work.
- **Diff mode.** Generating one-pager updates by re-running only the skills whose underlying data has changed (mostly news, financials, deals) rather than full regeneration.

---

## End of specification

Hand this document to Claude Code along with the existing project files. Tell it to start with Phase A and report after each phase before moving to the next. Each phase should produce testable output. Don't let it skip the iterative testing in Phase C — Sector Research quality determines everything downstream.

# Kelp One-Pager Agent

## What this project does

An AI system that generates professional financial one-pagers for any company. The user provides a company name (and optionally sector and business description); the system researches the company, plans the right sections for that company's profile, runs domain research in parallel, synthesizes higher-order analysis, validates the result, and compiles a styled HTML one-pager in the Kelp visual format.

**This is a SOURCING one-pager — a top-of-funnel screening doc.** An analyst reads it to decide whether a company is worth **deeper research (pursue vs pass)**, NOT to make a final investment/lending/deal decision. Every design choice should favour the signals that drive that triage — scale, momentum, distinctiveness, obvious dealbreakers — over exhaustive diligence detail.

## Who this is for

Every design decision serves four end users, each making the same **screen-in / screen-out** call:
- **PE analysts** — is this worth diligencing as an investment?
- **Investment bankers** — is there a live deal angle worth pitching?
- **Credit analysts** — worth a closer underwriting look; any obvious red flags?
- **Consultants** — is there an operational angle worth pursuing?

The persona-relevance test is the discipline that prevents scope creep: every domain, section, and data point must materially change at least one persona's **pursue-vs-pass** decision. If it only adds "context," it doesn't earn a place.

## Architecture in one paragraph

Skill-based parallel pipeline. **Phase 1** (Sector Research) runs alone — it determines what gets researched and is the only sequential bottleneck. **Phase 2** fires two branches in parallel: Branch A does Importance Scoring + Layout Planning (fast); Branch B runs ~9 Wave 1 domain research skills concurrently (6 generic + 3-5 sector-specific). **Phase 3** (JSON Assembly) continuously populates a skeleton as Wave 1 results arrive. **Phase 4** runs 3 Wave 2 synthesis skills in parallel (Investment Thesis, SWOT, Future Plan) — these read Wave 1 output rather than re-researching. **Phase 5** validates and deduplicates. **Phase 6** compiles JSON to HTML.

Total wall-clock time = Phase1 + max(Wave1 skills) + Wave2 + validation + compile. Roughly 30-60 seconds typical.

## Tech stack

- **Language**: Python 3.11+ (async-first)
- **AI providers**: Anthropic, OpenAI, Gemini — abstracted behind a shared model adapter so any skill's model can be swapped via config
- **Web framework**: FastAPI for the UI/API layer
- **CLI**: Typer + rich
- **Validation**: Pydantic for all JSON contracts between skills
- **Output**: HTML files saved to `output/`, browser-renderable, no build step

## Project structure

```
kelp-agent/
├── CLAUDE.md                          # This file — project blueprint
├── kelp_skill_specification.md        # Detailed spec for every skill (implementation reference)
├── orchestrator.py                    # Top-level pipeline coordinator
├── skill_functions.py                 # Shared library — model adapter, search, parsing
├── schemas.py                         # Pydantic models for all inter-skill contracts
├── config.py                          # Model assignments per skill, API keys, timeouts
├── skills/
│   ├── sector_research.py             # Phase 1
│   ├── importance_scoring.py          # Phase 2, Branch A
│   ├── layout_planning.py             # Phase 2, Branch A
│   ├── domain_financials.py           # Wave 1 — generic
│   ├── domain_corporate_structure.py  # Wave 1 — generic
│   ├── domain_market_position.py      # Wave 1 — generic
│   ├── domain_track_record.py         # Wave 1 — generic
│   ├── domain_credit_risk.py          # Wave 1 — generic
│   ├── domain_geography.py            # Wave 1 — generic
│   ├── domain_sector_specific.py      # Wave 1 — templated, instantiated per sector domain
│   ├── synthesis_investment_thesis.py # Wave 2
│   ├── synthesis_swot.py              # Wave 2
│   ├── synthesis_future_plan.py       # Wave 2
│   ├── json_population.py             # Phase 3 — continuous listener
│   ├── data_validation.py             # Phase 5
│   └── html_compiler.py               # Phase 6
├── main.py                            # FastAPI entry point
├── cli.py                             # Typer CLI entry point
├── templates/
│   └── index.html                     # Web UI for the agent
├── references/                        # Design references — read by skills, not modified
│   ├── Embio_OnePager.html            # Visual reference — the one-pager design to match
│   ├── kelp.css                       # Embedded CSS for compiled output
│   ├── Generic_Sections_Reference.csv # What each generic section contains
│   ├── Kelp_CSS_Classes_Reference.md  # CSS class definitions
│   ├── Sector_Specific_Sections_Metadata.csv  # Sector section catalog
│   └── sector_taxonomies.json         # 7 baseline sector domain templates (built in Phase C)
├── output/                            # Generated HTML one-pagers saved here
├── requirements.txt
└── .env                               # API keys (never commit)
```

## Skill organization principle

Skills are organized by **data domain**, not by output section. A single domain skill fetches related data once and feeds multiple final one-pager sections. For example, the Financials domain skill feeds the Key Stats bar, Financial Performance chart, Key Metrics table, and Working Capital section — all from one research pass. This eliminates redundant searches and prevents inconsistency (same revenue number appearing as ₹272.85 Cr in one section and ₹273 Cr in another).

The 6 generic domains and what they feed:
- **Financials & Ratios** → Key Stats, Financial Performance, Key Metrics, Working Capital
- **Corporate Structure** → Ownership, Shareholders, Leadership, Board, Compliance/ESG
- **Market Position** → Peers, Market Size, Products & Services, Channel Mix
- **Track Record** → Deals, Milestones, Recent News, Upcoming Catalysts
- **Credit & Risk** → Credit Ratings, Risk Flags
- **Geography** → Revenue by Geography, Global Presence

Sector-specific domains are determined dynamically by the Sector Research skill (Phase 1) based on the company's subsector and business description. Pharma might generate domains like "Pipeline," "Regulatory & Manufacturing," "API Economics." Banking might generate "Asset Quality," "Funding & Liquidity," "Capital Adequacy." A templated `domain_sector_specific.py` skill handles all of these — there is no hardcoded "Pharma Pipeline skill."

## Wave 1 vs Wave 2

**Wave 1** skills fetch data from the world via web search. They run in parallel and don't depend on each other. ~9 skills total (6 generic + 3-5 sector-specific).

**Wave 2** skills synthesize across what Wave 1 found. They cannot run until all of Wave 1 completes because they read Wave 1's output rather than doing their own research. The three Wave 2 skills (Investment Thesis, SWOT, Future Plan) themselves run in parallel with each other.

This wave structure is what gives the pipeline its speed. Sequential cost would be 4-9 minutes; parallel collapses it to roughly the slowest single Wave 1 skill plus a short Wave 2 + cleanup tail.

## Model routing (multi-provider design)

Every skill calls one function from the shared library — `skill_functions.call_model(prompt, system, model_config, ...)`. The `model_config` object specifies provider (anthropic / openai / gemini), model string, temperature, and an optional fallback config. Internally the adapter dispatches to the right provider and normalizes the response.

This means:
- A skill never knows or cares which provider answered
- Swapping a model for one skill = editing one line in `config.py`, no skill code changes
- Different skills use different models based on cost/quality tradeoff:
  - Sector Research → highest tier (Opus) — errors propagate everywhere
  - Synthesis skills → high tier (Opus) — these are the most-read sections
  - Generic domain research → mid tier (Sonnet) — retrieval + light structuring
  - Simple lookups, validation → low tier (Haiku) — mechanical work

Web search behavior also normalized: passing `enable_search=True` to `call_model` works regardless of provider; the adapter knows how to enable Anthropic's web_search tool, OpenAI's browsing, or Gemini's Google Search grounding.

## Schemas-first development

`schemas.py` defines every JSON contract between skills using Pydantic. Each skill validates input on entry and output on exit. This is non-negotiable — without strict schemas, parallel skills silently produce slightly-wrong-shape data that breaks the JSON assembly step in confusing ways.

Key schemas: `PipelineInput`, `SectorResearchOutput`, `Domain`, `SectionPlan`, `PlannedSection`, `DomainResearchOutput`, `Source`, `PersonaRelevance`, `FinalJSON`, `CompletedSection`. Full definitions in `kelp_skill_specification.md` Section 2.

## Concurrency rules

- Wave 1 skills: max 8 concurrent (rate-limit safety; full list still finishes in ~max-skill-duration)
- Per-skill timeout: 30 seconds. A timed-out skill returns a `SkillError` object so the pipeline continues with that section marked "Not Available" rather than blocking
- Total pipeline timeout: 120 seconds
- Wave 1 failures never block other Wave 1 skills; the pipeline degrades gracefully

## Telemetry and observability

Every skill records: skill name, start/end time, duration, tokens in/out, estimated cost in USD, provider, model used, success/failure. The orchestrator aggregates these into a telemetry object returned alongside the HTML output. End-of-run summary shows: total cost, total time, per-skill breakdown, sections populated vs partial vs unavailable. Critical for cost monitoring as this runs at scale.

## Failure handling philosophy

The pipeline never crashes from a single skill failure — it degrades. Specifically:
- Phase 1 failure → fatal, return error (can't proceed without sector research)
- Any Wave 1 skill failure → affected sections marked "Not Available," pipeline continues
- Wave 2 synthesis failure → template fallback message ("Investment thesis could not be synthesized from available data"), pipeline continues
- Validation finds inconsistency → flag but don't block; prefer canonical source of truth
- Compile failure → automatic retry with truncated input (keep only highest-importance sections)

The final HTML always renders, even if partial. Better a 70% complete one-pager with honest "Not Available" markers than no output at all.

## Design rules for the generated HTML (mandatory)

The HTML compiler must produce output matching `references/Embio_OnePager.html`. Specifically:
- White background throughout — NO dark headers, NO colored section backgrounds
- Two-column grid: left 1.85fr (main content), right 1fr (sidebar)
- Section headers: bold 11px text with 0.5px gray (#E5E8EE) bottom border
- Every section header carries a pill tag: gray "Generic" or blue "[Sector name]"
- Kelp green (#3C9E41) ONLY for the logo and positive status pills
- Font: Inter from Google Fonts, 11px body
- Tables: #F7F8FA header background, 0.5px borders, alternating row tinting
- Pills: `.pg` (green), `.pr` (red), `.pa` (amber), `.pb` (blue), `.pn` (gray)
- Risk flags use icons (`.fig`, `.fia`, `.fir`) with corresponding background tints
- SWOT: 2×2 grid with quadrant colors (green Strengths, red Weaknesses, blue Opportunities, amber Threats)
- "Not Available" for missing data — never invent data
- Footer carries source attribution + "KELP GLOBAL · COMPANY PROFILES" branding

Full CSS class definitions in `references/Kelp_CSS_Classes_Reference.md`. The compiler embeds `references/kelp.css` directly into output HTML for self-contained files.

## Citations

Every numerical claim and major statement in the final one-pager carries a clickable superscript citation linking to a References section at the bottom. The references include source name, URL where available, accessed date, and which specific data points came from that source. Citation tracking is the domain skills' responsibility — every `DomainResearchOutput` includes per-field source attribution that flows through to the final HTML.

## Implementation phases

Build in this order. Each phase produces something testable before moving to the next.

- **Phase A** — Project scaffolding, `schemas.py`, `config.py`
- **Phase B** — `skill_functions.py` (shared library) + test against all three providers
- **Phase C** — `skills/sector_research.py` + iterate against 3 test companies (Embio, Cipla, HDFC Bank). **Do not proceed until sector research output looks right** — everything downstream depends on it.
- **Phase D** — Importance Scoring, Layout Planning, all 6 generic domain skills, templated sector-specific domain skill
- **Phase E** — JSON Population, 3 Wave 2 synthesis skills
- **Phase F** — Data Validation, HTML Compiler
- **Phase G** — Orchestrator wiring everything together, CLI, web UI
- **Phase H** — End-to-end testing across all sectors

Full phase-by-phase guidance in `kelp_skill_specification.md` Section 6.

## Test companies per sector

- **Pharma** — Embio Limited (validation: result should match the existing Embio_OnePager.html)
- **Banking** — HDFC Bank
- **Technology / IT Services** — Infosys
- **Consumer / FMCG** — Hindustan Unilever (HUL)
- **Manufacturing** — Tata Steel
- **Real Estate** — DLF
- **Energy** — NTPC

## Running the system

```bash
# Setup (one-time)
pip install -r requirements.txt
cp .env.example .env  # then add API keys

# CLI
python cli.py generate "Embio Limited"
python cli.py generate "HDFC Bank" --sector banking
python cli.py list

# Web UI
python main.py  # serves at localhost:8000
```

## Reference files Claude Code must read

Before implementing anything, Claude Code should read these in order:
1. This file (CLAUDE.md) — project orientation
2. `kelp_skill_specification.md` — detailed spec for every skill, the source of truth for implementation
3. `references/Embio_OnePager.html` — the visual reference; the compiler must produce HTML in this exact style
4. `references/Generic_Sections_Reference.csv` — what each of the 23 generic sections contains
5. `references/Kelp_CSS_Classes_Reference.md` — every CSS class with its definition

If anything in this CLAUDE.md and the specification document conflict, the specification document wins — it has more detail and was written second.

## Code style conventions

- Type hints everywhere
- Async functions for all I/O (FastAPI routes, all skill `run()` functions, all model calls)
- Pydantic models for all inter-skill data
- No silent failures — log every error with skill name + context
- No invented data — "Not Available" is always preferable to a guess
- All skill prompts include the persona-relevance test where relevant
- Print/log progress with timestamps in CLI mode for visibility into parallel execution

## Open questions deferred to v2

Not in v1 scope, documented for future iteration:
- Caching layer for sector research results (high reuse potential)
- Multi-language output
- Excel export of the underlying research data
- Quality grading meta-skill (grade the one-pager, re-research weak sections)
- Diff mode (update existing one-pagers without full regeneration)

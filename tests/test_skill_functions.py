"""Phase B smoke test.

Two parts:
  1. Pure-Python utility tests (run offline, no keys needed): JSON parsing,
     schema validation, partial-merge, and bounded-concurrency runner.
  2. Provider "hello" calls — run for any provider whose API key is set,
     skipped (not failed) otherwise.

Run: python test_skill_functions.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import skill_functions as sf
from schemas import PipelineInput


def test_json_utils() -> None:
    assert sf.parse_json_safe('{"a": 1}') == {"a": 1}
    assert sf.parse_json_safe('```json\n{"a": 1}\n```') == {"a": 1}
    assert sf.parse_json_safe('Sure! Here it is: {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    assert sf.parse_json_safe("not json at all") is None
    print("  [ok] parse_json_safe (fenced / prose-wrapped / invalid)")

    ok, errs = sf.validate_against_schema({"company_name": "Embio Limited"}, PipelineInput)
    assert ok, errs
    bad_ok, bad_errs = sf.validate_against_schema({"sector": "pharma"}, PipelineInput)  # missing required name
    assert not bad_ok and bad_errs
    print("  [ok] validate_against_schema (accepts valid, rejects missing required)")


def test_merge() -> None:
    skeleton = {"sections": {"financials": {"revenue": "", "ebitda": "100"}}}
    merged = sf.merge_partial_results(
        skeleton, {"revenue": "272.85", "ebitda": "999"}, path="sections.financials"
    )
    fin = merged["sections"]["financials"]
    assert fin["revenue"] == "272.85", fin          # empty slot filled
    assert fin["ebitda"] == "100", fin              # already-populated value preserved
    print("  [ok] merge_partial_results (fills empty, preserves populated)")


def test_parallel() -> None:
    async def good(inp):
        await asyncio.sleep(0.01)
        return {"v": inp["x"] * 2}

    async def slow(inp):
        await asyncio.sleep(5)
        return {"v": "should not arrive"}

    async def boom(inp):
        raise ValueError("kaboom")

    async def run():
        return await sf.run_skills_in_parallel(
            skills=[good, slow, boom],
            inputs=[{"x": 21}, {}, {}],
            max_concurrent=8,
            per_skill_timeout=1,
            skill_names=["good", "slow", "boom"],
        )

    results = asyncio.run(run())
    assert results[0] == {"v": 42}, results[0]
    assert isinstance(results[1], sf.SkillError) and results[1].error_type == "timeout", results[1]
    assert isinstance(results[2], sf.SkillError) and results[2].error_type == "exception", results[2]
    # order preserved + failures isolated
    print("  [ok] run_skills_in_parallel (order kept, timeout + exception isolated)")


def test_telemetry() -> None:
    tc = sf.TelemetryCollector()
    resp = sf.ModelResponse(
        text="hi", tokens_in=100, tokens_out=50,
        estimated_cost_usd=sf._estimate_cost("claude-sonnet-4-6", 100, 50),
        provider="anthropic", model="claude-sonnet-4-6",
    )
    from datetime import datetime
    tc.record_from_response("demo_skill", resp, datetime.now())
    summary = tc.finalize()
    assert summary["total_tokens"] == 150, summary
    assert summary["total_cost_usd"] > 0, summary
    print(f"  [ok] telemetry (150 tokens, ${summary['total_cost_usd']} for sonnet 100in/50out)")


async def test_providers() -> None:
    providers = [
        ("anthropic", config.ANTHROPIC_API_KEY, config.ModelConfig("anthropic", "claude-sonnet-4-6", max_tokens=20)),
        ("openai", config.OPENAI_API_KEY, config.ModelConfig("openai", "gpt-4o-mini", max_tokens=20)),
        ("gemini", config.GEMINI_API_KEY, config.ModelConfig("gemini", "gemini-1.5-flash", max_tokens=20)),
    ]
    for name, key, mc in providers:
        if not key:
            print(f"  [skip] {name}: no API key set")
            continue
        resp = await sf.call_model(
            prompt="Say the single word: hello",
            system="You reply with exactly one word.",
            model_config=mc,
            response_format="text",
        )
        if resp.error:
            print(f"  [FAIL] {name}: {resp.error}")
        else:
            print(f"  [ok] {name}: '{resp.text.strip()[:40]}' "
                  f"({resp.tokens_used} tok, ${resp.estimated_cost_usd:.6f}, {resp.latency_ms}ms)")


def main() -> None:
    print("Pure-Python utilities (offline):")
    test_json_utils()
    test_merge()
    test_parallel()
    test_telemetry()
    print("\nProvider hello-calls:")
    asyncio.run(test_providers())
    print("\nPhase B smoke test complete.")


if __name__ == "__main__":
    main()

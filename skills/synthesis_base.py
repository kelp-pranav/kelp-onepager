"""Shared helpers for Wave 2 synthesis skills.

Synthesis skills do NOT research — they read Wave 1 DomainResearchOutputs and
synthesize cross-domain insight. This module turns those outputs into a compact
text digest for prompting, and provides a common JSON synthesis call wrapper.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import config
import skill_functions as sf
from schemas import DomainResearchOutput

_EMPTY = {"", "not available", "n/a", "na", "none", "unknown", "-"}
_MAX_VAL_LEN = 240


def _found(value: Any) -> bool:
    return value is not None and str(value).strip().lower() not in _EMPTY


def digest(
    wave_1_results: Dict[str, Union[DomainResearchOutput, Any]],
    domain_names: Optional[List[str]] = None,
) -> str:
    """Compact, prompt-ready summary of the requested domains' found data."""
    lines: List[str] = []
    for name, out in wave_1_results.items():
        if domain_names is not None and name not in domain_names:
            continue
        if not isinstance(out, DomainResearchOutput):
            continue
        lines.append(f"## {name} (completeness {out.completeness}, confidence {out.confidence})")
        for field, entry in out.data.items():
            val = entry.get("value") if isinstance(entry, dict) else entry
            if _found(val):
                vs = str(val)
                lines.append(f"- {field}: {vs[:_MAX_VAL_LEN]}{'…' if len(vs) > _MAX_VAL_LEN else ''}")
    return "\n".join(lines) if lines else "(no Wave 1 data available)"


async def synthesize(
    system: str,
    prompt: str,
    model_config: "config.ModelConfig",
    enable_search: bool = False,
    documents: str = "",
) -> Dict[str, Any]:
    """Run a JSON synthesis call; return parsed dict or {} on failure.

    ``enable_search`` lets an interpretive skill (e.g. Risk flags) supplement the
    Wave 1 digest with its own fresh web search. ``documents`` appends authoritative
    local-document ground truth to the prompt.
    """
    full_prompt = prompt + sf._documents_block(documents)
    resp = await sf.call_model(
        full_prompt, system, model_config,
        enable_search=enable_search, response_format="json",
    )
    if isinstance(resp.parsed, dict):
        return resp.parsed
    return {}

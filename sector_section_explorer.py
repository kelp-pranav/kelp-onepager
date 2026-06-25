"""Streamlit explorer for SECTOR-SPECIFIC sections only.

Runs scripts/gen_sector_only.py for a company, then visualises the chosen sector
sections, the domains that were considered-but-rejected (with persona reasoning),
and the actual vs. estimated-full-workflow cost.

    pip install streamlit
    streamlit run sector_section_explorer.py
"""

from __future__ import annotations

import difflib
import glob
import html as _html
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

import streamlit as st

# Bridge Streamlit Cloud secrets -> process environment. On the cloud there is no .env;
# keys live in st.secrets, which does NOT auto-populate os.environ. config.py reads via
# os.getenv and the spawned gen_sector_only.py subprocess inherits this env, so seeding
# os.environ here makes BOTH work. Locally (no secrets file) this is a harmless no-op and
# the existing .env path is used. Never overwrite a value already set in the environment.
for _k in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_FLASH_MODEL",
           "LITELLM_FLASH_LITE_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
           "GEMINI_API_KEY"):
    try:
        if not os.getenv(_k) and _k in st.secrets:
            os.environ[_k] = str(st.secrets[_k])
    except Exception:
        pass  # no secrets.toml configured (local dev) — fall through to .env

import sections_catalog

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
GEN_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "gen_sector_only.py")

CONF_EMOJI = {"high": "🟢", "medium": "🟡", "low": "🔴"}
PERSONAS = [
    ("PE analyst", "pe_analyst"),
    ("Banker", "banker"),
    ("Credit analyst", "credit_analyst"),
    ("Consultant", "consultant"),
]

st.set_page_config(page_title="Sector Section Explorer", page_icon="🧭", layout="wide")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "company"


@st.cache_data(show_spinner=False)
def _list_runs(_cache_buster: float):
    """All saved sector-only runs (archived + canonical), newest first per company.

    Scans output/runs/ (immutable per-run archive) plus the canonical
    output/*_sector_only.json, de-duplicated by (company, generated_at). The
    ``_cache_buster`` arg lets the caller invalidate the cache after a new run.
    """
    paths = glob.glob(os.path.join(OUTPUT_DIR, "runs", "*_sector_only.json"))
    paths += glob.glob(os.path.join(OUTPUT_DIR, "*_sector_only.json"))
    runs, seen = [], set()
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        company = d.get("company_name") or os.path.basename(p)
        gen = d.get("generated_at") or ""
        key = (company, gen)
        if key in seen:
            continue
        seen.add(key)
        runs.append({
            "path": p,
            "company": company,
            "generated_at": gen,
            "n_sections": len(d.get("sections") or []),
            "n_rejected": len(d.get("considered_not_chosen") or []),
            "cost": (d.get("cost") or {}).get("actual_total_usd"),
        })
    return runs


def _load_polished(company: str):
    """My offline-distilled version of a company's sections, if one exists. This is the
    'pass it through Claude' artifact — written by me ingesting the run JSON, rendered by
    Streamlit with NO runtime API call. Returns {heading: [blocks]} or None."""
    path = os.path.join(OUTPUT_DIR, "polished", f"{_slug(company)}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("sections")
    except (OSError, json.JSONDecodeError):
        return None


def _runs_mtime() -> float:
    """Latest mtime across saved run files — used to bust the _list_runs cache."""
    paths = (glob.glob(os.path.join(OUTPUT_DIR, "runs", "*_sector_only.json"))
             + glob.glob(os.path.join(OUTPUT_DIR, "*_sector_only.json")))
    return max((os.path.getmtime(p) for p in paths), default=0.0)


def _why_lines(reasoning: dict) -> list:
    """All four rationale points as plain bullets — but with NO stakeholder named. Strips
    'Pursue:/Pass:' prefixes and any persona references, and de-dupes identical points."""
    def _clean(t) -> str:
        t = re.sub(r"^\s*(pursue|pass)\s*[:\-–—]\s*", "", str(t or ""), flags=re.I)
        # drop explicit persona references so no individual stakeholder is named
        t = re.sub(r"\b(a |an |the )?(pe |private equity |investment )?"
                   r"(analyst|banker|investment banker|credit analyst|consultant)s?\b",
                   "", t, flags=re.I)
        t = re.sub(r"^[\s,;:\-–—]+", "", re.sub(r"\s{2,}", " ", t)).strip()
        return t[:1].upper() + t[1:] if t else ""

    out = []
    for key in ("pe_analyst", "banker", "credit_analyst", "consultant"):
        c = _clean(reasoning.get(key))
        if not c:
            continue
        if any(difflib.SequenceMatcher(None, c.lower(), p.lower()).ratio() > 0.85
               for p in out):
            continue
        out.append(c)
    return out


# Only verdicts that carry a CONCRETE, actionable correction are hard flags. A bare
# "unsupported" from the (weak, grounded-Flash-Lite) checker is treated as soft "could
# not independently verify" — it is too noisy/false-negative-prone to alarm the analyst.
_VERDICT_FLAG = {"corrected": "corrected", "scope_mismatch": "scope mismatch"}
_PCT = re.compile(r"-?\d[\d,]*\.?\d*\s*%")


def _has_source(field: dict) -> bool:
    return any((s or {}).get("url") for s in (field.get("sources") or []))


def _implausible(value) -> bool:
    """Deterministic sanity check — a percentage outside [-100%, 1000%] is almost surely wrong."""
    for m in _PCT.findall(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)):
        try:
            n = float(m.replace("%", "").replace(",", "").strip())
        except ValueError:
            continue
        if n < -100 or n > 1000:
            return True
    return False


def _trust_summary(fields: list, section_has_sources: bool = False):
    """(verified, flagged, unverified, unsourced, [ (label, reason, note) ]) for a section.

    - verified   : confirmed by the accuracy-verification pass.
    - flagged    : a CONCRETE correction (corrected / scope_mismatch) or an implausible value
                   — the only hard issues shown in the alarm expander.
    - unverified : checker couldn't independently confirm (soft; not alarming).
    - unsourced  : no per-field source AND the section has no references at all (genuine gap).

    `section_has_sources` lets a field ride the section-level reference list: the data WAS
    researched with sources; only the per-field link dropped, so we don't scream 'unsourced'
    on a section that clearly carries its references."""
    verified = flagged = unverified = unsourced = 0
    issues = []
    for f in fields or []:
        label = f.get("label", "")
        verdict = f.get("verdict")
        if verdict == "confirmed":
            verified += 1
        elif verdict in _VERDICT_FLAG:
            flagged += 1
            issues.append((label, _VERDICT_FLAG[verdict], f.get("verify_note", "")))
        elif verdict == "unsupported":
            unverified += 1
        if _implausible(f.get("value")):
            flagged += 1
            issues.append((label, "implausible value", ""))
        elif not _has_source(f) and not section_has_sources:
            unsourced += 1
    return verified, flagged, unverified, unsourced, issues


def _persona_lines(reasoning: dict) -> bool:
    """Render all rationale points as plain bullets — no named stakeholders.
    Returns True if anything was shown."""
    lines = _why_lines(reasoning)
    for ln in lines:
        st.write(f"- {ln}")
    return bool(lines)


def _score_bar(reasoning: dict) -> None:
    score = reasoning.get("overall_score") or 0
    st.caption(f"Overall persona-relevance score: {score}/100")
    st.progress(min(max(int(score), 0), 100) / 100)


# --------------------------------------------------------------------------- #
# Readable rendering of a field's (possibly nested) value                      #
# --------------------------------------------------------------------------- #
_TITLE_KEYS = ("name", "period", "partner", "segment", "bottler", "location",
               "target", "category", "market", "region", "metric", "label")
_SOURCE_KEYS = {"sources", "source", "url", "source_url", "link", "links",
                "citation", "citations"}
_DATE_KEYS = ("date", "period", "quarter", "year", "fy", "as_of_date", "renewal_date")
_MAIN_KEYS = ("headline", "event", "description", "details", "summary", "terms",
              "milestone", "target", "title", "status", "growth", "name", "value")


def _esc(s) -> str:
    """Escape markdown gotchas — chiefly '$' (Streamlit renders $…$ as LaTeX,
    which is what garbled the '$1 billion' values)."""
    return str(s).replace("\\", "\\\\").replace("$", "\\$").replace("*", "\\*")


def _inline(v) -> str:
    """One-line, human representation of a scalar or small nested structure.
    Source/URL keys are omitted — links become 🔗 icons, never raw text."""
    if isinstance(v, dict):
        return ", ".join(f"{k}: {_inline(x)}" for k, x in v.items()
                         if k not in _SOURCE_KEYS and x not in (None, "", "Not Available"))
    if isinstance(v, list):
        return "; ".join(_inline(x) for x in v)
    return _esc(v)


def _is_flat_dict_list(value) -> bool:
    return (isinstance(value, list) and len(value) > 0
            and all(isinstance(x, dict) for x in value)
            and all(not isinstance(v, (dict, list)) for x in value for v in x.values()))


def _item_sources(item) -> list:
    """Per-item sources embedded inside a dict value -> [{name, url}]."""
    out = []
    if not isinstance(item, dict):
        return out
    raw = item.get("sources")
    if isinstance(raw, list):
        for s in raw:
            if isinstance(s, dict) and (s.get("name") or s.get("url")):
                out.append({"name": s.get("name"), "url": s.get("url")})
    if not out and (item.get("url") or item.get("source")):
        out.append({"name": item.get("source") or item.get("name"), "url": item.get("url")})
    return out


def _strip_sources(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _SOURCE_KEYS}


def _first(item: dict, keys):
    for k in keys:
        v = item.get(k)
        if v not in (None, "", "Not Available"):
            return k, v
    return None, None


def _item_line(item: dict) -> str:
    """One readable bullet for a dict item: '**<date>** — <main>  · extras  🔗'.
    Embedded sources become a 🔗 icon; raw URLs never appear as text."""
    icon = _src_icons(_item_sources(item))
    date_k, date = _first(item, _DATE_KEYS)
    main_k, main = _first(item, _MAIN_KEYS)
    used = ({date_k, main_k} - {None}) | _SOURCE_KEYS
    extras = [f"{_esc(k)}: {_inline(v)}" for k, v in item.items()
              if k not in used and v not in (None, "", "Not Available")]
    if date is not None and main is not None:
        head = f"**{_esc(date)}** — {_esc(main)}"
    elif main is not None:
        head = _esc(main)
    elif date is not None:
        head = f"**{_esc(date)}**"
    else:
        head = _inline(_strip_sources(item)) or "_(item)_"
    line = "- " + head
    if extras:
        line += "  ·  " + "; ".join(extras)
    if icon:
        line += f"  {icon}"
    return line


def _value_md(value) -> str:
    """Render a nested value as compact markdown bullets ('smaller pointers').
    Source/URL keys never print as text — they become 🔗 icons."""
    lines = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _SOURCE_KEYS or v in (None, "", "Not Available"):
                continue
            if isinstance(v, (dict, list)):
                lines.append(f"- **{_esc(k)}:** {_inline(v)}")
            else:
                lines.append(f"- **{_esc(k)}:** {_esc(v)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                lines.append(_item_line(item))
            else:
                lines.append("- " + _esc(item))
    else:
        lines.append(_esc(value))
    return "\n".join(lines)


def _field_pairs(section: dict) -> list:
    """[(label, structured_value, sources)] for a section — from the new 'fields'
    key (with per-field sources), or recovered from legacy 'bullets' strings."""
    if section.get("fields"):
        return [(f.get("label", ""), f.get("value"), f.get("sources") or [])
                for f in section["fields"]]
    pairs = []
    for b in section.get("bullets") or []:
        if not isinstance(b, str):
            pairs.append(("", b, []))
            continue
        label, _, vstr = b.partition(": ")
        if not vstr:
            label, vstr = "", b
        try:
            value = json.loads(vstr)
        except (ValueError, TypeError):
            value = vstr
        pairs.append((label, value, []))
    return pairs


def _src_icons(sources: list) -> str:
    """Sources as small clickable 🔗 icons (hover shows the publisher name) —
    keeps the verification link without the verbose source text."""
    icons = []
    for s in sources or []:
        url = s.get("url")
        if not url:
            continue
        name = (s.get("name") or "source").replace('"', "'").replace("\\", "/")
        icons.append(f'[🔗](<{url}> "{name}")')
    return " ".join(icons)


_STOP = {"and", "or", "the", "of", "by", "for", "in", "on", "with", "a", "an",
         "to", "vs", "per", "its", "from", "key", "total", "overall"}


def _toks(s) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", str(s).lower())
            if t and t not in _STOP and len(t) > 2}


def _tok_match(a: str, b: str) -> bool:
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.82


def _affinity(label: str, heading: str) -> int:
    ft, ht = _toks(label), _toks(heading)
    return sum(1 for x in ft if any(_tok_match(x, y) for y in ht))


def _split_fields(headings: list, field_tuples: list) -> dict:
    """Assign each field to the section heading it best matches, so sections sharing
    one research domain each get their OWN distinct subset (deterministic, no API)."""
    assign = {h: [] for h in headings}
    for ft in field_tuples:
        scored = sorted(((_affinity(ft[0], h), -i, h) for i, h in enumerate(headings)),
                        reverse=True)
        assign[scored[0][2]].append(ft)
    # No empty sections: lend a best-matching field from the richest section.
    for h in [h for h in headings if not assign[h]]:
        donors = sorted((d for d in headings if len(assign[d]) > 1),
                        key=lambda d: len(assign[d]), reverse=True)
        if donors:
            best = max(assign[donors[0]], key=lambda ft: _affinity(ft[0], h))
            assign[donors[0]].remove(best)
            assign[h].append(best)
    return assign


# --------------------------------------------------------------------------- #
# Embio-style HTML rendering (deterministic, no API)                           #
# A scoped subset of references/kelp.css — every selector lives under          #
# `.kelp-op` so the bare element rules (table / ul / li) NEVER bleed into and   #
# break Streamlit's own UI. Do not add the global `*` / `body` resets here.     #
# --------------------------------------------------------------------------- #
_KELP_CSS = """
.kelp-op { font-family: 'Inter', sans-serif; color: #1A2332; }
.kelp-op .sec { margin: 0; }
.kelp-op .sec-h { font-size: 12.5px; font-weight: 700; color: #1A2332;
  border-bottom: 1.5px solid #1A2332; padding-bottom: 5px; margin-bottom: 8px;
  display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.kelp-op .sec-h .htext { display: flex; align-items: center; gap: 6px; }
.kelp-op .tag { display: inline-block; font-size: 8px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.07em; padding: 2px 6px; border-radius: 100px; }
.kelp-op .tag.tg { background: #F1EFE8; color: #5F5E5A; }
.kelp-op .tag.tp { background: #EAF3FF; color: #185FA5; }
.kelp-op .body-text { font-size: 11px; line-height: 1.6; color: #1A2332; margin: 4px 0; }
.kelp-op table { width: 100%; border-collapse: collapse; font-size: 10px; margin: 4px 0; }
.kelp-op th { background: #F7F8FA; color: #5A6878; font-size: 9.5px; font-weight: 600;
  text-align: left; padding: 4px 6px; border-bottom: 0.5px solid #E5E8EE; }
.kelp-op td { padding: 4px 6px; text-align: left; border-bottom: 0.5px solid #E5E8EE;
  font-size: 10px; vertical-align: top; }
.kelp-op tr:nth-child(even) td { background: #FCFCFC; }
.kelp-op ul { list-style: none; padding: 0; margin: 4px 0; }
.kelp-op li { font-size: 10.5px; position: relative; padding-left: 12px;
  margin-bottom: 3px; line-height: 1.45; }
.kelp-op li::before { content: "\\2022"; position: absolute; left: 0; color: #3C9E41; }
.kelp-op .pill { display: inline-block; font-size: 9px; padding: 1px 6px; border-radius: 100px;
  margin: 1px 2px; font-weight: 500; }
.kelp-op .pg { background: #EAF3DE; color: #27500A; }
.kelp-op .pr { background: #FCEBEB; color: #791F1F; }
.kelp-op .pa { background: #FAEEDA; color: #633806; }
.kelp-op .pb { background: #EAF3FF; color: #185FA5; }
.kelp-op .pn { background: #F1EFE8; color: #5F5E5A; }
.kelp-op .dr { display: flex; gap: 8px; padding: 3px 0; border-bottom: 0.5px solid #E5E8EE;
  align-items: flex-start; }
.kelp-op .dr:last-child { border-bottom: none; }
.kelp-op .dl { width: 150px; flex-shrink: 0; color: #5A6878; font-weight: 500; font-size: 9.5px; }
.kelp-op .dv { flex: 1; color: #1A2332; font-size: 10.5px; }
.kelp-op .ni { padding: 3.5px 0; border-bottom: 0.5px solid #E5E8EE; }
.kelp-op .ni:last-child { border-bottom: none; }
.kelp-op .nd { font-size: 8.5px; color: #8A9AB0; margin-bottom: 1px; font-weight: 600; }
.kelp-op .nt { color: #1A2332; font-weight: 500; line-height: 1.35; font-size: 10.5px; }
.kelp-op .ns { font-size: 9px; color: #8A9AB0; margin-top: 1px; }
.kelp-op .src { font-size: 9px; color: #8A9AB0; margin-top: 8px; line-height: 1.4;
  border-top: 0.5px solid #E5E8EE; padding-top: 5px; }
.kelp-op sup a { text-decoration: none; font-size: 9px; }
.kelp-op .take { padding: 6px 9px; border-left: 2.5px solid #3C9E41; background: #F5FBF5;
  border-radius: 0 3px 3px 0; margin: 4px 0 8px; font-size: 10.5px; line-height: 1.5; }
.kelp-op .take b { color: #27500A; }
"""

_CONF_PILL = {"high": "pg", "medium": "pa", "low": "pr"}


def _h(s) -> str:
    """HTML-escape a scalar for safe insertion into the Embio markup."""
    return _html.escape(str(s), quote=True)


def _h_sources(sources: list) -> str:
    """Sources as small clickable superscript 🔗 icons (hover = publisher name).
    Icon only — raw URLs never appear as text."""
    links = []
    for s in sources or []:
        url = (s or {}).get("url")
        if not url:
            continue
        name = _h((s.get("name") or "source"))
        links.append(f'<sup><a href="{_h(url)}" title="{name}" target="_blank">🔗</a></sup>')
    return "".join(links)


def _src_footer(pairs: list) -> str:
    """A single Embio-style '.src' footer line listing this section's distinct sources."""
    names, seen = [], set()
    for _l, _v, srcs in pairs:
        for s in srcs or []:
            nm = (s or {}).get("name")
            if nm and nm not in seen:
                seen.add(nm)
                names.append(nm)
    if not names:
        return ""
    return f'<div class="src">Source: {_h("; ".join(names))}</div>'


def _h_rows(scalars: list) -> str:
    """Short label/value pairs as Embio detail rows (.dr = gray label | value 🔗)."""
    out = []
    for label, value, sources in scalars:
        lbl = _h(str(label).rstrip(":").strip()) or "&nbsp;"
        out.append(f'<div class="dr"><div class="dl">{lbl}</div>'
                   f'<div class="dv">{_h(value)} {_h_sources(sources)}</div></div>')
    return "".join(out)


def _h_bullets(items: list, sources: list) -> str:
    """A list of scalars as tight green bullets; section sources ride the last item."""
    lis = "".join(f"<li>{_h(_inline(x) if isinstance(x, (dict, list)) else x)}</li>"
                  for x in items if x not in (None, "", "Not Available"))
    return f"<ul>{lis}</ul>" if lis else ""


# --- crispness helpers: distil verbose research into the important bits only --- #
_LABELISH = ("period", "date", "quarter", "year", "fy", "entity", "company",
             "region", "segment", "asset", "name", "metric", "market", "category",
             # identity / dimension columns (the row's "what", not its value)
             "practice", "area", "vertical", "geography", "country", "product",
             "partner", "offering", "division", "subsidiary")


def _labelish(c: str) -> bool:
    return any(t in str(c).lower() for t in _LABELISH)


_ABSENCE = re.compile(
    r"\b(not found|not (?:explicitly |consistently |publicly )?(?:found|reported|"
    r"disclosed|available)|were not (?:explicitly )?found|no (?:specific|publicly "
    r"disclosed|distinct)|not (?:a |as a )?(?:distinct|publicly))\b", re.I)
_DROP_SENTENCE = re.compile(
    r"\b(reflects the impact|is defined as|refers to|are not consistently|"
    r"frequently reports|is primarily based|is embedded within)\b", re.I)
_NUM = re.compile(r"[+\-]?\$?\d")


def _sentences(t: str) -> list:
    return [s for s in re.split(r"(?<=[.!?])\s+", " ".join(str(t).split())) if s]


def _first_sentence(s, cap: int = 150) -> str:
    """First sentence of a value, capped — used to keep notes/headlines crisp."""
    s = " ".join(str(s).split())
    if not s:
        return ""
    first = _sentences(s)[0]
    if len(first) > cap:
        first = first[:cap].rsplit(" ", 1)[0].rstrip(" ,;.") + "…"
    return first


def _cell(r: dict, c: str) -> str:
    v = r.get(c)
    if v in (None, "", "Not Available"):
        return ""
    return _inline(v) if isinstance(v, (dict, list)) else str(v)


def _table(headers: list, rows_html: list) -> str:
    head = "".join(f"<th>{_h(str(c).replace('_', ' '))}</th>" for c in headers)
    return (f"<table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody></table>")


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
_TIME = ("period", "date", "quarter", "year", "fy", "as_of")


def _period_rank(s) -> tuple:
    """A sortable recency key from a free-text period like 'Latest twelve months
    ending April 2026' or 'Fiscal years ... to 2025' — newest sorts highest."""
    t = str(s).lower()
    yrs = [int(y) for y in re.findall(r"(?:19|20)\d{2}", t)]
    mo = max((n for name, n in _MONTHS.items() if name in t), default=0)
    boost = 1 if re.search(r"latest|as of|current|ltm|ttm|trailing|most recent", t) else 0
    return (max(yrs) if yrs else 0, mo, boost)


_KEEP_PER_SERIES = 3   # keep the latest N points per series (was 1→2) for fuller trend


def _collapse_to_latest(rows: list, cols: list) -> list:
    """For a sourcing one-pager, keep the latest few points of each series (enough to show
    the trend, not the full history). Group by identity columns (entity/metric/…, excluding
    the time column) and keep the most-recent N per group. No-op without time/series structure."""
    time_cols = [c for c in cols if any(t in c.lower() for t in _TIME)]
    group_cols = [c for c in cols if _labelish(c) and c not in time_cols]
    if not time_cols or not group_cols:
        return rows
    pcol = time_cols[0]
    groups, order = {}, []
    for r in rows:
        key = tuple(_cell(r, c) for c in group_cols)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    collapsed = []
    for k in order:
        grp = sorted(groups[k], key=lambda r: _period_rank(_cell(r, pcol)), reverse=True)
        collapsed.extend(grp[:_KEEP_PER_SERIES])
    return collapsed if len(collapsed) < len(rows) else rows


_NOTE_KEYS = ("note", "detail", "comment", "driver", "commentary", "context", "remark")
# Lead with a non-time IDENTITY column (practice/region/segment/…) when one exists — the
# row's "what" reads better as the lead than its period. Pure {period, value} series have
# no identity column, so they correctly fall through to leading on the time column.
_LEAD_PRIORITY = ("practice", "area", "region", "market", "segment", "vertical",
                  "geography", "country", "product", "partner", "entity", "company",
                  "asset", "category", "name", "metric",
                  "period", "quarter", "fiscalyear", "fy", "year", "date", "as_of")


def _lead_col(cols: list):
    low = {c: str(c).lower() for c in cols}
    for p in _LEAD_PRIORITY:
        for c in cols:
            if p in low[c]:
                return c
    return cols[0] if cols else None


def _records_to_bullets(rows: list, lead, value_cols: list, note_cols: list) -> str:
    """Each record as ONE crisp pointer: '**lead** — value · value (note) 🔗'. Used when
    records carry a single data value per row, where a table would be overkill."""
    lis = []
    for r in rows:
        head = _cell(r, lead) if lead else ""
        vals = [_cell(r, c) for c in value_cols if _cell(r, c)]
        notes = [_first_sentence(_cell(r, c), 110) for c in note_cols if _cell(r, c)]
        body = " · ".join(_h(v) for v in vals)
        seg = f"<b>{_h(head)}</b>" if head else ""
        line = f"{seg} — {body}" if seg and body else (seg or body)
        if notes:
            line += f' <span style="color:#8A9AB0;">({_h("; ".join(notes))})</span>'
        icon = _h_sources(_item_sources(r))
        if icon:
            line += f" {icon}"
        lis.append(f"<li>{line}</li>")
    return f"<ul>{''.join(lis)}</ul>"


def _h_records(rows: list) -> str:
    """A list of dict records → a TABLE when it's a real matrix (≥2 data columns), or
    crisp BULLET pointers when each row carries a single value — whichever reads better.
    Sparse / single-value record sets become pointers; multi-metric grids become tables."""
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return ""
    cols, seen = [], set()
    for r in rows:
        for k in r:
            if k not in _SOURCE_KEYS and k not in seen:
                seen.add(k)
                cols.append(k)
    rows = _collapse_to_latest(rows, cols)   # keep only the latest point per series
    maxlen = {c: max((len(_cell(r, c)) for r in rows), default=0) for c in cols}
    # Label columns (period/entity/region…) stay as columns regardless of length;
    # only genuinely long free-text columns become the trimmed 'note' column.
    narrow = [c for c in cols if maxlen[c] > 0 and (_labelish(c) or maxlen[c] <= 40)]
    wide = [c for c in cols if maxlen[c] > 0 and c not in narrow]

    note_cols = [c for c in cols if maxlen[c] > 0
                 and any(n in str(c).lower() for n in _NOTE_KEYS)]
    value_cols = [c for c in narrow if not _labelish(c) and c not in note_cols]
    lead = _lead_col([c for c in narrow if _labelish(c)])

    # Drop rows that carry NO actual data — only a label/period with every value/note/text
    # cell empty or "Not Available" (e.g. a {practice_area, growth:"Not Available", period}
    # row). These render as blank rows showing just a stray period, which looks broken.
    data_cols = value_cols + wide + note_cols
    if data_cols:
        rows = [r for r in rows if any(_cell(r, c) for c in data_cols)] or rows

    # PURE TEXT records ({description/event, period} with no numeric column) → news-style
    # date+headline cards, so the text is shown — never collapse to a bare repeated period.
    if not value_cols and wide:
        return _h_dated(rows)

    # POINTER form: one data value per row (the {period, value(, note)} shape) reads far
    # better as bullets than as a 1-2-column table.
    if len(value_cols) <= 1 and lead:
        return _records_to_bullets(rows, lead, value_cols, note_cols)

    # Not enough tabular structure → render as news/bullets instead.
    if len(narrow) < 2:
        return _h_dated(rows)

    # TABLE form (real matrix): lead with the label-ish column, metrics after.
    narrow.sort(key=lambda c: 0 if _labelish(c) else 1)

    note_col = wide[0] if wide else None          # keep ONE note column, trimmed
    headers = narrow + ([note_col] if note_col else [])
    trs = []
    for r in rows:
        cells = ""
        for c in headers:
            val = _cell(r, c)
            if c == note_col:
                val = _first_sentence(val, 170)
            cells += f"<td>{_h(val)}</td>"
        trs.append(f"<tr>{cells}<td>{_h_sources(_item_sources(r))}</td></tr>")
    return _table(headers + [""], trs)


def _h_dated(items: list) -> str:
    """News-style records: bold date header + a crisp one-sentence headline + 🔗.
    Remaining short fields become tidy `.dr` rows; verbose text is dropped, not dumped."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            out.append(f'<div class="ni"><div class="nt">{_h(_first_sentence(item, 220))}</div></div>')
            continue
        _dk, date = _first(item, _DATE_KEYS)
        _mk, main = _first(item, _MAIN_KEYS)
        icon = _h_sources(_item_sources(item))
        nd = f'<div class="nd">{_h(date)}</div>' if date is not None else ""
        nt = (f'<div class="nt">{_h(_first_sentence(main, 300))} {icon}</div>'
              if main is not None else "")

        used = ({_dk, _mk} - {None}) | _SOURCE_KEYS
        rest_rows = []
        for k, v in item.items():
            if k in used or v in (None, "", "Not Available"):
                continue
            sval = _inline(v) if isinstance(v, (dict, list)) else str(v)
            if len(sval) <= 60:                    # keep crisp extras (a touch more)
                rest_rows.append((str(k).replace("_", " "), sval, []))
        body = _h_rows(rest_rows)
        if not nt and icon and not nd:
            body = f'<div class="nt">{icon}</div>' + body
        out.append(f'<div class="ni">{nd}{nt}{body}</div>')
    return "".join(out)


def _parse_point(it: str) -> tuple:
    """Parse one crammed bullet → (label, value, note). Handles 'Region: +1% (note)'
    and '+2% for Q1 2026 (note)' and '$0.96 per case for 2023 (note)'."""
    note = ""
    m = re.search(r"\(([^)]*)\)\s*\.?$", it)
    if m:
        note = m.group(1).strip()
        it = (it[:m.start()] + it[m.end():]).strip().rstrip(".").strip()
    label, value = "", ""
    if ":" in it and not _NUM.match(it.strip()):
        label, rest = it.split(":", 1)
        label, rest = label.strip(), rest.strip()
        vm = re.match(r"^([+\-]?\$?\d[\d.,]*\s*%?)", rest)
        value = vm.group(1).strip() if vm else rest
        rem = rest[vm.end():].strip(" .") if vm else ""
        if rem:
            note = (rem + ("; " + note if note else "")).strip()
    else:
        vm = re.match(r"^([+\-]?\$?\d[\d.,]*\s*%?)", it.strip())
        value = vm.group(1).strip() if vm else ""
        parts = re.split(r"\bfor\b", it, 1)
        if len(parts) == 2:
            label = parts[1].strip().rstrip(".")
        else:
            label = it[vm.end():].strip(" .") if vm else it
    return (label or "—", value, _first_sentence(note, 90))


def _distill_bulleted(raw: str) -> str:
    """A blob of `*`-bullets grouped under 'Header:' lines → sub-headed tables, with the
    narrative preamble dropped."""
    groups, cur, started = [], {"head": "", "items": []}, False
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^[\*\-•]\s+", line):
            started = True
            cur["items"].append(re.sub(r"^[\*\-•]\s+", "", line).strip())
        elif line.endswith(":"):
            if cur["items"] or cur["head"]:
                groups.append(cur)
            cur = {"head": line[:-1].strip(), "items": []}
        # non-bullet, non-header lines before any bullet are preamble → dropped
    if cur["items"] or cur["head"]:
        groups.append(cur)

    html = ""
    for g in groups:
        if g["head"]:
            html += (f'<div class="body-text" style="font-weight:600;margin:6px 0 2px;">'
                     f'{_h(g["head"])}</div>')
        points = [_parse_point(it) for it in g["items"]]
        if points and sum(1 for p in points if p[1]) >= max(1, len(points) // 2):
            trs = ["<tr>" + f"<td>{_h(lbl)}</td><td>{_h(val)}</td><td>{_h(note)}</td></tr>"
                   for lbl, val, note in points]
            html += _table(["Item", "Value", "Note"], trs)
        else:
            html += "<ul>" + "".join(f"<li>{_h(_first_sentence(it, 160))}</li>"
                                     for it in g["items"]) + "</ul>"
    return html


def _distill_prose(text: str) -> str:
    """Long free-text → only the important bit. Absence blobs collapse to a 'Not disclosed'
    pill + one line; bulleted blobs become tables; other prose is trimmed to ~2 sentences."""
    raw = str(text).replace("\r", "")
    if re.search(r"(^|\n)\s*[\*\-•]\s+", raw):
        return _distill_bulleted(raw)
    sents = _sentences(raw)
    if sents and _ABSENCE.search(sents[0]):
        return (f'<div class="body-text"><span class="pill pn">Not disclosed</span> '
                f'{_h(_first_sentence(raw, 200))}</div>')
    kept = [s for s in sents if not _DROP_SENTENCE.search(s)] or sents
    keep = " ".join(kept[:3])
    if len(keep) > 460:
        keep = _first_sentence(keep, 440)
    elif len(kept) > 3 or len(keep) < len(" ".join(sents)):
        keep += "…"
    return f'<div class="body-text">{_h(keep)}</div>'


def _h_value(value, sources: list) -> str:
    """Render one rich (non-scalar) field value as crisp Embio markup."""
    if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
        html = _h_records(value)
        # Safety net: never drop data because it didn't fit a table/pointer. If the
        # structured render came back empty, show every record inline as bullets so no
        # information is lost — readability second, completeness first.
        if html:
            return html
        return _h_bullets([_inline(x) for x in value if _inline(x).strip()], sources)
    if isinstance(value, list):
        return _h_bullets(value, sources)
    if isinstance(value, dict):
        rows = [(k, v, []) for k, v in value.items()
                if k not in _SOURCE_KEYS and v not in (None, "", "Not Available")
                and not isinstance(v, (dict, list))]
        nested = "".join(f'<div class="body-text"><b>{_h(k)}:</b> {_h(_inline(v))}</div>'
                         for k, v in value.items()
                         if k not in _SOURCE_KEYS and isinstance(v, (dict, list)))
        return _h_rows(rows) + nested
    if isinstance(value, str) and len(value.strip()) > 140:
        return _distill_prose(value)
    return f'<div class="body-text">{_h(value)}</div>'


def _sec_header(idx: int, heading: str, conf: str, tag_label: str) -> str:
    conf_pill = _CONF_PILL.get(conf, "pn")
    tag = f'<span class="tag tp">{_h(tag_label)}</span>' if tag_label else ""
    return (f'<div class="sec-h"><span class="htext">{idx}. {_h(heading)}</span>'
            f'<span style="display:flex;gap:4px;align-items:center;">'
            f'<span class="pill {conf_pill}">{_h(conf)}</span>{tag}</span></div>')


def _blocks_to_html(blocks: list) -> str:
    """Render my offline-distilled blocks (the 'polished' artifact) as Embio markup.
    Block types: take | stats | table | bullets | note."""
    out = ""
    for b in blocks or []:
        t = b.get("t")
        if t == "take":
            out += f'<div class="take"><b>Screening read —</b> {_h(b["text"])}</div>'
        elif t == "stats":
            out += "".join(
                f'<div class="dr"><div class="dl">{_h(l)}</div>'
                f'<div class="dv">{_h(v)}</div></div>' for l, v in b["items"])
        elif t == "table":
            if b.get("title"):
                out += (f'<div class="body-text" style="font-weight:600;margin:6px 0 2px;">'
                        f'{_h(b["title"])}</div>')
            trs = ["<tr>" + "".join(f"<td>{_h(c)}</td>" for c in row) + "</tr>"
                   for row in b["rows"]]
            out += _table(b["headers"], trs)
        elif t == "bullets":
            out += "<ul>" + "".join(f"<li>{_h(x)}</li>" for x in b["items"]) + "</ul>"
        elif t == "note":
            out += f'<div class="body-text">{_h(b["text"])}</div>'
    return out


def _polished_section_html(idx: int, heading: str, conf: str, tag_label: str,
                           blocks: list, src_names: list) -> str:
    head = _sec_header(idx, heading, conf, tag_label)
    src = (f'<div class="src">Source: {_h("; ".join(src_names))}</div>'
           if src_names else "")
    return f'<div class="kelp-op"><div class="sec">{head}{_blocks_to_html(blocks)}{src}</div></div>'


def _section_html(idx: int, heading: str, conf: str, tag_label: str, pairs: list) -> str:
    """A whole section as one compact Embio '.sec' card (header + body + source footer)."""
    head = _sec_header(idx, heading, conf, tag_label)

    # Split short scalars (dense .dr rows up top) from richer values (labelled blocks).
    scalars, rich = [], []
    for label, value, sources in pairs:
        if not isinstance(value, bool) and (
            isinstance(value, (int, float))
            or (isinstance(value, str) and len(value.strip()) <= 120)
        ):
            scalars.append((label, value, sources))
        else:
            rich.append((label, value, sources))

    body = _h_rows(scalars)
    for label, value, sources in rich:
        lbl = str(label).rstrip(":").strip()
        if lbl:
            body += (f'<div class="body-text" style="font-weight:600;margin-bottom:0;">'
                    f'{_h(lbl)} {_h_sources(sources)}</div>')
        body += _h_value(value, sources)

    if not pairs:
        body = ('<div class="body-text" style="color:#8A9AB0;font-style:italic;">'
                'This facet is covered by a related section in the same research domain.</div>')

    return (f'<div class="kelp-op"><div class="sec">{head}{body}'
            f'{_src_footer(pairs)}</div></div>')


# --------------------------------------------------------------------------- #
# Sidebar — inputs + run                                                       #
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("🧭 Sector Section Explorer")
    company = st.text_input("Company name", placeholder="e.g. Coca-Cola")
    sector_hint = st.text_input("Sector hint (optional)", placeholder="e.g. consumer / FMCG")
    description = st.text_input("Business description hint (optional)",
                                placeholder="one line about what they do")
    run = st.button("Run Sector-Specific Research", type="primary", use_container_width=True)
    st.caption("Researches ONLY sector-specific sections — skips the 6 generic domains "
               "and Wave 2 synthesis. Calls the live LLM (billable).")

    # ---- Previous runs: load any saved run instantly (no LLM call, no cost) ----
    st.divider()
    st.subheader("📂 Previous runs")
    _runs = _list_runs(_runs_mtime())
    if not _runs:
        st.caption("No saved runs yet — run one above.")
    else:
        _companies = sorted({r["company"] for r in _runs}, key=str.lower)
        _pick = st.selectbox("Pick a company", ["—"] + _companies, key="prev_company")
        if _pick != "—":
            _company_runs = sorted(
                [r for r in _runs if r["company"] == _pick],
                key=lambda r: r["generated_at"], reverse=True,
            )

            def _run_label(i: int) -> str:
                r = _company_runs[i]
                gen = r["generated_at"] or "(no timestamp)"
                cost = f" · ${r['cost']:.4f}" if r["cost"] is not None else ""
                return f"{gen} · {r['n_sections']} sec{cost}"

            _i = st.selectbox("Run (newest first)", range(len(_company_runs)),
                              format_func=_run_label, key="prev_run")
            if st.button("📂 Load this run", use_container_width=True):
                _chosen = _company_runs[_i]
                try:
                    with open(_chosen["path"], "r", encoding="utf-8") as fh:
                        st.session_state["result"] = json.load(fh)
                    st.session_state["result_path"] = _chosen["path"]
                except (OSError, json.JSONDecodeError) as exc:
                    st.error(f"Could not load run: {exc}")

if run:
    if not company.strip():
        st.sidebar.error("Company name is required.")
        st.stop()

    # gen_sector_only.py takes (company, description). Fold the sector hint into the
    # description so it still informs the planner.
    desc_arg = description.strip()
    if sector_hint.strip():
        desc_arg = (desc_arg + f" (Sector: {sector_hint.strip()})").strip()

    cmd = [sys.executable, GEN_SCRIPT, company.strip(), desc_arg]
    with st.status(f"Running sector-specific research for “{company.strip()}”… "
                   "(live LLM, typically ~2–4 min)", expanded=True) as status:
        st.write(f"`{' '.join(cmd)}`")
        timer_box = st.empty()
        log_box = st.empty()
        log_lines: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except Exception as exc:  # couldn't even launch
            status.update(label="Failed to launch", state="error")
            st.error(f"Could not start gen_sector_only.py: {exc}")
            st.stop()

        # Stream output on a background thread so the main thread can tick a live
        # elapsed-time clock even during the long silent gaps between grounded calls.
        line_q: "queue.Queue[str | None]" = queue.Queue()

        def _pump(stream, q):
            for ln in stream:
                q.put(ln.rstrip("\n"))
            q.put(None)  # sentinel = process finished

        threading.Thread(target=_pump, args=(proc.stdout, line_q), daemon=True).start()

        t0 = time.perf_counter()
        done = False
        while not done:
            try:
                while True:
                    item = line_q.get_nowait()
                    if item is None:
                        done = True
                        break
                    log_lines.append(item)
            except queue.Empty:
                pass
            elapsed = time.perf_counter() - t0
            timer_box.markdown(f"### ⏱️ {elapsed:5.1f}s elapsed")
            if log_lines:
                log_box.code("\n".join(log_lines[-25:]))
            if not done:
                time.sleep(0.3)
        proc.wait()
        timer_box.markdown(f"### ⏱️ {time.perf_counter() - t0:5.1f}s total")

        if proc.returncode != 0:
            status.update(label="Research failed", state="error")
            st.error(f"gen_sector_only.py exited with status {proc.returncode}.")
            st.code("\n".join(log_lines) or "(no output captured)")
            st.stop()
        status.update(label=f"Research complete in {time.perf_counter() - t0:.0f}s",
                      state="complete")

    json_path = os.path.join(OUTPUT_DIR, f"{_slug(company)}_sector_only.json")
    if not os.path.exists(json_path):
        st.error(f"Output JSON not found where expected:\n`{json_path}`")
        st.stop()
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            st.session_state["result"] = json.load(fh)
        st.session_state["result_path"] = json_path
    except (OSError, json.JSONDecodeError) as exc:
        st.error(f"Could not read result JSON: {exc}")
        st.stop()


# --------------------------------------------------------------------------- #
# Main — render the last result (persists across reruns via session_state)     #
# --------------------------------------------------------------------------- #
data = st.session_state.get("result")
if not data:
    st.title("🧭 Sector Section Explorer")
    st.info("Enter a company in the sidebar and click **Run Sector-Specific Research** "
            "to see which sector-specific sections get chosen — and which get rejected.")
    st.stop()

sections = data.get("sections") or []
considered = data.get("considered_not_chosen") or []
cost = data.get("cost") or {}
est = (cost.get("estimated_full_workflow_usd") or {})
# Cards rendered = one per research DOMAIN (a domain's multiple planned headings are
# consolidated into a single section), so the metric must match what's shown, not the
# raw planned-heading count.
n_domains = len({s.get("domain_name") for s in sections})

st.title(f"🧭 {data.get('company_name', 'Company')} — sector-specific sections")
st.caption(f"Loaded from `{st.session_state.get('result_path', '')}`")

# Inject the scoped Embio/Kelp stylesheet once (only affects `.kelp-op` blocks).
st.html(f"<style>{_KELP_CSS}</style>")

# 1) Header metrics row -------------------------------------------------------
c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
c1.metric("Resolved subsector", data.get("resolved_subsector") or "—")
c2.metric("Sections kept", n_domains,
          help=(f"{n_domains} consolidated section(s) from {len(sections)} planned headings"
                if n_domains != len(sections) else None))
c3.metric("Domains rejected", len(considered))
c4.metric("Elapsed", f"{cost.get('elapsed_seconds', 0):.1f}s")
c5.metric("Actual cost", f"${cost.get('actual_total_usd', 0):.4f}")

# Cost breakdown: tokens (exact) vs grounding (per-search, $0 under Google's free tier).
if cost.get("grounding_usd") is not None:
    gcalls = cost.get("grounded_calls", 0)
    grate = cost.get("grounding_rate_per_call", 0) or 0
    st.caption(
        f"💸 tokens **${cost.get('token_usd', 0):.4f}** + grounding "
        f"**${cost.get('grounding_usd', 0):.4f}** ({gcalls} web-search calls × "
        f"${grate:.3f}) = **${cost.get('actual_total_usd', 0):.4f}**  ·  "
        "grounding is $0 while under Google's free daily tier, billed past it."
    )

# 2) Estimate callout ---------------------------------------------------------
est_total = est.get("estimated_total_usd")
actual_total = cost.get("actual_total_usd")
# New runs fold grounding into BOTH figures (the per-call surcharge flows through the
# per-domain cost the estimate extrapolates from). Old runs (no grounding data) are
# token-only — overlay an estimated grounding figure so the comparison still includes it.
has_grounding = cost.get("grounding_usd") is not None
grate = cost.get("grounding_rate_per_call") or 0.035
if est_total is not None and actual_total is not None:
    if has_grounding:
        incl = " — both include grounding"
        est_caption_extra = (f"  ·  of which grounding: actual "
                             f"${cost.get('grounding_usd', 0):.4f}, full-run scales with it")
    else:
        # token-only old run: estimate grounding ≈ one grounded search per ~2 calls.
        est_g = round((cost.get("actual_calls", 0) / 2) * grate, 4)
        actual_total = round(actual_total + est_g, 4)
        est_total = round(est_total + est_g * (10 / max(1, len(sections))), 4)
        incl = " — grounding estimated & added (older run had none)"
        est_caption_extra = (f"  ·  grounding estimate added: ~${est_g:.4f} "
                             f"(@ ${grate:.3f}/search)")
    st.info(f"💡 **If the full workflow ran (generic + sector sections): "
            f"~${est_total:.2f} estimated** — vs **${actual_total:.4f} actual** "
            f"for sector-only{incl}.")
    st.caption(est.get("method", "Estimate extrapolated from this run's average "
                        "per-domain cost — not an actual full run.")
               + f"  ·  avg/domain this run: ${est.get('avg_cost_per_domain_this_run', 0):.4f}"
               + est_caption_extra)

st.divider()

# 3) Chosen sections — one consolidated card per research domain (n_domains computed above).
st.header(f"✅ Chosen sector-specific sections — {n_domains}")
if n_domains and n_domains != len(sections):
    st.caption(f"One section per research domain — the planner proposed {len(sections)} "
               f"section headings across {n_domains} domain(s); each domain's research is "
               "shown once, as a single consolidated section.")

# Offline-distilled ("polished") version of this company, if I've written one. When present,
# each section is rendered from my hand-distilled blocks (crisp, only-what-matters, no API)
# instead of the raw research. Falls back to deterministic rendering per-section.
polished = _load_polished(data.get("company_name", ""))
if polished:
    st.success("✨ **Distilled by Claude (offline — no API).** Each section below is the "
               "crisp screening view: only the facts that move a pursue-vs-pass call. "
               "Use the per-section expanders to verify against the raw sources.")

if not sections:
    st.info("No sector-specific sections were produced for this company.")
else:
    # Group sections by their research domain, then SPLIT that domain's shared fields
    # across its sections so each section shows its own distinct subset.
    groups, by_domain = [], {}
    for s in sections:
        dom = s.get("domain_name") or s.get("heading") or "—"
        if dom not in by_domain:
            by_domain[dom] = len(groups)
            groups.append([])
        groups[by_domain[dom]].append(s)

    idx = 0
    for items in groups:
        # Collapse a research domain into ONE section. The planner sometimes lists several
        # `sections_covered` for one domain, but the research returns a single flat field-bag —
        # splitting it produced thin/duplicate sections (e.g. two copies of the same 5 fields).
        # One domain = one section, showing all its data once, reads far better for screening.
        idx += 1
        it = items[0]
        heading = it.get("heading", "(untitled)")
        conf = it.get("confidence", "low")
        fld = _field_pairs(it)  # the domain's full field-bag (sections in a group share it)
        tag_label = data.get("resolved_subsector") or ""

        # Section sources: per-field sources first; fall back to the domain's reference list
        # (research collected these while fetching these very fields — the per-field link is
        # just lossy). This is what lets a field count as sourced-at-section-level.
        refs, seen = [], set()
        def _add_ref(sc):
            k = (sc.get("name"), sc.get("url"))
            if k not in seen and (sc.get("name") or sc.get("url")):
                seen.add(k)
                refs.append(sc)
        for _l, _v, _srcs in fld:
            for sc in _srcs or []:
                _add_ref(sc)
        for sc in (it.get("references") or []):
            _add_ref(sc)

        with st.container(border=True):
            blocks = (polished or {}).get(heading)
            if blocks:
                # My offline-distilled, crisp version of this section.
                src_names = list(dict.fromkeys(
                    r.get("name") for r in refs if r.get("name")))
                st.html(_polished_section_html(idx, heading, conf, tag_label,
                                               blocks, src_names))
            else:
                # Deterministic Embio rendering of the raw research.
                st.html(_section_html(idx, heading, conf, tag_label, fld))

            cols = st.columns(2)
            with cols[0]:
                if refs:
                    with st.expander(f"🔗 All {len(refs)} sources for this section"):
                        for r in refs:
                            name = (r.get("name") or "source").strip()
                            url = r.get("url")
                            st.markdown(f"- [{_esc(name)}](<{url}>)" if url
                                        else f"- {_esc(name)}")
            with cols[1]:
                with st.expander("Why this section was included"):
                    reasoning = it.get("reasoning") or {}
                    _score_bar(reasoning)
                    st.markdown("**Why it's important for an analyst:**")
                    if not _persona_lines(reasoning):
                        st.caption("_(No analyst rationale recorded.)_")

            # Data points planned for this section that research could NOT find.
            miss = it.get("missing_fields")
            if miss:
                with st.expander(f"⚠️ {len(miss)} data point(s) considered but not found"):
                    for m in miss:
                        st.markdown(f"- {_esc(m)}")
            elif miss is None:  # older run — derive a count from completeness
                comp, nf = it.get("completeness") or 0, len(it.get("fields") or [])
                gap = max(0, round(nf / comp) - nf) if 0 < comp < 0.999 and nf else 0
                if gap:
                    st.caption(f"⚠️ ~{gap} planned data point(s) not found "
                               "(re-run to list which).")

# 4) Considered but not included ---------------------------------------------
st.divider()
st.header("🚫 Considered but not included")
if not considered:
    st.info("Every proposed sector domain cleared the persona-relevance bar — "
            "nothing was rejected this run.")
else:
    st.caption("These domains were proposed by sector research but cut before any data "
               "was fetched, because no persona's decision materially changed.")
    for c in considered:
        st.markdown(
            f"<h3 style='color:#8a8f98;margin-bottom:0'>🚫 {c.get('domain_name', '(unnamed)')}</h3>",
            unsafe_allow_html=True,
        )
        st.markdown(f"**{c.get('rejected_reason', 'Rejected.')}**")
        covered = c.get("would_have_covered") or []
        if covered:
            st.write("**Would have covered:** " + ", ".join(str(x) for x in covered))
        with st.expander("Persona reasoning"):
            reasoning = c.get("reasoning") or {}
            _score_bar(reasoning)
            if not _persona_lines(reasoning):
                st.caption("_No persona's decision was changed by this domain — "
                           "which is exactly why it was cut._")
        st.write("")

# 4.5) Data considered but not found — gaps across all sections ---------------
# Aggregate by research domain (sections sharing a domain share the same gaps).
_gaps, _seen_dom = [], set()
for s in sections:
    dom = s.get("domain_name") or s.get("heading")
    if dom in _seen_dom:
        continue
    _seen_dom.add(dom)
    miss = s.get("missing_fields")
    if miss:
        _gaps.append((dom, miss, None))
    elif miss is None:  # older run — derived count, no names
        comp, nf = s.get("completeness") or 0, len(s.get("fields") or [])
        gap = max(0, round(nf / comp) - nf) if 0 < comp < 0.999 and nf else 0
        if gap:
            _gaps.append((dom, None, gap))

if _gaps:
    st.divider()
    _total = sum(len(m) if m else c for _d, m, c in _gaps)
    st.header(f"🔍 Data considered but not found — {_total}")
    st.caption("These data points were planned for the chosen sections but the research "
               "could not find them — the gaps to chase in deeper diligence.")
    for dom, miss, count in _gaps:
        st.markdown(f"**{_esc(dom)}**")
        if miss:
            st.html('<div class="kelp-op" style="display:flex;flex-wrap:wrap;gap:4px;">'
                    + "".join(f'<span class="pill pa">{_h(m)}</span>' for m in miss)
                    + "</div>")
        else:
            st.caption(f"~{count} planned data point(s) not found "
                       "(older run — re-run to list which).")
        st.write("")

# 5) Generic sections (names only) — what a FULL one-pager would also include ----
st.divider()
_generic = sections_catalog.generic_section_names()
st.header(f"📋 Generic sections — {len(_generic)} (not researched in sector-only mode)")
st.caption("Sector-only mode skips the generic domains. A full one-pager would also carry "
           "these standard sections — names listed for reference.")
st.html(
    '<div class="kelp-op" style="display:flex;flex-wrap:wrap;gap:4px;">'
    + "".join(f'<span class="pill pn">{_h(n)}</span>' for n in _generic)
    + "</div>"
)

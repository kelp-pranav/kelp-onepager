"""FastAPI web UI for the Kelp One-Pager Agent.

    python main.py            # serves at http://localhost:8000

The UI runs a generation as a background job and shows live status, cost
incurred, time elapsed and an estimated time remaining. The final one-pager
JSON is written to output/ and downloadable from the page.

Endpoints
---------
GET  /                  → live dashboard
POST /start             → kick off a job, returns {"job_id": ...}
GET  /status/{job_id}   → live status (polled by the page)
GET  /download/{job_id} → final one-pager JSON as a file attachment
GET  /cost              → lifetime cost ledger (JSON)
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import config
import skill_functions as sf
from orchestrator import generate_one_pager
from schemas import PipelineInput

app = FastAPI(title="Kelp One-Pager Agent")
templates = Jinja2Templates(directory="templates")

# In-memory job registry. Single-process; fine for a local/single-user UI.
JOBS: Dict[str, Dict[str, Any]] = {}

# Ordered pipeline phases with a human label and an expected duration (seconds)
# used purely for the progress bar and the time-remaining estimate. Wave 1 is by
# far the heaviest (parallel web research); the rest are quick tails.
PHASES = [
    ("sector_research",   "Sector research",                         12.0),
    ("sector_dedup",      "De-duplicating sector sections",           3.0),
    ("wave_1_and_layout", "Domain research (Wave 1) + layout",       35.0),
    ("json_population",   "Assembling JSON skeleton",                 1.0),
    ("sector_swap",       "Sector data check & swap",                 6.0),
    ("synthesis",         "Synthesis — thesis / SWOT / future / risk", 12.0),
    ("presentation",      "Formatting & analysis",                    8.0),
    ("coverage",          "Coverage gap check",                       4.0),
    ("validation",        "Validation & dedup",                       3.0),
]
PHASE_INDEX = {key: i for i, (key, _label, _w) in enumerate(PHASES)}
EST_TOTAL = sum(w for _k, _l, w in PHASES)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "company"


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "state": "queued",          # queued | running | done | error
        "phase": None,              # current phase key
        "phase_started_at": None,   # perf_counter when current phase began
        "t0": time.perf_counter(),
        "cost_before": sf.snapshot()[0],
        "error": None,
        "telemetry": None,
        "json_path": None,
        "filename": None,
    }
    return job_id


def _on_phase(job_id: str):
    def cb(phase: str) -> None:
        job = JOBS.get(job_id)
        if job is None:
            return
        job["phase"] = phase
        job["phase_started_at"] = time.perf_counter()
        if phase != "done":
            job["state"] = "running"
    return cb


async def _run_job(job_id: str, inp: PipelineInput) -> None:
    job = JOBS[job_id]
    job["state"] = "running"
    try:
        final, tel = await generate_one_pager(inp, progress=_on_phase(job_id))

        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        filename = f"{_slug(inp.company_name)}.json"
        path = os.path.join(config.OUTPUT_DIR, filename)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(final.model_dump_json(indent=2))

        job["telemetry"] = tel
        job["json_path"] = path
        job["filename"] = filename
        job["state"] = "done"
        job["phase"] = "done"
    except Exception as exc:  # surface failure to the UI, don't crash the server
        job["state"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__


def _status_payload(job_id: str) -> Dict[str, Any]:
    job = JOBS[job_id]
    now = time.perf_counter()
    elapsed = now - job["t0"]
    cost = round(max(0.0, sf.get_cumulative_cost() - job["cost_before"]), 6)

    phase = job["phase"]
    idx = PHASE_INDEX.get(phase) if phase and phase != "done" else None

    # Progress fraction + remaining estimate from phase weights.
    if job["state"] == "done":
        progress = 1.0
        remaining = 0.0
        phase_label = "Complete"
    elif idx is not None:
        done_w = sum(w for _k, _l, w in PHASES[:idx])
        cur_w = PHASES[idx][2]
        in_phase = now - (job["phase_started_at"] or now)
        in_phase_capped = min(in_phase, cur_w)
        progress = min(0.99, (done_w + in_phase_capped) / EST_TOTAL)
        remaining = max(1.0, (EST_TOTAL - done_w) - in_phase_capped)
        phase_label = PHASES[idx][1]
    else:  # queued, no phase yet
        progress = 0.0
        remaining = EST_TOTAL
        phase_label = "Starting…"

    payload: Dict[str, Any] = {
        "state": job["state"],
        "phase": phase,
        "phase_label": phase_label,
        "phase_index": (idx + 1) if idx is not None else (len(PHASES) if job["state"] == "done" else 0),
        "phase_total": len(PHASES),
        "progress": round(progress, 4),
        "elapsed_s": round(elapsed, 1),
        "remaining_s": round(remaining, 1),
        "cost_usd": cost,
        "error": job["error"],
    }
    if job["state"] == "done":
        tel = job["telemetry"] or {}
        payload["filename"] = job["filename"]
        payload["download_url"] = f"/download/{job_id}"
        payload["summary"] = {
            "company": tel.get("company"),
            "resolved_subsector": tel.get("resolved_subsector"),
            "duration_s": round((tel.get("total_duration_ms", 0)) / 1000, 1),
            "run_cost_usd": tel.get("run_cost_usd"),
            "lifetime_cost_usd": tel.get("lifetime_cost_usd"),
            "sections_populated": tel.get("sections_populated"),
            "sections_partial": tel.get("sections_partial"),
            "sections_unavailable": tel.get("sections_unavailable"),
            "domains_succeeded": tel.get("domains_succeeded"),
            "warnings": tel.get("warnings", []),
        }
    return payload


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/start", response_class=JSONResponse)
async def start(company: str = Form(...), sector: str = Form(""), description: str = Form("")):
    if not config.GEMINI_API_KEY:
        return JSONResponse({"error": "GEMINI_API_KEY not set — add it to .env."}, status_code=400)
    company = company.strip()
    if not company:
        return JSONResponse({"error": "Company name is required."}, status_code=400)

    inp = PipelineInput(
        company_name=company,
        sector=sector.strip() or None,
        business_description=description.strip() or None,
    )
    job_id = _new_job()
    asyncio.create_task(_run_job(job_id, inp))
    return JSONResponse({"job_id": job_id})


@app.get("/status/{job_id}", response_class=JSONResponse)
async def status(job_id: str):
    if job_id not in JOBS:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(_status_payload(job_id))


@app.get("/download/{job_id}")
async def download(job_id: str):
    job = JOBS.get(job_id)
    if job is None or not job.get("json_path") or not os.path.exists(job["json_path"]):
        return JSONResponse({"error": "result not ready"}, status_code=404)
    return FileResponse(job["json_path"], media_type="application/json", filename=job["filename"])


@app.get("/cost")
async def cost():
    return JSONResponse(sf.load_persistent_ledger())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

"""Typer CLI for the Kelp One-Pager Agent.

    python cli.py generate "Embio Limited"
    python cli.py generate "HDFC Bank" --sector banking
    python cli.py list
    python cli.py cost
"""

from __future__ import annotations

import asyncio
import os
import re

import typer
from rich.console import Console
from rich.table import Table

import config
import skill_functions as sf
from orchestrator import generate_one_pager
from schemas import PipelineInput

app = typer.Typer(add_completion=False, help="Generate professional financial one-pagers.")
console = Console()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "company"


@app.command()
def generate(
    company: str = typer.Argument(..., help="Company name"),
    sector: str = typer.Option(None, "--sector", "-s", help="Sector hint (optional)"),
    description: str = typer.Option(None, "--description", "-d", help="Business description (optional)"),
):
    """Research a company and write its one-pager JSON to output/."""
    if not config.GEMINI_API_KEY:
        console.print("[red]GEMINI_API_KEY not set (add it to .env).[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Generating one-pager for[/bold] [cyan]{company}[/cyan] …")
    inp = PipelineInput(company_name=company, sector=sector, business_description=description)
    final, tel = asyncio.run(generate_one_pager(inp))

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    path = os.path.join(config.OUTPUT_DIR, f"{_slug(company)}.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(final.model_dump_json(indent=2))

    t = Table(title=f"{company} — generation summary", show_header=False)
    t.add_row("Subsector", str(tel["resolved_subsector"]))
    t.add_row("Duration", f"{tel['total_duration_ms']/1000:.1f}s")
    t.add_row("Sections", f"{tel['sections_populated']} populated / "
                          f"{tel['sections_partial']} partial / {tel['sections_unavailable']} n/a")
    t.add_row("Domains succeeded", str(tel["domains_succeeded"]))
    t.add_row("This run cost", f"${tel['run_cost_usd']:.6f} "
                              f"({tel['run_calls']} calls, {tel['run_grounded_calls']} grounded)")
    t.add_row("[bold]LIFETIME cost[/bold]", f"[bold]${tel['lifetime_cost_usd']:.6f}[/bold]")
    t.add_row("Output", path)
    console.print(t)

    # Per-step (per-phase) cost + time breakdown
    ph = Table(title="Per-step breakdown")
    ph.add_column("Phase")
    ph.add_column("Cost", justify="right")
    ph.add_column("Time", justify="right")
    for phase, cost in tel["phase_costs"].items():
        ms = tel["phase_timings"].get(phase, 0)
        ph.add_row(phase, f"${cost:.6f}", f"{ms/1000:.1f}s")
    console.print(ph)

    if tel["warnings"]:
        console.print(f"[yellow]Warnings:[/yellow] {'; '.join(tel['warnings'][:6])}")


@app.command(name="list")
def list_pagers():
    """List generated one-pagers in the output directory."""
    if not os.path.isdir(config.OUTPUT_DIR):
        console.print("No output directory yet.")
        return
    files = sorted(f for f in os.listdir(config.OUTPUT_DIR) if f.endswith(".json"))
    if not files:
        console.print("No one-pagers generated yet.")
        return
    t = Table(title="Generated one-pagers")
    t.add_column("File")
    t.add_column("Size", justify="right")
    for f in files:
        size = os.path.getsize(os.path.join(config.OUTPUT_DIR, f))
        t.add_row(f, f"{size/1024:.1f} KB")
    console.print(t)


@app.command()
def cost():
    """Show lifetime Gemini spend from the persistent ledger."""
    led = sf.load_persistent_ledger()
    t = Table(title="Lifetime Gemini spend")
    t.add_column("Run")
    t.add_column("Cost", justify="right")
    t.add_column("Calls", justify="right")
    for e in led.get("entries", []):
        t.add_row(e["label"][:60], f"${e['cost_usd']:.6f}", str(e["calls"]))
    t.add_row("[bold]TOTAL[/bold]", f"[bold]${led['lifetime_cost_usd']:.6f}[/bold]",
              f"[bold]{led['lifetime_calls']}[/bold]")
    console.print(t)


if __name__ == "__main__":
    app()

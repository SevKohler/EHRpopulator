#!/usr/bin/env python3
"""
EHR Populator — main CLI entry point.

Usage examples:
  python main.py generate vital_signs.opt --format OPENEHR_FLAT --count 5
  python main.py generate patient_profile.json --format FHIR_R4 --count 3 --upload
  python main.py analyze my_template.opt
"""

from __future__ import annotations
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the agents/ directory
load_dotenv(Path(__file__).parent / ".env")
from typing import Annotated, Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from models import ResourceFormat, TemplateType
from pipeline import Pipeline
from agents import TemplateAnalyzerAgent
from llm_client import build_llm

app = typer.Typer(
    name="ehrpopulator",
    help="Generate synthetic openEHR and FHIR test data using AI agents.",
    add_completion=False,
)
console = Console()


def load_config(config_path: Path | None) -> dict:
    # Priority: explicit --config > config.local.yaml > config.yaml
    candidates = []
    if config_path:
        candidates.append(config_path)
    candidates += [
        Path("config.local.yaml"),
        Path("config.yaml"),
    ]
    for p in candidates:
        if p.exists():
            with open(p) as f:
                cfg = yaml.safe_load(f)
            # Resolve ${ENV_VAR} placeholders
            return _resolve_env(cfg)
    console.print("[red]No config file found.[/red] "
                  "Copy config.yaml to config.local.yaml and fill in your settings.")
    raise typer.Exit(1)


def _resolve_env(obj):
    """Recursively resolve ${VAR} placeholders in config values."""
    import re
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)(?::([^}]*))?\}",
                      lambda m: os.environ.get(m.group(1), m.group(2) or m.group(0)),
                      obj)
    elif isinstance(obj, dict):
        return {k: _resolve_env(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_env(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def generate(
    template: Annotated[Path, typer.Argument(help="OPT (.opt/.xml) or StructureDefinition (.json) file")],
    format: Annotated[ResourceFormat, typer.Option("--format", "-f",
        help="Output format")] = ResourceFormat.FHIR_R4,
    count: Annotated[int, typer.Option("--count", "-n",
        help="Number of patient records to generate")] = 1,
    demographic_context: Annotated[str, typer.Option("--demographic-context", "-d",
        help="Patient population description")] = "general adult patients",
    upload: Annotated[bool, typer.Option("--upload",
        help="Upload generated resources to configured servers")] = False,
    ig: Annotated[Optional[str], typer.Option("--ig",
        help="FHIR Implementation Guide: local directory, .tgz package, or packages.fhir.org URL")] = None,
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
    output_dir: Annotated[Optional[Path], typer.Option("--output", "-o")] = None,
):
    """Generate synthetic clinical data from a template.

    For openEHR: provide an OPT file (.opt / .xml) — the validator service converts
    it to a web template automatically before analysis.

    For FHIR: provide a StructureDefinition (.json). Optionally pass --ig with your
    Implementation Guide package for richer ValueSet and population context.
    """
    if not template.exists():
        console.print(f"[red]Template file not found:[/red] {template}")
        raise typer.Exit(1)

    config = load_config(config_path)
    if output_dir:
        config.setdefault("pipeline", {})["output_dir"] = str(output_dir)

    pipeline = Pipeline(config)
    results = pipeline.run(
        template_path=template,
        format=format,
        count=count,
        demographic_context=demographic_context,
        upload=upload,
        ig_path=ig,
    )

    # Print result table
    table = Table(title="Generation Results")
    table.add_column("Patient")
    table.add_column("Attempts")
    table.add_column("Valid")
    table.add_column("Issues")

    for r in results:
        valid_str = "[green]✓[/green]" if r.valid else "[red]✗[/red]"
        table.add_row(
            r.patient_id,
            str(r.generation_attempt),
            valid_str,
            str(len(r.validation_issues)),
        )
    console.print(table)


@app.command()
def analyze(
    template: Annotated[Path, typer.Argument(help="OPT or StructureDefinition file to analyze")],
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
):
    """Analyze a template and print its structure summary."""
    if not template.exists():
        console.print(f"[red]File not found:[/red] {template}")
        raise typer.Exit(1)

    config = load_config(config_path)
    llm = build_llm(config["llm"])
    agent = TemplateAnalyzerAgent(llm)

    from pipeline import _detect_template_type
    template_type = _detect_template_type(template)
    content = template.read_text(encoding="utf-8")

    console.print(f"Analyzing [cyan]{template.name}[/cyan] as {template_type.value}...")
    with console.status("Calling LLM..."):
        analysis = agent.analyze(content, template_type)

    console.print(f"\n[bold]{analysis.name}[/bold] ({analysis.template_id})")
    console.print(f"Type: {analysis.template_type.value}")
    console.print(f"Description: {analysis.description}")
    console.print(f"\nClinical concepts: {', '.join(analysis.clinical_concepts)}")

    if analysis.required_elements:
        console.print(f"\n[bold]Required elements ({len(analysis.required_elements)}):[/bold]")
        for el in analysis.required_elements:
            vs = f" [ValueSet: {el.value_set_url}]" if el.value_set_url else ""
            console.print(f"  {el.path} [{el.data_type}]{vs}")

    if analysis.optional_elements:
        console.print(f"\n[bold]Optional elements ({len(analysis.optional_elements)}):[/bold]")
        for el in analysis.optional_elements:
            vs = f" [ValueSet: {el.value_set_url}]" if el.value_set_url else ""
            console.print(f"  {el.path} [{el.data_type}]{vs}")

    if analysis.notes:
        console.print(f"\nNotes: {analysis.notes}")


if __name__ == "__main__":
    app()

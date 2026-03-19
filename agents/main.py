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

def _check_terminology(config: dict) -> None:
    """Warn if terminology server is missing expected code systems."""
    import requests as _requests
    base = config.get("terminology", {}).get("base_url", "http://localhost:8085/fhir")
    checks = [
        ("SNOMED CT", "http://snomed.info/sct"),
        ("LOINC",     "http://loinc.org"),
        ("ICD-10",    "http://hl7.org/fhir/sid/icd-10"),
    ]
    missing = []
    try:
        for label, url in checks:
            r = _requests.get(f"{base}/CodeSystem", params={"url": url}, timeout=5)
            if r.json().get("total", 0) == 0:
                missing.append(label)
    except Exception:
        console.print("  [yellow]Warning:[/yellow] Cannot reach terminology server — code lookups will be skipped.")
        return

    if missing:
        console.print(f"\n  [yellow]Warning:[/yellow] These code systems are not yet loaded in Snowstorm: "
                      f"[bold]{', '.join(missing)}[/bold]")
        console.print("  Terminology lookups will be skipped for those systems.")
        console.print("  Watch progress: [dim]sudo docker compose logs -f terminology-loader[/dim]")
        if not typer.confirm("\n  Continue anyway?", default=True):
            raise typer.Exit(0)
    else:
        console.print("  [green]✓[/green] Terminology ready (SNOMED, LOINC, ICD-10)")


TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
OPENEHR_DIR = TEMPLATES_DIR / "openehr"
FHIR_DIR = TEMPLATES_DIR / "fhir"


def _discover_templates(standard: str) -> list[Path]:
    """Find all template files for the given standard."""
    if standard == "openehr":
        folder = OPENEHR_DIR
        patterns = ["*.opt", "*.xml"]
    else:
        folder = FHIR_DIR
        patterns = ["*.json"]

    files: list[Path] = []
    for pattern in patterns:
        files.extend(sorted(folder.glob(pattern)))
    return files


@app.command()
def run(
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
    output_dir: Annotated[Optional[Path], typer.Option("--output", "-o")] = None,
):
    """Interactive mode — asks what you want and generates accordingly.

    Run with no arguments to start:
      python main.py run
    """
    console.print("\n[bold cyan]EHR Populator[/bold cyan] — Interactive Generator\n")

    # 1. Standard
    console.print("Which standard?")
    console.print("  [bold]1[/bold]  openEHR  (OPT files from templates/openehr/)")
    console.print("  [bold]2[/bold]  FHIR R4  (StructureDefinitions from templates/fhir/)")
    std_choice = typer.prompt("  Choice", default="1").strip()
    is_fhir = std_choice in ("2", "fhir", "FHIR", "FHIR_R4")

    template_dir = FHIR_DIR if is_fhir else OPENEHR_DIR
    template_paths = _discover_templates("fhir" if is_fhir else "openehr")

    if not template_paths:
        console.print(f"\n[red]No templates found in {template_dir}[/red]")
        console.print(f"  Drop your {'StructureDefinition .json' if is_fhir else 'OPT .opt/.xml'} files into: [cyan]{template_dir}[/cyan]")
        raise typer.Exit(1)

    console.print(f"\nFound [green]{len(template_paths)}[/green] template(s) in [cyan]{template_dir.relative_to(TEMPLATES_DIR.parent)}[/cyan]:")
    for p in template_paths:
        console.print(f"  - {p.name}")

    # 2. Format (openEHR only — FHIR is always FHIR_R4)
    if is_fhir:
        format = ResourceFormat.FHIR_R4
    else:
        console.print("\nOutput format:")
        console.print("  [bold]1[/bold]  OPENEHR_FLAT       (flat JSON for EHRbase)")
        console.print("  [bold]2[/bold]  OPENEHR_CANONICAL  (canonical openEHR JSON)")
        fmt_choice = typer.prompt("  Choice", default="1").strip()
        format = ResourceFormat.OPENEHR_CANONICAL if fmt_choice == "2" else ResourceFormat.OPENEHR_FLAT

    # 3. Count
    count = typer.prompt("\nHow many patients to generate", default="1")
    try:
        count = int(count)
    except ValueError:
        count = 1

    # 4. Scenario
    console.print("\nDescribe the patients / clinical scenario you want to generate.")
    console.print("  [dim]Examples: 'elderly patients with COPD and type 2 diabetes'[/dim]")
    console.print("  [dim]          'rare metabolic disease, focus on lysosomal storage disorders'[/dim]")
    console.print("  [dim]          'post-operative ICU patients with sepsis complications'[/dim]")
    scenario = typer.prompt("  Scenario").strip() or "general adult patients"

    # 5. Upload
    upload = typer.confirm("\nUpload results to configured servers?", default=False)

    console.print()

    # Run
    config = load_config(config_path)
    if output_dir:
        config.setdefault("pipeline", {})["output_dir"] = str(output_dir)

    _check_terminology(config)

    pipeline = Pipeline(config)
    results = pipeline.run(
        template_paths=template_paths,
        format=format,
        count=count,
        scenario=scenario,
        upload=upload,
    )

    table = Table(title="Generation Results")
    table.add_column("Patient")
    table.add_column("Template")
    table.add_column("Attempts")
    table.add_column("Valid")
    table.add_column("Issues")
    for r in results:
        valid_str = "[green]✓[/green]" if r.valid else "[red]✗[/red]"
        table.add_row(r.patient_id, r.template_id, str(r.generation_attempt),
                      valid_str, str(len(r.validation_issues)))
    console.print(table)

@app.command()
def generate(
    templates: Annotated[list[Path], typer.Argument(help="One or more OPT (.opt/.xml) or StructureDefinition (.json) files")],
    scenario: Annotated[str, typer.Option("--scenario", "-s",
        help="Describe what you want to generate, e.g. 'diabetic patients with hypertension and CKD stage 3'")] = "general adult patients",
    format: Annotated[ResourceFormat, typer.Option("--format", "-f",
        help="Output format")] = ResourceFormat.OPENEHR_FLAT,
    count: Annotated[int, typer.Option("--count", "-n",
        help="Number of patients to generate")] = 1,
    upload: Annotated[bool, typer.Option("--upload",
        help="Upload generated resources to configured servers")] = False,
    ig: Annotated[Optional[str], typer.Option("--ig",
        help="FHIR Implementation Guide: local directory, .tgz package, or packages.fhir.org URL")] = None,
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
    output_dir: Annotated[Optional[Path], typer.Option("--output", "-o")] = None,
):
    """Generate synthetic clinical data from one or more templates.

    Describe what you want via --scenario and provide OPT or StructureDefinition files.
    When multiple templates are given, one composition per template is generated per patient,
    all sharing the same patient journey.

    Examples:
      python main.py generate vital_signs.opt --scenario "elderly patients with COPD" --count 5
      python main.py generate vitals.opt labs.opt --scenario "post-operative ICU patients" -n 3
      python main.py generate obs.json --format FHIR_R4 --scenario "pregnant women, third trimester"
    """
    for t in templates:
        if not t.exists():
            console.print(f"[red]Template file not found:[/red] {t}")
            raise typer.Exit(1)

    config = load_config(config_path)
    if output_dir:
        config.setdefault("pipeline", {})["output_dir"] = str(output_dir)

    pipeline = Pipeline(config)
    results = pipeline.run(
        template_paths=templates,
        format=format,
        count=count,
        scenario=scenario,
        upload=upload,
        ig_path=ig,
    )

    # Print result table
    table = Table(title="Generation Results")
    table.add_column("Patient")
    table.add_column("Template")
    table.add_column("Attempts")
    table.add_column("Valid")
    table.add_column("Issues")

    for r in results:
        valid_str = "[green]✓[/green]" if r.valid else "[red]✗[/red]"
        table.add_row(
            r.patient_id,
            r.template_id,
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

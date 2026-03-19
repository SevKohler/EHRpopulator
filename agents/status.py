#!/usr/bin/env python3
"""
EHR Populator — stack status checker.

Run via: make status

Checks:
  - Validator service (required for OPT conversion + validation)
  - Snowstorm (required for terminology lookups)
  - Terminology loading progress (SNOMED, LOINC, ICD-10)
  - EHRbase (optional — only needed for upload)
  - FHIR server (optional — only needed for upload)
  - Templates folder
  - API key
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import requests
import yaml
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

HERE = Path(__file__).parent
ROOT = HERE.parent


def load_config() -> dict:
    for p in [HERE / "config.local.yaml", HERE / "config.yaml"]:
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f)
    return {}


def check(url: str, timeout: int = 5) -> bool:
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def terminology_loaded(fhir_base: str, system_url: str) -> bool:
    try:
        r = requests.get(f"{fhir_base}/CodeSystem", params={"url": system_url}, timeout=5)
        return r.json().get("total", 0) > 0
    except Exception:
        return False


def main():
    config = load_config()
    validator_url  = config.get("validator",    {}).get("base_url",  "http://localhost:8181")
    snowstorm_url  = config.get("terminology",  {}).get("base_url",  "http://localhost:8085/fhir")
    ehrbase_url    = config.get("ehrbase",       {}).get("base_url",  "http://localhost:8080/ehrbase")
    fhir_url       = config.get("fhir_server",  {}).get("base_url",  "http://localhost:8090/fhir")
    ehrbase_on     = config.get("ehrbase",       {}).get("enabled",   False)
    fhir_on        = config.get("fhir_server",  {}).get("enabled",   False)

    console.print("\n[bold cyan]EHR Populator — Stack Status[/bold cyan]\n")

    # ── Services ──────────────────────────────────────────────────────────────
    svc_table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    svc_table.add_column("Service")
    svc_table.add_column("URL")
    svc_table.add_column("Status")
    svc_table.add_column("Note")

    validator_ok = check(f"{validator_url}/health")
    svc_table.add_row(
        "Validator",
        validator_url,
        "[green]UP[/green]" if validator_ok else "[red]DOWN[/red]",
        "" if validator_ok else "Required — run: sudo docker compose up -d",
    )

    snowstorm_base = snowstorm_url.replace("/fhir", "")
    snowstorm_ok = check(f"{snowstorm_base}/branches")
    svc_table.add_row(
        "Snowstorm",
        snowstorm_url,
        "[green]UP[/green]" if snowstorm_ok else "[red]DOWN[/red]",
        "" if snowstorm_ok else "Required — run: sudo docker compose up -d",
    )

    if ehrbase_on:
        ehrbase_ok = check(f"{ehrbase_url}/rest/openehr/v1/definition/template/adl1.4", timeout=5)
        svc_table.add_row(
            "EHRbase",
            ehrbase_url,
            "[green]UP[/green]" if ehrbase_ok else "[red]DOWN[/red]",
            "" if ehrbase_ok else "Needed for openEHR upload",
        )

    if fhir_on:
        fhir_ok = check(f"{fhir_url}/metadata")
        svc_table.add_row(
            "FHIR Server",
            fhir_url,
            "[green]UP[/green]" if fhir_ok else "[red]DOWN[/red]",
            "" if fhir_ok else "Needed for FHIR upload",
        )

    console.print(svc_table)

    # ── Terminology ───────────────────────────────────────────────────────────
    console.print("[bold]Terminology (Snowstorm)[/bold]")
    term_table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    term_table.add_column("Code System")
    term_table.add_column("Status")
    term_table.add_column("Note")

    terminologies = [
        ("SNOMED CT",  "http://snomed.info/sct",           "terminology/seeds/SnomedCT_*.zip"),
        ("LOINC",      "http://loinc.org",                 "terminology/seeds/Loinc_*.zip or *.json"),
        ("ICD-10",     "http://hl7.org/fhir/sid/icd-10",  "terminology/seeds/icd*.xml or *.zip"),
    ]

    all_loaded = True
    for label, url, hint in terminologies:
        if snowstorm_ok:
            loaded = terminology_loaded(snowstorm_url, url)
            status = "[green]Loaded[/green]" if loaded else "[yellow]Not loaded[/yellow]"
            note   = "" if loaded else f"Drop file into {hint}"
            if not loaded:
                all_loaded = False
        else:
            status = "[dim]Unknown[/dim]"
            note   = "Snowstorm is down"
            all_loaded = False
        term_table.add_row(label, status, note)

    console.print(term_table)

    if snowstorm_ok and not all_loaded:
        console.print("  [dim]If loading is in progress:[/dim] sudo docker compose logs -f terminology-loader\n")

    # ── Templates ─────────────────────────────────────────────────────────────
    console.print("[bold]Templates[/bold]")
    tmpl_table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    tmpl_table.add_column("Folder")
    tmpl_table.add_column("Files")

    for folder, patterns in [("templates/openehr", ["*.opt", "*.xml"]),
                               ("templates/fhir",   ["*.json"])]:
        d = ROOT / folder
        files = []
        for p in patterns:
            files += [f.name for f in d.glob(p)]
        if files:
            tmpl_table.add_row(folder, ", ".join(files))
        else:
            tmpl_table.add_row(folder, "[dim]empty — drop your templates here[/dim]")

    console.print(tmpl_table)

    # ── API key ───────────────────────────────────────────────────────────────
    console.print("[bold]API Key[/bold]")
    provider = config.get("llm", {}).get("provider", "anthropic")
    key_vars = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "azure": "AZURE_OPENAI_KEY"}
    key_var  = key_vars.get(provider, "ANTHROPIC_API_KEY")
    key_set  = bool(os.environ.get(key_var))
    console.print(f"  {key_var}: {'[green]set[/green]' if key_set else '[red]not set[/red] — edit agents/.env'}\n")

    # ── Summary ───────────────────────────────────────────────────────────────
    ready = validator_ok and snowstorm_ok and key_set
    if ready and all_loaded:
        console.print("[bold green]✓ All systems ready.[/bold green] Run: [bold]make run[/bold]\n")
    elif ready:
        console.print("[bold yellow]⚠ Services up but terminology still loading.[/bold yellow] "
                      "You can run [bold]make run[/bold] — lookups will be skipped for unloaded systems.\n")
    else:
        console.print("[bold red]✗ Not ready.[/bold red] Fix the issues above, then run [bold]make run[/bold]\n")


if __name__ == "__main__":
    main()

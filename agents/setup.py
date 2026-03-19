#!/usr/bin/env python3
"""
EHR Populator setup.

Run via: make setup

1. Waits for Snowstorm to be up
2. Loads SNOMED CT, LOINC, ICD-10 from terminology/seeds/
3. Shows Rich progress bars — polls until all terminologies are confirmed in Snowstorm
"""

import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import requests
import yaml
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

console = Console()

TERMINOLOGIES = [
    ("SNOMED CT", "http://snomed.info/sct"),
    ("LOINC",     "http://loinc.org"),
    ("ICD-10",    "http://hl7.org/fhir/sid/icd-10"),
]


def load_config() -> dict:
    for p in [Path(__file__).parent / "config.local.yaml",
              Path(__file__).parent / "config.yaml"]:
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f)
    return {}


def snowstorm_up(snowstorm_url: str) -> bool:
    try:
        requests.get(f"{snowstorm_url}/branches", timeout=3)
        return True
    except Exception:
        return False


def code_system_loaded(fhir_base: str, url: str) -> bool:
    try:
        r = requests.get(f"{fhir_base}/CodeSystem", params={"url": url}, timeout=5)
        return r.json().get("total", 0) > 0
    except Exception:
        return False


def main():
    config = load_config()
    term_cfg = config.get("terminology", {})
    fhir_base = term_cfg.get("base_url", "http://localhost:8085/fhir")
    snowstorm_url = fhir_base.replace("/fhir", "")

    console.print("\n[bold cyan]EHR Populator — Setup[/bold cyan]\n")

    # -------------------------------------------------------------------------
    # 1. Wait for Snowstorm
    # -------------------------------------------------------------------------
    console.print("Waiting for Snowstorm...", end=" ", highlight=False)
    while not snowstorm_up(snowstorm_url):
        console.print(".", end="", highlight=False)
        time.sleep(5)
    console.print(" [green]ready[/green]\n")

    # -------------------------------------------------------------------------
    # 2. Load terminology files
    # -------------------------------------------------------------------------
    from load_terminology import (
        load_seeds, load_fhir_dir, complete_snomed,
        load_state, save_state, code_system_exists,
        SEEDS_DIR,
    )

    def _log(msg):
        console.print(f"  [dim]{msg}[/dim]")

    console.print("[bold]Loading terminology files...[/bold]")
    seed_results = load_seeds(snowstorm_url, fhir_base, log=_log)

    if SEEDS_DIR.exists() and any(
        p.suffix in (".json",) and not p.name.startswith(".")
        for p in (Path(__file__).parent.parent / "terminology" / "fhir").iterdir()
        if (Path(__file__).parent.parent / "terminology" / "fhir").exists()
    ):
        console.print("\n[bold]Reloading FHIR folder...[/bold]")
        load_fhir_dir(fhir_base, log=_log)

    # -------------------------------------------------------------------------
    # 3. Watch progress until all terminologies confirmed in Snowstorm
    # -------------------------------------------------------------------------
    console.print()

    # Collect any running SNOMED import IDs
    snomed_imports: dict[str, str] = {}   # filename -> import_id
    for fname, result in seed_results.items():
        if result.snomed_import_id:
            snomed_imports[fname] = result.snomed_import_id

    # Track which terminologies already had errors during loading
    load_errors: dict[str, str] = {}     # system_url -> error message
    for fname, result in seed_results.items():
        if not result.ok and result.snomed_import_id is None:
            # Map filename to system URL
            from load_terminology import _is_snomed_rf2, _is_loinc
            if _is_snomed_rf2(fname):
                load_errors["http://snomed.info/sct"] = result.message
            elif _is_loinc(fname):
                load_errors["http://loinc.org"] = result.message
            else:
                load_errors["http://hl7.org/fhir/sid/icd-10"] = result.message

    loaded_state = load_state()

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("{task.fields[note]}"),
        console=console,
        transient=False,
    ) as progress:
        tasks = {}
        for label, url in TERMINOLOGIES:
            note = "[red]✗ failed[/red]" if url in load_errors else "waiting..."
            tid = progress.add_task(
                f"[cyan]{label}[/cyan]",
                total=1, completed=1 if url in load_errors else 0,
                note=note,
            )
            tasks[url] = tid

        while True:
            all_done = True

            for label, url in TERMINOLOGIES:
                tid = tasks[url]
                task = progress.tasks[tid]
                if task.completed:
                    continue

                all_done = False

                # Check if it's loaded in Snowstorm already
                if code_system_loaded(fhir_base, url):
                    progress.update(tid, completed=1, note="[green]✓ loaded[/green]")
                    continue

                # Poll SNOMED import if running
                if url == "http://snomed.info/sct" and snomed_imports:
                    for fname, import_id in list(snomed_imports.items()):
                        status, done = complete_snomed(
                            snowstorm_url, import_id, loaded_state, fname
                        )
                        note = f"importing... ({status})"
                        if done:
                            if status == "COMPLETED":
                                progress.update(tid, completed=1, note="[green]✓ loaded[/green]")
                            else:
                                progress.update(tid, completed=1,
                                                note=f"[red]✗ import {status}[/red]")
                                load_errors[url] = f"Snowstorm import status: {status}"
                            snomed_imports.pop(fname)
                        else:
                            progress.update(tid, note=note)
                    continue

                progress.update(tid, note="waiting...")

            if all_done:
                break
            time.sleep(10)

    # -------------------------------------------------------------------------
    # 4. Summary
    # -------------------------------------------------------------------------
    if load_errors:
        console.print("\n[bold red]Some terminologies failed to load:[/bold red]")
        for url, msg in load_errors.items():
            label = next((l for l, u in TERMINOLOGIES if u == url), url)
            console.print(f"  [red]✗ {label}:[/red] {msg}")
        console.print("\nCheck that your files in [dim]terminology/seeds/[/dim] are valid, then run [dim]make setup[/dim] again.\n")
    else:
        console.print("\n[bold green]✓ All terminologies loaded.[/bold green]")
        console.print("You can now run: [bold]make popu[/bold]\n")


if __name__ == "__main__":
    main()

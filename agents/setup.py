#!/usr/bin/env python3
"""
EHR Populator setup — watches terminology loading progress.

Run via: make setup

Polls Snowstorm until SNOMED, LOINC and ICD-10 are all loaded.
If the terminology-loader container exits with an error, fetches its logs
and displays the stacktrace so you can diagnose the problem.
"""

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import requests
import yaml
from rich.console import Console

console = Console()

TERMINOLOGIES = [
    ("SNOMED CT", "http://snomed.info/sct"),
    ("LOINC",     "http://loinc.org"),
    ("ICD-10",    "http://hl7.org/fhir/sid/icd-10"),
]

# Keywords in logs that indicate a specific terminology failed
TERMINOLOGY_ERROR_HINTS = {
    "http://snomed.info/sct": ["snomed", "rf2", "snapshot"],
    "http://loinc.org":       ["loinc"],
    "http://hl7.org/fhir/sid/icd-10": ["icd", "claml", "icd10"],
}


def load_config() -> dict:
    for p in [Path(__file__).parent / "config.local.yaml",
              Path(__file__).parent / "config.yaml"]:
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f)
    return {}


def check_loaded(fhir_base: str, system_url: str) -> bool:
    try:
        r = requests.get(f"{fhir_base}/CodeSystem",
                         params={"url": system_url}, timeout=5)
        return r.json().get("total", 0) > 0
    except Exception:
        return False


def snowstorm_up(fhir_base: str) -> bool:
    try:
        requests.get(fhir_base.replace("/fhir", "/branches"), timeout=3)
        return True
    except Exception:
        return False


def get_loader_logs(since: str) -> str:
    """Return logs from the terminology-loader container since a given ISO timestamp."""
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "--no-log-prefix", f"--since={since}", "terminology-loader"],
            capture_output=True, text=True,
            cwd=Path(__file__).parent.parent,
        )
        return result.stdout + result.stderr
    except Exception:
        return ""


def loader_exited_with_error() -> bool:
    """True if terminology-loader container has exited with a non-zero exit code."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--status", "exited", "terminology-loader"],
            capture_output=True, text=True,
            cwd=Path(__file__).parent.parent,
        )
        # If the container shows up in 'exited' list, check exit code
        if "terminology-loader" not in result.stdout:
            return False
        # Get exit code
        inspect = subprocess.run(
            ["docker", "inspect", "--format={{.State.ExitCode}}", "$(docker compose ps -q terminology-loader)"],
            capture_output=True, text=True, shell=False,
            cwd=Path(__file__).parent.parent,
        )
        # Simpler: check if container is running at all
        ps = subprocess.run(
            ["docker", "compose", "ps", "terminology-loader"],
            capture_output=True, text=True,
            cwd=Path(__file__).parent.parent,
        )
        # If status shows Exit/Exited and not running, check logs for ERROR
        output = ps.stdout.lower()
        if "exit" in output and "running" not in output:
            logs = get_loader_logs()
            return "error" in logs.lower() or "exception" in logs.lower() or "failed" in logs.lower()
        return False
    except Exception:
        return False


def extract_error_lines(logs: str, system_url: str) -> list[str]:
    """
    Extract relevant error lines from container logs.
    Returns lines around ERROR/Exception/Traceback that mention the terminology.
    """
    hints = TERMINOLOGY_ERROR_HINTS.get(system_url, [])
    lines = logs.splitlines()

    # Find error blocks
    error_lines = []
    in_traceback = False
    traceback_buf = []

    for i, line in enumerate(lines):
        lower = line.lower()

        # Start of a traceback
        if "traceback" in lower or "exception" in lower or lower.strip().startswith("error"):
            in_traceback = True
            traceback_buf = [line]
            continue

        if in_traceback:
            traceback_buf.append(line)
            # End of traceback: blank line or new log entry (timestamp-like)
            if not line.strip() or (len(line) > 0 and line[0].isdigit() and ":" in line[:20]):
                # Check if traceback is relevant to this terminology
                block = "\n".join(traceback_buf).lower()
                if not hints or any(h in block for h in hints):
                    error_lines.extend(traceback_buf)
                    error_lines.append("")
                in_traceback = False
                traceback_buf = []
            continue

        # Plain ERROR lines
        if "error" in lower or "failed" in lower or "fatal" in lower:
            if not hints or any(h in lower for h in hints):
                error_lines.append(line)

    # Flush remaining traceback
    if traceback_buf:
        block = "\n".join(traceback_buf).lower()
        if not hints or any(h in block for h in hints):
            error_lines.extend(traceback_buf)

    # If nothing found but container failed, return last 30 lines of logs
    if not error_lines:
        error_lines = lines[-30:]

    return error_lines


def main():
    config = load_config()
    fhir_base = config.get("terminology", {}).get("base_url", "http://localhost:8085/fhir")

    console.print("\n[bold cyan]EHR Populator — Setup[/bold cyan]\n")

    # Wait for Snowstorm
    console.print("Waiting for Snowstorm terminology server...", end=" ")
    while not snowstorm_up(fhir_base):
        console.print(".", end="")
        time.sleep(5)
    console.print(" [green]ready[/green]\n")

    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskID

    start_times = {url: time.time() for _, url in TERMINOLOGIES}
    # Only look at logs produced from this point forward
    log_since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("{task.fields[note]}"),
        console=console,
        transient=False,
    ) as progress:
        tasks: dict[str, TaskID] = {}
        for label, url in TERMINOLOGIES:
            tid = progress.add_task(f"[cyan]{label}[/cyan]", total=1, completed=0, note="loading...")
            tasks[url] = tid

        failed: dict[str, list[str]] = {}  # url -> error lines

        while True:
            pending = False
            logs = get_loader_logs(log_since)
            container_failed = loader_exited_with_error()

            for label, url in TERMINOLOGIES:
                tid = tasks[url]
                task = progress.tasks[tid]
                if task.completed:
                    continue

                if check_loaded(fhir_base, url):
                    progress.update(tid, completed=1, note="[green]✓ loaded[/green]")
                    continue

                # Check if loader has crashed and this terminology isn't loaded
                if container_failed:
                    error_lines = extract_error_lines(logs, url)
                    failed[url] = error_lines
                    progress.update(tid, completed=1, note="[red]✗ loader failed[/red]")
                    continue

                # Check logs for ERROR lines related to this terminology
                hints = TERMINOLOGY_ERROR_HINTS.get(url, [])
                lower_logs = logs.lower()
                if any(h in lower_logs for h in hints):
                    # Look for error lines specific to this terminology
                    error_lines = extract_error_lines(logs, url)
                    if error_lines:
                        failed[url] = error_lines
                        progress.update(tid, completed=1, note="[red]✗ error in logs[/red]")
                        continue

                elapsed = int(time.time() - start_times[url])
                progress.update(tid, note=f"loading... ({elapsed}s)")
                pending = True

            if not pending:
                break
            time.sleep(10)

    if failed:
        for label, url in TERMINOLOGIES:
            if url in failed:
                console.print(f"\n[bold red]✗ {label} failed to load[/bold red]")
                console.print("[dim]--- terminology-loader output ---[/dim]")
                for line in failed[url]:
                    console.print(f"  [red]{line}[/red]" if line.strip() else "")
                console.print("[dim]--- end ---[/dim]")
        console.print("\nRetry: [dim]sudo docker compose restart terminology-loader && make setup[/dim]\n")
    else:
        console.print("\n[bold green]✓ All terminologies loaded.[/bold green]")
        console.print("You can now run: [bold]make popu[/bold]\n")


if __name__ == "__main__":
    main()

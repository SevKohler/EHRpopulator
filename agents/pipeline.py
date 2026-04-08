"""
Generation pipeline — orchestrates the three agents and the validation loop.

Flow:
  1. Parse template (OPT XML or FHIR StructureDefinition)
  2. TemplateAnalyzerAgent → TemplateAnalysis
  3. JourneyGeneratorAgent × N → [PatientJourney, ...]
  4. For each journey:
     a. ResourceComposerAgent → raw JSON
     b. Validate via Java validator service
     c. If invalid and retries remain: feed errors back to step 4a
  5. Upload to EHRbase / FHIR server (optional)
  6. Save output files
"""

from __future__ import annotations
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

from models import (
    ResourceFormat, TemplateType, TemplateAnalysis,
    PatientJourney, GeneratedResource, ValidationResult, ValidationIssue,
)
from template_parser import parse_web_template, parse_structure_definition
from agents import TemplateAnalyzerAgent, JourneyGeneratorAgent, ResourceComposerAgent, TerminologyResolverAgent, _compose_thread_local
from tools import TerminologyTools, EHRbaseTools
from llm_client import build_llm

console = Console()


class Pipeline:

    def __init__(self, config: dict):
        self.config = config
        llm_cfg = config["llm"]
        llm = build_llm(llm_cfg)

        terminology_cfg = config.get("terminology", {})
        ehrbase_cfg = config.get("ehrbase", {})
        fhir_server_cfg = config.get("fhir_server", {})
        validator_cfg = config.get("validator", {})

        self.terminology = TerminologyTools(
            base_url=terminology_cfg.get("base_url", "http://localhost:8085/fhir"),
            bearer_token=terminology_cfg.get("bearer_token")
                         or os.environ.get("TERMINOLOGY_TOKEN"),
        )

        self.ehrbase: EHRbaseTools | None = None
        if ehrbase_cfg.get("enabled"):
            self.ehrbase = EHRbaseTools(
                base_url=ehrbase_cfg["base_url"],
                username=ehrbase_cfg.get("username", "ehrbase-user"),
                password=ehrbase_cfg.get("password")
                          or os.environ.get("EHRBASE_PASSWORD", ""),
            )

        self.fhir_server_url: str | None = None
        self.fhir_bearer: str | None = None
        if fhir_server_cfg.get("enabled"):
            self.fhir_server_url = fhir_server_cfg["base_url"]
            self.fhir_bearer = fhir_server_cfg.get("bearer_token") \
                               or os.environ.get("FHIR_BEARER_TOKEN")

        self.validator_url = validator_cfg.get("base_url", "http://localhost:8181")
        self.max_retries = config.get("pipeline", {}).get("max_retries", 5)
        self.output_dir = Path(config.get("pipeline", {}).get("output_dir", "../output"))
        self.workers = config.get("pipeline", {}).get("workers", 4)
        max_llm_concurrency = config.get("pipeline", {}).get("max_llm_concurrency", self.workers)
        self._llm_semaphore = threading.Semaphore(max_llm_concurrency)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.template_analyzer = TemplateAnalyzerAgent(llm)
        self.journey_generator = JourneyGeneratorAgent(llm)
        self.resolver = TerminologyResolverAgent(llm, self.terminology)
        self.composer = ResourceComposerAgent(llm, self.terminology, self.ehrbase)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        template_paths: list[Path],
        format: ResourceFormat,
        count: int = 1,
        scenario: str = "general adult patients",
        upload: bool = False,
        ig_path: str | None = None,
    ) -> list[GeneratedResource]:

        # Step 1: Load and analyze all templates
        # Each entry: (analysis, opt_xml_or_None, struct_def_or_None)
        template_infos: list[tuple[TemplateAnalysis, str | None, str | None]] = []

        for template_path in template_paths:
            template_content = template_path.read_text(encoding="utf-8")
            template_type = _detect_template_type(template_path)
            opt_xml: str | None = None
            struct_def: str | None = None

            if template_type == TemplateType.OPENEHR_OPT:
                # Check for cached web template next to the OPT file
                cached_wt = template_path.with_suffix(".json")
                if cached_wt.exists():
                    console.print(f"  Using cached web template: [cyan]{cached_wt.name}[/cyan]")
                    web_template = cached_wt.read_text(encoding="utf-8")
                else:
                    console.print(f"  Converting [cyan]{template_path.name}[/cyan] → web template...")
                    web_template = self._opt_to_web_template(template_content)
                    if not web_template:
                        raise RuntimeError(
                            f"OPT conversion failed for {template_path.name}. "
                            f"Start the validator: cd validator && mvn spring-boot:run"
                        )
                    cached_wt.write_text(web_template, encoding="utf-8")
                    console.print(f"  Saved web template → [dim]{cached_wt}[/dim]")
                opt_xml = template_content
                template_content = web_template
                template_type = TemplateType.OPENEHR_WEB_TEMPLATE

            elif template_type == TemplateType.FHIR_STRUCTURE_DEF:
                struct_def = template_content

            console.print(f"[bold]Analyzing:[/bold] {template_path.name}")
            with console.status("Parsing template structure..."):
                analysis = self.template_analyzer.analyze(template_content, template_type)

            if ig_path and template_type == TemplateType.FHIR_STRUCTURE_DEF:
                console.print(f"  Loading IG from: [cyan]{ig_path}[/cyan]")
                with console.status("Loading Implementation Guide..."):
                    from template_parser import load_ig_context
                    analysis.ig_context = load_ig_context(ig_path)
                console.print(f"  IG: [cyan]{analysis.ig_context.ig_name}[/cyan] "
                              f"({len(analysis.ig_context.value_sets)} ValueSets)")

            console.print(f"  [cyan]{analysis.name}[/cyan] — "
                          f"{len(analysis.required_elements)} required, "
                          f"{len(analysis.optional_elements)} optional elements")
            template_infos.append((analysis, opt_xml, struct_def))

        analyses = [a for a, _, _ in template_infos]

        results: list[GeneratedResource] = []
        lock = threading.Lock()

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Live display: progress bar on top, verbose log for patient 1 below
        log_lines: list[str] = []
        log_lock = threading.Lock()
        LOG_MAX = 30

        progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"),
            BarColumn(), MofNCompleteColumn(),
            console=console, auto_refresh=False,
        )
        task = progress.add_task(
            f"Generating {count} patient(s) × {len(analyses)} template(s)...",
            total=count,
        )

        def _get_renderable():
            with log_lock:
                lines = log_lines[-LOG_MAX:]
            if not lines:
                body = "[dim]Waiting for patient 1...[/dim]"
                content = Panel(body, title="[bold cyan]Patient 1 — live[/bold cyan]",
                                border_style="dim cyan", padding=(0, 1))
            else:
                parts = [item if not isinstance(item, str) else item for item in lines]
                content = Panel(Group(*parts), title="[bold cyan]Patient 1 — live[/bold cyan]",
                                border_style="dim cyan", padding=(0, 1))
            return Group(progress, content)

        def process_patient(i: int, live: Live) -> list[GeneratedResource]:
            verbose = (i == 0)

            def log(msg: str) -> None:
                if not verbose:
                    return
                with log_lock:
                    log_lines.append(msg)
                live.update(_get_renderable())

            log("[bold]Generating journey...[/bold]")
            with self._llm_semaphore:
                journey = self.journey_generator.generate(
                    analyses=analyses,
                    scenario=scenario,
                    patient_index=i,
                )

            log(Panel(
                escape(journey.narrative),
                title=f"[green]{journey.age}y {journey.gender}[/green]",
                title_align="left",
                expand=False,
            ))

            log("[bold]Resolving terminology codes...[/bold]")
            journey = self.resolver.resolve(journey, analyses, log_fn=log)

            patient_dir = self.output_dir / journey.patient_id
            patient_dir.mkdir(parents=True, exist_ok=True)
            (patient_dir / "journey.json").write_text(
                journey.model_dump_json(indent=2), encoding="utf-8"
            )

            def compose_template(args):
                j, analysis, opt_xml, struct_def = args
                entries = journey.compositions.get(analysis.template_id, [{}])
                if not entries:
                    entries = [{}]
                multi = len(entries) > 1
                log(f"\n[bold]Template {j+1}/{len(template_infos)}:[/bold] "
                    f"[cyan]{escape(analysis.name)}[/cyan]"
                    + (f"  ([dim]{len(entries)} encounters[/dim])" if multi else ""))

                resources = []
                for entry_idx, field_values in enumerate(entries):
                    if multi:
                        log(f"  [dim]Encounter {entry_idx+1}/{len(entries)}[/dim]")
                    with self._llm_semaphore:
                        resource = self._compose_with_validation(
                            journey, analysis, format, opt_xml, struct_def, log,
                            field_values=field_values,
                        )
                    entry_index = entry_idx if multi else None
                    with lock:
                        self._save(resource, i, entry_index=entry_index)
                    if upload:
                        self._upload(resource)
                    resources.append(resource)
                return resources

            with ThreadPoolExecutor(max_workers=len(template_infos)) as template_executor:
                nested = list(template_executor.map(
                    compose_template,
                    [(j, analysis, opt_xml, struct_def)
                     for j, (analysis, opt_xml, struct_def) in enumerate(template_infos)],
                ))
                patient_resources = [r for group in nested for r in group]
            return patient_resources

        with Live(_get_renderable(), console=console, refresh_per_second=4) as live:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {executor.submit(process_patient, i, live): i for i in range(count)}
                for future in as_completed(futures):
                    i = futures[future]
                    try:
                        patient_resources = future.result()
                        with lock:
                            results.extend(patient_resources)
                    except Exception as e:
                        tb = traceback.format_exc()
                        with log_lock:
                            if i == 0:
                                log_lines.append(f"[red]Patient {i+1} failed:[/red] {escape(str(e))}")
                        live.console.print(f"  [red]Patient {i+1} failed:[/red] {e}\n{tb}")
                    progress.advance(task)
                    live.update(_get_renderable())

        valid_count = sum(1 for r in results if r.valid)
        console.print(f"\n[bold]Done.[/bold] {valid_count}/{len(results)} compositions valid. "
                      f"Output: {self.output_dir}")
        return results

    # ------------------------------------------------------------------
    # Validation loop
    # ------------------------------------------------------------------

    def _compose_with_validation(
        self,
        journey: PatientJourney,
        analysis: TemplateAnalysis,
        format: ResourceFormat,
        opt_xml: str | None,
        structure_def_json: str | None,
        log_fn: Callable[[str], None] | None = None,
        field_values: dict | None = None,
    ) -> GeneratedResource:

        if log_fn is None:
            log_fn = lambda _: None  # noqa: E731

        # For canonical output: always generate FLAT internally, then convert.
        # This keeps all LLM composition logic in one place (flat format only).
        compose_format = (
            ResourceFormat.OPENEHR_FLAT
            if format == ResourceFormat.OPENEHR_CANONICAL
            else format
        )

        validation_errors = ""
        resource = None

        for attempt in range(1, self.max_retries + 1):
            log_fn(f"  [dim]Attempt {attempt}/{self.max_retries} — composing...[/dim]")

            raw = self.composer.compose(
                journey, analysis, compose_format.value, validation_errors,
                field_values=field_values,
            )
            raw = _strip_markdown(raw)
            if not raw.strip() or raw.lstrip().startswith("<|"):
                log_fn(f"  [red]LLM returned unusable response on attempt {attempt}[/red]")
                log_fn(f"  [dim]Leaked content: {raw[:200]}[/dim]")
                validation_errors = "Your previous response was empty or contained tool-call markup instead of JSON. You MUST return a valid JSON object only."
                continue

            # Convert flat → canonical via the SDK if canonical output was requested
            if format == ResourceFormat.OPENEHR_CANONICAL:
                log_fn(f"  [dim]Converting flat → canonical...[/dim]")
                canonical = self._flat_to_canonical(raw, opt_xml, analysis.template_id)
                if canonical is None:
                    log_fn(f"  [yellow]Flat→canonical conversion failed — retrying[/yellow]")
                    validation_errors = "Flat-to-canonical conversion failed. Check all paths are valid."
                    continue
                raw = canonical

            log_fn(f"  [dim]Validating...[/dim]")
            result = self._validate(raw, format, opt_xml, structure_def_json)

            resource = GeneratedResource(
                patient_id=journey.patient_id,
                template_id=analysis.template_id,
                format=format,
                content=raw,
                generation_attempt=attempt,
                valid=result.valid,
                validation_issues=result.issues,
            )

            if result.valid:
                log_fn(f"  [green]✓ Valid on attempt {attempt}[/green]")
                return resource

            errors = result.errors
            log_fn(f"  [yellow]✗ Attempt {attempt}: {len(errors)} error(s)[/yellow]")
            for err in errors[:8]:
                loc = escape(err.location or "/")
                msg = escape(err.message[:150])
                log_fn(f"    [red]•[/red] [dim]{loc}[/dim] {msg}")
            if len(errors) > 8:
                log_fn(f"    [dim]… and {len(errors) - 8} more[/dim]")

            if attempt < self.max_retries:
                validation_errors = result.error_summary()
            else:
                log_fn(f"  [red]Max retries reached — saving best-effort result.[/red]")

        if resource is None:
            log_fn(f"  [red]All {self.max_retries} attempts returned empty responses.[/red]")
            return GeneratedResource(
                patient_id=journey.patient_id,
                template_id=analysis.template_id,
                format=format,
                content="{}",
                generation_attempt=self.max_retries,
                valid=False,
                validation_issues=[ValidationIssue(
                    severity="ERROR", location="/",
                    message="LLM returned empty response on all attempts",
                )],
            )
        return resource

    def _flat_to_canonical(
        self,
        flat_json: str,
        opt_xml: str | None,
        template_id: str,
    ) -> str | None:
        """POST flat JSON to the validator /to-canonical endpoint and return canonical JSON."""
        try:
            payload: dict[str, Any] = {
                "flat_json": flat_json,
                "template_id": template_id,
            }
            if opt_xml:
                payload["opt_xml"] = opt_xml

            r = requests.post(
                f"{self.validator_url}/to-canonical",
                json=payload,
                timeout=30,
            )
            if r.status_code == 200:
                return r.text
            console.print(
                f"  [yellow]/to-canonical returned {r.status_code}:[/yellow] {r.text[:300]}"
            )
        except requests.exceptions.ConnectionError:
            console.print(
                f"  [red]Cannot reach validator at {self.validator_url}.[/red] "
                "Start it with: cd validator && mvn spring-boot:run"
            )
        return None

    def _opt_to_web_template(self, opt_xml: str) -> str | None:
        """POST OPT XML to the Java validator service and get back web template JSON."""
        try:
            r = requests.post(
                f"{self.validator_url}/webtemplate",
                data=opt_xml.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.text
            console.print(f"  [yellow]Validator /webtemplate returned {r.status_code}:[/yellow] {r.text[:200]}")
        except requests.exceptions.ConnectionError:
            console.print(
                f"  [red]Cannot reach validator at {self.validator_url}.[/red] "
                "Start it with: cd validator && mvn spring-boot:run"
            )
        return None

    def _validate(
        self,
        content: str,
        format: ResourceFormat,
        opt_xml: str | None,
        structure_def_json: str | None,
    ) -> ValidationResult:
        try:
            payload: dict[str, Any] = {
                "content": content,
                "format": format.value,
            }
            if opt_xml:
                payload["opt_xml"] = opt_xml
            if structure_def_json:
                payload["structure_definition_json"] = structure_def_json

            r = requests.post(
                f"{self.validator_url}/validate",
                json=payload,
                timeout=30,
            )
            data = r.json()
            issues = [
                ValidationIssue(
                    severity=i["severity"],
                    location=i["location"],
                    message=i["message"],
                )
                for i in data.get("issues", [])
            ]
            return ValidationResult(
                valid=data["valid"],
                issues=issues,
                issue_count=data.get("issue_count", len(issues)),
            )
        except requests.exceptions.ConnectionError:
            console.print(
                "  [yellow]Warning:[/yellow] Validator service not reachable at "
                f"{self.validator_url}. Skipping validation.")
            return ValidationResult(valid=True, issues=[], issue_count=0)
        except Exception as e:
            console.print(f"  [red]Validator error:[/red] {e}")
            return ValidationResult(
                valid=False,
                issues=[ValidationIssue(severity="ERROR", location="/",
                                        message=str(e))],
                issue_count=1,
            )

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def _upload(self, resource: GeneratedResource) -> None:
        if not resource.valid:
            console.print("  [yellow]Skipping upload — resource is not valid[/yellow]")
            return

        if resource.format in (ResourceFormat.OPENEHR_FLAT, ResourceFormat.OPENEHR_CANONICAL):
            self._upload_openehr(resource)
        elif resource.format == ResourceFormat.FHIR_R4:
            self._upload_fhir(resource)

    def _upload_openehr(self, resource: GeneratedResource) -> None:
        if not self.ehrbase:
            return
        try:
            ehr_url = f"{self.ehrbase.base_url}/rest/openehr/v1/ehr"
            ehr_resp = requests.post(ehr_url, auth=self.ehrbase.auth,
                                     json={"_type": "EHR_STATUS",
                                           "is_modifiable": True,
                                           "is_queryable": True},
                                     timeout=10)
            ehr_id = ehr_resp.headers.get("ETag", "").strip('"')
            if not ehr_id:
                console.print("  [red]Could not create EHR[/red]")
                return

            ct = ("application/openehr.wt.flat.schema+json"
                  if resource.format == ResourceFormat.OPENEHR_FLAT
                  else "application/json")
            comp_url = f"{self.ehrbase.base_url}/rest/openehr/v1/ehr/{ehr_id}/composition"
            resp = requests.post(comp_url, auth=self.ehrbase.auth,
                                 headers={"Content-Type": ct},
                                 data=resource.content.encode(), timeout=15)
            if resp.status_code == 201:
                console.print("  [green]↑[/green] Uploaded to EHRbase")
            else:
                console.print(f"  [red]EHRbase upload failed:[/red] {resp.status_code}")
        except Exception as e:
            console.print(f"  [red]EHRbase upload error:[/red] {e}")

    def _upload_fhir(self, resource: GeneratedResource) -> None:
        if not self.fhir_server_url:
            return
        try:
            data = json.loads(resource.content)
            rt = data.get("resourceType", "Bundle")
            url = f"{self.fhir_server_url}/{rt}"
            headers = {"Content-Type": "application/fhir+json"}
            if self.fhir_bearer:
                headers["Authorization"] = f"Bearer {self.fhir_bearer}"
            resp = requests.post(url, headers=headers,
                                 data=resource.content.encode(), timeout=15)
            if resp.status_code in (200, 201):
                console.print("  [green]↑[/green] Uploaded to FHIR server")
            else:
                console.print(f"  [red]FHIR upload failed:[/red] {resp.status_code}")
        except Exception as e:
            console.print(f"  [red]FHIR upload error:[/red] {e}")

    # ------------------------------------------------------------------
    # Save output
    # ------------------------------------------------------------------

    def _save(self, resource: GeneratedResource, index: int, entry_index: int | None = None) -> None:
        # Valid → output/{patient_id}/flat_{template_id}[_{n}].json
        # Failed → output/errors/{patient_id}/flat_{template_id}[_{n}].json + .errors.json
        template_slug = re.sub(r"[^\w\-]", "_", resource.template_id)
        suffix = f"_{entry_index + 1}" if entry_index is not None else ""
        filename = f"flat_{template_slug}{suffix}.json"

        if resource.valid:
            out_dir = self.output_dir / resource.patient_id
        else:
            out_dir = self.output_dir / "errors" / resource.patient_id
        out_dir.mkdir(parents=True, exist_ok=True)

        out_path = out_dir / filename
        try:
            pretty = json.dumps(json.loads(resource.content), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pretty = resource.content
        out_path.write_text(pretty, encoding="utf-8")
        resource.output_path = str(out_path)

        if not resource.valid:
            errors_data = {
                "patient_id": resource.patient_id,
                "template_id": resource.template_id,
                "format": resource.format.value,
                "generation_attempt": resource.generation_attempt,
                "issue_count": len(resource.validation_issues),
                "errors": [
                    {"severity": i.severity, "location": i.location, "message": i.message}
                    for i in resource.validation_issues
                    if i.severity in ("ERROR", "FATAL")
                ],
                "warnings": [
                    {"severity": i.severity, "location": i.location, "message": i.message}
                    for i in resource.validation_issues
                    if i.severity == "WARNING"
                ],
            }
            errors_path = out_dir / filename.replace(".json", ".errors.json")
            errors_path.write_text(json.dumps(errors_data, indent=2), encoding="utf-8")

            # Save the last prompt for debugging
            last_prompt = getattr(_compose_thread_local, "last_prompt", None)
            if last_prompt:
                prompt_path = out_dir / filename.replace(".json", ".prompt.txt")
                prompt_path.write_text(last_prompt, encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_template_type(path: Path) -> TemplateType:
    if path.suffix.lower() in (".opt", ".xml"):
        return TemplateType.OPENEHR_OPT
    content = path.read_text(encoding="utf-8", errors="ignore")[:500]
    if '"resourceType"' in content and '"StructureDefinition"' in content:
        return TemplateType.FHIR_STRUCTURE_DEF
    if "<?xml" in content and "template" in content.lower():
        return TemplateType.OPENEHR_OPT
    # Web template: JSON with "templateId" and "tree" keys
    if '"templateId"' in content and '"tree"' in content:
        return TemplateType.OPENEHR_WEB_TEMPLATE
    return TemplateType.FHIR_STRUCTURE_DEF


def _strip_markdown(text: str) -> str:
    """Remove ```json ... ``` fences from LLM output."""
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text.strip())
    return text.strip()

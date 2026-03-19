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
from pathlib import Path
from typing import Any

import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from models import (
    ResourceFormat, TemplateType, TemplateAnalysis,
    PatientJourney, GeneratedResource, ValidationResult, ValidationIssue,
)
from template_parser import parse_web_template, parse_structure_definition
from agents import TemplateAnalyzerAgent, JourneyGeneratorAgent, ResourceComposerAgent
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
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.template_analyzer = TemplateAnalyzerAgent(llm)
        self.journey_generator = JourneyGeneratorAgent(llm)
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
                console.print(f"  Converting [cyan]{template_path.name}[/cyan] → web template...")
                web_template = self._opt_to_web_template(template_content)
                if not web_template:
                    raise RuntimeError(
                        f"OPT conversion failed for {template_path.name}. "
                        f"Start the validator: cd validator && mvn spring-boot:run"
                    )
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
        total = count * len(analyses)

        # Wipe and recreate journey debug folder (keep max 10 samples)
        journey_dir = self.output_dir / "journeys"
        if journey_dir.exists():
            import shutil
            shutil.rmtree(journey_dir)
        journey_dir.mkdir(parents=True)
        JOURNEY_SAMPLE_LIMIT = 10

        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                      console=console) as progress:
            task = progress.add_task(f"Generating {count} patient(s) × {len(analyses)} template(s)...",
                                     total=total)

            for i in range(count):
                progress.update(task, description=f"Patient {i+1}/{count}: generating journey...")

                # Step 2: Generate one patient journey covering ALL templates
                journey = self.journey_generator.generate(
                    analyses=analyses,
                    scenario=scenario,
                    patient_index=i,
                )

                # Save journey for inspection (up to JOURNEY_SAMPLE_LIMIT)
                if i < JOURNEY_SAMPLE_LIMIT:
                    (journey_dir / f"journey_{i+1}_{journey.patient_id}.json").write_text(
                        journey.model_dump_json(indent=2), encoding="utf-8"
                    )

                # Step 3+4: Compose + validate for each template
                for analysis, opt_xml, struct_def in template_infos:

                    progress.update(task,
                        description=f"Patient {i+1}/{count}: composing {analysis.name} ({format.value})...")
                    resource = self._compose_with_validation(
                        journey, analysis, format, opt_xml, struct_def)

                    results.append(resource)
                    self._save(resource, i)

                    if upload:
                        progress.update(task, description=f"Patient {i+1}/{count}: uploading...")
                        self._upload(resource)

                    progress.advance(task)

        valid_count = sum(1 for r in results if r.valid)
        console.print(f"\n[bold]Done.[/bold] {valid_count}/{total} compositions valid. "
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
    ) -> GeneratedResource:

        validation_errors = ""

        for attempt in range(1, self.max_retries + 1):
            # Compose
            raw = self.composer.compose(journey, analysis, format.value, validation_errors)
            raw = _strip_markdown(raw)

            # Validate via Java validator service
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
                console.print(f"  [green]✓[/green] Valid on attempt {attempt}")
                return resource

            error_count = len(result.errors)
            console.print(f"  [yellow]✗[/yellow] Attempt {attempt}: {error_count} error(s)")

            if attempt < self.max_retries:
                validation_errors = result.error_summary()
            else:
                console.print(f"  [red]Max retries reached.[/red] Saving best-effort result.")

        return resource  # type: ignore[return-value]

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

    def _save(self, resource: GeneratedResource, index: int) -> None:
        suffix = "json"
        filename = (f"{resource.format.value.lower()}_{resource.patient_id}"
                    f"_attempt{resource.generation_attempt}_{index}.{suffix}")
        out_path = self.output_dir / filename
        out_path.write_text(resource.content, encoding="utf-8")

        # Save metadata alongside
        meta_path = self.output_dir / (filename.replace(".json", ".meta.json"))
        meta = {
            "patient_id": resource.patient_id,
            "template_id": resource.template_id,
            "format": resource.format.value,
            "valid": resource.valid,
            "generation_attempt": resource.generation_attempt,
            "issue_count": len(resource.validation_issues),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


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

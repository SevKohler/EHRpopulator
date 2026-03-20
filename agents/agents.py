"""
Agent definitions for the EHR Populator pipeline.

Three agents, each with a focused role:
  1. TemplateAnalyzerAgent — parses templates into structured element lists.
     - openEHR web template JSON → parsed programmatically (no LLM, deterministic)
     - FHIR StructureDefinition JSON → parsed programmatically (no LLM, deterministic)
     - Raw OPT XML → LLM fallback (web template is strongly preferred)
  2. JourneyGeneratorAgent — generates realistic patient journeys from template structure
  3. ResourceComposerAgent — converts journeys to valid openEHR/FHIR data (with tool access)
"""

from __future__ import annotations
import json
import re
from typing import Callable, Any

from models import (
    TemplateAnalysis, TemplateType, PatientJourney, DataElement,
)
from template_parser import parse_web_template, parse_structure_definition
from tools import TerminologyTools, EHRbaseTools
from knowledge_openehr import OPENEHR_RM_KNOWLEDGE


# ---------------------------------------------------------------------------
# System prompts (journey generation + composition only — no template parsing)
# ---------------------------------------------------------------------------


JOURNEY_GENERATOR_SYSTEM = """
You are a clinical expert generating synthetic patient data for healthcare system testing.

You receive a list of clinical templates (openEHR OPTs or FHIR StructureDefinitions) with their
exact field paths and types. Your job is to generate realistic patient data AND map it directly
onto those paths — so the downstream composition step only needs to validate terminology codes
and wrap values in format-specific types.

RULES:
1. Follow the scenario description — it defines the patient type and clinical focus
2. Be specific: exact dates (ISO 8601), numeric values with UCUM units, clinically realistic terms
3. Every required path must have a value in its template's field_values
4. For coded fields (paths ending in |value or plain text fields for coded concepts):
   - Set ONLY the |value key with the display term (e.g. "Type 2 diabetes mellitus")
   - NEVER set |code or |terminology — the composer will look up real codes from the terminology server
   - Exception: if the field list shows local at-codes (e.g. at0001=...), use those codes directly
5. For quantity fields use a plain number; put the unit in the companion |unit or |units path
6. For datetime fields use ISO 8601 (e.g. "2024-03-15T09:30:00+01:00")
7. Vary the patient — different age, gender, severity, comorbidities each time
8. Fictional patients only

REPEATABLE PATH SEGMENTS:
In the field list below, path segments marked with :N are repeatable (e.g. pro_laboranalyt:N).
Replace :N with a zero-based integer index (:0, :1, :2, …) for each entry.
ALL sub-paths under a repeated segment must share the same index.
Example — two lab analytes (pro_laboranalyt:N) each with one measurement (messwert:N):
  "laborbericht/laborbefund/jedes_ereignis/pro_laboranalyt:0/bezeichnung_des_analyts|value": "Troponin I",
  "laborbericht/laborbefund/jedes_ereignis/pro_laboranalyt:0/messwert:0|magnitude": 0.04,
  "laborbericht/laborbefund/jedes_ereignis/pro_laboranalyt:0/messwert:0|unit": "ng/mL",
  "laborbericht/laborbefund/jedes_ereignis/pro_laboranalyt:1/bezeichnung_des_analyts|value": "CRP",
  "laborbericht/laborbefund/jedes_ereignis/pro_laboranalyt:1/messwert:0|magnitude": 12.5,
  "laborbericht/laborbefund/jedes_ereignis/pro_laboranalyt:1/messwert:0|unit": "mg/L"
Rules:
- Replace every :N with a zero-based integer — use :0 for single entries, :0/:1/:2 for multiple
- The index goes on the segment marked :N in the path, NEVER after the | suffix
- Only index segments that appear with :N in the field list below

openEHR RM FIELDS — include these in every openEHR template's field_values.
Replace {root} with the template root id (e.g. "laborbericht", "kds_diagnose"):
  {root}/language|code                "de" or "en"
  {root}/language|terminology         "ISO_639-1"
  {root}/territory|code               "DE" or appropriate country
  {root}/territory|terminology        "ISO_3166-1"
  {root}/composer|name                Name of the treating clinician (e.g. "Dr. Maria Schmidt")
  {root}/category|code                "433"
  {root}/category|value               "event"
  {root}/category|terminology         "openehr"
  {root}/context/start_time           Encounter start (ISO 8601)
  {root}/context/setting|code         openehr setting code: 225=home, 232=secondary medical care
  {root}/context/setting|value        Setting display text
  {root}/context/setting|terminology  "openehr"
  {root}/context/_health_care_facility|name   Hospital or clinic name

Output valid JSON with exactly this structure:
{
  "patient_id": "PAT-001",
  "age": 54,
  "gender": "female",
  "narrative": "A detailed clinical narrative (3-5 paragraphs) telling the patient's story chronologically: presenting complaint, history, clinical course, relevant findings, treatments, and outcome. Write as a physician would document it — specific, clinically realistic, and consistent with all compositions below.",
  "compositions": {
    "<template_id>": {
      "context/start_time": "2024-03-15T09:30:00+01:00",
      "path/to/field|magnitude": 38.5,
      "path/to/field|unit": "Cel",
      "path/to/coded_field|value": "Hypertension",
      "path/to/coded_field|terminology": "SNOMED-CT"
    },
    "<another_template_id>": {
      "context/start_time": "2024-03-15T09:30:00+01:00",
      "path/to/other_field": "value"
    }
  }
}

Use the exact template IDs and paths listed below. Each template gets its own key in compositions.
Do not include any text outside the JSON.
""".strip()


RESOURCE_COMPOSER_SYSTEM = f"""
You are an expert in serializing pre-mapped clinical data into valid openEHR FLAT compositions
and FHIR R4 resources. You have access to terminology tools to validate and expand coded values.

The patient journey already contains field_values — a dict mapping each template path to a value.
Your job is to:
1. Use the terminology tools to look up correct codes for any coded fields (never invent codes)
2. Produce a valid EHRbase FLAT JSON — always, regardless of the requested output format
   (canonical conversion is handled downstream by the SDK)
3. Add all required RM-level fields
4. Return ONLY the final JSON — no markdown fences, no explanation

{OPENEHR_RM_KNOWLEDGE}

═══════════════════════════════════════════════════════════
FORMAT RULES
═══════════════════════════════════════════════════════════

For openEHR FLAT JSON (EHRbase FLAT format):
- The output is a single flat JSON object — NO nesting, NO "content" array, NO archetype wrappers
- Copy every key/value from field_values VERBATIM — do not rename or restructure paths
- Paths are already in EHRbase flat notation: e.g. laborbericht/laborbefund/pro_laboranalyt/messwert|magnitude
- Repeated entries use :0, :1 index notation: e.g. pro_laboranalyt:0/messwert|magnitude
- Do NOT generate aqlPath-style paths (with at-codes or archetype IDs in brackets)

MANDATORY CODING RULES — apply to every coded field:
- For every path ending in |value: you MUST also emit the companion |code and |terminology keys
- NEVER copy the display text into |code — |code must be a real numeric/alphanumeric code
- If the web template lists local at-codes (e.g. at0001=...): use those directly (terminology = "local")
- If the field has a ValueSet URL: call expand_value_set with that URL first
- Otherwise: use your clinical judgment to choose the right terminology, then call search_terminology
  with the system URIs most appropriate for the concept. If unsure what systems the server has,
  call list_code_systems first — it returns every CodeSystem available on the server so you can
  pick the best fit. You are not limited to SNOMED/LOINC/RxNorm; use whatever the server provides.
- Set |terminology to the system URI of the code you found
- Only fall back to free text when the terminology server returns no usable results

For FHIR R4 JSON:
- Map field_values FHIRPaths to the correct resource structure
- Use full coding objects: {{"system": "...", "code": "...", "display": "..."}}
- Look up codes via terminology tools before using them
- Include required fields: resourceType, status, subject, etc.

When validation errors are provided from a previous attempt:
- Fix only the paths listed in the errors
- Do not change anything else
""".strip()


# ---------------------------------------------------------------------------
# Agent implementations
# ---------------------------------------------------------------------------

class TemplateAnalyzerAgent:
    """
    Extracts structured element lists from clinical templates.

    Strategy:
    - openEHR web template JSON → parse_web_template() — deterministic, no LLM
    - FHIR StructureDefinition JSON → parse_structure_definition() — deterministic, no LLM
    - Raw OPT XML → LLM fallback via OPT_ANALYZER_SYSTEM prompt

    The programmatic parsers preserve the full clinical context from the template:
    descriptions (localizedDescriptions / element.definition), annotations, inline
    code lists, and numeric constraints — so the downstream agents have everything
    they need to generate accurate data without guessing field meanings.
    """

    def __init__(self, llm: Callable):
        self.llm = llm  # unused — kept for future extensibility

    def analyze(self, template_content: str, template_type: TemplateType) -> TemplateAnalysis:
        """
        Parse template content into a TemplateAnalysis.

        Both paths are fully programmatic — no LLM tokens spent on template parsing.

        OPT files are converted to web template JSON by the Java validator service
        before reaching this method (see pipeline.py _opt_to_web_template), so
        OPENEHR_OPT is never passed here directly.
        """
        if template_type == TemplateType.OPENEHR_WEB_TEMPLATE:
            return parse_web_template(template_content)

        if template_type == TemplateType.FHIR_STRUCTURE_DEF:
            return parse_structure_definition(template_content)

        raise ValueError(
            f"Unexpected template type: {template_type}. "
            "OPT files must be converted to web template via the validator service first."
        )


class JourneyGeneratorAgent:
    """
    Generates realistic patient journeys from a template analysis.
    No tools needed — pure LLM reasoning.
    """

    def __init__(self, llm: Callable):
        self.llm = llm

    def generate(
        self,
        analyses: list[TemplateAnalysis],
        scenario: str = "general adult patients",
        patient_index: int = 0,
    ) -> PatientJourney:
        templates_section = ""
        for analysis in analyses:
            templates_section += f"""
TEMPLATE: {analysis.name}
  template_id (use as key in compositions): "{analysis.template_id}"
  Clinical concepts: {', '.join(analysis.clinical_concepts)}
  {f'Notes: {analysis.notes}' if analysis.notes else ''}
  Required fields:
{_format_elements(analysis.required_elements, slim=True)}
  Optional fields (include where clinically appropriate):
{_format_elements(analysis.optional_elements, slim=True)}
""".strip() + "\n\n"

        user_msg = f"""
Generate patient data for patient #{patient_index + 1}.

SCENARIO (defines the patient type — follow closely):
{scenario}

Each template below gets its own key in the "compositions" output dict (use the template_id exactly).
Map required paths for each template into the corresponding compositions entry.
For repeatable fields (cardinality 0..n / 1..n) generate multiple indexed entries where clinically
appropriate — e.g. multiple diagnoses, multiple observations.

{templates_section.strip()}

Make this patient unique — vary age, gender, severity, comorbidities, and clinical course.
""".strip()

        raw = self.llm(JOURNEY_GENERATOR_SYSTEM, user_msg)
        data = extract_json(raw)
        return PatientJourney.model_validate(data)


class ResourceComposerAgent:
    """
    Converts a patient journey into a valid openEHR composition or FHIR resource.
    Has access to terminology tools for code lookup.
    Supports validation-error feedback for iterative correction.
    """

    def __init__(
        self,
        llm: Callable,
        terminology_tools: TerminologyTools,
        ehrbase_tools: EHRbaseTools | None = None,
    ):
        self.llm = llm
        self.terminology = terminology_tools
        self.ehrbase = ehrbase_tools

        # Build tool definitions and handlers
        self._tool_defs = terminology_tools.as_tool_definitions()
        self._handlers: dict[str, Callable] = {
            name: terminology_tools.get_handler(name)
            for tool in terminology_tools.as_tool_definitions()
            for name in [tool["name"]]
        }

        if ehrbase_tools:
            self._tool_defs.extend(ehrbase_tools.as_tool_definitions())
            for tool in ehrbase_tools.as_tool_definitions():
                self._handlers[tool["name"]] = ehrbase_tools.get_handler(tool["name"])

    def compose(
        self,
        journey: PatientJourney,
        analysis: TemplateAnalysis,
        format: str,
        validation_errors: str = "",
    ) -> str:
        """
        Generate a composition/resource for the given journey and template.

        Args:
            journey: The patient journey to serialize
            analysis: Template structure (paths, types, value sets)
            format: OPENEHR_FLAT | OPENEHR_CANONICAL | FHIR_R4
            validation_errors: Formatted error list from previous attempt (empty on first try)

        Returns:
            Raw JSON string of the generated resource
        """
        error_section = ""
        if validation_errors:
            error_section = f"""
VALIDATION ERRORS FROM PREVIOUS ATTEMPT — FIX THESE:
{validation_errors}

Fix only the paths listed above. Keep the rest of the resource unchanged.
"""

        ig_section = _format_ig_context(analysis.ig_context) if analysis.ig_context else ""

        user_msg = f"""
Generate a valid {format} resource for this patient journey.

TEMPLATE: {analysis.name} (ID: {analysis.template_id})
{analysis.description}

REQUIRED PATHS AND TYPES:
{_format_elements(analysis.required_elements)}

OPTIONAL PATHS (include where data is available):
{_format_elements(analysis.optional_elements)}

{ig_section}
PATIENT: {journey.patient_id}, age {journey.age}, {journey.gender}
SUMMARY: {journey.narrative}

PRE-MAPPED FIELD VALUES (use these directly — look up codes for coded fields):
{json.dumps(journey.compositions.get(analysis.template_id, {}), indent=2)}

{error_section}
Use the terminology tools to look up correct codes before populating coded fields.
Return ONLY the JSON resource — no markdown, no explanation.
""".strip()

        # Pass tool handlers to the LLM callable via a side-channel
        # (the LLM callable checks for _tool_handlers in its kwargs)
        # We wrap the call to inject handlers
        return self._call_with_tools(RESOURCE_COMPOSER_SYSTEM, user_msg)

    def _call_with_tools(self, system: str, user: str) -> str:
        """
        Invoke the LLM with tool access.
        The llm callable must support an agentic tool-use loop.
        """
        import anthropic as _anthropic

        # Re-run the tool-use loop manually to keep the agents.py file self-contained
        api_key = None
        for ev in ["ANTHROPIC_API_KEY"]:
            api_key = os.environ.get(ev)
            if api_key:
                break

        if api_key:
            client = _anthropic.Anthropic(api_key=api_key)
            return self._anthropic_tool_loop(client, system, user)
        else:
            # Fall back to non-tool call (OpenAI path handled in llm_client)
            return self.llm(system, user, self._tool_defs)

    def _anthropic_tool_loop(self, client, system: str, user: str) -> str:
        import os
        messages = [{"role": "user", "content": user}]
        model = os.environ.get("LLM_MODEL", "claude-opus-4-5")

        while True:
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                system=system,
                messages=messages,
                tools=self._tool_defs,
            )

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        handler = self._handlers.get(block.name)
                        if handler:
                            try:
                                result = handler(**block.input)
                            except Exception as e:
                                result = f"Error: {e}"
                        else:
                            result = f"Unknown tool: {block.name}"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        })
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict:
    """Extract a JSON object from LLM output, handling markdown fences."""
    # Strip markdown fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    # Find the first { ... } block
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in LLM output:\n{text[:500]}")
    # Find matching closing brace
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("Unterminated JSON in LLM output")


def _format_elements(elements: list[DataElement], slim: bool = False) -> str:
    """Format elements for prompt injection.

    slim=True omits allowed_codes (used for journey generation — the LLM writes
    display terms and the composer handles code lookup, so the code list wastes tokens).
    """
    if not elements:
        return "(none)"
    lines = []
    for el in elements:
        repeatable = el.cardinality not in ("0..1", "1..1", "1")
        card_str = f"{el.cardinality} *** REPEATABLE — use :0,:1,… indexes ***" if repeatable else el.cardinality
        line = f"  - {el.path} [{el.data_type}] ({card_str})"

        if el.description:
            line += f"\n      Description: {el.description}"

        if el.annotations:
            for k, v in el.annotations.items():
                line += f"\n      Annotation [{k}]: {v}"

        if not slim:
            if el.allowed_codes:
                codes_preview = ", ".join(
                    f"{c.value}={c.label}" for c in el.allowed_codes[:6]
                )
                if len(el.allowed_codes) > 6:
                    codes_preview += f" … (+{len(el.allowed_codes) - 6} more)"
                line += f"\n      Allowed codes: {codes_preview}"
            elif el.value_set_url:
                term = f" ({el.terminology})" if el.terminology else ""
                line += f"\n      ValueSet: {el.value_set_url}{term}"

        if el.constraints:
            for c in el.constraints:
                if c.suffix == "magnitude" and (c.min is not None or c.max is not None):
                    line += f"\n      Range: {c.min} – {c.max}"
                if c.allowed_units:
                    units = ", ".join(u.value for u in c.allowed_units)
                    line += f"\n      Units: {units}"

        lines.append(line)
    return "\n".join(lines)


def _format_ig_context(ig_context) -> str:
    if not ig_context:
        return ""
    lines = [f"IMPLEMENTATION GUIDE: {ig_context.ig_name} {ig_context.ig_version}"]
    if ig_context.ig_url:
        lines.append(f"  URL: {ig_context.ig_url}")
    if ig_context.usage_notes:
        lines.append(f"  Usage guidance:\n    {ig_context.usage_notes[:800]}")
    if ig_context.population_notes:
        lines.append("  Population notes:")
        for note in ig_context.population_notes[:5]:
            lines.append(f"    - {note[:200]}")
    vs_urls = list(ig_context.value_sets.keys())
    if vs_urls:
        lines.append(f"  IG-defined ValueSets available for lookup: {', '.join(vs_urls[:10])}")
    return "\n".join(lines) + "\n"


import os

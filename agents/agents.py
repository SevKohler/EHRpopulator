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
    TemplateAnalysis, TemplateType, PatientJourney,
    DataElement, PatientDemographics, ClinicalEvent,
)
from template_parser import parse_web_template, parse_structure_definition
from tools import TerminologyTools, EHRbaseTools


# ---------------------------------------------------------------------------
# System prompts (journey generation + composition only — no template parsing)
# ---------------------------------------------------------------------------


JOURNEY_GENERATOR_SYSTEM = """
You are a clinical data expert generating realistic synthetic patient journeys
for healthcare system testing. Journeys must be:

1. Medically realistic and internally consistent
2. Specific — include exact values (not placeholders): dates, numeric measurements, real codes
3. Diverse — vary demographics, severity, and clinical presentations between patients
4. Complete — cover all data points listed in the template structure
5. Safe — fictional patients only, no real patient data

Use real medical terminology:
- ICD-10 codes for diagnoses (e.g., E11.9 for type 2 diabetes)
- SNOMED CT display terms for findings and procedures
- LOINC names for lab tests and observations
- Drug names from standard formularies

Output valid JSON with this structure:
{
  "demographics": {
    "patient_id": "string",
    "age": number,
    "gender": "male | female | other",
    "relevant_history": "string"
  },
  "events": [
    {
      "timestamp": "ISO 8601 datetime",
      "event_type": "string",
      "description": "string",
      "data_points": {
        "field_name": "value"
      }
    }
  ],
  "narrative_summary": "string"
}

Generate realistic timestamps in chronological order.
Include specific numeric values with units for all measurements.
Do not include any explanation text outside the JSON.
""".strip()


RESOURCE_COMPOSER_SYSTEM = """
You are an expert in serializing clinical data to valid openEHR compositions
and FHIR R4 resources. You have access to tools to look up valid terminology codes.

RULES:
1. ALWAYS call terminology tools before populating coded fields — never invent codes
2. Use real standard codes: SNOMED CT, LOINC, ICD-10, UCUM units
3. Populate ALL required elements from the template structure
4. Respect exact data types and paths from the template analysis
5. Return ONLY the JSON — no markdown fences, no explanation text

For openEHR FLAT JSON:
- Use exact flat paths from the template (e.g., vitals/body_temperature:0/any_event:0/temperature|magnitude)
- Include ctx/template_id, ctx/language, ctx/territory, ctx/time
- Use UCUM for units: Cel (not °C), mm[Hg], kg, cm, /min, /s

For openEHR CANONICAL JSON:
- Include archetype_details with template_id and archetype_id
- Use correct DV_* types: DV_CODED_TEXT with defining_code, DV_QUANTITY with magnitude+units
- Include proper language and territory objects

For FHIR R4 JSON:
- Set correct resourceType
- Include meta.profile if a profile URL was provided
- Use full coding objects: {"system": "...", "code": "...", "display": "..."}
- Status fields are required: use appropriate values (final, active, etc.)
- Include subject reference for patient-linked resources

When validation errors are provided from a previous attempt:
- Read each error carefully with its path location
- Fix the specific error at that path
- Do not change parts of the resource that were not flagged
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
        analysis: TemplateAnalysis,
        demographic_context: str = "general adult patients",
        patient_index: int = 0,
    ) -> PatientJourney:
        user_msg = f"""
Generate a realistic synthetic patient journey for patient #{patient_index + 1}.
Patient population context: {demographic_context}

The journey must provide clinical data for ALL of these template elements:

REQUIRED ELEMENTS:
{_format_elements(analysis.required_elements)}

OPTIONAL ELEMENTS (include where clinically relevant):
{_format_elements(analysis.optional_elements)}

Template: {analysis.name} ({analysis.template_id})
Clinical concepts: {', '.join(analysis.clinical_concepts)}
Notes: {analysis.notes}

Generate a unique, realistic patient — vary age, gender, severity, and presentation.
Ensure all required elements have specific values that can be directly used in the composition.
""".strip()

        raw = self.llm(JOURNEY_GENERATOR_SYSTEM, user_msg)
        return PatientJourney.model_validate(extract_json(raw))


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
PATIENT JOURNEY:
{journey.model_dump_json(indent=2)}

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


def _format_elements(elements: list[DataElement]) -> str:
    if not elements:
        return "(none)"
    lines = []
    for el in elements:
        line = f"  - {el.path} [{el.data_type}] ({el.cardinality})"

        if el.description:
            line += f"\n      Description: {el.description}"

        if el.annotations:
            for k, v in el.annotations.items():
                line += f"\n      Annotation [{k}]: {v}"

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

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
import threading
from typing import Callable, Any

_compose_thread_local = threading.local()

try:
    from toon_format import encode as _toon_encode
    _TOON_AVAILABLE = True
except ImportError:
    _toon_encode = None
    _TOON_AVAILABLE = False


def _to_toon(data) -> str:
    if _TOON_AVAILABLE:
        return _toon_encode(data)
    return json.dumps(data, indent=2)

from models import (
    TemplateAnalysis, TemplateType, PatientJourney, DataElement,
)
from template_parser import parse_web_template, parse_structure_definition
from tools import TerminologyTools, EHRbaseTools
from knowledge_openehr import OPENEHR_RM_KNOWLEDGE

_INDEX_RE = re.compile(r':\d+')


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
9. Each template's compositions entry is a LIST of clinical sessions (even if only one).
   - Each element in the list = one COMPOSITION = one distinct clinical act/session
   - A new composition is needed whenever a clinically separate event occurs for
     that template type — even on the same day. Examples:
       • Sodium measured on admission → laborbericht composition 1
       • HbA1c ordered separately later that day → laborbericht composition 2
       • A diagnosis documented → diagnose composition (different template, separate list)
   - Within a single session, use repeatable path indexes (:0, :1, …) for multiple
     results reported together in one order (e.g. a full metabolic panel: sodium,
     potassium, creatinine all in one laborbericht with pro_laboranalyt:0/:1/:2)
   - The narrative drives this: each distinct clinical event described in the
     narrative should map to its own composition entry

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
    "<template_id>": [
      {
        "context/start_time": "2024-03-15T09:30:00+01:00",
        "path/to/field|magnitude": 38.5,
        "path/to/field|unit": "Cel",
        "path/to/coded_field|value": "Hypertension"
      },
      {
        "context/start_time": "2024-03-22T14:15:00+01:00",
        "path/to/field|magnitude": 37.2,
        "path/to/field|unit": "Cel",
        "path/to/coded_field|value": "Hypertension"
      }
    ],
    "<another_template_id>": [
      {
        "context/start_time": "2024-03-15T09:30:00+01:00",
        "path/to/other_field": "value"
      }
    ]
  }
}

Use the exact template IDs and paths listed below. Each template gets its own key in compositions.
Do not include any text outside the JSON.
""".strip()


RESOURCE_COMPOSER_SYSTEM = f"""
You are an expert in serializing pre-mapped clinical data into valid openEHR FLAT compositions
and FHIR R4 resources.

The patient journey already contains field_values — a dict mapping each template path to a value.
All coded fields have already been resolved: |code and |terminology keys are pre-filled.
Your job is to:
1. Copy ALL field_values verbatim into the output JSON — codes are pre-resolved, do not change them
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

MANDATORY — A valid composition MUST include ALL of the following:
1. RM metadata: <root>/language|code, <root>/language|terminology, <root>/territory|code,
   <root>/territory|terminology, <root>/composer|name, <root>/category|code,
   <root>/category|value, <root>/category|terminology, <root>/context/start_time,
   <root>/context/setting|code, <root>/context/setting|value, <root>/context/setting|terminology
2. ALL clinical content paths from field_values — lab results, diagnoses, observations, etc.
   WITHOUT these clinical paths the validator will reject the composition with "content is required".
   Omitting clinical content is the most common error — always include every path from field_values.

MANDATORY CODING RULES — apply to every coded field:
- ALL |code and |terminology keys are PRE-RESOLVED — copy them verbatim from field_values
- NEVER change or invent codes — if |code is present in field_values, use it exactly as-is
- For every path ending in |value: the companion |code and |terminology are already in field_values
- If a |code key is missing from field_values (resolver gap): leave |code empty rather than inventing

For FHIR R4 JSON:
- Map field_values FHIRPaths to the correct resource structure
- Use full coding objects: {{"system": "...", "code": "...", "display": "..."}}
- Look up codes via terminology tools before using them
- Include required fields: resourceType, status, subject, etc.

When validation errors are provided from a previous attempt:
- Fix only the paths listed in the errors
- Do not change anything else

CRITICAL — the response must ALWAYS be a non-empty JSON object containing actual
clinical content paths (lab results, diagnoses, observations, etc.).
A response with only metadata fields (language, territory, composer, category) is
invalid and will fail with "content is required".
If you are unsure of a value, use a plausible placeholder — never return an empty
object or omit the clinical data section.
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
            aid = id(analysis)
            if aid not in _journey_section_cache:
                _journey_section_cache[aid] = f"""
TEMPLATE: {analysis.name}
  template_id (use as key in compositions): "{analysis.template_id}"
  Clinical concepts: {', '.join(analysis.clinical_concepts)}
  {f'Notes: {analysis.notes}' if analysis.notes else ''}
  Required fields:
{_format_elements(analysis.required_elements, slim=True)}
  Optional fields (include where clinically appropriate):
{_format_elements(analysis.optional_elements, slim=True)}
""".strip() + "\n\n"
            templates_section += _journey_section_cache[aid]

        user_msg = f"""
Generate patient data for patient #{patient_index + 1}.

SCENARIO (defines the patient type — follow closely):
{scenario}

{templates_section.strip()}

STEP 1 — Before writing JSON, think through the clinical timeline:
For each template above, list every distinct clinical event in the patient's story that
would produce a separate composition of that type. Example:
  KDS_Laborbericht: [admission labs 2024-03-15, follow-up labs 2024-03-22]
  KDS_Diagnose: [diagnosis documented 2024-03-15]

STEP 2 — Write the JSON using that timeline.
Each template key maps to a LIST — one dict per event from step 1.
Multiple analytes/diagnoses within one event use :0/:1 indexes inside one dict.

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
        field_values: dict | None = None,
    ) -> str:
        """
        Generate a composition/resource for the given journey and template.

        Args:
            journey: The patient journey to serialize
            analysis: Template structure (paths, types, value sets)
            format: OPENEHR_FLAT | OPENEHR_CANONICAL | FHIR_R4
            validation_errors: Formatted error list from previous attempt (empty on first try)
            field_values: Pre-mapped values for this specific encounter; if None, falls back
                          to the first (or only) entry in journey.compositions[template_id]

        Returns:
            Raw JSON string of the generated resource
        """
        if field_values is None:
            entries = journey.compositions.get(analysis.template_id, [{}])
            field_values = entries[0] if entries else {}

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
PATIENT: {journey.patient_id}, age {journey.age}, {journey.gender}

CODED FIELDS (pre-resolved — |code and |terminology are already in field_values, copy verbatim):
{_format_coded_elements(analysis.required_elements + analysis.optional_elements)}

{ig_section}
PRE-MAPPED FIELD VALUES (copy every key verbatim — codes are already resolved):
{_to_toon(field_values)}

{error_section}
Return ONLY the JSON resource — no markdown, no explanation.
""".strip()

        _compose_thread_local.last_prompt = user_msg  # stored per-thread for failure diagnostics
        return self._call_llm(RESOURCE_COMPOSER_SYSTEM, user_msg)

    def _call_llm(self, system: str, user: str) -> str:
        """Invoke the LLM for pure JSON serialization — no tools needed."""
        return self.llm(system, user)

    def _anthropic_tool_loop(self, client, system: str, user: str) -> str:
        import os
        messages = [{"role": "user", "content": user}]
        model = os.environ.get("LLM_MODEL", "claude-opus-4-5")
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "32768"))

        while True:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
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


TERMINOLOGY_RESOLVER_SYSTEM = """
You are a clinical terminology expert selecting the best code match.
Given a clinical display term and a list of candidate codes from a terminology server,
return the single best match as JSON: {"code": "...", "system": "..."}
Return ONLY the JSON object. No markdown, no explanation.
""".strip()


class TerminologyResolverAgent:
    """
    Pre-resolves all coded fields in a PatientJourney before composition.
    Fills |code and |terminology into field_values so the composer needs no tools.

    Three resolution modes (in priority order per field):
    1. Local at-codes  — matched directly from DataElement.allowed_codes (no I/O)
    2. Constrained     — ValueSet URL known → expand + fuzzy-match (HTTP, no LLM)
    3. Unconstrained   — search_terminology directly in Python; LLM picks if ambiguous
    """

    _resolved_codes_cache: dict[tuple[str, str], tuple[str, str]] = {}
    _cache_lock = threading.Lock()

    def __init__(self, llm: Callable, terminology_tools: TerminologyTools):
        self.llm = llm
        self.terminology = terminology_tools

    def resolve(
        self,
        journey: PatientJourney,
        analyses: list[TemplateAnalysis],
        log_fn: Callable[[str], None] | None = None,
    ) -> PatientJourney:
        """Return a new PatientJourney with |code and |terminology filled for all coded fields."""
        if log_fn is None:
            log_fn = lambda _: None  # noqa: E731

        element_index = self._build_element_index(analyses)
        new_compositions: dict[str, list[dict[str, Any]]] = {}

        total_resolved = 0
        for template_id, entries in journey.compositions.items():
            new_entries = []
            for fv in entries:
                enriched, n = self._resolve_field_values(fv, element_index)
                total_resolved += n
                new_entries.append(enriched)
            new_compositions[template_id] = new_entries

        if total_resolved:
            log_fn(f"  [green]✓[/green] Pre-resolved {total_resolved} terminology codes")

        return journey.model_copy(update={"compositions": new_compositions})

    def _build_element_index(self, analyses: list[TemplateAnalysis]) -> dict[str, DataElement]:
        """normalized_base_path → DataElement for every coded field."""
        index: dict[str, DataElement] = {}
        for analysis in analyses:
            for el in analysis.required_elements + analysis.optional_elements:
                if "CODED" not in el.data_type.upper() and "CODEABLECONCEPT" not in el.data_type.upper():
                    continue
                base = el.path[:-6] if el.path.endswith("|value") else el.path
                index[_normalize_path(base)] = el
        return index

    def _resolve_field_values(
        self,
        field_values: dict[str, Any],
        element_index: dict[str, DataElement],
    ) -> tuple[dict[str, Any], int]:
        result = dict(field_values)
        n = 0
        for key, value in field_values.items():
            if not key.endswith("|value") or not isinstance(value, str) or not value.strip():
                continue
            base = key[:-6]
            code_key = base + "|code"
            if field_values.get(code_key):
                continue  # already resolved
            element = element_index.get(_normalize_path(base))
            if element is None:
                continue
            resolved = self._resolve_code(value, element)
            if resolved:
                result[code_key] = resolved[0]
                result[base + "|terminology"] = resolved[1]
                n += 1
        return result, n

    def _resolve_code(self, display_term: str, element: DataElement) -> tuple[str, str] | None:
        cache_key = (display_term.lower().strip(),
                     element.value_set_url or element.terminology or element.path)
        with self._cache_lock:
            if cache_key in self._resolved_codes_cache:
                return self._resolved_codes_cache[cache_key]

        result = None
        local_codes = [ac for ac in element.allowed_codes if ac.terminology in ("local", "")]
        if local_codes:
            result = self._resolve_local(display_term, element)
        elif element.value_set_url:
            result = self._resolve_constrained(display_term, element.value_set_url)
        else:
            result = self._resolve_unconstrained(display_term, element)

        if result:
            with self._cache_lock:
                self._resolved_codes_cache[cache_key] = result
        return result

    def _resolve_local(self, display_term: str, element: DataElement) -> tuple[str, str] | None:
        term_lower = display_term.lower().strip()
        for ac in element.allowed_codes:
            if ac.label.lower().strip() == term_lower:
                return (ac.value, "local")
        for ac in element.allowed_codes:
            label = ac.label.lower().strip()
            if term_lower in label or label in term_lower:
                return (ac.value, "local")
        return None

    def _resolve_constrained(self, display_term: str, value_set_url: str) -> tuple[str, str] | None:
        raw = self.terminology._get("/ValueSet/$expand", {"url": value_set_url, "count": "50"})
        try:
            contains = json.loads(raw).get("expansion", {}).get("contains", [])
        except (json.JSONDecodeError, ValueError):
            return None
        if not contains:
            return None
        term_lower = display_term.lower().strip()
        for e in contains:
            if e.get("display", "").lower().strip() == term_lower:
                return (e["code"], e.get("system", ""))
        for e in contains:
            d = e.get("display", "").lower()
            if term_lower in d or d in term_lower:
                return (e["code"], e.get("system", ""))
        # filtered fallback
        raw2 = self.terminology._get("/ValueSet/$expand", {"url": value_set_url, "filter": display_term, "count": "5"})
        try:
            filtered = json.loads(raw2).get("expansion", {}).get("contains", [])
            if filtered:
                return (filtered[0]["code"], filtered[0].get("system", ""))
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
        return None

    def _resolve_unconstrained(self, display_term: str, element: DataElement) -> tuple[str, str] | None:
        systems = self._infer_systems(element)
        system_to_vs = {
            "http://snomed.info/sct": "http://snomed.info/sct?fhir_vs",
            "http://loinc.org": "http://loinc.org/vs",
            "http://hl7.org/fhir/sid/icd-10": "http://hl7.org/fhir/sid/icd-10",
            "http://www.nlm.nih.gov/research/umls/rxnorm": "http://www.nlm.nih.gov/research/umls/rxnorm/vs",
        }
        all_matches = []
        for system in systems:
            vs_url = system_to_vs.get(system, system)
            raw = self.terminology._get("/ValueSet/$expand", {"url": vs_url, "filter": display_term, "count": "5"})
            try:
                for entry in json.loads(raw).get("expansion", {}).get("contains", []):
                    all_matches.append({
                        "code": entry.get("code"),
                        "display": entry.get("display", ""),
                        "system": entry.get("system", system),
                    })
            except (json.JSONDecodeError, ValueError):
                pass

        if not all_matches:
            return None

        term_lower = display_term.lower().strip()
        for m in all_matches:
            if m["display"].lower().strip() == term_lower:
                return (m["code"], m["system"])

        if len(all_matches) == 1:
            return (all_matches[0]["code"], all_matches[0]["system"])

        # Multiple candidates — ask LLM to pick (text only, no tools)
        try:
            user_msg = (
                f'Clinical term: "{display_term}"\n'
                f'Path: {element.path}\n'
                f'Candidates:\n{json.dumps(all_matches[:6], indent=2)}\n\n'
                f'Return ONLY: {{"code": "...", "system": "..."}}'
            )
            picked = extract_json(self.llm(TERMINOLOGY_RESOLVER_SYSTEM, user_msg))
            if picked.get("code"):
                return (str(picked["code"]), str(picked.get("system", "")))
        except Exception:
            pass

        return (all_matches[0]["code"], all_matches[0]["system"])

    def _infer_systems(self, element: DataElement) -> list[str]:
        hint = (element.terminology or "").lower()
        path = element.path.lower()
        if "snomed" in hint:
            return ["http://snomed.info/sct"]
        if "loinc" in hint:
            return ["http://loinc.org"]
        if "icd" in hint:
            return ["http://hl7.org/fhir/sid/icd-10"]
        if "rxnorm" in hint or "medication" in hint:
            return ["http://www.nlm.nih.gov/research/umls/rxnorm"]
        if any(x in path for x in ("analyt", "labortest", "loinc")):
            return ["http://loinc.org", "http://snomed.info/sct"]
        if any(x in path for x in ("diagnos", "problem", "condition")):
            return ["http://snomed.info/sct", "http://hl7.org/fhir/sid/icd-10"]
        if any(x in path for x in ("medic", "drug", "substanz")):
            return ["http://www.nlm.nih.gov/research/umls/rxnorm", "http://snomed.info/sct"]
        return ["http://snomed.info/sct", "http://loinc.org"]


def _normalize_path(path: str) -> str:
    return _INDEX_RE.sub(":N", path)


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


def _format_coded_elements(elements: list[DataElement]) -> str:
    """List only coded fields (DV_CODED_TEXT) with their value sets/allowed codes.
    Used in the composer prompt — non-coded fields are simply copied from field_values.
    """
    coded = [el for el in elements if "CODED" in el.data_type.upper()]
    if not coded:
        return "(none — all fields are plain text/quantity, copy from field_values)"
    lines = []
    for el in coded:
        repeatable = el.cardinality not in ("0..1", "1..1", "1")
        card = " (repeatable)" if repeatable else ""
        line = f"  - {el.path}{card}"
        if el.allowed_codes:
            codes = ", ".join(f"{c.value}={c.label}" for c in el.allowed_codes[:6])
            if len(el.allowed_codes) > 6:
                codes += f" (+{len(el.allowed_codes)-6} more)"
            line += f" → codes: {codes}"
        elif el.value_set_url:
            line += f" → ValueSet: {el.value_set_url}"
        elif el.terminology:
            line += f" → terminology: {el.terminology}"
        lines.append(line)
    return "\n".join(lines)


def _format_elements(elements: list[DataElement], slim: bool = False) -> str:
    """Format elements for prompt injection.

    slim=True omits allowed_codes and descriptions (used for journey generation and
    composition — the LLM writes display terms and the composer handles code lookup).
    """
    if not elements:
        return "(none)"
    lines = []
    for el in elements:
        repeatable = el.cardinality not in ("0..1", "1..1", "1")
        card_str = f"{el.cardinality} *** REPEATABLE — use :0,:1,… indexes ***" if repeatable else el.cardinality
        line = f"  - {el.path} [{el.data_type}] ({card_str})"

        # Skip descriptions and annotations for slim mode (composer already has field_values)
        if not slim and el.description:
            line += f"\n      Description: {el.description[:120]}"

        # Annotations are always skipped — they add tokens without helping composition

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

# Cache for formatted journey prompt sections (keyed by analysis object id).
# Analysis objects are created once per pipeline run and reused across patients,
# so id() is stable for the lifetime of the run.
_journey_section_cache: dict[int, str] = {}

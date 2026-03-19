"""
Shared data models for the EHR Populator agent pipeline.
"""

from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class ResourceFormat(str, Enum):
    FHIR_R4 = "FHIR_R4"
    OPENEHR_FLAT = "OPENEHR_FLAT"
    OPENEHR_CANONICAL = "OPENEHR_CANONICAL"


class TemplateType(str, Enum):
    OPENEHR_OPT = "openehr_opt"
    OPENEHR_WEB_TEMPLATE = "openehr_web_template"   # preferred over raw OPT
    FHIR_STRUCTURE_DEF = "fhir_structure_definition"


class AllowedCode(BaseModel):
    """A single allowed code for a coded element."""
    value: str           # the code itself
    label: str           # display / preferred term
    terminology: str = ""


class InputConstraint(BaseModel):
    """Numeric or unit constraints for DV_QUANTITY / decimal fields."""
    suffix: str                          # "magnitude" or "unit"
    type: str                            # "DECIMAL", "CODED_TEXT", etc.
    min: float | None = None
    max: float | None = None
    allowed_units: list[AllowedCode] = Field(default_factory=list)


class DataElement(BaseModel):
    """A single clinical data element extracted from a template."""
    path: str
    name: str
    data_type: str                        # DV_CODED_TEXT, DV_QUANTITY, CodeableConcept, …
    required: bool = False
    cardinality: str = "0..1"
    value_set_url: str | None = None      # FHIR ValueSet URL or openEHR terminology binding
    terminology: str | None = None        # SNOMED-CT, LOINC, local, …
    # Human-readable clinical meaning — from localizedDescriptions or element.definition
    description: str = ""
    # Designer annotations from the archetype/template (e.g. "use SNOMED CT preferred term")
    annotations: dict[str, str] = Field(default_factory=dict)
    # Inline allowed codes (saves a terminology lookup for small local value sets)
    allowed_codes: list[AllowedCode] = Field(default_factory=list)
    # Numeric/unit constraints for quantity fields
    constraints: list[InputConstraint] = Field(default_factory=list)
    example_value: str | None = None


class IgContext(BaseModel):
    """
    Supplementary context from a FHIR Implementation Guide loaded alongside
    a StructureDefinition. The IG parser extracts this and injects it into
    the composer agent's prompt so it understands population intent, expected
    value set bindings, and narrative usage guidance.
    """
    ig_url: str = ""                       # canonical IG URL (e.g. https://hl7.org/fhir/us/core)
    ig_name: str = ""
    ig_version: str = ""
    # ValueSet resources indexed by canonical URL → full resource JSON string
    value_sets: dict[str, str] = Field(default_factory=dict)
    # CodeSystem resources indexed by canonical URL
    code_systems: dict[str, str] = Field(default_factory=dict)
    # SearchParameter / CapabilityStatement population notes extracted as free text
    population_notes: list[str] = Field(default_factory=list)
    # Narrative from IG pages relevant to the profile (e.g. must-support guidance)
    usage_notes: str = ""


class TemplateAnalysis(BaseModel):
    """Structured analysis of a clinical template, produced by the TemplateAnalyzerAgent."""
    template_id: str
    template_type: TemplateType
    name: str
    description: str = ""
    required_elements: list[DataElement] = Field(default_factory=list)
    optional_elements: list[DataElement] = Field(default_factory=list)
    clinical_concepts: list[str] = Field(default_factory=list)
    notes: str = ""
    # FHIR only: IG context loaded alongside the StructureDefinition
    ig_context: IgContext | None = None


class ClinicalEvent(BaseModel):
    """A single event in a patient journey."""
    timestamp: str                         # ISO 8601
    event_type: str                        # e.g. "vital_signs", "diagnosis", "medication"
    description: str
    data_points: dict[str, Any] = Field(default_factory=dict)  # field name -> value


class PatientDemographics(BaseModel):
    patient_id: str
    age: int
    gender: str
    relevant_history: str = ""


class PatientJourney(BaseModel):
    """A realistic patient journey, produced by the JourneyGeneratorAgent."""
    demographics: PatientDemographics
    events: list[ClinicalEvent] = Field(default_factory=list)
    narrative_summary: str = ""


class ValidationIssue(BaseModel):
    severity: str    # ERROR, WARNING, INFORMATION
    location: str    # FHIRPath or openEHR AQL path
    message: str


class ValidationResult(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    issue_count: int = 0

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity in ("ERROR", "FATAL")]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    def error_summary(self) -> str:
        """Formatted error list to feed back into the composer agent prompt."""
        if not self.errors:
            return ""
        lines = [f"- [{e.location}] {e.message}" for e in self.errors]
        return "\n".join(lines)


class GeneratedResource(BaseModel):
    """A generated openEHR composition or FHIR resource."""
    patient_id: str
    template_id: str
    format: ResourceFormat
    content: str                    # Raw JSON string
    generation_attempt: int = 1
    valid: bool = False
    validation_issues: list[ValidationIssue] = Field(default_factory=list)

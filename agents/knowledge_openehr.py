"""
openEHR Reference Model knowledge — loaded from rm_classes.json and rendered
as a structured text block for injection into agent system prompts.

Source: agents/openEHRKnowledge/rm_classes.json
        (extracted from the official openEHR EHR IM and Common IM PDFs)
"""

import json
from pathlib import Path

_KNOWLEDGE_DIR = Path(__file__).parent / "openEHRKnowledge"
_RM_CLASSES_FILE    = _KNOWLEDGE_DIR / "rm_classes.json"
_TERMINOLOGY_FILE   = _KNOWLEDGE_DIR / "openehr_terminology.json"


def _load_rm() -> dict:
    with open(_RM_CLASSES_FILE, encoding="utf-8") as f:
        return json.load(f)


def _load_terminology() -> dict:
    with open(_TERMINOLOGY_FILE, encoding="utf-8") as f:
        return json.load(f)


def _render_attributes(attributes: list[dict]) -> str:
    if not attributes:
        return "  (no additional attributes)"
    lines = []
    for attr in attributes:
        mult = attr.get("multiplicity", "")
        required = "REQUIRED" if mult.startswith("1") else "optional"
        notes = f" — {attr['notes']}" if attr.get("notes") else ""
        lines.append(
            f"  {attr['name']} [{attr['type']}] ({required}): {attr['description']}{notes}"
        )
    return "\n".join(lines)


def _render_class(cls: dict) -> str:
    lines = []
    abstract = " (abstract)" if cls.get("abstract") else ""
    lines.append(f"{'─'*56}")
    lines.append(f"{cls['name']}{abstract}  ←  extends {cls.get('parent_class', '?')}")
    lines.append(f"  Purpose: {cls['description']}")
    if cls.get("when_to_use"):
        lines.append(f"  Use when: {cls['when_to_use']}")
    lines.append("  Attributes:")
    lines.append(_render_attributes(cls.get("attributes", [])))
    invs = cls.get("invariants", [])
    if invs:
        lines.append(f"  Constraints: {'; '.join(invs[:3])}")
    return "\n".join(lines)


def _render_terminology(term: dict) -> str:
    lines = [
        "\n## openEHR TERMINOLOGY CODE SYSTEMS",
        "(terminology_id = \"openehr\" in all compositions)\n",
    ]
    for cs in term.get("code_systems", []):
        name = cs["name"]
        desc = cs.get("description", "")
        concepts = cs.get("concepts", [])
        lines.append(f"{name}  [{desc}]")
        has_definitions = any(c.get("definition") for c in concepts)
        if has_definitions:
            for c in concepts:
                defn = f" — {c['definition']}" if c.get("definition") else ""
                lines.append(f"  {c['code']}={c['display']}{defn}")
        else:
            lines.append("  " + ",  ".join(f"{c['code']}={c['display']}" for c in concepts))
    return "\n".join(lines)


def _build_knowledge_text(rm: dict) -> str:
    sections = []

    ehr = rm.get("ehr_information_model", {})
    common = rm.get("common_information_model", {})

    sections.append("═══════════════════════════════════════════════════════")
    sections.append("openEHR REFERENCE MODEL — CLASS DEFINITIONS")
    sections.append("(extracted from openEHR EHR IM and Common IM specifications)")
    sections.append("═══════════════════════════════════════════════════════")

    # --- Composition & context ---
    sections.append("\n## COMPOSITION AND EVENT CONTEXT\n")
    for key in ("composition", "event_context"):
        cls = ehr.get(key)
        if cls:
            sections.append(_render_class(cls))

    # --- Entry hierarchy ---
    sections.append("\n## ENTRY CLASSES\n")
    for key in ("entry", "care_entry", "admin_entry",
                "observation", "evaluation",
                "instruction", "activity",
                "action", "instruction_details", "ism_transition"):
        cls = ehr.get(key)
        if cls:
            sections.append(_render_class(cls))

    sections.append("""
─────────────────────────────────────────────────
ISM NOTE: careflow_step terminology = "local"  (at-code from the archetype, e.g. "at0016")
          current_state terminology = "openehr"
          Most common state for administered medication / completed procedure: 532=completed
""")

    # --- Party types ---
    sections.append("\n## PARTY TYPES (who/subject fields)\n")
    generic = common.get("generic_package", {})
    for key in ("party_proxy", "party_self", "party_identified", "party_related", "participation"):
        cls = generic.get(key)
        if cls:
            sections.append(_render_class(cls))

    sections.append("""
─────────────────────────────────────────────────
FLAT JSON ctx shortcuts (for openEHR flat format only):
  ctx/template_id              template ID string
  ctx/language                 ISO 639-1 (e.g. "en", "de")
  ctx/territory                ISO 3166-1 alpha-2 (e.g. "DE", "US")
  ctx/time                     authoring time (ISO 8601)
  ctx/composer_name            authoring clinician name
  ctx/health_care_facility_name  facility name
  ctx/id_scheme                "local"
  ctx/id_namespace             "local"
""")

    return "\n".join(sections)


# Build once at import time
_rm_data   = _load_rm()
_term_data = _load_terminology()
OPENEHR_RM_KNOWLEDGE = _build_knowledge_text(_rm_data) + "\n" + _render_terminology(_term_data)


def get_class_info(class_name: str) -> dict | None:
    """Return raw JSON definition for a class by name (case-insensitive)."""
    key = class_name.lower()
    ehr = _rm_data.get("ehr_information_model", {})
    common = _rm_data.get("common_information_model", {})
    return (
        ehr.get(key)
        or common.get("archetyped_package", {}).get(key)
        or common.get("generic_package", {}).get(key)
    )

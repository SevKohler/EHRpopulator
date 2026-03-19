"""
Programmatic parsers for openEHR web templates and FHIR StructureDefinitions.

These replace the LLM-based TemplateAnalyzerAgent for structured template formats.
Parsing is deterministic and extracts:
  - All element paths (exactly as EHRbase expects them in flat compositions)
  - RM types / FHIR types
  - Cardinality (required vs optional)
  - Inline allowed codes (skips terminology lookup for small local value sets)
  - Numeric constraints and allowed units for DV_QUANTITY
  - localizedDescriptions → element description (clinical meaning)
  - annotations → designer notes (what the field represents, usage guidance)

The LLM sees this enriched element list and can generate accurate data
without having to guess field meanings or valid code ranges.
"""

from __future__ import annotations
import json
from typing import Any

from models import (
    TemplateAnalysis, TemplateType, DataElement,
    AllowedCode, InputConstraint, IgContext,
)

# RM types that are leaf data values (not containers)
_LEAF_RM_TYPES = {
    "DV_TEXT", "DV_CODED_TEXT", "DV_QUANTITY", "DV_COUNT",
    "DV_BOOLEAN", "DV_DATE", "DV_DATE_TIME", "DV_TIME",
    "DV_DURATION", "DV_IDENTIFIER", "DV_URI", "DV_MULTIMEDIA",
    "DV_PROPORTION", "DV_ORDINAL", "DV_SCALE", "CODE_PHRASE",
    "STRING", "BOOLEAN", "INTEGER", "REAL",
}

# RM types to skip entirely (internal structural nodes, not data)
_SKIP_RM_TYPES = {"HISTORY", "ITEM_TREE", "ITEM_LIST", "ITEM_SINGLE", "ISM_TRANSITION"}


# ---------------------------------------------------------------------------
# openEHR web template parser
# ---------------------------------------------------------------------------

def parse_web_template(json_str: str) -> TemplateAnalysis:
    """
    Parse an EHRbase web template JSON into a TemplateAnalysis.

    Web template JSON shape (from GET /definition/template/adl1.4/{id}):
    {
      "templateId": "...",
      "version": "...",
      "defaultLanguage": "en",
      "tree": { ... recursive node tree ... }
    }

    Each node may have:
      id, name, rmType, nodeId, min, max, aqlPath,
      localizedNames, localizedDescriptions, annotations,
      inputs: [ { suffix, type, list: [{value, label}], validation: {range} } ],
      children: [ ... ]
    """
    data = json.loads(json_str)
    template_id = data.get("templateId", "unknown")
    default_lang = data.get("defaultLanguage", "en")
    tree = data.get("tree", {})

    root_name = _localized(tree, "localizedNames", default_lang) or template_id
    root_desc = _localized(tree, "localizedDescriptions", default_lang) or ""

    required_elements: list[DataElement] = []
    optional_elements: list[DataElement] = []

    _walk_node(tree, "", default_lang, required_elements, optional_elements)

    # Collect unique clinical concepts from top-level section names
    clinical_concepts = [
        _localized(child, "localizedNames", default_lang) or child.get("id", "")
        for child in tree.get("children", [])
        if child.get("rmType") not in _SKIP_RM_TYPES
    ]

    return TemplateAnalysis(
        template_id=template_id,
        template_type=TemplateType.OPENEHR_WEB_TEMPLATE,
        name=root_name,
        description=root_desc,
        required_elements=required_elements,
        optional_elements=optional_elements,
        clinical_concepts=[c for c in clinical_concepts if c],
        notes=f"Parsed from web template. Default language: {default_lang}.",
    )


def _walk_node(
    node: dict,
    parent_path: str,
    lang: str,
    required: list[DataElement],
    optional: list[DataElement],
) -> None:
    rm_type = node.get("rmType", "")
    node_id = node.get("id", "")
    aql_path = node.get("aqlPath", "")
    min_occ = node.get("min", 0)
    max_occ = node.get("max", 1)   # -1 means unbounded

    if rm_type in _SKIP_RM_TYPES:
        # Recurse into children but don't emit this node itself
        for child in node.get("children", []):
            _walk_node(child, parent_path, lang, required, optional)
        return

    if rm_type in _LEAF_RM_TYPES:
        # This is a data value node — emit it
        element = _build_element(node, aql_path or parent_path, lang, min_occ, max_occ)
        if min_occ >= 1:
            required.append(element)
        else:
            optional.append(element)
        return

    # Structural node (OBSERVATION, CLUSTER, ELEMENT, EVENT, etc.)
    # Recurse into children; use aqlPath as the running path
    current_path = aql_path or parent_path
    for child in node.get("children", []):
        _walk_node(child, current_path, lang, required, optional)


def _build_element(node: dict, path: str, lang: str, min_occ: int, max_occ: int) -> DataElement:
    rm_type = node.get("rmType", "UNKNOWN")
    name = _localized(node, "localizedNames", lang) or node.get("id", path.split("/")[-1])
    description = _localized(node, "localizedDescriptions", lang) or ""
    annotations = _extract_annotations(node)
    allowed_codes: list[AllowedCode] = []
    constraints: list[InputConstraint] = []
    value_set_url: str | None = None
    terminology: str | None = None

    for inp in node.get("inputs", []):
        suffix = inp.get("suffix", "")
        inp_type = inp.get("type", "")

        # Inline code list (local value set — no server lookup needed)
        code_list = inp.get("list", [])
        if code_list:
            allowed_codes = [
                AllowedCode(
                    value=item.get("value", ""),
                    label=item.get("label", item.get("value", "")),
                    terminology=item.get("terminologyId", "local"),
                )
                for item in code_list
            ]

        # External terminology binding
        if inp.get("terminology"):
            terminology = inp["terminology"]
        if inp.get("listOpen") is False and not code_list:
            # Bound to external value set
            value_set_url = inp.get("defaultValue")

        # Numeric range constraints
        validation = inp.get("validation", {})
        range_constraint = validation.get("range", {})
        if range_constraint or inp_type == "DECIMAL":
            ic = InputConstraint(
                suffix=suffix,
                type=inp_type,
                min=range_constraint.get("min"),
                max=range_constraint.get("max"),
            )
            # Allowed units come from the companion "unit" input
            constraints.append(ic)

    # Collect allowed units from any "unit" suffix input
    for inp in node.get("inputs", []):
        if inp.get("suffix") == "unit" and inp.get("list"):
            for c in constraints:
                if c.suffix == "magnitude":
                    c.allowed_units = [
                        AllowedCode(value=u["value"], label=u.get("label", u["value"]))
                        for u in inp["list"]
                    ]

    cardinality = f"{min_occ}..{'*' if max_occ == -1 else max_occ}"

    return DataElement(
        path=path,
        name=name,
        data_type=rm_type,
        required=min_occ >= 1,
        cardinality=cardinality,
        value_set_url=value_set_url,
        terminology=terminology,
        description=description,
        annotations=annotations,
        allowed_codes=allowed_codes,
        constraints=constraints,
    )


def _localized(node: dict, key: str, lang: str) -> str:
    mapping = node.get(key, {})
    return mapping.get(lang) or next(iter(mapping.values()), "") if mapping else ""


def _extract_annotations(node: dict) -> dict[str, str]:
    raw = node.get("annotations", {})
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items()}
    return {}


# ---------------------------------------------------------------------------
# FHIR StructureDefinition parser
# ---------------------------------------------------------------------------

def parse_structure_definition(json_str: str) -> TemplateAnalysis:
    """
    Parse a FHIR R4 StructureDefinition JSON into a TemplateAnalysis.

    Extracts element definitions from snapshot.element (preferred) or
    differential.element. Each element's description comes from:
      - element.definition  (full clinical description)
      - element.short       (brief label)
      - element.comment     (additional usage notes → stored as annotation)
    """
    data = json.loads(json_str)
    template_id = data.get("url") or data.get("id") or "unknown"
    name = data.get("title") or data.get("name") or template_id
    description = data.get("description") or ""
    resource_type = data.get("type", "")

    # Prefer snapshot (complete) over differential (changes only)
    elements: list[dict] = (
        data.get("snapshot", {}).get("element", [])
        or data.get("differential", {}).get("element", [])
    )

    required_elements: list[DataElement] = []
    optional_elements: list[DataElement] = []

    for el in elements:
        path = el.get("id") or el.get("path", "")
        # Skip the root element itself
        if "." not in path:
            continue

        min_occ = el.get("min", 0)
        max_occ_raw = el.get("max", "1")
        max_occ = -1 if max_occ_raw == "*" else int(max_occ_raw)

        # Type(s)
        types = el.get("type", [])
        data_type = " | ".join(t.get("code", "") for t in types) if types else "string"

        # Description from the element definition (clinical meaning)
        elem_description = el.get("definition") or el.get("short") or ""

        # Annotations: short label + comment + mustSupport flag
        annotations: dict[str, str] = {}
        if el.get("short"):
            annotations["short"] = el["short"]
        if el.get("comment"):
            annotations["comment"] = el["comment"]
        if el.get("mustSupport"):
            annotations["mustSupport"] = "true"
        if el.get("isModifier"):
            annotations["isModifier"] = el.get("isModifierReason", "true")

        # ValueSet binding
        binding = el.get("binding", {})
        value_set_url = binding.get("valueSet")
        binding_strength = binding.get("strength")
        if binding_strength:
            annotations["binding_strength"] = binding_strength

        # Inline fixed / pattern codes
        allowed_codes: list[AllowedCode] = []
        if "fixedCoding" in el:
            fc = el["fixedCoding"]
            allowed_codes.append(AllowedCode(
                value=fc.get("code", ""),
                label=fc.get("display", ""),
                terminology=fc.get("system", ""),
            ))
        if "patternCodeableConcept" in el:
            for coding in el["patternCodeableConcept"].get("coding", []):
                allowed_codes.append(AllowedCode(
                    value=coding.get("code", ""),
                    label=coding.get("display", ""),
                    terminology=coding.get("system", ""),
                ))

        cardinality = f"{min_occ}..{'*' if max_occ == -1 else max_occ}"
        is_required = min_occ >= 1

        element = DataElement(
            path=path,
            name=el.get("short") or path.split(".")[-1],
            data_type=data_type,
            required=is_required,
            cardinality=cardinality,
            value_set_url=value_set_url,
            description=elem_description,
            annotations=annotations,
            allowed_codes=allowed_codes,
        )

        if is_required:
            required_elements.append(element)
        else:
            optional_elements.append(element)

    clinical_concepts = list({el.path.split(".")[0] for el in required_elements + optional_elements})

    return TemplateAnalysis(
        template_id=template_id,
        template_type=TemplateType.FHIR_STRUCTURE_DEF,
        name=name,
        description=description,
        required_elements=required_elements,
        optional_elements=optional_elements,
        clinical_concepts=clinical_concepts,
        notes=f"Parsed from FHIR StructureDefinition. Resource type: {resource_type}.",
    )


# ---------------------------------------------------------------------------
# FHIR Implementation Guide loader
# ---------------------------------------------------------------------------

def load_ig_context(ig_path_or_url: str) -> IgContext:
    """
    Load a FHIR Implementation Guide and extract context relevant to data generation:
    - ValueSet and CodeSystem resources (for offline code lookup / validator pre-loading)
    - Population guidance from SearchParameter / CapabilityStatement resources
    - Narrative usage notes from ImplementationGuide.definition.page content

    Accepts either:
    - A local directory containing an IG package (e.g. unpacked .tgz from packages.fhir.org)
    - A local .tgz / .tar.gz IG package file
    - A FHIR package registry URL (e.g. https://packages.fhir.org/hl7.fhir.us.core/7.0.0)

    The returned IgContext is attached to the TemplateAnalysis and:
    1. Passed to the Java validator so it can register ValueSets/CodeSystems locally
       (reducing reliance on the remote terminology server for IG-specific codes)
    2. Injected into the ResourceComposerAgent's prompt as population guidance
    """
    import os
    import tarfile
    import tempfile

    ig_path_or_url = ig_path_or_url.strip()

    # Resolve remote package
    if ig_path_or_url.startswith("http"):
        ig_dir = _download_ig_package(ig_path_or_url)
    elif ig_path_or_url.endswith((".tgz", ".tar.gz")):
        ig_dir = _unpack_ig_package(ig_path_or_url)
    else:
        ig_dir = ig_path_or_url  # assume local directory

    return _parse_ig_directory(ig_dir)


def _parse_ig_directory(ig_dir: str) -> IgContext:
    """Walk an unpacked IG package directory and extract relevant resources."""
    import os

    ctx = IgContext()
    ig_manifest: dict = {}

    for fname in os.listdir(ig_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(ig_dir, fname)
        try:
            with open(fpath, encoding="utf-8") as f:
                resource = json.load(f)
        except Exception:
            continue

        rt = resource.get("resourceType", "")

        if rt == "ImplementationGuide":
            ctx.ig_url = resource.get("url", "")
            ctx.ig_name = resource.get("title") or resource.get("name") or ""
            ctx.ig_version = resource.get("version", "")
            # Extract narrative usage notes from page definitions
            pages = resource.get("definition", {}).get("page", {})
            ctx.usage_notes = _extract_ig_pages(pages)

        elif rt == "ValueSet":
            vs_url = resource.get("url")
            if vs_url:
                ctx.value_sets[vs_url] = json.dumps(resource)

        elif rt == "CodeSystem":
            cs_url = resource.get("url")
            if cs_url:
                ctx.code_systems[cs_url] = json.dumps(resource)

        elif rt == "CapabilityStatement":
            # Extract must-support / population notes from CapabilityStatement
            for rest in resource.get("rest", []):
                for res in rest.get("resource", []):
                    doc = res.get("documentation")
                    if doc:
                        ctx.population_notes.append(
                            f"{res.get('type', 'Unknown')}: {doc}"
                        )

        elif rt == "SearchParameter":
            desc = resource.get("description")
            if desc:
                ctx.population_notes.append(
                    f"SearchParameter {resource.get('name', '')}: {desc}"
                )

    return ctx


def _extract_ig_pages(page: dict, depth: int = 0) -> str:
    """Recursively extract page titles from an IG page tree."""
    if not page or depth > 2:
        return ""
    lines = []
    title = page.get("title") or page.get("nameUrl", "")
    if title:
        lines.append(title)
    for sub in page.get("page", []):
        lines.append(_extract_ig_pages(sub, depth + 1))
    return "\n".join(filter(None, lines))


def _download_ig_package(url: str) -> str:
    """Download a FHIR package from packages.fhir.org and unpack it."""
    import tempfile
    import urllib.request

    tmp = tempfile.mkdtemp(prefix="ig_")
    tgz_path = os.path.join(tmp, "package.tgz")
    urllib.request.urlretrieve(url, tgz_path)
    return _unpack_ig_package(tgz_path)


def _unpack_ig_package(tgz_path: str) -> str:
    import tarfile, tempfile, os

    out_dir = tempfile.mkdtemp(prefix="ig_unpacked_")
    with tarfile.open(tgz_path, "r:gz") as tar:
        tar.extractall(out_dir)
    # FHIR packages have a 'package/' subdirectory
    package_dir = os.path.join(out_dir, "package")
    return package_dir if os.path.isdir(package_dir) else out_dir


import os

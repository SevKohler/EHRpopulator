"""
Tool implementations available to the composer agent.

Each function corresponds to a tool the LLM can call during composition.
Tools cover:
  - Terminology: code lookup, ValueSet expansion
  - EHRbase: template info, EHR creation, composition upload
  - FHIR server: resource upload
"""

from __future__ import annotations
import os
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

try:
    from toon_format import encode as _toon_encode
    _TOON_AVAILABLE = True
except ImportError:
    _TOON_AVAILABLE = False


def _vs_to_toon(raw: str) -> str:
    """Extract coding entries from a FHIR ValueSet expansion and encode as TOON.
    Falls back to raw string if TOON is unavailable or parsing fails."""
    if not _TOON_AVAILABLE:
        return raw
    try:
        data = json.loads(raw)
        contains = data.get("expansion", {}).get("contains", [])
        if not contains:
            return raw
        entries = [
            {"code": e.get("code", ""), "display": e.get("display", ""), "system": e.get("system", "")}
            for e in contains
        ]
        return _toon_encode({"matches": entries})
    except Exception:
        return raw


def _params_to_toon(raw: str) -> str:
    """Extract key fields from a FHIR Parameters response and encode as TOON."""
    if not _TOON_AVAILABLE:
        return raw
    try:
        data = json.loads(raw)
        params = {}
        for p in data.get("parameter", []):
            name = p.get("name")
            value = p.get("valueString") or p.get("valueCode") or p.get("valueBoolean")
            if name and value is not None:
                params[name] = value
        return _toon_encode(params) if params else raw
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# Terminology tools
# ---------------------------------------------------------------------------

class TerminologyTools:
    """
    Tools for looking up clinical codes.

    Uses two servers:
    - snomed_url: Snowstorm (SNOMED CT native API + FHIR endpoint)
    - loinc_url:  tx.fhir.org or another FHIR tx server for LOINC, ICD-10, etc.

    If only one URL is configured both route to it.
    """

    def __init__(self, base_url: str = "http://localhost:8085/fhir", bearer_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Accept": "application/fhir+json"}
        if bearer_token:
            self.headers["Authorization"] = f"Bearer {bearer_token}"
        self._cache: dict[tuple, str] = {}

    def expand_value_set(self, value_set_url: str, filter: str = "") -> str:
        """
        Expand a FHIR ValueSet to get valid codes. Use before populating any coded clinical field.
        Returns JSON with code, display, and system for each concept.
        """
        params = {"url": value_set_url, "count": "20"}
        if filter:
            params["filter"] = filter
        return _vs_to_toon(self._get("/ValueSet/$expand", params))

    def lookup_code(self, system: str, code: str) -> str:
        """
        Look up a specific code in a code system to verify it is valid and get its display name.
        """
        return _params_to_toon(self._get("/CodeSystem/$lookup", {"system": system, "code": code}))

    def search_snomed(self, description: str) -> str:
        """
        Search for SNOMED CT concepts matching a clinical description.
        Returns matching concepts with their code and preferred display term.
        Use this to find SNOMED codes for diagnoses, procedures, and findings.
        """
        params = {
            "url": "http://snomed.info/sct?fhir_vs",
            "filter": description,
            "count": "5",
        }
        return _vs_to_toon(self._get("/ValueSet/$expand", params))

    def search_loinc(self, description: str) -> str:
        """
        Search for LOINC codes matching a lab test, vital sign, or observation description.
        Returns matching LOINC codes with display names.
        """
        params = {
            "url": "http://loinc.org/vs",
            "filter": description,
            "count": "5",
        }
        return _vs_to_toon(self._get("/ValueSet/$expand", params))

    def list_code_systems(self) -> str:
        """
        List all CodeSystems available on the terminology server.
        Returns a JSON array of {url, name, version} for each system.
        Call this first if you are unsure which terminologies the server supports,
        then pick the most appropriate system for your coded field.
        """
        raw = self._get("/CodeSystem", {"_summary": "true", "_count": "100"})
        try:
            data = json.loads(raw)
            systems = [
                {
                    "url": res.get("resource", {}).get("url", ""),
                    "name": res.get("resource", {}).get("name") or res.get("resource", {}).get("title", ""),
                    "version": res.get("resource", {}).get("version", ""),
                }
                for res in data.get("entry", [])
            ]
            return _toon_encode({"code_systems": systems}) if _TOON_AVAILABLE else json.dumps({"code_systems": systems})
        except Exception as e:
            return json.dumps({"error": str(e), "raw": raw[:500]})

    def search_terminology(self, description: str, systems: list[str] | None = None) -> str:
        """
        Search one or more terminologies for a clinical concept and return the best matches.

        systems: list of system URIs to search. Supported:
          "http://snomed.info/sct"   — SNOMED CT (diagnoses, findings, procedures, specimens, body sites)
          "http://loinc.org"         — LOINC (lab tests, vital signs, clinical observations)
          "http://www.nlm.nih.gov/research/umls/rxnorm" — RxNorm (medications)
        If omitted, searches SNOMED and LOINC.

        Returns JSON with matches from each system searched.
        """
        if not systems:
            systems = ["http://snomed.info/sct", "http://loinc.org"]

        system_to_vs = {
            "http://snomed.info/sct": "http://snomed.info/sct?fhir_vs",
            "http://loinc.org": "http://loinc.org/vs",
            "http://www.nlm.nih.gov/research/umls/rxnorm": "http://www.nlm.nih.gov/research/umls/rxnorm/vs",
        }

        def _fetch(system: str) -> tuple[str, str]:
            vs_url = system_to_vs.get(system, system)
            return system, self._get("/ValueSet/$expand", {"url": vs_url, "filter": description, "count": "5"})

        results: dict[str, list] = {"matches": []}
        with ThreadPoolExecutor(max_workers=len(systems)) as executor:
            for system, raw in executor.map(lambda s: _fetch(s), systems):
                try:
                    data = json.loads(raw)
                    for entry in data.get("expansion", {}).get("contains", []):
                        results["matches"].append({
                            "code": entry.get("code"),
                            "display": entry.get("display"),
                            "system": entry.get("system", system),
                        })
                except Exception:
                    pass

        return _toon_encode(results) if _TOON_AVAILABLE else json.dumps(results)

    def validate_code(self, system: str, code: str, value_set_url: str) -> str:
        """
        Validate whether a code is valid in a given ValueSet.
        """
        return self._get("/ValueSet/$validate-code", {"url": value_set_url, "system": system, "code": code})

    def snomed_ecl(self, ecl: str, limit: int = 10) -> str:
        """
        Query SNOMED CT using an ECL (Expression Constraint Language) expression.
        Use for precise concept retrieval, e.g.:
          "< 73211009"              → subtypes of Diabetes mellitus
          "^ 816080008"            → concepts in a reference set
          "< 404684003 |finding|"  → all clinical findings
        Returns matching concepts with code and display.
        Snowstorm-native endpoint — more powerful than ValueSet expand for SNOMED.
        """
        cache_key = ("snomed_ecl", ecl, limit)
        if cache_key in self._cache:
            return self._cache[cache_key]
        params = {"ecl": ecl, "limit": str(limit), "active": "true"}
        # Snowstorm native API: /browser/concepts  (not FHIR)
        snowstorm_base = self.base_url.replace("/fhir", "")
        try:
            r = requests.get(
                f"{snowstorm_base}/browser/concepts",
                params=params,
                headers=self.headers,
                timeout=15,
            )
            result = r.text
        except Exception as e:
            result = json.dumps({"error": str(e)})
        self._cache[cache_key] = result
        return result

    def _get(self, path: str, params: dict) -> str:
        cache_key = (path, tuple(sorted(params.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            r = requests.get(self.base_url + path, params=params,
                             headers=self.headers, timeout=15)
            result = r.text
        except Exception as e:
            result = json.dumps({"error": str(e)})
        self._cache[cache_key] = result
        return result

    def as_tool_definitions(self) -> list[dict]:
        """Return Anthropic-format tool definitions for all terminology tools."""
        return [
            {
                "name": "expand_value_set",
                "description": "Expand a FHIR ValueSet to get valid codes.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "value_set_url": {"type": "string"},
                        "filter": {"type": "string"},
                    },
                    "required": ["value_set_url"],
                },
            },
            {
                "name": "lookup_code",
                "description": "Verify a code and get its display name.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "system": {"type": "string"},
                        "code": {"type": "string"},
                    },
                    "required": ["system", "code"],
                },
            },
            {
                "name": "search_snomed",
                "description": "Search SNOMED CT by clinical description.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                    },
                    "required": ["description"],
                },
            },
            {
                "name": "search_loinc",
                "description": "Search LOINC by lab test or observation description.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                    },
                    "required": ["description"],
                },
            },
            {
                "name": "list_code_systems",
                "description": "List all CodeSystems on the terminology server (url, name, version).",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "search_terminology",
                "description": "Search terminologies for a clinical concept. Defaults to SNOMED+LOINC.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "systems": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "System URIs: http://snomed.info/sct, http://loinc.org, http://www.nlm.nih.gov/research/umls/rxnorm",
                        },
                    },
                    "required": ["description"],
                },
            },
            {
                "name": "validate_code",
                "description": "Validate a code against a ValueSet.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "system": {"type": "string"},
                        "code": {"type": "string"},
                        "value_set_url": {"type": "string"},
                    },
                    "required": ["system", "code", "value_set_url"],
                },
            },
            {
                "name": "snomed_ecl",
                "description": "Query SNOMED CT with an ECL expression (e.g. '< 73211009' for diabetes subtypes).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ecl": {"type": "string"},
                        "limit": {"type": "integer", "description": "Max results (default 10)"},
                    },
                    "required": ["ecl"],
                },
            },
        ]

    def get_handler(self, name: str):
        return {
            "list_code_systems": self.list_code_systems,
            "expand_value_set": self.expand_value_set,
            "lookup_code": self.lookup_code,
            "search_snomed": self.search_snomed,
            "search_loinc": self.search_loinc,
            "search_terminology": self.search_terminology,
            "validate_code": self.validate_code,
            "snomed_ecl": self.snomed_ecl,
        }.get(name)


# ---------------------------------------------------------------------------
# EHRbase tools
# ---------------------------------------------------------------------------

class EHRbaseTools:
    """Tools for interacting with an EHRbase openEHR server."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)
        self.headers = {"Accept": "application/json"}

    def get_template_example(self, template_id: str) -> str:
        """
        Retrieve an example composition from EHRbase for a given template ID.
        Returns a skeleton flat JSON composition showing the expected structure.
        Call this first when generating openEHR compositions for a template.
        """
        url = (f"{self.base_url}/rest/openehr/v1/definition/template/adl1.4"
               f"/{template_id}/example")
        try:
            r = requests.get(url, auth=self.auth,
                             headers={**self.headers, "Accept": "application/openehr.wt.flat.schema+json"},
                             timeout=15)
            return r.text
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_template_schema(self, template_id: str) -> str:
        """
        Get the web template (schema) for a given template ID from EHRbase.
        Returns all paths, types, and value sets for the template.
        """
        url = f"{self.base_url}/rest/openehr/v1/definition/template/adl1.4/{template_id}"
        try:
            r = requests.get(url, auth=self.auth, headers=self.headers, timeout=15)
            return r.text
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_web_template_for_opt(self, opt_xml: str) -> str | None:
        """
        Upload an OPT to EHRbase (if not already there) and fetch its web template JSON.
        Returns the web template JSON string, or None if EHRbase is unreachable.
        Used by the pipeline to convert a raw OPT into a richer web template before
        passing it to the template analyzer — avoids LLM-based OPT parsing.
        """
        import xml.etree.ElementTree as ET

        # Extract template_id from OPT XML
        try:
            root = ET.fromstring(opt_xml)
            ns = {"t": "http://schemas.openehr.org/v1"}
            tid_el = root.find(".//t:template_id/t:value", ns) or root.find(".//template_id/value")
            template_id = tid_el.text.strip() if tid_el is not None else None
            if not template_id:
                return None
        except Exception:
            return None

        # Try fetching the web template (may already be uploaded)
        url = f"{self.base_url}/rest/openehr/v1/definition/template/adl1.4/{template_id}"
        try:
            r = requests.get(
                url, auth=self.auth,
                headers={**self.headers, "Accept": "application/openehr.wt+json"},
                timeout=10,
            )
            if r.status_code == 200:
                return r.text

            # Not found — upload the OPT first, then fetch
            if r.status_code == 404:
                upload = requests.post(
                    f"{self.base_url}/rest/openehr/v1/definition/template/adl1.4",
                    auth=self.auth,
                    headers={"Content-Type": "application/xml"},
                    data=opt_xml.encode(),
                    timeout=15,
                )
                if upload.status_code in (200, 201, 409):  # 409 = already exists
                    r2 = requests.get(
                        url, auth=self.auth,
                        headers={**self.headers, "Accept": "application/openehr.wt+json"},
                        timeout=10,
                    )
                    if r2.status_code == 200:
                        return r2.text
        except Exception:
            pass

        return None

    def as_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_template_example",
                "description": "Get an example flat JSON composition from EHRbase for a template. Use this to understand the expected JSON structure.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "template_id": {"type": "string", "description": "The openEHR template ID"},
                    },
                    "required": ["template_id"],
                },
            },
            {
                "name": "get_template_schema",
                "description": "Get the full web template schema for a template from EHRbase, including all paths and value sets.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "template_id": {"type": "string"},
                    },
                    "required": ["template_id"],
                },
            },
        ]

    def get_handler(self, name: str):
        return {
            "get_template_example": self.get_template_example,
            "get_template_schema": self.get_template_schema,
        }.get(name)

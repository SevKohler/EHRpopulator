#!/usr/bin/env python3
"""
Terminology loader — loads SNOMED CT, LOINC and ICD-10 into Snowstorm.

Reads from:
  terminology/seeds/   — loaded once, state tracked in .loaded
  terminology/fhir/    — reloaded on every run

Runs on the host via the agents venv, talking to Snowstorm at localhost:8085.
"""

import csv
import io
import json
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).parent.parent
SEEDS_DIR = REPO_ROOT / "terminology" / "seeds"
FHIR_DIR  = REPO_ROOT / "terminology" / "fhir"
STATE_FILE = SEEDS_DIR / ".loaded"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> set:
    if STATE_FILE.exists():
        return set(l.strip() for l in STATE_FILE.read_text().splitlines() if l.strip())
    return set()


def save_state(loaded: set):
    STATE_FILE.write_text("\n".join(sorted(loaded)) + "\n")


# ---------------------------------------------------------------------------
# Snowstorm helpers
# ---------------------------------------------------------------------------

def code_system_exists(fhir_base: str, url: str) -> bool:
    try:
        r = requests.get(f"{fhir_base}/CodeSystem", params={"url": url}, timeout=10)
        return r.json().get("total", 0) > 0
    except Exception:
        return False


def post_code_system(fhir_base: str, resource: dict) -> tuple[bool, str]:
    """POST a FHIR CodeSystem to Snowstorm. Returns (ok, message)."""
    try:
        r = requests.post(
            f"{fhir_base}/CodeSystem",
            headers={"Content-Type": "application/fhir+json"},
            json=resource,
            timeout=600,
        )
        if r.status_code in (200, 201):
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:400]}"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------

def _is_snomed_rf2(name: str) -> bool:
    low = name.lower()
    return "snomedct" in low or "snomed_ct" in low or low.startswith("sct_")


def _is_loinc(name: str) -> bool:
    return "loinc" in name.lower()


# ---------------------------------------------------------------------------
# SNOMED CT RF2 import (async — Snowstorm polls internally)
# ---------------------------------------------------------------------------

def start_snomed_import(snowstorm_url: str, zip_path: Path) -> str | None:
    """
    Create a Snowstorm import job and upload the RF2 zip.
    Returns the import ID, or None on failure.
    """
    r = requests.post(
        f"{snowstorm_url}/imports",
        json={"type": "SNAPSHOT", "branchPath": "MAIN", "createCodeSystemVersion": True},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create import job: {r.status_code} {r.text[:300]}")

    location = r.headers.get("Location", "")
    import_id = location.rstrip("/").split("/")[-1] if location else None
    if not import_id:
        try:
            import_id = r.json().get("id")
        except Exception:
            pass
    if not import_id:
        raise RuntimeError(f"Could not get import ID. Location={location} body={r.text[:200]}")

    with open(zip_path, "rb") as f:
        r = requests.post(
            f"{snowstorm_url}/imports/{import_id}/archive",
            files={"file": (zip_path.name, f, "application/zip")},
            timeout=7200,
        )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to upload RF2: {r.status_code} {r.text[:300]}")

    return import_id


def poll_snomed_import(snowstorm_url: str, import_id: str) -> str:
    """Return current Snowstorm import status."""
    data = requests.get(f"{snowstorm_url}/imports/{import_id}", timeout=10).json()
    return data.get("status", "UNKNOWN")


# ---------------------------------------------------------------------------
# LOINC CSV → FHIR CodeSystem
# ---------------------------------------------------------------------------

def loinc_zip_to_fhir(zip_path: Path) -> dict:
    """
    Read LoincTable/Loinc.csv from the LOINC distribution zip and build
    a FHIR R4 CodeSystem with all active concepts.
    """
    with zipfile.ZipFile(zip_path) as zf:
        # Find Loinc.csv inside the zip (may be in a subfolder)
        csv_name = next(
            (n for n in zf.namelist() if n.lower().endswith("loinc.csv") and "loincuniversallabordersobservations" not in n.lower()),
            None,
        )
        if csv_name is None:
            raise FileNotFoundError(f"Could not find Loinc.csv in {zip_path.name}")

        with zf.open(csv_name) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig")
            reader = csv.DictReader(text)
            concepts = []
            for row in reader:
                code = row.get("LOINC_NUM", "").strip()
                status = row.get("STATUS", "").strip().upper()
                if not code or status == "DEPRECATED":
                    continue
                display = (
                    row.get("LONG_COMMON_NAME") or
                    row.get("SHORTNAME") or
                    row.get("COMPONENT", "")
                ).strip()
                if not display:
                    continue
                concepts.append({"code": code, "display": display})

    # Try to extract version from filename e.g. Loinc_2.82.zip
    version = ""
    for part in zip_path.stem.split("_"):
        if part.replace(".", "").isdigit():
            version = part
            break

    return {
        "resourceType": "CodeSystem",
        "url": "http://loinc.org",
        "version": version,
        "name": "LOINC",
        "title": "Logical Observation Identifiers Names and Codes (LOINC)",
        "status": "active",
        "content": "complete",
        "concept": concepts,
    }


# ---------------------------------------------------------------------------
# ICD-10 ClaML → FHIR CodeSystem
# ---------------------------------------------------------------------------

def claml_to_fhir(xml_path: Path) -> dict:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    tag = root.tag
    ns = tag[:tag.index("}") + 1] if tag.startswith("{") else ""

    def t(name):
        return f"{ns}{name}"

    title_el = root.find(t("Title"))
    title   = title_el.text.strip() if title_el is not None else "ICD"
    version = title_el.get("version", "") if title_el is not None else ""

    title_lower = title.lower()
    if "icd-10" in title_lower or "icd10" in title_lower:
        system_url = "http://hl7.org/fhir/sid/icd-10"
    elif "icd-11" in title_lower or "icd11" in title_lower:
        system_url = "http://hl7.org/fhir/sid/icd-11"
    else:
        system_url = f"urn:claml:{title.replace(' ', '_')}"

    concepts = []
    for cls in root.iter(t("Class")):
        code = cls.get("code", "").strip()
        if not code:
            continue
        display = ""
        for rubric in cls.findall(t("Rubric")):
            if rubric.get("kind") in ("preferred", "preferredLong"):
                label = rubric.find(t("Label"))
                if label is not None:
                    display = "".join(label.itertext()).strip()
                    break
        if not display:
            continue
        concept: dict = {"code": code, "display": display}
        parent = cls.find(t("SuperClass"))
        if parent is not None and parent.get("code"):
            concept["property"] = [{"code": "parent", "valueCode": parent.get("code")}]
        concepts.append(concept)

    return {
        "resourceType": "CodeSystem",
        "url": system_url,
        "version": version,
        "name": title.replace(" ", "_"),
        "title": title,
        "status": "active",
        "content": "complete",
        "property": [{"code": "parent", "type": "code", "description": "Parent code"}],
        "concept": concepts,
    }


def claml_zip_to_fhir(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        xml_name = next(
            (n for n in zf.namelist() if n.lower().endswith(".xml")),
            None,
        )
        if xml_name is None:
            raise FileNotFoundError(f"No XML found in {zip_path.name}")
        with zf.open(xml_name) as raw:
            tmp = Path(f"/tmp/_claml_{zip_path.stem}.xml")
            tmp.write_bytes(raw.read())
    try:
        return claml_to_fhir(tmp)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Custom FHIR JSON (CodeSystem / ValueSet)
# ---------------------------------------------------------------------------

def load_fhir_json(fhir_base: str, json_path: Path, always_reload: bool = False) -> tuple[bool, str]:
    try:
        resource = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"parse error: {e}"

    rt = resource.get("resourceType", "")
    if rt not in ("CodeSystem", "ValueSet"):
        return True, f"skipped (resourceType={rt})"

    url = resource.get("url", json_path.name)
    if not always_reload and rt == "CodeSystem" and code_system_exists(fhir_base, url):
        return True, "already loaded"

    return post_code_system(fhir_base, resource)


# ---------------------------------------------------------------------------
# High-level load functions (called by setup.py)
# ---------------------------------------------------------------------------

class LoadResult:
    def __init__(self):
        self.ok = False
        self.message = ""
        self.snomed_import_id: str | None = None  # for async polling


def load_seeds(snowstorm_url: str, fhir_base: str, log=print) -> dict:
    """
    Process all files in terminology/seeds/.
    Returns a dict: filename -> LoadResult (for snomed: result.snomed_import_id is set)
    """
    if not SEEDS_DIR.exists():
        return {}

    loaded = load_state()
    results = {}

    for path in sorted(SEEDS_DIR.iterdir()):
        if path.name.startswith(".") or path.suffix == ".md":
            continue

        name = path.name
        result = LoadResult()
        results[name] = result

        if name in loaded:
            result.ok = True
            result.message = "already loaded"
            continue

        try:
            if path.suffix == ".zip":
                if _is_snomed_rf2(name):
                    if code_system_exists(fhir_base, "http://snomed.info/sct"):
                        result.ok = True
                        result.message = "already loaded"
                        loaded.add(name); save_state(loaded)
                    else:
                        import_id = start_snomed_import(snowstorm_url, path)
                        result.snomed_import_id = import_id
                        result.message = f"importing (job {import_id})"

                elif _is_loinc(name):
                    if code_system_exists(fhir_base, "http://loinc.org"):
                        result.ok = True
                        result.message = "already loaded"
                        loaded.add(name); save_state(loaded)
                    else:
                        log(f"  Converting LOINC CSV from {name}...")
                        cs = loinc_zip_to_fhir(path)
                        log(f"  Uploading {len(cs['concept'])} LOINC concepts to Snowstorm...")
                        ok, msg = post_code_system(fhir_base, cs)
                        result.ok = ok
                        result.message = msg
                        if ok:
                            loaded.add(name); save_state(loaded)

                else:
                    # Assume ICD-10 ClaML zip
                    if code_system_exists(fhir_base, "http://hl7.org/fhir/sid/icd-10"):
                        result.ok = True
                        result.message = "already loaded"
                        loaded.add(name); save_state(loaded)
                    else:
                        log(f"  Converting ICD-10 ClaML from {name}...")
                        cs = claml_zip_to_fhir(path)
                        log(f"  Uploading {len(cs['concept'])} ICD-10 concepts to Snowstorm...")
                        ok, msg = post_code_system(fhir_base, cs)
                        result.ok = ok
                        result.message = msg
                        if ok:
                            loaded.add(name); save_state(loaded)

            elif path.suffix == ".xml":
                if code_system_exists(fhir_base, "http://hl7.org/fhir/sid/icd-10"):
                    result.ok = True
                    result.message = "already loaded"
                    loaded.add(name); save_state(loaded)
                else:
                    log(f"  Converting ClaML {name}...")
                    cs = claml_to_fhir(path)
                    log(f"  Uploading {len(cs['concept'])} concepts...")
                    ok, msg = post_code_system(fhir_base, cs)
                    result.ok = ok
                    result.message = msg
                    if ok:
                        loaded.add(name); save_state(loaded)

            elif path.suffix == ".json":
                ok, msg = load_fhir_json(fhir_base, path, always_reload=False)
                result.ok = ok
                result.message = msg
                if ok and msg != "already loaded":
                    loaded.add(name); save_state(loaded)

            else:
                result.ok = True
                result.message = f"skipped (unknown extension)"

        except Exception as e:
            result.ok = False
            result.message = str(e)

    return results


def load_fhir_dir(fhir_base: str, log=print):
    """Reload all JSONs from terminology/fhir/ (always)."""
    if not FHIR_DIR.exists():
        return
    for path in sorted(FHIR_DIR.iterdir()):
        if path.suffix != ".json" or path.name.startswith("."):
            continue
        ok, msg = load_fhir_json(fhir_base, path, always_reload=True)
        log(f"  {path.name}: {msg}" if ok else f"  ERROR {path.name}: {msg}")


def complete_snomed(snowstorm_url: str, import_id: str, loaded: set, name: str) -> tuple[str, bool]:
    """
    Poll a running SNOMED import. Returns (status_string, done).
    Caller should save state when done+ok.
    """
    status = poll_snomed_import(snowstorm_url, import_id)
    done = status in ("COMPLETED", "FAILED", "CANCELLED")
    if status == "COMPLETED":
        loaded.add(name)
        save_state(loaded)
    return status, done

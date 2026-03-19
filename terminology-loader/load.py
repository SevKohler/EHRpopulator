#!/usr/bin/env python3
"""
Terminology loader — runs at container startup.

Two folders with different reload behaviour:

  /terminology/seeds/    SNOMED CT RF2 zips, LOINC JSON, ICD-10 JSON
                         Loaded ONCE. State tracked in seeds/.loaded.
                         Never reloaded unless .loaded is deleted.

  /terminology/fhir/     Your own FHIR CodeSystems and ValueSets.
                         Reloaded on EVERY restart — safe for small files
                         you iterate on frequently.

Supported file types:
  *.zip   → SNOMED CT RF2 import (seeds only)
  *.json  → FHIR CodeSystem or ValueSet POST
"""

import json
import os
import subprocess
import sys
import time
import requests

import xml.etree.ElementTree as ET

SNOWSTORM_URL = os.environ.get("SNOWSTORM_URL", "http://snowstorm:8080")
FHIR_BASE = f"{SNOWSTORM_URL}/fhir"
SEEDS_DIR = "/terminology/seeds"
FHIR_DIR = "/terminology/fhir"
STATE_FILE = os.path.join(SEEDS_DIR, ".loaded")


# ---------------------------------------------------------------------------
# Snowstorm health
# ---------------------------------------------------------------------------

def wait_for_snowstorm(timeout=300):
    print("Waiting for Snowstorm...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{SNOWSTORM_URL}/branches", timeout=5).status_code == 200:
                print("Snowstorm ready.\n", flush=True)
                return
        except Exception:
            pass
        time.sleep(5)
    print("ERROR: Snowstorm did not become ready.", flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# State helpers (seeds only)
# ---------------------------------------------------------------------------

def load_state() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(l.strip() for l in f if l.strip())
    return set()


def save_state(loaded: set):
    with open(STATE_FILE, "w") as f:
        for name in sorted(loaded):
            f.write(name + "\n")


# ---------------------------------------------------------------------------
# Snowstorm checks
# ---------------------------------------------------------------------------

def code_system_exists(url: str) -> bool:
    try:
        r = requests.get(f"{FHIR_BASE}/CodeSystem",
                         params={"url": url}, timeout=10)
        return r.json().get("total", 0) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_snomed(zip_path: str, loaded: set):
    name = os.path.basename(zip_path)
    if name in loaded:
        print(f"  [seeds] SKIP {name} (already loaded)", flush=True)
        return
    if code_system_exists("http://snomed.info/sct"):
        print(f"  [seeds] SKIP {name} (SNOMED already in Snowstorm)", flush=True)
        loaded.add(name)
        save_state(loaded)
        return

    print(f"  [seeds] Importing SNOMED CT from {name} — this takes 20-60 min...", flush=True)

    r = requests.post(f"{SNOWSTORM_URL}/imports",
                      json={"type": "SNAPSHOT", "branchPath": "MAIN", "createCodeSystemVersion": True},
                      timeout=30)
    if r.status_code not in (200, 201):
        print(f"  ERROR creating import job: {r.status_code} {r.text[:200]}", flush=True)
        return

    # Snowstorm returns the import ID in the Location header (e.g. /imports/{id})
    location = r.headers.get("Location", "")
    import_id = location.rstrip("/").split("/")[-1] if location else None
    if not import_id:
        # Fall back to response body
        try:
            import_id = r.json().get("id")
        except Exception:
            pass
    if not import_id:
        print(f"  ERROR: could not get import ID. Location: {location}, body: {r.text[:200]}", flush=True)
        return
    print(f"  Import job ID: {import_id}", flush=True)

    with open(zip_path, "rb") as f:
        r = requests.post(f"{SNOWSTORM_URL}/imports/{import_id}/archive",
                          files={"file": (name, f, "application/zip")},
                          timeout=7200)
    if r.status_code not in (200, 201):
        print(f"  ERROR uploading RF2: {r.status_code} {r.text[:200]}", flush=True)
        return

    while True:
        time.sleep(30)
        data = requests.get(f"{SNOWSTORM_URL}/imports/{import_id}", timeout=10).json()
        status = data.get("status", "UNKNOWN")
        print(f"  Status: {status}", flush=True)
        if status == "COMPLETED":
            print(f"  SNOMED CT loaded.\n", flush=True)
            loaded.add(name)
            save_state(loaded)
            return
        elif status in ("FAILED", "CANCELLED"):
            print(f"  SNOMED import failed. Details: {json.dumps(data)}\n", flush=True)
            return


def claml_to_fhir(xml_path: str) -> dict:
    """
    Convert a ClaML XML file (ICD-10, ICD-11, or similar) to a FHIR R4 CodeSystem.

    ClaML is the XML format distributed by WHO/DIMDI/BfArM for ICD releases.
    Extracts: code, preferred display label, parent code (for hierarchy).
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Strip namespace if present
    tag = root.tag
    ns = ""
    if tag.startswith("{"):
        ns = tag[:tag.index("}") + 1]

    def t(name):
        return f"{ns}{name}"

    # Title / version from metadata
    title_el = root.find(t("Title"))
    title = title_el.text.strip() if title_el is not None else "ICD"
    version = title_el.get("version", "") if title_el is not None else ""

    # Detect ICD system URL from title text
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

        # Preferred display label
        display = ""
        for rubric in cls.findall(t("Rubric")):
            if rubric.get("kind") in ("preferred", "preferredLong"):
                label = rubric.find(t("Label"))
                if label is not None and label.text:
                    display = "".join(label.itertext()).strip()
                    break

        if not display:
            continue

        concept: dict = {"code": code, "display": display}

        # Parent code
        parent = cls.find(t("SuperClass"))
        if parent is not None and parent.get("code"):
            concept["property"] = [{"code": "parent", "valueCode": parent.get("code")}]

        concepts.append(concept)

    print(f"  Converted {len(concepts)} concepts from ClaML", flush=True)

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


def load_claml(xml_path: str, loaded: set):
    name = os.path.basename(xml_path)
    if name in loaded:
        print(f"  [seeds] SKIP {name} (already loaded)", flush=True)
        return

    print(f"  [seeds] Converting ClaML: {name}...", flush=True)
    try:
        resource = claml_to_fhir(xml_path)
    except Exception as e:
        print(f"  ERROR parsing ClaML {name}: {e}", flush=True)
        return

    url = resource.get("url", "")
    if code_system_exists(url):
        print(f"  [seeds] SKIP: {url} already in Snowstorm", flush=True)
        loaded.add(name)
        save_state(loaded)
        return

    print(f"  Loading CodeSystem {url}...", flush=True)
    r = requests.post(f"{FHIR_BASE}/CodeSystem",
                      headers={"Content-Type": "application/fhir+json"},
                      json=resource,
                      timeout=300)
    if r.status_code in (200, 201):
        print(f"  OK: {name} loaded as {url}", flush=True)
        loaded.add(name)
        save_state(loaded)
    else:
        print(f"  ERROR {r.status_code}: {r.text[:300]}", flush=True)


def _hapi_cli_upload(zip_path: str, system_url: str, label: str, loaded: set):
    """Upload a terminology zip to Snowstorm using hapi-fhir-cli."""
    name = os.path.basename(zip_path)
    if name in loaded:
        print(f"  [seeds] SKIP {name} (already loaded)", flush=True)
        return
    if code_system_exists(system_url):
        print(f"  [seeds] SKIP {name} ({label} already in Snowstorm)", flush=True)
        loaded.add(name)
        save_state(loaded)
        return

    print(f"  [seeds] Uploading {label} via hapi-fhir-cli — this may take a few minutes...", flush=True)
    cmd = [
        "hapi-fhir-cli", "upload-terminology",
        "-d", zip_path,
        "-v", "r4",
        "-t", FHIR_BASE,
        "-u", system_url,
    ]
    result = subprocess.run(cmd)  # streams stdout/stderr directly to docker logs
    if result.returncode == 0:
        print(f"  OK: {label} loaded.", flush=True)
        loaded.add(name)
        save_state(loaded)
    else:
        print(f"  ERROR: {label} upload failed (exit {result.returncode})", flush=True)


def load_loinc_zip(zip_path: str, loaded: set):
    _hapi_cli_upload(zip_path, "http://loinc.org", "LOINC", loaded)


def load_claml_zip(zip_path: str, loaded: set):
    _hapi_cli_upload(zip_path, "http://hl7.org/fhir/sid/icd-10", "ICD-10", loaded)


def _is_snomed_rf2(filename: str) -> bool:
    low = filename.lower()
    return "snomedct" in low or "snomed_ct" in low or low.startswith("sct_")


def _is_loinc(filename: str) -> bool:
    return "loinc" in filename.lower()


def load_fhir_json(json_path: str, *, always_reload: bool = False) -> bool:
    name = os.path.basename(json_path)
    try:
        with open(json_path, encoding="utf-8") as f:
            resource = json.load(f)
    except Exception as e:
        print(f"  ERROR reading {name}: {e}", flush=True)
        return False

    rt = resource.get("resourceType", "")
    if rt not in ("CodeSystem", "ValueSet"):
        print(f"  SKIP {name}: resourceType is '{rt}', expected CodeSystem or ValueSet", flush=True)
        return True

    url = resource.get("url", name)

    if not always_reload and rt == "CodeSystem" and code_system_exists(url):
        print(f"  [seeds] SKIP {name}: {url} already in Snowstorm", flush=True)
        return True

    print(f"  Loading {rt}: {url}...", flush=True)
    r = requests.post(f"{FHIR_BASE}/{rt}",
                      headers={"Content-Type": "application/fhir+json"},
                      json=resource,
                      timeout=300)
    if r.status_code in (200, 201):
        print(f"  OK: {name}", flush=True)
        return True
    else:
        print(f"  ERROR {r.status_code}: {r.text[:300]}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    wait_for_snowstorm()

    # --- Seeds (load once) ---
    if os.path.isdir(SEEDS_DIR):
        seed_files = [f for f in sorted(os.listdir(SEEDS_DIR))
                      if not f.startswith(".") and not f.endswith(".md")]

        if seed_files:
            print("=== Seeds (load once) ===", flush=True)
            loaded = load_state()

            for filename in seed_files:
                path = os.path.join(SEEDS_DIR, filename)
                if filename.endswith(".zip"):
                    if _is_snomed_rf2(filename):
                        load_snomed(path, loaded)
                    elif _is_loinc(filename):
                        load_loinc_zip(path, loaded)
                    else:
                        load_claml_zip(path, loaded)
                elif filename.endswith(".json"):
                    if filename in loaded:
                        print(f"  [seeds] SKIP {filename} (already loaded)", flush=True)
                    else:
                        if load_fhir_json(path, always_reload=False):
                            loaded.add(filename)
                            save_state(loaded)
                elif filename.endswith(".xml"):
                    load_claml(path, loaded)
                else:
                    print(f"  SKIP {filename}: unknown extension", flush=True)
        else:
            print("Seeds folder is empty — nothing to load.", flush=True)
    else:
        print("No seeds/ folder found, skipping.", flush=True)

    # --- FHIR (always reload) ---
    if os.path.isdir(FHIR_DIR):
        fhir_files = [f for f in sorted(os.listdir(FHIR_DIR))
                      if not f.startswith(".") and not f.endswith(".md")
                      and f.endswith(".json")]

        if fhir_files:
            print("\n=== FHIR CodeSystems / ValueSets (always reload) ===", flush=True)
            for filename in fhir_files:
                load_fhir_json(os.path.join(FHIR_DIR, filename), always_reload=True)
        else:
            print("\nFHIR folder is empty — nothing to reload.", flush=True)
    else:
        print("No fhir/ folder found, skipping.", flush=True)

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()

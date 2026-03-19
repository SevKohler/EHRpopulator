#!/usr/bin/env python3
"""
Terminology loader — runs once at container startup.

Scans /terminology for files and loads them into Snowstorm:
  SnomedCT_*.zip        → SNOMED CT RF2 import
  Loinc_*.json          → LOINC FHIR CodeSystem
  icd10*.json / icd-10*.json → ICD-10 FHIR CodeSystem
  *.json (other)        → Generic FHIR CodeSystem or ValueSet POST

Already-loaded code systems are detected via Snowstorm's FHIR API
and skipped — safe to restart without re-loading.

State is also persisted to /terminology/.loaded so restarts are instant.
"""

import json
import os
import sys
import time
import glob
import requests

SNOWSTORM = os.environ.get("SNOWSTORM_URL", "http://snowstorm:8080")
FHIR_BASE = f"{SNOWSTORM}/fhir"
TERMINOLOGY_DIR = "/terminology"
STATE_FILE = os.path.join(TERMINOLOGY_DIR, ".loaded")


def wait_for_snowstorm(timeout=300):
    print("Waiting for Snowstorm...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{SNOWSTORM}/branches", timeout=5)
            if r.status_code == 200:
                print("Snowstorm is ready.", flush=True)
                return
        except Exception:
            pass
        time.sleep(5)
    print("ERROR: Snowstorm did not become ready in time.", flush=True)
    sys.exit(1)


def load_state() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_state(loaded: set):
    with open(STATE_FILE, "w") as f:
        for entry in sorted(loaded):
            f.write(entry + "\n")


def already_in_snowstorm(system_url: str) -> bool:
    """Check if a CodeSystem is already loaded in Snowstorm."""
    try:
        r = requests.get(f"{FHIR_BASE}/CodeSystem",
                         params={"url": system_url}, timeout=10)
        data = r.json()
        return data.get("total", 0) > 0
    except Exception:
        return False


def load_snomed(zip_path: str, loaded: set) -> bool:
    name = os.path.basename(zip_path)
    if name in loaded:
        print(f"  SNOMED: {name} already loaded (state file), skipping.", flush=True)
        return True
    if already_in_snowstorm("http://snomed.info/sct"):
        print(f"  SNOMED: already in Snowstorm, skipping {name}.", flush=True)
        loaded.add(name)
        return True

    print(f"  SNOMED: importing {name} (this takes 20-60 min)...", flush=True)
    # Create import job
    r = requests.post(f"{SNOWSTORM}/imports",
                      json={"branchPath": "MAIN", "createCodeSystemVersion": True},
                      timeout=30)
    if r.status_code not in (200, 201):
        print(f"  ERROR creating SNOMED import job: {r.status_code} {r.text}", flush=True)
        return False

    import_id = r.json()["id"]
    print(f"  Import job: {import_id}", flush=True)

    # Upload the RF2 zip
    with open(zip_path, "rb") as f:
        r = requests.post(f"{SNOWSTORM}/imports/{import_id}/archive",
                          files={"file": (name, f, "application/zip")},
                          timeout=7200)  # 2h timeout for upload + import
    if r.status_code not in (200, 201):
        print(f"  ERROR uploading RF2: {r.status_code} {r.text[:200]}", flush=True)
        return False

    # Poll until complete
    print(f"  Polling import status...", flush=True)
    while True:
        time.sleep(30)
        r = requests.get(f"{SNOWSTORM}/imports/{import_id}", timeout=10)
        status = r.json().get("status", "UNKNOWN")
        print(f"  Status: {status}", flush=True)
        if status == "COMPLETED":
            print(f"  SNOMED loaded successfully.", flush=True)
            loaded.add(name)
            return True
        elif status in ("FAILED", "CANCELLED"):
            print(f"  SNOMED import failed: {r.text[:200]}", flush=True)
            return False


def load_fhir_resource(json_path: str, loaded: set) -> bool:
    name = os.path.basename(json_path)
    if name in loaded:
        print(f"  {name}: already loaded (state file), skipping.", flush=True)
        return True

    with open(json_path, encoding="utf-8") as f:
        resource = json.load(f)

    rt = resource.get("resourceType", "")
    url = resource.get("url", "")

    if rt not in ("CodeSystem", "ValueSet"):
        print(f"  {name}: not a CodeSystem or ValueSet (resourceType={rt}), skipping.", flush=True)
        return True

    if url and already_in_snowstorm(url):
        print(f"  {name}: {url} already in Snowstorm, skipping.", flush=True)
        loaded.add(name)
        return True

    print(f"  Loading {rt}: {url or name}...", flush=True)
    r = requests.post(f"{FHIR_BASE}/{rt}",
                      headers={"Content-Type": "application/fhir+json"},
                      json=resource,
                      timeout=300)
    if r.status_code in (200, 201):
        print(f"  Loaded {name} successfully.", flush=True)
        loaded.add(name)
        return True
    else:
        print(f"  ERROR loading {name}: {r.status_code} {r.text[:300]}", flush=True)
        return False


def main():
    wait_for_snowstorm()
    loaded = load_state()

    files = sorted(os.listdir(TERMINOLOGY_DIR))
    if not any(f for f in files if not f.startswith(".") and not f.endswith(".md")):
        print("No terminology files found in /terminology — nothing to load.", flush=True)
        print("Drop your files there and restart this service.", flush=True)
        return

    for filename in files:
        if filename.startswith(".") or filename.endswith(".md"):
            continue

        filepath = os.path.join(TERMINOLOGY_DIR, filename)

        if filename.endswith(".zip"):
            # SNOMED CT RF2
            load_snomed(filepath, loaded)
            save_state(loaded)

        elif filename.endswith(".json"):
            load_fhir_resource(filepath, loaded)
            save_state(loaded)

        else:
            print(f"  Skipping unknown file type: {filename}", flush=True)

    print("Terminology loading complete.", flush=True)


if __name__ == "__main__":
    main()

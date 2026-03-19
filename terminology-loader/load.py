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
import sys
import time
import requests

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
                      json={"branchPath": "MAIN", "createCodeSystemVersion": True},
                      timeout=30)
    if r.status_code not in (200, 201):
        print(f"  ERROR creating import job: {r.status_code} {r.text[:200]}", flush=True)
        return

    import_id = r.json()["id"]
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
        status = requests.get(f"{SNOWSTORM_URL}/imports/{import_id}",
                              timeout=10).json().get("status", "UNKNOWN")
        print(f"  Status: {status}", flush=True)
        if status == "COMPLETED":
            print(f"  SNOMED CT loaded.\n", flush=True)
            loaded.add(name)
            save_state(loaded)
            return
        elif status in ("FAILED", "CANCELLED"):
            print(f"  SNOMED import failed.\n", flush=True)
            return


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
                    load_snomed(path, loaded)
                elif filename.endswith(".json"):
                    if filename in loaded:
                        print(f"  [seeds] SKIP {filename} (already loaded)", flush=True)
                    else:
                        if load_fhir_json(path, always_reload=False):
                            loaded.add(filename)
                            save_state(loaded)
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

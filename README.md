# EHR Populator

Generate synthetic test data for **openEHR** and **FHIR** from your own templates using AI agents.

Describe the patients you want, drop your templates in a folder → get back validated compositions and resources, ready to upload.

---

## How it works

```
templates/openehr/   ← your OPT files
templates/fhir/      ← your StructureDefinition files
        ↓  (OPTs converted to web template via EHRbase SDK)
  Template structure  ←  paths, types, descriptions, annotations, value sets
        ↓
  Patient journey     ←  LLM generates narrative + pre-maps values onto template paths
        ↓
  Composition / Resource  ←  LLM validates codes via Snowstorm, serializes to openEHR or FHIR JSON
        ↓       ↑ fix errors and retry (up to N times)
  Validation  ←  HAPI FHIR or EHRbase SDK (offline)
        ↓
  Upload (optional)  →  EHRbase / FHIR server
```

One journey covers **all templates in the folder** — a cancer patient journey spans diagnosis, labs, vitals, and medication OPTs simultaneously.

---

## Setup

### 1. Python setup (one time)

```bash
make setup
```

### 2. Add terminology files

Drop your files into `terminology/seeds/` — loaded once by `make setup`, never committed.

| File | What |
|---|---|
| `SnomedCT_InternationalRF2_*.zip` | SNOMED CT — needs [license](https://www.snomed.org/snomed-ct/get-snomed) |
| `Loinc_*.zip` | LOINC — download from [loinc.org](https://loinc.org/downloads/) |
| `icd10*.xml` or `icd10*.zip` | ICD-10 ClaML (international or national, e.g. ICD-10-GM) |

Your own FHIR `CodeSystem` / `ValueSet` JSON files go in `terminology/fhir/` — loaded on setup.

### 3. Set your API key

```bash
# Edit agents/.env:
ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Add your templates

```
templates/openehr/   ← .opt or .xml files
templates/fhir/      ← StructureDefinition .json files
```

### 5. Start the stack

```bash
docker compose up -d
```

Starts Snowstorm, the validator, EHRbase, and the FHIR server. Start only what you need:

```bash
# Terminology + validation only (no EHRbase/FHIR server)
docker compose up -d validator snowstorm snowstorm-es

# If you already have EHRbase/Snowstorm running elsewhere
docker compose up -d validator fhir
```

### 6. Load terminology

```bash
make setup
```

This installs Python dependencies and imports your terminology files from `terminology/seeds/` into Snowstorm. Run once — progress is shown in the terminal. Skip files that are already loaded.

> **First run:** SNOMED CT import takes 20–60 min, LOINC and ICD-10 a few minutes.

---

## Usage

### Interactive (recommended)

```bash
make popu
```

You will be asked:
1. Standard — openEHR or FHIR R4 (all templates in the folder are used automatically)
2. Output format — openEHR flat / canonical (FHIR is always FHIR R4)
3. How many patients
4. Scenario description — e.g. *"rare metabolic disease patients with Gaucher disease, include diagnostic workup and enzyme replacement therapy"*
5. Upload to server?

### Non-interactive

```bash
# openEHR, 5 patients
make generate ARGS="templates/openehr/vital_signs.opt --scenario 'elderly COPD patients' --count 5"

# Multiple OPTs — one journey spans all
make generate ARGS="templates/openehr/diagnosis.opt templates/openehr/labs.opt --scenario 'cancer patients' --count 3"

# FHIR
make generate ARGS="templates/fhir/MyProfile.json --format FHIR_R4 --scenario 'diabetic patients' --count 5"

# Upload after generation
make generate ARGS="templates/openehr/my_template.opt --scenario 'ICU patients' --count 5 --upload"

# Inspect a template's structure
make analyze ARGS="templates/openehr/my_template.opt"
```

Output lands in `output/`. Each record gets a `.json` and a `.meta.json` with validation status.
Sample journeys (up to 10) are saved to `output/journeys/` for inspection — wiped on each run.

### Options

| Option | Default | |
|---|---|---|
| `--scenario` / `-s` | `general adult patients` | Describe the patients to generate |
| `--format` / `-f` | `OPENEHR_FLAT` | `FHIR_R4`, `OPENEHR_FLAT`, `OPENEHR_CANONICAL` |
| `--count` / `-n` | `1` | Number of patients |
| `--upload` | off | Upload to EHRbase / FHIR server after generation |
| `--ig` | — | FHIR IG package (local dir, `.tgz`, or packages.fhir.org URL) |

---

## Make commands

| Command | What |
|---|---|
| `make setup` | Install Python deps and load terminology into Snowstorm (run once) |
| `make popu` | Interactive generation |
| `make generate ARGS="..."` | Non-interactive generation |
| `make analyze ARGS="..."` | Inspect a template's structure |

Docker compose is managed directly:

| Command | What |
|---|---|
| `docker compose up -d` | Start the full stack |
| `docker compose up -d validator snowstorm snowstorm-es` | Start terminology + validation only |
| `docker compose down` | Stop everything |
| `docker compose logs -f <service>` | Watch logs for a service |

---

## Using existing servers

If you already have Snowstorm, EHRbase, or a FHIR server running, you don't need docker compose for those. Edit `agents/config.yaml`:

```yaml
terminology:
  base_url: "https://your-snowstorm.example.com/fhir"

ehrbase:
  enabled: true
  base_url: "https://your-ehrbase.example.com/ehrbase"
  username: "your-user"

fhir_server:
  enabled: true
  base_url: "https://your-fhir.example.com/fhir"
```

The validator is always required (it's part of this repo) — start it with `make start-light` or `sudo docker compose up -d validator`.

---

## Supported LLM providers

Set in `agents/config.yaml`:

```yaml
llm:
  provider: anthropic   # anthropic | openai | azure
  model: claude-opus-4-6
```

API key via `agents/.env`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `AZURE_OPENAI_KEY`.

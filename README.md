# EHR Populator

Generate synthetic test data for **openEHR** and **FHIR** from your own templates using AI agents.

Describe the patients you want, provide OPT or StructureDefinition files → get back validated compositions and resources, ready to upload.

---

## How it works

```
OPT(s) / StructureDefinition(s)
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

One journey covers **all provided OPTs** — a cancer patient journey spans diagnosis, labs, vitals, medication OPTs simultaneously.

```
┌─────────────────────────────────────────────────────────────────┐
│                     Python Agent Pipeline                        │
│                                                                  │
│  ┌──────────────────────┐                                        │
│  │  TemplateAnalyzer    │  OPT → web template (EHRbase SDK)     │
│  │                      │  StructureDefinition → parsed directly │
│  │                      │  paths, types, descriptions, annotations│
│  └──────────┬───────────┘                                        │
│             │ TemplateAnalysis × N templates (no LLM used)      │
│             ▼                                                     │
│  ┌──────────────────────┐                                        │
│  │  JourneyGenerator    │  Scenario → clinical narrative +       │
│  │                      │  field_values mapped to template paths  │
│  └──────────┬───────────┘                                        │
│             │ PatientJourney (shared across all templates)        │
│             ▼                                                     │
│  ┌──────────────────────┐   tool calls    ┌──────────────────┐  │
│  │  ResourceComposer    │◄───────────────►│    Snowstorm     │  │
│  │  (once per template) │                 │  ValueSet expand │  │
│  │  openEHR / FHIR JSON │                 │  SNOMED ECL      │  │
│  └──────────┬───────────┘                 │  LOINC search    │  │
│             │ raw JSON                    └──────────────────┘  │
│             ▼                                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Validation Loop                        │   │
│  │   POST /validate ──► Java Validator ──► errors?          │   │
│  │         ▲                                    │            │   │
│  │         └──── feed errors back to composer ◄─┘            │   │
│  │                     (up to max_retries)                   │   │
│  └──────────┬─────────────────────────────────────────────── ┘  │
│             ▼                                                     │
│       Upload (optional) → EHRbase / FHIR server                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│              Java Validator Service  :8181                       │
│                                                                  │
│  POST /validate                                                  │
│  ┌──────────────┐    ┌─────────────────────────────────────┐   │
│  │  FHIR_R4     │───►│  HAPI FHIR 7.x                      │   │
│  │              │    │  base spec + inline StructureDefinition│  │
│  │              │    │  code validation via Snowstorm        │   │
│  └──────────────┘    └─────────────────────────────────────┘   │
│  ┌──────────────┐    ┌─────────────────────────────────────┐   │
│  │  OPENEHR_*   │───►│  EHRbase SDK (offline)              │   │
│  │              │    │  flat JSON → Composition via         │   │
│  │              │    │  FlatJasonProvider + WebTemplate     │   │
│  │              │    │  → LocatableValidator                │   │
│  └──────────────┘    └─────────────────────────────────────┘   │
│                                                                  │
│  POST /webtemplate                                               │
│  ┌──────────────┐    ┌─────────────────────────────────────┐   │
│  │  OPT XML     │───►│  EHRbase SDK OPTParser              │   │
│  │              │    │  → web template JSON                 │   │
│  └──────────────┘    └─────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Setup

### 1. Add terminology files

Drop your files into the right folder — content is gitignored, never committed.

**`terminology/seeds/`** — loaded once, skipped on restart:
- `SnomedCT_InternationalRF2_*.zip` — needs [SNOMED license](https://www.snomed.org/snomed-ct/get-snomed)
- `Loinc_*.json` — FHIR R4 CodeSystem from [loinc.org](https://loinc.org/downloads/)
- `icd10*.xml` — ICD-10 ClaML XML from WHO/DIMDI (auto-converted to FHIR CodeSystem)

**`terminology/fhir/`** — reloaded on every restart:
- Your own FHIR `CodeSystem` or `ValueSet` JSON files

### 2. Set your API key

```bash
# Edit agents/.env and set your key:
ANTHROPIC_API_KEY=sk-ant-...
```

Get your key at [console.anthropic.com](https://console.anthropic.com/).

### 3. Python setup (one time)

```bash
make setup
```

### 4. Start everything

```bash
sudo sysctl -w vm.max_map_count=262144   # required for Elasticsearch (once per boot)
sudo docker compose up -d
```

This starts EHRbase, HAPI FHIR, Snowstorm, the Java validator, and the terminology loader.

> **First run takes time** — SNOMED CT import takes 20–60 min, LOINC a few minutes.
> Watch progress: `sudo docker compose logs -f terminology-loader`

To make the `vm.max_map_count` setting permanent:
```bash
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
```

---

## Usage

### Interactive (recommended)

```bash
make run
```

You will be asked:
1. Template file(s) — one or more OPTs or StructureDefinitions
2. Output format — openEHR flat / canonical / FHIR R4
3. How many patients
4. Scenario description — e.g. *"rare metabolic disease patients with Gaucher disease, include diagnostic workup and enzyme replacement therapy"*
5. Upload to server?

### Non-interactive

```bash
# Single OPT
make generate ARGS="my_template.opt --scenario 'elderly COPD patients' --format OPENEHR_FLAT --count 5"

# Multiple OPTs — one journey spans all templates
make generate ARGS="diagnosis.opt labs.opt vitals.opt --scenario 'cancer patients on chemotherapy' --count 3"

# FHIR
make generate ARGS="MyProfile.json --format FHIR_R4 --scenario 'diabetic patients with CKD stage 3' --count 5"

# Upload to servers after generation
make generate ARGS="my_template.opt --scenario 'post-op ICU patients' --count 5 --upload"

# Inspect template structure
make analyze ARGS="my_template.opt"
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

## Reload your FHIR terminology files

```bash
sudo docker compose restart terminology-loader
sudo docker compose logs -f terminology-loader
```

---

## Supported LLM providers

Set in `agents/config.yaml`:

```yaml
llm:
  provider: anthropic   # anthropic | openai | azure
  model: claude-opus-4-6
```

API key via `agents/.env`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `AZURE_OPENAI_KEY`.

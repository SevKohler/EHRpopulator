# EHR Populator

Generate synthetic test data for **openEHR** and **FHIR** from your own templates using AI agents.

Feed in an OPT or StructureDefinition → get back validated compositions and resources, ready to upload.

---

## How it works

```
OPT / StructureDefinition
        ↓  (parsed to web template via EHRbase SDK)
  Template structure  ←  paths, types, descriptions, annotations, value sets
        ↓
  Patient journey     ←  LLM generates realistic clinical scenario
        ↓
  Composition / Resource  ←  LLM serializes to openEHR or FHIR JSON
        ↓       ↑ fix errors and retry (up to N times)
  Validation  ←  HAPI FHIR or EHRbase SDK
        ↓
  Upload (optional)  →  EHRbase / FHIR server
```

Terminology lookups (SNOMED CT, LOINC, ICD-10) are done via **Snowstorm** during generation.

---

## Setup

### 1. Add terminology files

Drop your files into the right folder — content is gitignored, never committed.

**`terminology/seeds/`** — loaded once, skipped on restart:
- `SnomedCT_InternationalRF2_*.zip` — needs [SNOMED license](https://www.snomed.org/snomed-ct/get-snomed)
- `Loinc_*.json` — FHIR R4 CodeSystem from [loinc.org](https://loinc.org/downloads/)
- `icd10*.json` — ICD-10 FHIR CodeSystem

**`terminology/fhir/`** — reloaded on every restart:
- Your own FHIR `CodeSystem` or `ValueSet` JSON files

### 2. Configure

```bash
cp agents/config.yaml agents/config.local.yaml
# Set your LLM API key — or export ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Python setup (one time)

```bash
make setup
```

### 4. Start everything

```bash
docker compose up -d
```

This starts EHRbase, HAPI FHIR, Snowstorm, the Java validator, and the terminology loader.

> **First run takes time** — SNOMED CT import takes 20-60 min, LOINC a few minutes.
> Watch progress: `docker compose logs -f terminology-loader`

---

## Usage

```bash
# Generate openEHR flat compositions from an OPT
make generate ARGS="my_template.opt --format OPENEHR_FLAT --count 5"

# Generate FHIR resources from a StructureDefinition
make generate ARGS="MyProfile.json --format FHIR_R4 --count 5"

# Generate and upload directly to EHRbase / FHIR server
make generate ARGS="my_template.opt --format OPENEHR_FLAT --count 5 --upload"

# Inspect what the parser sees in your template before generating
make analyze ARGS="my_template.opt"
```

Output lands in `output/`. Each record gets a `.json` and a `.meta.json` with validation status.

### Options

| Option | Default | |
|---|---|---|
| `--format` | `FHIR_R4` | `FHIR_R4`, `OPENEHR_FLAT`, `OPENEHR_CANONICAL` |
| `--count` / `-n` | `1` | Number of records |
| `--demographic-context` / `-d` | `general adult patients` | Patient population hint |
| `--upload` | off | Upload to EHRbase / FHIR server after generation |
| `--ig` | — | FHIR IG package (local dir, `.tgz`, or packages.fhir.org URL) |

---

## Reload your FHIR terminology files

```bash
docker compose restart terminology-loader
docker compose logs -f terminology-loader
```

---

## Supported LLM providers

Set in `agents/config.local.yaml`:

```yaml
llm:
  provider: anthropic   # anthropic | openai | azure | ollama
  model: claude-opus-4-5
```

API key via env var: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `AZURE_OPENAI_KEY`.

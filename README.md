# EHR Populator

Generate realistic synthetic test data for **openEHR** and **FHIR R4** systems using a multi-agent LLM pipeline. Feed in your Operational Templates (OPT) or FHIR StructureDefinitions, describe a patient population, and get back validated compositions and resources — ready to upload to EHRbase or a FHIR server.

---

## Architecture

The system is split into two layers: a **Python agent pipeline** for LLM orchestration, and a **Java validation service** for standards-compliant validation. Validation must be Java because the authoritative tooling (HAPI FHIR, EHRbase SDK) lives there.

```
┌─────────────────────────────────────────────────────────────────┐
│                     Python Agent Pipeline                        │
│                                                                  │
│  ┌──────────────────────┐                                        │
│  │  TemplateAnalyzer    │  Parses OPT XML or FHIR               │
│  │  Agent               │  StructureDefinition → structured      │
│  │                      │  element list (paths, types, VS URLs)  │
│  └──────────┬───────────┘                                        │
│             │ TemplateAnalysis                                    │
│             ▼                                                     │
│  ┌──────────────────────┐                                        │
│  │  JourneyGenerator    │  Generates realistic patient           │
│  │  Agent               │  journeys: demographics, clinical      │
│  │                      │  events, specific values & ICD codes   │
│  └──────────┬───────────┘                                        │
│             │ PatientJourney                                      │
│             ▼                                                     │
│  ┌──────────────────────┐   tool calls    ┌──────────────────┐  │
│  │  ResourceComposer    │ ◄────────────── │  Terminology     │  │
│  │  Agent               │ ───────────────►│  Tools           │  │
│  │                      │                 │  (Snowstorm)     │  │
│  │  Converts journey to │                 └──────────────────┘  │
│  │  openEHR/FHIR JSON   │                                        │
│  └──────────┬───────────┘                                        │
│             │ raw JSON                                            │
│             ▼                                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Validation Loop                        │   │
│  │                                                           │   │
│  │   POST /validate ──► Java Validator ──► errors?          │   │
│  │         ▲                                    │            │   │
│  │         │         feed errors back           │            │   │
│  │         └──────── to ResourceComposer ◄──────┘            │   │
│  │                   (up to max_retries)                     │   │
│  └──────────┬─────────────────────────────────────────────── ┘  │
│             │ valid JSON                                          │
│             ▼                                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │   Upload (optional)                                       │   │
│  │   openEHR → EHRbase REST API                             │   │
│  │   FHIR    → FHIR Server REST API                         │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                Java Validator Service  :8181                     │
│                                                                  │
│  POST /validate                                                  │
│  ┌─────────────────┐      ┌──────────────────────────────────┐  │
│  │  FHIR_R4        │─────►│  HAPI FHIR 7.x                  │  │
│  │                 │      │  • Base R4 spec                  │  │
│  │                 │      │  • Inline StructureDefinition    │  │
│  │                 │      │  • Snowstorm for code validation  │  │
│  └─────────────────┘      └──────────────────────────────────┘  │
│  ┌─────────────────┐      ┌──────────────────────────────────┐  │
│  │  OPENEHR_FLAT   │─────►│  EHRbase SDK (offline)           │  │
│  │  OPENEHR_       │      │  + EHRbase server (optional)     │  │
│  │  CANONICAL      │      │  • OPT-based structural check    │  │
│  │                 │      │  • Server-side full validation   │  │
│  └─────────────────┘      └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Template parsing (no LLM involved)

Before any agent runs, the template is parsed into a structured element list programmatically:

| Input | How it's parsed |
|---|---|
| **OPT XML** (`.opt` / `.xml`) | Posted to the Java validator's `/webtemplate` endpoint, which uses the EHRbase SDK (`opt-normalizer` + `web-template`) to convert it to web template JSON. Then parsed the same way as a web template. No running EHRbase server needed. |
| **openEHR web template JSON** | Parsed by `template_parser.py` — walks the node tree, extracts flat paths, RM types, cardinality, inline code lists, constraints, `localizedDescriptions`, and `annotations` from every element. |
| **FHIR StructureDefinition JSON** | Parsed by `template_parser.py` — extracts FHIRPath, types, ValueSet bindings, `element.definition`, `element.short`, and `element.comment` as description and annotations. |

The resulting `TemplateAnalysis` contains every element's path, type, description, annotations, allowed codes, and numeric ranges — everything the downstream agents need without any LLM guesswork.

### Agents

| Agent | Input | Output | Tools |
|---|---|---|---|
| **TemplateAnalyzerAgent** | Web template JSON or FHIR StructureDefinition | `TemplateAnalysis` — deterministically parsed, no LLM | none |
| **JourneyGeneratorAgent** | `TemplateAnalysis` + demographic context | `PatientJourney` — realistic events with specific clinical values | none |
| **ResourceComposerAgent** | `PatientJourney` + `TemplateAnalysis` + validation errors from previous attempt | Raw openEHR/FHIR JSON | Snowstorm terminology tools |

### Terminology Tools (Snowstorm)

The `ResourceComposerAgent` can call these tools before populating coded fields:

| Tool | Purpose |
|---|---|
| `expand_value_set` | Expand a FHIR ValueSet URL → list of valid codes |
| `lookup_code` | Verify a specific code exists in a CodeSystem |
| `search_snomed` | Full-text search for SNOMED CT concepts |
| `search_loinc` | Full-text search for LOINC codes |
| `validate_code` | Check a code is valid in a given ValueSet |
| `snomed_ecl` | ECL query for precise SNOMED subtree retrieval (e.g. `< 73211009`) |

### Validation Loop

The pipeline calls the Java validator after each composition attempt. If validation fails, the structured error list (with path locations) is injected back into the next `ResourceComposerAgent` prompt. This repeats up to `max_retries` times. On each retry only the specific failing paths are asked to be fixed — the rest of the resource is left unchanged.

---

## Project Structure

```
EHRpopulator/
├── agents/                     Python agent pipeline
│   ├── main.py                 CLI entry point (typer)
│   ├── pipeline.py             Orchestrator — wires agents + validation loop
│   ├── agents.py               Agent classes + system prompts
│   ├── tools.py                Snowstorm terminology tools + EHRbase tools
│   ├── llm_client.py           LLM provider factory (Anthropic / OpenAI / Azure / Ollama)
│   ├── models.py               Shared Pydantic models
│   ├── config.yaml             Configuration template
│   └── requirements.txt
│
├── validator/                  Java validation service (Spring Boot)
│   ├── pom.xml
│   ├── Dockerfile
│   └── src/main/java/org/ehrpopulator/validator/
│       ├── ValidatorApplication.java
│       ├── api/
│       │   ├── ValidationController.java   POST /validate, GET /health
│       │   ├── ValidationRequest.java
│       │   └── ValidationResponse.java
│       ├── fhir/
│       │   └── FhirValidationService.java  HAPI FHIR 7.x
│       └── openehr/
│           ├── OpenEhrValidationService.java  EHRbase SDK + server
│           └── OptRegistry.java               In-memory OPT store
│
├── docker-compose.yaml         EHRbase + HAPI FHIR + Snowstorm + validator
├── templates/                  Put your OPT and StructureDefinition files here
└── output/                     Generated files written here (gitignored)
```

---

## Prerequisites

- **Java 21** and **Maven 3.9+** (for the validator)
- **Python 3.11+** (for the agents)
- **Docker + Docker Compose** (for EHRbase, FHIR, Snowstorm)
- An LLM API key — Anthropic Claude (recommended), OpenAI, or Azure OpenAI
- A SNOMED CT RF2 release file (for Snowstorm) — requires an [IHTSDO license](https://www.snomed.org/snomed-ct/get-snomed). For testing you can point at the public IHTSDO browser instead (see config).

---

## Setup

### 1. Start the infrastructure

```bash
docker compose up -d ehrbase fhir snowstorm
```

Wait for Snowstorm to be ready (can take a minute):

```bash
docker compose logs -f snowstorm
# Look for: "Started SnowstormApplication"
```

**Load SNOMED CT into Snowstorm** (required before use):

```bash
# Create an import job
curl -X POST "http://localhost:8085/imports" \
  -H "Content-Type: application/json" \
  -d '{"branchPath":"MAIN","createCodeSystemVersion":true}'

# The response contains an importId. Upload your RF2 zip:
curl -X POST "http://localhost:8085/imports/{importId}/archive" \
  -F "file=@/path/to/SnomedCT_Release.zip"
```

For testing without a license, use the public browser in your config instead:
```yaml
terminology:
  base_url: "https://browser.ihtsdotools.org/snowstorm/snomed-ct/fhir"
```

### 2. Build and start the Java validator

```bash
cd validator
mvn package -DskipTests
docker compose up -d validator
```

Verify it's up:

```bash
curl http://localhost:8181/health
# → ok
```

### 3. Install Python dependencies

```bash
cd agents
pip install -r requirements.txt
```

### 4. Configure

```bash
cp agents/config.yaml agents/config.local.yaml
```

Edit `config.local.yaml`:

```yaml
llm:
  provider: anthropic
  model: claude-opus-4-5
  # api_key here or set ANTHROPIC_API_KEY env var

ehrbase:
  enabled: true               # set true to enable upload
  base_url: "http://localhost:8080/ehrbase"
  username: "ehrbase-user"
  password: "SuperSecretPassword"

fhir_server:
  enabled: false              # set true to enable upload
  base_url: "http://localhost:8090/fhir"

terminology:
  base_url: "http://localhost:8085/fhir"   # Snowstorm FHIR endpoint

validator:
  base_url: "http://localhost:8181"
```

Alternatively, use environment variables — the config supports `${ENV_VAR}` placeholders:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export EHRBASE_PASSWORD=SuperSecretPassword
```

---

## Usage

All commands are run from the `agents/` directory.

### Generate test data

```bash
python main.py generate <template-file> [options]
```

| Option | Default | Description |
|---|---|---|
| `--format` / `-f` | `FHIR_R4` | `FHIR_R4`, `OPENEHR_FLAT`, or `OPENEHR_CANONICAL` |
| `--count` / `-n` | `1` | Number of patient records to generate |
| `--demographic-context` / `-d` | `general adult patients` | Describe the patient population |
| `--upload` | off | Upload valid records to EHRbase / FHIR server after generation |
| `--ig` | — | FHIR IG: local directory, `.tgz` package, or packages.fhir.org URL |
| `--config` / `-c` | `config.local.yaml` | Path to config file |
| `--output` / `-o` | `../output` | Output directory |

**Examples:**

```bash
# Generate 5 openEHR flat compositions from an OPT file
python main.py generate templates/vital_signs_v1.opt \
  --format OPENEHR_FLAT \
  --count 5

# Generate 10 FHIR R4 resources from a StructureDefinition
python main.py generate templates/MyPatientProfile.json \
  --format FHIR_R4 \
  --count 10 \
  --demographic-context "ICU patients with sepsis, 40-80 years old"

# Generate and upload directly to EHRbase
python main.py generate templates/sepsis_screening.opt \
  --format OPENEHR_FLAT \
  --count 20 \
  --upload

# Generate FHIR data and upload to FHIR server
python main.py generate templates/observation_profile.json \
  --format FHIR_R4 \
  --count 5 \
  --upload

# Use a different config (e.g. pointing at a staging environment)
python main.py generate templates/my.opt \
  --config /etc/ehrpopulator/staging.yaml
```

### Analyze a template

Inspect what the agent sees before generating — useful for debugging or understanding your template's element structure:

```bash
python main.py analyze templates/vital_signs_v1.opt
python main.py analyze templates/MyPatientProfile.json
```

Output shows all required and optional elements with their paths, data types, and ValueSet URLs.

---

## Supported LLM Providers

Configure via the `llm` block in your config file:

**Anthropic Claude** (recommended — best instruction following for structured clinical data):
```yaml
llm:
  provider: anthropic
  model: claude-opus-4-5
  # api_key: set via ANTHROPIC_API_KEY env var
```

**OpenAI / Codex:**
```yaml
llm:
  provider: openai
  model: gpt-4o
  # api_key: set via OPENAI_API_KEY env var
```

**Azure OpenAI:**
```yaml
llm:
  provider: azure
  model: your-deployment-name
  base_url: "https://your-resource.openai.azure.com"
  # api_key: set via AZURE_OPENAI_KEY env var
```

**Ollama (local models):**
```yaml
llm:
  provider: ollama
  model: llama3.1:70b
  base_url: "http://localhost:11434"
```

---

## Output

Each generated record produces two files in the output directory:

```
output/
  fhir_r4_patient-1_attempt1_0.json        # The generated resource
  fhir_r4_patient-1_attempt1_0.meta.json   # Metadata: valid, attempts, issue count
```

The metadata file:
```json
{
  "patient_id": "patient-1",
  "template_id": "MyPatientProfile",
  "format": "FHIR_R4",
  "valid": true,
  "generation_attempt": 2,
  "issue_count": 0
}
```

If a record fails validation after all retries, it is still saved with `"valid": false` so you can inspect what the model produced.

---

## Validate an Existing Resource

You can call the Java validator directly without the agent pipeline — useful for validating existing files:

```bash
curl -s -X POST http://localhost:8181/validate \
  -H "Content-Type: application/json" \
  -d '{
    "format": "FHIR_R4",
    "content": '"$(cat output/my_resource.json | jq -Rs .)"'
  }' | jq .
```

With an inline StructureDefinition for profile validation:

```bash
curl -s -X POST http://localhost:8181/validate \
  -H "Content-Type: application/json" \
  -d '{
    "format": "FHIR_R4",
    "content": '"$(cat output/resource.json | jq -Rs .)"',
    "structure_definition_json": '"$(cat templates/MyProfile.json | jq -Rs .)"'
  }' | jq .
```

With an OPT for openEHR validation:

```bash
curl -s -X POST http://localhost:8181/validate \
  -H "Content-Type: application/json" \
  -d '{
    "format": "OPENEHR_FLAT",
    "content": '"$(cat output/composition.json | jq -Rs .)"',
    "opt_xml": '"$(cat templates/vital_signs_v1.opt | jq -Rs .)"'
  }' | jq .
```

Response:
```json
{
  "valid": false,
  "issue_count": 2,
  "issues": [
    {
      "severity": "ERROR",
      "location": "Patient.identifier[0].system",
      "message": "Value is not a valid URI"
    },
    {
      "severity": "WARNING",
      "location": "Patient.name[0]",
      "message": "Name should have a family name"
    }
  ]
}
```

---

## Tuning Generation Quality

**Increase retries** if validation frequently fails on complex templates:
```yaml
pipeline:
  max_retries: 8
```

**Be specific with demographic context** — the journey generator uses this to shape clinical presentations:
```bash
python main.py generate my.opt -n 10 \
  -d "elderly patients (70-90), female majority, multi-morbidity: T2DM, hypertension, CKD"
```

**Use a more capable model** for templates with many coded fields or complex cardinality:
```yaml
llm:
  model: claude-opus-4-5   # more capable but slower/more expensive
```

**Enable EHRbase server-side validation** for the strictest openEHR checks (in `validator/src/main/resources/application.yaml`):
```yaml
validator:
  ehrbase:
    enabled: true
    base-url: "http://localhost:8080/ehrbase"
```

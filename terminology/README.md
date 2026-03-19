# Terminology Files

Drop your terminology files here. They are loaded automatically into Snowstorm
when you run `docker compose up`.

Files are only loaded once — restarts are instant (state tracked in `.loaded`).

## What to put here

| File | Source | Notes |
|---|---|---|
| `SnomedCT_InternationalRF2_*.zip` | [snomed.org](https://www.snomed.org/snomed-ct/get-snomed) | Requires SNOMED license |
| `Loinc_*.json` | [loinc.org](https://loinc.org/downloads/) → FHIR R4 package | Free account required |
| `icd10*.json` | NLM or HL7 | FHIR CodeSystem format |
| `MyValueSet.json` | Your own | Any FHIR CodeSystem or ValueSet |
| `MyCodeSystem.json` | Your own | Any FHIR CodeSystem or ValueSet |

## Supported formats

- **`.zip`** — treated as SNOMED CT RF2 release
- **`.json`** — must be a FHIR R4 `CodeSystem` or `ValueSet` resource

## Notes

- All files in this folder **except this README** are gitignored
- The `.loaded` state file is created automatically — do not delete it unless you want to re-load everything
- To force a re-load of a specific file, remove its name from `.loaded` and restart the loader:
  `docker compose restart terminology-loader`

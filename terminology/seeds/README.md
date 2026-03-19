# Seeds — loaded once

Drop large base terminology files here. They are loaded into Snowstorm **once**
and never again (state tracked in `.loaded`).

To force a re-load, delete `.loaded` and restart:
```bash
rm terminology/seeds/.loaded
docker compose restart terminology-loader
```

## What to put here

| File | What it is |
|---|---|
| `SnomedCT_InternationalRF2_*.zip` | SNOMED CT RF2 release — needs snomed.org license |
| `Loinc_*.json` | LOINC FHIR R4 CodeSystem — download from loinc.org |
| `icd10*.json` | ICD-10 FHIR CodeSystem |

All files in this folder (except this README and `.loaded`) are gitignored.

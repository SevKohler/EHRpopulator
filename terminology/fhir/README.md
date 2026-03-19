# FHIR CodeSystems & ValueSets — reloaded every time

Drop your own FHIR CodeSystem and ValueSet JSON files here.
They are uploaded to Snowstorm on **every** `terminology-loader` restart,
so changes are always picked up.

```bash
# After adding or editing a file:
docker compose restart terminology-loader
docker compose logs -f terminology-loader
```

## What to put here

Any FHIR R4 JSON file with `"resourceType": "CodeSystem"` or `"resourceType": "ValueSet"`.

Examples:
- Project-specific value sets
- National extensions or profiles
- Custom code systems

All files in this folder (except this README) are gitignored.

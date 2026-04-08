.PHONY: setup popu generate analyze load-loinc

# Install Python deps and load terminology into Snowstorm
setup:
	cd agents && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	cd agents && .venv/bin/python setup.py  # loads SNOMED/LOINC/ICD-10, then watches progress

# Start interactive generation
popu:
	rm -rf output
	cd agents && .venv/bin/python main.py run $(ARGS)

# Non-interactive generation
# Example: make generate ARGS="templates/openehr/vitals.opt --scenario 'COPD patients' --count 5"
generate:
	rm -rf output
	cd agents && .venv/bin/python main.py generate $(ARGS)

# Inspect a template's structure
analyze:
	cd agents && .venv/bin/python main.py analyze $(ARGS)

# Load LOINC into Snowstorm manually (run this if make setup reports LOINC failed).
# Runs hapi-fhir-cli inside the Snowstorm container so it shares /tmp with Snowstorm.
# Usage: make load-loinc LOINC_ZIP=terminology/seeds/Loinc_2.82.zip
LOINC_ZIP ?= $(firstword $(wildcard terminology/seeds/Loinc_*.zip))
load-loinc:
	@echo "Copying $(LOINC_ZIP) into Snowstorm container..."
	docker cp $(LOINC_ZIP) ehrpopulator-snowstorm-1:/tmp/loinc.zip
	@echo "Copying hapi-fhir-cli into Snowstorm container..."
	docker cp terminology/hapi-fhir-cli/hapi-fhir-cli.jar ehrpopulator-snowstorm-1:/tmp/hapi-fhir-cli.jar
	@echo "Running upload-terminology inside container (this may take several minutes)..."
	docker exec ehrpopulator-snowstorm-1 java -jar /tmp/hapi-fhir-cli.jar \
		upload-terminology -d /tmp/loinc.zip -v r4 \
		-t http://localhost:8080/fhir -u http://loinc.org

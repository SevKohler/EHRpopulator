.PHONY: setup popu generate analyze

# Install Python deps and load terminology into Snowstorm
setup:
	cd agents && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	cd agents && .venv/bin/python setup.py  # loads SNOMED/LOINC/ICD-10, then watches progress

# Start interactive generation
popu:
	cd agents && .venv/bin/python main.py run $(ARGS)

# Non-interactive generation
# Example: make generate ARGS="templates/openehr/vitals.opt --scenario 'COPD patients' --count 5"
generate:
	cd agents && .venv/bin/python main.py generate $(ARGS)

# Inspect a template's structure
analyze:
	cd agents && .venv/bin/python main.py analyze $(ARGS)

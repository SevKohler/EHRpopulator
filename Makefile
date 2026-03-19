.PHONY: setup run generate analyze shell

# Install Python dependencies into agents/.venv
setup:
	cd agents && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	@echo "Done. Run: make run"

# Interactive mode — asks scenario, count, format interactively
run:
	cd agents && .venv/bin/python main.py run $(ARGS)

# Non-interactive generate — pass all args via ARGS=
# Example: make generate ARGS="vitals.opt labs.opt --scenario 'diabetic patients' --count 5"
generate:
	cd agents && .venv/bin/python main.py generate $(ARGS)

# Analyze a template structure
# Example: make analyze ARGS="templates/vital_signs.opt"
analyze:
	cd agents && .venv/bin/python main.py analyze $(ARGS)

# Drop into a shell with the venv active
shell:
	cd agents && bash --init-file <(echo "source .venv/bin/activate")

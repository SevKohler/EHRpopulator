.PHONY: setup generate analyze shell

# Install Python dependencies into agents/.venv
setup:
	cd agents && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	@echo "Done. Run: make generate ARGS=\"my_template.opt --format OPENEHR_FLAT --count 5\""

# Generate data — pass args via ARGS=
# Example: make generate ARGS="templates/vital_signs.opt --format OPENEHR_FLAT --count 5"
generate:
	cd agents && .venv/bin/python main.py generate $(ARGS)

# Analyze a template
# Example: make analyze ARGS="templates/vital_signs.opt"
analyze:
	cd agents && .venv/bin/python main.py analyze $(ARGS)

# Drop into a shell with the venv active
shell:
	cd agents && bash --init-file <(echo "source .venv/bin/activate")

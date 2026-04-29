# Nomos Oracle — operator entrypoints.
#
# All targets run inside the project venv at .venv/. If you bootstrap
# differently, override PYTHON= on the command line:
#
#     make test PYTHON=/path/to/python
#
# Most targets cd into oracle/ because pyproject.toml's rootdir lives
# there.

# Absolute paths so targets that `cd oracle` still resolve the venv at repo root.
REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PYTHON ?= $(REPO_ROOT)/.venv/bin/python
PYTEST ?= $(REPO_ROOT)/.venv/bin/pytest
STREAMLIT ?= $(REPO_ROOT)/.venv/bin/streamlit

# Default DB path — override on the command line:
#     make verify-chain DB=/var/lib/oracle/prod.db
DB ?= /tmp/oracle.db

# Default fixture for the offline daily run.
FIXTURE ?= oracle/tests/fixtures/minimal_estr.yaml

# Default contract id for the offline daily run.
CONTRACT_ID ?= IRS-DEMO-0001


.PHONY: help test test-integration lint run-daily dashboard verify-chain


help:
	@echo "Nomos Oracle — make targets:"
	@echo "  test              run unit + property tests (no network)"
	@echo "  test-integration  also hit real ECB SDW endpoints"
	@echo "  lint              static checks (ruff if available, else pyflakes)"
	@echo "  run-daily         one offline cycle via FakeCollector → \$$DB"
	@echo "  dashboard         launch Streamlit dashboard against \$$DB"
	@echo "  verify-chain      verify_integrity(\$$DB); exit 0 ok / 1 corrupt"
	@echo ""
	@echo "Override DB=, FIXTURE=, CONTRACT_ID= on the command line."


test:
	cd oracle && $(PYTEST)


test-integration:
	cd oracle && $(PYTEST) --run-integration


lint:
	@if [ -x $(REPO_ROOT)/.venv/bin/ruff ]; then \
		$(REPO_ROOT)/.venv/bin/ruff check oracle; \
	else \
		$(PYTHON) -m pyflakes oracle; \
	fi


run-daily:
	$(PYTHON) -m oracle.scheduler.daily_run \
		--fixture $(FIXTURE) \
		--contract-id $(CONTRACT_ID) \
		--db-path $(DB)


dashboard:
	PYTHONPATH=$(REPO_ROOT) $(STREAMLIT) run oracle/dashboard/app.py -- --db-path $(DB)


verify-chain:
	$(PYTHON) -m oracle.scheduler.verify_chain --db-path $(DB)

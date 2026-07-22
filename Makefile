# USDA CDL -> Icechunk Zarr pipeline
#
# Store target: set STORE to a local path / s3://bucket/prefix (default
# ./cdl_store_local), or set ACCOUNT to target the Source Coop product instead.
#
#   make init-store
#   make ingest RESOLUTION=30m YEARS=2025
#   make backfill-30m ACCOUNT=my-account
#   make validate RESOLUTION=30m YEARS=2025

STORE      ?= ./cdl_store_local
ACCOUNT    ?=
RESOLUTION ?= 30m
YEARS      ?=
WORKERS    ?=
SAMPLES    ?= 12
DATA_DIR   ?= data
CREDS_FILE ?=
CLEANUP    ?=
OVERWRITE  ?=

CLI = uv run usda-cdl

ifeq ($(ACCOUNT),)
  STORE_FLAGS = --store $(STORE)
else
  STORE_FLAGS = --source-coop-account $(ACCOUNT)
endif
STORE_FLAGS += $(CREDS_FLAG)
YEARS_FLAG     = $(if $(YEARS),--years $(YEARS))
CLEANUP_FLAG   = $(if $(CLEANUP),--cleanup)
OVERWRITE_FLAG = $(if $(OVERWRITE),--overwrite)
WORKERS_FLAG   = $(if $(WORKERS),--workers $(WORKERS))
# only pass a creds file when explicitly requested; otherwise the source-coop
# CLI's cached login is used (a stale creds.json must not shadow a fresh login)
CREDS_FLAG     = $(if $(CREDS_FILE),--credentials-file $(CREDS_FILE))

.DEFAULT_GOAL := help

.PHONY: help setup test lint init-store ingest backfill-30m backfill-10m validate info publish clean-local-store clean-local-data

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  \033[1m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Variables: STORE=$(STORE)  ACCOUNT=$(ACCOUNT)  RESOLUTION=$(RESOLUTION)"
	@echo "             YEARS=$(YEARS)  WORKERS=$(WORKERS)  DATA_DIR=$(DATA_DIR)"

setup: ## Install dependencies (uv sync)
	uv sync

test: ## Run the test suite
	uv run pytest -q

lint: ## Fix lint issues and reformat code with ruff
	uv run ruff check --fix src tests
	uv run ruff format src tests

init-store: ## Create the icechunk repo and empty 30m/10m group structure
	$(CLI) init-store $(STORE_FLAGS)

ingest: ## Ingest year(s): make ingest RESOLUTION=30m YEARS=2025 (CLEANUP=1 to delete sources)
	$(CLI) ingest $(STORE_FLAGS) --resolution $(RESOLUTION) $(YEARS_FLAG) \
		--data-dir $(DATA_DIR) $(WORKERS_FLAG) $(CLEANUP_FLAG) $(OVERWRITE_FLAG)

backfill-30m: ## Ingest all 30m years (2008-2025); CLEANUP=1 to delete sources as it goes
	$(CLI) ingest $(STORE_FLAGS) --resolution 30m --data-dir $(DATA_DIR) $(WORKERS_FLAG) $(CLEANUP_FLAG) $(OVERWRITE_FLAG)

backfill-10m: ## Ingest all 10m years (2024-2025); CLEANUP=1 to delete the ~10 GB sources as it goes
	$(CLI) ingest $(STORE_FLAGS) --resolution 10m --data-dir $(DATA_DIR) $(WORKERS_FLAG) $(CLEANUP_FLAG) $(OVERWRITE_FLAG)

validate: ## Verify store contents against source rasters
	$(CLI) validate $(STORE_FLAGS) --resolution $(RESOLUTION) $(YEARS_FLAG) \
		--data-dir $(DATA_DIR) --samples $(SAMPLES)

info: ## Show store structure, tags, and recent snapshots
	$(CLI) info $(STORE_FLAGS)

publish: ## Sync the locally built store to Source Coop; OVERWRITE=1 wipes the remote store first
	$(CLI) publish --store $(STORE) --source-coop-account $(or $(ACCOUNT),chill) \
		$(CREDS_FLAG) $(WORKERS_FLAG) $(OVERWRITE_FLAG)

clean-local-store: ## Remove the local icechunk store ($(STORE))
	rm -rf $(STORE)

clean-local-data: ## Remove downloaded/extracted source files ($(DATA_DIR))
	rm -rf $(DATA_DIR)

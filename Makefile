# kokoro-tone - local image build, no published port.
#
#   make up        build + start both containers
#   make test      uv sync + pytest
#   make narrate   FILE=script.txt [OUT=narration.mp3]

SHELL   := /bin/bash
COMPOSE := docker compose
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"$$/\1/p' src/kokoro_tone/__init__.py)
OUT     ?= narration.mp3

# The engine tag lives in exactly one file; compose and the build inherit it.
export KOKORO_VERSION := $(shell cat engine.version)

.DEFAULT_GOAL := help
.PHONY: help install lint fmt fmt-check type-check check verify security-check pin-check test \
        version engine config build up down restart ps logs \
        narrate analyse clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies from uv.lock
	uv sync --frozen

lint: ## Lint with ruff
	uv run ruff check .

fmt: ## Format with ruff
	uv run ruff format .

fmt-check: ## Verify formatting
	uv run ruff format --check .

verify: check test ## Everything CI runs

type-check: ## Type-check with mypy
	uv run mypy

check: fmt-check lint type-check security-check pin-check ## Format, lint, type, security and pin checks

security-check: ## Refuse dynamic-evaluation sinks in src/
	@! grep -rnE "\beval\(|\bexec\(|pickle|yaml\.load|shell\s*=\s*True|os\.system|os\.popen|__import__|literal_eval" src/ \
	  || { echo "dynamic-evaluation sink in src/"; exit 1; }

pin-check: ## The engine tag may appear only in engine.version
	@test -s engine.version || { echo "engine.version is empty"; exit 1; }
	@! grep -rn "kokoro-fastapi-cpu:v" \
	  --exclude=engine.version --exclude=Makefile \
	  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=adr . \
	  || { echo "engine tag literal outside engine.version"; exit 1; }

test: ## Run the test suite
	uv run pytest -q

version: ## Print the CalVer version
	@echo $(VERSION)

engine: ## Print the pinned engine tag
	@cat engine.version

config: ## Validate compose interpolation (requires .env)
	$(COMPOSE) config >/dev/null

build: ## Build the gateway image
	docker build -t kokoro-tone:$(VERSION) .

up: ## Build, then start the engine and gateway
	$(COMPOSE) up -d --build

down: ## Stop and remove both containers
	$(COMPOSE) down

restart: ## Recreate containers from the current image (no rebuild)
	$(COMPOSE) up -d --force-recreate

ps: ## Show container state and health
	$(COMPOSE) ps

logs: ## Follow logs from both containers
	$(COMPOSE) logs -f --tail=100

narrate: ## Render a transcript through the running stack: make narrate FILE=script.txt [OUT=out.mp3]
	@test -n "$(FILE)" || { echo "FILE is required (make narrate FILE=script.txt)"; exit 2; }
	$(COMPOSE) exec -T kokoro-tone python scripts/narrate.py - --out - \
		--url http://127.0.0.1:8000 < "$(FILE)" > "$(OUT)"

analyse: ## Measure an audio file: make analyse FILE=out.mp3
	@test -n "$(FILE)" || { echo "FILE is required (make analyse FILE=out.mp3)"; exit 2; }
	uv run python scripts/analyse.py "$(FILE)"

clean: ## Remove the venv and caches
	rm -rf .venv .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

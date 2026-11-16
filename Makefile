.DEFAULT_GOAL := help
.PHONY: help install fmt lint typecheck test check run up down

help: ## List the available commands
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install the app and the development tools into the active virtualenv
	python -m pip install --editable ".[dev]"

fmt: ## Format the code
	ruff format .

lint: ## Lint the code
	ruff check .

typecheck: ## Type-check the application code
	mypy app

test: ## Run the tests
	pytest

check: ## Run every check that must pass before asking for a push
	ruff format --check .
	ruff check .
	mypy app
	pytest

run: ## Run the API on this machine with auto-reload (reads .env)
	uvicorn app.main:create_app --factory --ws none --reload

up: ## Build and start Postgres, Redis and the API in Docker
	docker compose up --build --detach --wait

down: ## Stop the Docker services (data is kept)
	docker compose down

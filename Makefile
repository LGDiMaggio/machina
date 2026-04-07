.PHONY: install test lint format typecheck ci docs clean

install:
	pip install -e ".[dev,all]"

test:
	pytest tests/ -v --cov=machina --cov-report=term-missing

test-unit:
	pytest tests/unit -v --cov=machina --cov-report=term-missing

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

typecheck:
	mypy src/machina

ci: lint typecheck test

docs:
	mkdocs serve

docs-build:
	mkdocs build

clean:
	rm -rf dist/ build/ *.egg-info .pytest_cache .mypy_cache htmlcov .coverage site/
	find . -type d -name __pycache__ -exec rm -rf {} +

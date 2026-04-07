.PHONY: install test lint format typecheck ci docs clean release

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

## release VERSION=x.y.z  — bump version, update changelog date, commit, tag, push
release:
ifndef VERSION
	$(error VERSION is not set. Usage: make release VERSION=x.y.z)
endif
	@echo "Bumping version to $(VERSION)..."
	sed -i 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml
	sed -i 's/__version__ = ".*"/__version__ = "$(VERSION)"/' src/machina/__init__.py
	sed -i 's/^\(## \[Unreleased\]\)/\1\n\n## [$(VERSION)] - $(shell date +%Y-%m-%d)/' CHANGELOG.md
	git add pyproject.toml src/machina/__init__.py CHANGELOG.md
	git commit -m "Release v$(VERSION)"
	git tag v$(VERSION)
	git push origin main --tags
	@echo "Done! GitHub Actions will build, publish to PyPI, and create the GitHub Release."

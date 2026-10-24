# Contributing to Machina

Thank you for your interest in contributing to Machina! Whether it's a new connector, a domain model improvement, a bug fix, or documentation — we appreciate your help.

## Getting Started

1. **Fork** the repository on GitHub
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/LGDiMaggio/machina.git
   cd machina
   ```
3. **Install** in development mode:
   ```bash
   pip install -e ".[dev,all]"
   ```
4. **Create a branch** for your change:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Workflow

### Running Tests

```bash
make test          # All tests with coverage
make test-unit     # Unit tests only
pytest tests/ -x   # Stop on first failure
```

### Linting & Formatting

```bash
make lint          # Check for lint errors (ruff)
make format        # Auto-format code (ruff)
make typecheck     # Type checking (mypy strict)
```

### Full CI Check

```bash
make ci            # Runs: lint + typecheck + test
```

All three checks must pass before submitting a PR.

## Code Standards

- **Type annotations** on all public functions and methods. Use `|` union syntax (Python 3.11+).
- **Docstrings** on all public classes and functions (Google style).
- **Async-first**: all I/O-bound code must be `async`. Sync wrappers are provided separately.
- **Pydantic models** for domain entities with validation.
- **Protocol classes** for interfaces (not ABC).
- **structlog** for logging. Always include `connector=`, `asset_id=`, `operation=` context where applicable.

## Connector Contributions

New connectors are especially welcome! See the existing connectors in `src/machina/connectors/` for the pattern:

1. Implement the `BaseConnector` protocol
2. Declare `capabilities` (e.g., `["read_assets", "read_work_orders"]`)
3. Normalize all data to domain entities (`Asset`, `WorkOrder`, etc.)
4. Add integration tests with mocked API responses
5. Add documentation in `docs/connectors/`

## Pull Request Process

1. Ensure all checks pass: `make ci`
2. Update `CHANGELOG.md` with a brief description of your change
3. Write or update tests for your change
4. Submit a PR against the `main` branch
5. A maintainer will review your PR — we aim for <48h response time

### Codecov and PRs that merge several branches

Codecov picks a PR's comparison base itself. If the PR's history reaches an old branch point
before it reaches the current tip of `main`, Codecov uses that old commit, finds no coverage
report for it, and `codecov/patch` passes without measuring anything ("No report found to
compare against"). A green `codecov/patch` is therefore only evidence once the base is checked.

When a PR merges three or more existing branches:

1. Build the history heads first: start at the branches' common fork point, merge each branch
   with `git merge --no-ff`, merge `origin/main` last, then add fix commits on top. Starting at
   `main` and merging the branches after it picks the wrong base from the third merge on, and a
   later `git merge main` changes nothing because `main` is already an ancestor.
2. Before each push, `python scripts/codecov_base_check.py predict --head HEAD` (after
   `git fetch origin main`) must end with `OK, equals the GitHub base`.
3. After CI finishes, `python scripts/codecov_base_check.py verify --pr <number>` must pass
   every check (it uses the `gh` CLI and Codecov's public API; no token). It confirms the
   comparison base is `main`'s tip with a processed report, `base_totals` is set, both coverage
   uploads (`core`, `extras`) are in the head report, and `codecov/patch` holds a measurement and
   succeeded.

`codecov/patch` is not a required check yet. Treat it as evidence only when `verify` passes.

## Reporting Issues

Use [GitHub Issues](https://github.com/LGDiMaggio/machina/issues) to report bugs or request features. Please include:

- Python version and OS
- Machina version (`pip show machina-ai`)
- Minimal reproduction steps
- Expected vs. actual behavior

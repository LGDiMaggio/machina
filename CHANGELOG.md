# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project scaffolding: pyproject.toml, CI, linting, testing setup
- Core domain model: Asset, WorkOrder, FailureMode, SparePart, Alarm, MaintenancePlan, Plant
- BaseConnector protocol and ConnectorRegistry
- Exception hierarchy (MachinaError and subclasses)
- Configuration system with YAML and environment variable support
- LLM abstraction layer (LiteLLM wrapper)
- Structured logging with structlog

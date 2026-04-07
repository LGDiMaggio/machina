# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-07

### Added

- Project scaffolding: pyproject.toml, CI, linting, testing setup
- Core domain model: `Asset`, `WorkOrder`, `FailureMode`, `SparePart`, `Alarm`, `MaintenancePlan`, `Plant`
- `BaseConnector` protocol and `ConnectorRegistry`
- Exception hierarchy (`MachinaError` and subclasses)
- Configuration system with YAML and environment variable support
- LLM abstraction layer (LiteLLM wrapper) with function-calling tool definitions
- Structured logging with structlog and action tracing (`ActionTrace`)
- `GenericCmmsConnector` for JSON/CSV-based CMMS integration
- `TelegramConnector` for maintenance notifications and commands
- `DocumentStore` connector with RAG (ChromaDB + LangChain document loaders)
- `Agent` runtime with domain-aware prompting, tool dispatch, and conversation loop
- `EntityResolver` for natural language → Asset/WorkOrder resolution
- `FailureAnalyzer`, `WorkOrderFactory`, `MaintenanceScheduler` domain services
- `knowledge_agent` quickstart example
- 306 unit tests, 98% coverage

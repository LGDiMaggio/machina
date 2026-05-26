# Exceptions

All Machina exceptions inherit from [`MachinaError`](#machinaerror), so a single `except MachinaError` catches everything framework-level while still letting unrelated exceptions surface.

The hierarchy is grouped by concern: connector errors, domain validation errors, agent runtime errors. Workflow execution errors and sandbox policy violations are top-level under `MachinaError`.

## Root

### `MachinaError`

::: machina.exceptions.MachinaError

## Connector errors

All connector errors derive from `ConnectorError`. Catch the root if you don't care about the specific failure mode; catch the leaves when the recovery action differs.

### `ConnectorError`

::: machina.exceptions.ConnectorError

### `ConnectorAuthError`

::: machina.exceptions.ConnectorAuthError

### `ConnectorTimeoutError`

::: machina.exceptions.ConnectorTimeoutError

### `ConnectorConfigError`

::: machina.exceptions.ConnectorConfigError

### `ConnectorSchemaError`

::: machina.exceptions.ConnectorSchemaError

### `ConnectorLockedError`

::: machina.exceptions.ConnectorLockedError

### `ConnectorTransientError`

::: machina.exceptions.ConnectorTransientError

### `ConnectorDriverError`

::: machina.exceptions.ConnectorDriverError

### `ConnectorDependencyError`

::: machina.exceptions.ConnectorDependencyError

## Domain errors

### `DomainValidationError`

::: machina.exceptions.DomainValidationError

### `AssetNotFoundError`

::: machina.exceptions.AssetNotFoundError

## Agent / LLM / workflow errors

### `AgentError`

::: machina.exceptions.AgentError

### `LLMError`

::: machina.exceptions.LLMError

### `WorkflowError`

::: machina.exceptions.WorkflowError

## Sandbox policy

### `SandboxViolationError`

::: machina.exceptions.SandboxViolationError

"""Machina exception hierarchy.

All exceptions inherit from MachinaError, enabling callers to catch
any framework error with a single ``except MachinaError`` clause.
"""


class MachinaError(Exception):
    """Base exception for all Machina errors."""


# --- Connector errors ---


class ConnectorError(MachinaError):
    """Error communicating with an external system via a connector."""


class ConnectorAuthError(ConnectorError):
    """Authentication or authorization failure in a connector."""


class ConnectorTimeoutError(ConnectorError):
    """A connector operation timed out."""


class ConnectorConfigError(ConnectorError):
    """A connector's configuration is invalid or incomplete."""


class ConnectorSchemaError(ConnectorError):
    """A schema mapping does not match the external data source."""


class ConnectorLockedError(ConnectorError):
    """The target resource is locked by another process."""


# --- Domain errors ---


class DomainValidationError(MachinaError):
    """A domain entity failed validation."""


class AssetNotFoundError(MachinaError):
    """The requested asset was not found in the registry."""


# --- Agent errors ---


class AgentError(MachinaError):
    """Error in the agent runtime layer."""


class LLMError(AgentError):
    """Error calling or processing an LLM response."""


class WorkflowError(AgentError):
    """Error executing a workflow step."""

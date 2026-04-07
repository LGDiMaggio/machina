"""Tests for the exception hierarchy."""

from machina.exceptions import (
    AgentError,
    AssetNotFoundError,
    ConnectorAuthError,
    ConnectorError,
    ConnectorTimeoutError,
    DomainValidationError,
    LLMError,
    MachinaError,
    WorkflowError,
)


class TestExceptionHierarchy:
    """Verify exception inheritance."""

    def test_connector_errors_inherit_from_machina(self) -> None:
        assert issubclass(ConnectorError, MachinaError)
        assert issubclass(ConnectorAuthError, ConnectorError)
        assert issubclass(ConnectorTimeoutError, ConnectorError)

    def test_domain_errors_inherit_from_machina(self) -> None:
        assert issubclass(DomainValidationError, MachinaError)
        assert issubclass(AssetNotFoundError, MachinaError)

    def test_agent_errors_inherit_from_machina(self) -> None:
        assert issubclass(AgentError, MachinaError)
        assert issubclass(LLMError, AgentError)
        assert issubclass(WorkflowError, AgentError)

    def test_catch_all_with_machina_error(self) -> None:
        with self._raises_machina(ConnectorAuthError("bad creds")):
            pass

    def _raises_machina(self, exc: MachinaError):  # type: ignore[no-untyped-def]
        """Helper: assert that the exception is caught as MachinaError."""
        import contextlib

        @contextlib.contextmanager
        def _ctx():  # type: ignore[no-untyped-def]
            try:
                raise exc
            except MachinaError:
                yield

        return _ctx()

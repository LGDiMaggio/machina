"""Tests for the structured logging module."""

from __future__ import annotations

import logging

from machina.observability.logging import configure_logging, get_logger


class TestConfigureLogging:
    """Test logging configuration."""

    def test_configure_default(self) -> None:
        configure_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1

    def test_configure_debug_level(self) -> None:
        configure_logging(level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_configure_json_output(self) -> None:
        configure_logging(json_output=True)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        formatter = root.handlers[0].formatter
        assert formatter is not None

    def test_configure_replaces_handlers(self) -> None:
        root = logging.getLogger()
        root.addHandler(logging.StreamHandler())
        root.addHandler(logging.StreamHandler())
        assert len(root.handlers) >= 2
        configure_logging()
        assert len(root.handlers) == 1

    def test_configure_case_insensitive_level(self) -> None:
        configure_logging(level="warning")
        root = logging.getLogger()
        assert root.level == logging.WARNING


class TestGetLogger:
    """Test logger creation with context binding."""

    def test_get_logger_returns_bound_logger(self) -> None:
        logger = get_logger("test")
        assert hasattr(logger, "info")  # structlog BoundLogger or LazyProxy

    def test_get_logger_with_context(self) -> None:
        logger = get_logger("test", connector="sap_pm", asset_id="P-201")
        # The bound context should be accessible
        assert logger is not None

    def test_get_logger_without_name(self) -> None:
        logger = get_logger()
        assert hasattr(logger, "info")  # structlog BoundLogger or LazyProxy

    def test_get_logger_without_context(self) -> None:
        logger = get_logger("test.module")
        assert hasattr(logger, "info")  # structlog BoundLogger or LazyProxy

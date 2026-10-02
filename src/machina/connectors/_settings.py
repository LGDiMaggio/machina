"""Validate flat YAML ``settings`` into a connector's pydantic config model.

Connectors whose constructor takes a parsed config object (Excel/CSV, SQL,
the Generic CMMS YAML mapping) also accept the same data as the flat
``settings`` dict a ``machina.yaml`` entry carries, so every YAML entry point
can build them.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from machina.exceptions import ConnectorConfigError

ModelT = TypeVar("ModelT", bound=BaseModel)


def validate_settings(model: type[ModelT], settings: dict[str, Any]) -> ModelT:
    """Validate ``settings`` into ``model`` without echoing input values.

    Pydantic's default error text repeats the offending input, and connector
    settings carry secrets — an ODBC DSN with ``PWD=...``, an API key. The
    raised error names each field path and problem only, and does not chain
    the original exception (whose text would carry the input again).

    Args:
        model: The pydantic config model to validate into.
        settings: The flat settings mapping (e.g. from ``machina.yaml``).

    Returns:
        The validated model instance.

    Unknown top-level keys are refused (by name) rather than silently
    ignored, so a typo such as ``capabilites: read_write`` cannot quietly
    produce a read-only connector.

    Raises:
        ConnectorConfigError: If ``settings`` contain unknown keys or do not
            validate.
    """
    if model.model_config.get("extra") != "allow":
        unknown = sorted(set(settings) - set(model.model_fields))
        if unknown:
            raise ConnectorConfigError(
                f"Unknown {model.__name__} settings: {', '.join(unknown)} — "
                f"expected: {', '.join(sorted(model.model_fields))}"
            )
    try:
        return model.model_validate(settings)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<settings>'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConnectorConfigError(f"Invalid {model.__name__} settings — {problems}") from None

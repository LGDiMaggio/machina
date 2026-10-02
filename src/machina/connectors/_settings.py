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

    Raises:
        ConnectorConfigError: If ``settings`` do not validate.
    """
    try:
        return model.model_validate(settings)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<settings>'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConnectorConfigError(f"Invalid {model.__name__} settings — {problems}") from None

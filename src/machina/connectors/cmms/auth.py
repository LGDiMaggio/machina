"""Authentication strategies for REST-based CMMS connectors.

Each strategy is a small pydantic model with an ``apply()`` method that
returns the HTTP headers augmented with the appropriate credentials. The
:data:`AuthStrategy` type alias is a discriminated union so configurations
can round-trip through YAML/JSON in the future.

Example:
    ```python
    from machina.connectors.cmms import BearerAuth, GenericCmmsConnector

    cmms = GenericCmmsConnector(
        url="https://cmms.example.com/api",
        auth=BearerAuth(token="secret"),
    )
    ```
"""

from __future__ import annotations

import base64
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class BearerAuth(BaseModel):
    """Bearer token in the ``Authorization`` header.

    Produces ``Authorization: Bearer <token>``, the default scheme used by
    most modern CMMS REST APIs (UpKeep, MaintainX, Limble, Fiix).
    """

    type: Literal["bearer"] = "bearer"
    token: str

    def apply(self, headers: dict[str, str]) -> dict[str, str]:
        """Return ``headers`` with a ``Bearer <token>`` Authorization entry."""
        return {**headers, "Authorization": f"Bearer {self.token}"}


class BasicAuth(BaseModel):
    """HTTP Basic authentication (RFC 7617).

    Produces ``Authorization: Basic <base64(user:password)>``. Common in
    older or on-premise CMMS deployments (eMaint, some Infor EAM setups).
    """

    type: Literal["basic"] = "basic"
    username: str
    password: str

    def apply(self, headers: dict[str, str]) -> dict[str, str]:
        """Return ``headers`` with a ``Basic <base64>`` Authorization entry."""
        creds = f"{self.username}:{self.password}".encode()
        encoded = base64.b64encode(creds).decode("ascii")
        return {**headers, "Authorization": f"Basic {encoded}"}


class ApiKeyHeaderAuth(BaseModel):
    """API key passed in a custom HTTP header.

    Produces ``<header_name>: <value>``. Used by CMMS APIs that prefer a
    dedicated key header (e.g. ``X-API-Key``, ``api-key``) over the
    ``Authorization`` header.
    """

    type: Literal["api_key"] = "api_key"
    header_name: str = "X-API-Key"
    value: str

    def apply(self, headers: dict[str, str]) -> dict[str, str]:
        """Return ``headers`` with the configured API key header."""
        return {**headers, self.header_name: self.value}


class NoAuth(BaseModel):
    """No authentication — public or intranet-only endpoints."""

    type: Literal["none"] = "none"

    def apply(self, headers: dict[str, str]) -> dict[str, str]:
        """Return ``headers`` unchanged (no credentials added)."""
        return dict(headers)


AuthStrategy = Annotated[
    BearerAuth | BasicAuth | ApiKeyHeaderAuth | NoAuth,
    Field(discriminator="type"),
]
"""Discriminated union of supported authentication strategies.

Use the concrete classes (``BearerAuth``, ``BasicAuth``,
``ApiKeyHeaderAuth``, ``NoAuth``) to instantiate. The discriminator
``type`` field enables deterministic deserialization from external config.
"""

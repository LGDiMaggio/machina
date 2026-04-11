"""Authentication strategies for REST-based CMMS connectors.

Each strategy is a small pydantic model with an ``apply()`` method that
returns the HTTP headers augmented with the appropriate credentials. The
:data:`AuthStrategy` type alias is a discriminated union so configurations
can round-trip through YAML/JSON in the future.

Example:
    ```python
    import os
    from machina.connectors.cmms import BearerAuth, GenericCmmsConnector

    cmms = GenericCmmsConnector(
        url="https://cmms.example.com/api",
        auth=BearerAuth(token=os.environ["CMMS_API_TOKEN"]),
    )
    ```
"""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

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


class OAuth2ClientCredentials(BaseModel):
    """OAuth 2.0 Client Credentials grant (RFC 6749 §4.4).

    Fetches a bearer token from ``token_url`` using ``client_id`` and
    ``client_secret``, then adds ``Authorization: Bearer <token>`` to
    every request. Call :meth:`fetch_token` during connector ``connect()``
    before making API calls.

    Used by SAP S/4HANA, some Oracle Cloud deployments, and other
    enterprise systems that require OAuth2 machine-to-machine auth.

    Example:
        ```python
        import os
        from machina.connectors.cmms import OAuth2ClientCredentials

        auth = OAuth2ClientCredentials(
            token_url="https://sap.example.com/oauth/token",
            client_id=os.environ["SAP_CLIENT_ID"],
            client_secret=os.environ["SAP_CLIENT_SECRET"],
        )
        # During connector connect():
        await auth.fetch_token(httpx_client)
        # Then apply() works synchronously:
        headers = auth.apply({})
        ```
    """

    type: Literal["oauth2_client_credentials"] = "oauth2_client_credentials"
    token_url: str
    client_id: str
    client_secret: str
    scope: str = ""
    _access_token: str = ""

    async def fetch_token(self, client: Any) -> str:
        """Fetch an access token from the token endpoint.

        Args:
            client: An ``httpx.AsyncClient`` instance used to POST to the
                token endpoint.

        Returns:
            The access token string.

        Raises:
            ConnectorAuthError: If the token request fails.
        """
        from machina.exceptions import ConnectorAuthError

        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            data["scope"] = self.scope
        resp = await client.post(self.token_url, data=data)
        if resp.status_code != 200:
            raise ConnectorAuthError(f"OAuth2 token request failed: HTTP {resp.status_code}")
        body = resp.json()
        token: str = body.get("access_token", "")
        if not token:
            raise ConnectorAuthError("OAuth2 response missing access_token")
        self._access_token = token
        return token

    def apply(self, headers: dict[str, str]) -> dict[str, str]:
        """Return ``headers`` with a ``Bearer <token>`` Authorization entry.

        Raises:
            ConnectorAuthError: If :meth:`fetch_token` has not been called.
        """
        if not self._access_token:
            from machina.exceptions import ConnectorAuthError

            raise ConnectorAuthError("OAuth2 token not fetched — call fetch_token() first")
        return {**headers, "Authorization": f"Bearer {self._access_token}"}


AuthStrategy = Annotated[
    BearerAuth | BasicAuth | ApiKeyHeaderAuth | NoAuth | OAuth2ClientCredentials,
    Field(discriminator="type"),
]
"""Discriminated union of supported authentication strategies.

Use the concrete classes (``BearerAuth``, ``BasicAuth``,
``ApiKeyHeaderAuth``, ``NoAuth``, ``OAuth2ClientCredentials``) to
instantiate. The discriminator ``type`` field enables deterministic
deserialization from external config.
"""

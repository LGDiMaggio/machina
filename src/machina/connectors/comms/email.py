"""EmailConnector — Email integration for maintenance notifications and interaction.

Provides a communication channel via standard SMTP/IMAP (zero external
dependencies) with an optional Gmail API backend for Google Workspace
environments.
"""

from __future__ import annotations

import asyncio
import contextlib
import email as email_lib
import email.mime.text
import email.utils
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.comms.telegram import IncomingMessage, MessageHandler
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)


class EmailConnector:
    """Connector for email via SMTP/IMAP or Gmail API.

    Uses the Python standard library (``smtplib`` + ``imaplib``) by
    default — no extra dependencies required.  For Google Workspace
    environments, pass ``gmail_credentials_file`` to use the Gmail API
    backend instead (requires ``pip install machina-ai[gmail]``).

    Args:
        smtp_host: SMTP server hostname.
        smtp_port: SMTP server port (default 465 for SSL).
        imap_host: IMAP server hostname for receiving mail.
        imap_port: IMAP server port (default 993 for SSL).
        username: Email account username (usually the email address).
        password: Email account password or app-specific password.
        use_tls: Whether to use TLS/SSL (default ``True``).
        from_address: The ``From`` address for outgoing mail.
            Defaults to *username* if not set.
        gmail_credentials_file: Path to a Gmail API OAuth credentials JSON
            file.  When set, SMTP/IMAP are ignored and the Gmail API is
            used instead.
        poll_interval: Seconds between IMAP inbox polls (default 30).

    Example:
        ```python
        from machina.connectors import Email

        email_conn = Email(
            smtp_host="smtp.example.com",
            imap_host="imap.example.com",
            username="agent@example.com",
            password="${EMAIL_PASSWORD}",
        )
        await email_conn.connect()
        await email_conn.send_message("tech@example.com", "WO-2026-42 created")
        ```
    """

    capabilities: ClassVar[list[str]] = ["send_message", "receive_message"]

    def __init__(
        self,
        *,
        smtp_host: str = "",
        smtp_port: int = 465,
        imap_host: str = "",
        imap_port: int = 993,
        username: str = "",
        password: str = "",
        use_tls: bool = True,
        from_address: str = "",
        gmail_credentials_file: str | None = None,
        poll_interval: int = 30,
    ) -> None:
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._from_address = from_address or username
        self._gmail_credentials_file = gmail_credentials_file
        self._poll_interval = poll_interval
        self._connected = False
        self._smtp: Any = None
        self._gmail_service: Any = None

    @property
    def _is_gmail(self) -> bool:
        return self._gmail_credentials_file is not None

    async def connect(self) -> None:
        """Establish SMTP connection or authenticate with Gmail API."""
        if self._is_gmail:
            await self._connect_gmail()
        else:
            await self._connect_smtp()
        self._connected = True

    async def disconnect(self) -> None:
        """Close SMTP connection or release Gmail resources."""
        if self._smtp is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._smtp.quit)
            self._smtp = None
        self._gmail_service = None
        self._connected = False
        logger.info("disconnected", connector="EmailConnector")

    async def health_check(self) -> ConnectorHealth:
        """Check email connectivity."""
        if not self._connected:
            return ConnectorHealth(status=ConnectorStatus.UNHEALTHY, message="Not connected")
        return ConnectorHealth(status=ConnectorStatus.HEALTHY, message="Connected")

    async def send_message(
        self,
        to: str,
        text: str,
        *,
        subject: str = "Machina Notification",
    ) -> None:
        """Send an email message.

        Args:
            to: Recipient email address.
            text: Message body (plain text).
            subject: Email subject line.

        Raises:
            ConnectorError: If not connected or sending fails.
        """
        self._ensure_connected()

        if self._is_gmail:
            await self._send_gmail(to, text, subject=subject)
        else:
            await self._send_smtp(to, text, subject=subject)

        logger.debug(
            "message_sent",
            connector="EmailConnector",
            to=to,
            subject=subject,
        )

    async def listen(self, handler: MessageHandler) -> None:
        """Poll the IMAP inbox for new messages and dispatch to handler.

        For Gmail API mode, polls using the Gmail API ``users.messages.list``
        endpoint.  For SMTP/IMAP mode, connects to the IMAP server and
        polls for ``UNSEEN`` messages.

        This is a blocking call that polls until cancelled.

        Args:
            handler: Async callback that receives an :class:`IncomingMessage`
                     and returns the response text.
        """
        self._ensure_connected()
        logger.info(
            "listening", connector="EmailConnector", mode="gmail" if self._is_gmail else "imap"
        )

        try:
            while True:
                messages = await self._fetch_new_messages()
                for msg in messages:
                    response = await handler(msg)
                    if response and msg.chat_id:
                        await self.send_message(
                            msg.chat_id, response, subject="Re: " + msg.text[:50]
                        )
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # SMTP / IMAP backend
    # ------------------------------------------------------------------

    async def _connect_smtp(self) -> None:
        """Establish SMTP connection with authentication."""
        import smtplib

        if not self._smtp_host:
            raise ConnectorError("smtp_host is required for EmailConnector")
        if not self._username:
            raise ConnectorError("username is required for EmailConnector")

        try:
            if self._use_tls:
                smtp = await asyncio.to_thread(smtplib.SMTP_SSL, self._smtp_host, self._smtp_port)
            else:
                smtp = await asyncio.to_thread(smtplib.SMTP, self._smtp_host, self._smtp_port)
                await asyncio.to_thread(smtp.starttls)

            await asyncio.to_thread(smtp.login, self._username, self._password)
            self._smtp = smtp
            logger.info(
                "connected",
                connector="EmailConnector",
                mode="smtp",
                host=self._smtp_host,
            )
        except smtplib.SMTPAuthenticationError as exc:
            raise ConnectorAuthError(f"SMTP authentication failed: {exc}") from exc
        except Exception as exc:
            raise ConnectorError(f"SMTP connection failed: {exc}") from exc

    async def _send_smtp(self, to: str, text: str, *, subject: str) -> None:
        """Send email via SMTP."""
        if self._smtp is None:
            raise ConnectorError("SMTP connection not established")

        msg = email.mime.text.MIMEText(text, "plain", "utf-8")
        msg["From"] = self._from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)

        try:
            await asyncio.to_thread(self._smtp.sendmail, self._from_address, [to], msg.as_string())
        except Exception as exc:
            raise ConnectorError(f"Failed to send email: {exc}") from exc

    async def _fetch_imap_messages(self) -> list[IncomingMessage]:
        """Fetch unseen messages from IMAP inbox."""
        import imaplib

        if not self._imap_host:
            return []

        messages: list[IncomingMessage] = []
        try:
            if self._use_tls:
                imap = await asyncio.to_thread(imaplib.IMAP4_SSL, self._imap_host, self._imap_port)
            else:
                imap = await asyncio.to_thread(imaplib.IMAP4, self._imap_host, self._imap_port)

            await asyncio.to_thread(imap.login, self._username, self._password)
            await asyncio.to_thread(imap.select, "INBOX")

            _, data = await asyncio.to_thread(imap.search, None, "UNSEEN")
            msg_ids = data[0].split() if data[0] else []

            for msg_id in msg_ids:
                _, msg_data = await asyncio.to_thread(imap.fetch, msg_id, "(RFC822)")
                if msg_data[0] is None:
                    continue
                raw_email = msg_data[0][1]
                parsed = email_lib.message_from_bytes(raw_email)

                body = ""
                if parsed.is_multipart():
                    for part in parsed.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="replace")
                            break
                else:
                    payload = parsed.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")

                from_addr = parsed.get("From", "")
                messages.append(
                    IncomingMessage(
                        text=body.strip(),
                        chat_id=from_addr,
                        user_id=from_addr,
                        user_name=from_addr,
                        channel="email",
                        raw=parsed,
                    )
                )

            await asyncio.to_thread(imap.logout)
        except Exception as exc:
            logger.warning(
                "imap_fetch_error",
                connector="EmailConnector",
                error=str(exc),
            )

        return messages

    # ------------------------------------------------------------------
    # Gmail API backend
    # ------------------------------------------------------------------

    async def _connect_gmail(self) -> None:
        """Authenticate with Gmail API using OAuth credentials."""
        try:
            from google_auth_oauthlib.flow import (
                InstalledAppFlow,  # type: ignore[import-not-found]
            )
            from googleapiclient.discovery import build  # type: ignore[import-not-found]
        except ImportError:
            msg = (
                "Google API libraries are required for Gmail backend. "
                "Install with: pip install machina-ai[gmail]"
            )
            raise ImportError(msg) from None

        scopes = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        ]

        try:
            flow = InstalledAppFlow.from_client_secrets_file(self._gmail_credentials_file, scopes)
            creds = await asyncio.to_thread(flow.run_local_server, port=0)
            self._gmail_service = build("gmail", "v1", credentials=creds)
            logger.info("connected", connector="EmailConnector", mode="gmail")
        except Exception as exc:
            raise ConnectorAuthError(f"Gmail authentication failed: {exc}") from exc

    async def _send_gmail(self, to: str, text: str, *, subject: str) -> None:
        """Send email via Gmail API."""
        import base64

        if self._gmail_service is None:
            raise ConnectorError("Gmail service not initialised")

        msg = email.mime.text.MIMEText(text, "plain", "utf-8")
        msg["From"] = self._from_address
        msg["To"] = to
        msg["Subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        body = {"raw": raw}

        try:
            await asyncio.to_thread(
                self._gmail_service.users().messages().send(userId="me", body=body).execute
            )
        except Exception as exc:
            raise ConnectorError(f"Gmail send failed: {exc}") from exc

    async def _fetch_gmail_messages(self) -> list[IncomingMessage]:
        """Fetch unread messages via Gmail API."""
        import base64

        if self._gmail_service is None:
            return []

        messages: list[IncomingMessage] = []
        try:
            results = await asyncio.to_thread(
                self._gmail_service.users()
                .messages()
                .list(userId="me", q="is:unread", maxResults=10)
                .execute
            )
            msg_list = results.get("messages", [])

            for msg_meta in msg_list:
                msg_detail = await asyncio.to_thread(
                    self._gmail_service.users()
                    .messages()
                    .get(userId="me", id=msg_meta["id"], format="full")
                    .execute
                )
                headers = {
                    h["name"]: h["value"] for h in msg_detail.get("payload", {}).get("headers", [])
                }
                from_addr = headers.get("From", "")

                # Extract plain text body
                body = ""
                payload = msg_detail.get("payload", {})
                if "parts" in payload:
                    for part in payload["parts"]:
                        if part.get("mimeType") == "text/plain":
                            data = part.get("body", {}).get("data", "")
                            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                            break
                elif "body" in payload:
                    data = payload["body"].get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

                messages.append(
                    IncomingMessage(
                        text=body.strip(),
                        chat_id=from_addr,
                        user_id=from_addr,
                        user_name=from_addr,
                        channel="email",
                        raw=msg_detail,
                    )
                )

                # Mark as read
                await asyncio.to_thread(
                    self._gmail_service.users()
                    .messages()
                    .modify(
                        userId="me",
                        id=msg_meta["id"],
                        body={"removeLabelIds": ["UNREAD"]},
                    )
                    .execute
                )
        except Exception as exc:
            logger.warning(
                "gmail_fetch_error",
                connector="EmailConnector",
                error=str(exc),
            )

        return messages

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_new_messages(self) -> list[IncomingMessage]:
        """Route to the correct fetch method based on backend."""
        if self._is_gmail:
            return await self._fetch_gmail_messages()
        return await self._fetch_imap_messages()

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")

"""Tests for the EmailConnector."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.connectors.comms.email import EmailConnector
from machina.connectors.comms.types import IncomingMessage
from machina.exceptions import ConnectorAuthError, ConnectorError


class TestEmailConnectorInit:
    """Test EmailConnector initialisation and properties."""

    def test_capabilities(self) -> None:
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        assert "send_message" in conn.capabilities
        assert "receive_message" in conn.capabilities

    def test_is_gmail_false_by_default(self) -> None:
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        assert conn._is_gmail is False

    def test_is_gmail_true_with_credentials(self) -> None:
        conn = EmailConnector(gmail_credentials_file="/path/creds.json")
        assert conn._is_gmail is True

    def test_from_address_defaults_to_username(self) -> None:
        conn = EmailConnector(smtp_host="smtp.test.com", username="u@test.com", password="p")
        assert conn._from_address == "u@test.com"

    def test_from_address_explicit(self) -> None:
        conn = EmailConnector(
            smtp_host="smtp.test.com",
            username="u@test.com",
            password="p",
            from_address="agent@test.com",
        )
        assert conn._from_address == "agent@test.com"


class TestEmailConnectorSMTP:
    """Test SMTP backend."""

    @pytest.mark.asyncio
    async def test_connect_without_smtp_host_raises(self) -> None:
        conn = EmailConnector(username="u", password="p")
        with pytest.raises(ConnectorError, match="smtp_host"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_without_username_raises(self) -> None:
        conn = EmailConnector(smtp_host="smtp.test.com", password="p")
        with pytest.raises(ConnectorError, match="username"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_smtp_ssl(self) -> None:
        """Connect via SMTP_SSL (use_tls=True, default)."""
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP_SSL", return_value=mock_smtp):
            conn = EmailConnector(
                smtp_host="smtp.test.com",
                smtp_port=465,
                username="u@test.com",
                password="pass123",
            )
            await conn.connect()

        assert conn._connected is True
        assert conn._smtp is mock_smtp
        mock_smtp.login.assert_called_once_with("u@test.com", "pass123")

    @pytest.mark.asyncio
    async def test_connect_smtp_starttls(self) -> None:
        """Connect via SMTP + STARTTLS (use_tls=False)."""
        mock_smtp = MagicMock()

        with patch("smtplib.SMTP", return_value=mock_smtp):
            conn = EmailConnector(
                smtp_host="smtp.test.com",
                smtp_port=587,
                username="u@test.com",
                password="pass123",
                use_tls=False,
            )
            await conn.connect()

        assert conn._connected is True
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("u@test.com", "pass123")

    @pytest.mark.asyncio
    async def test_connect_auth_error(self) -> None:
        """SMTP authentication error raises ConnectorAuthError."""
        import smtplib

        mock_smtp = MagicMock()
        mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")

        with (
            patch("smtplib.SMTP_SSL", return_value=mock_smtp),
            pytest.raises(ConnectorAuthError, match="SMTP authentication failed"),
        ):
            conn = EmailConnector(
                smtp_host="smtp.test.com",
                username="u@test.com",
                password="wrong",
            )
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_generic_error(self) -> None:
        """Generic SMTP error raises ConnectorError."""
        with (
            patch("smtplib.SMTP_SSL", side_effect=OSError("Connection refused")),
            pytest.raises(ConnectorError, match="SMTP connection failed"),
        ):
            conn = EmailConnector(
                smtp_host="smtp.test.com",
                username="u@test.com",
                password="p",
            )
            await conn.connect()

    @pytest.mark.asyncio
    async def test_disconnect_closes_smtp(self) -> None:
        """Disconnect calls quit() on the SMTP connection."""
        mock_smtp = MagicMock()
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        conn._connected = True
        conn._smtp = mock_smtp

        await conn.disconnect()
        assert conn._connected is False
        assert conn._smtp is None
        mock_smtp.quit.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_not_connected(self) -> None:
        """Disconnect when not connected is safe."""
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        await conn.disconnect()
        assert conn._connected is False

    @pytest.mark.asyncio
    async def test_send_message_smtp(self) -> None:
        """Send email via SMTP."""
        mock_smtp = MagicMock()
        conn = EmailConnector(
            smtp_host="smtp.test.com",
            username="agent@test.com",
            password="p",
        )
        conn._connected = True
        conn._smtp = mock_smtp

        await conn.send_message("tech@test.com", "WO created", subject="Alert")

        mock_smtp.sendmail.assert_called_once()
        call_args = mock_smtp.sendmail.call_args
        assert call_args[0][0] == "agent@test.com"
        assert call_args[0][1] == ["tech@test.com"]
        raw_msg = call_args[0][2]
        assert "Alert" in raw_msg
        assert "agent@test.com" in raw_msg
        assert "tech@test.com" in raw_msg

    @pytest.mark.asyncio
    async def test_send_message_not_connected_raises(self) -> None:
        """Send message when not connected raises error."""
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.send_message("to@test.com", "text")

    @pytest.mark.asyncio
    async def test_send_message_smtp_none_raises(self) -> None:
        """Send message with no SMTP connection raises error."""
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        conn._connected = True
        conn._smtp = None

        with pytest.raises(ConnectorError, match="SMTP connection not established"):
            await conn.send_message("to@test.com", "text")

    @pytest.mark.asyncio
    async def test_send_message_smtp_error(self) -> None:
        """Send failure raises ConnectorError."""
        mock_smtp = MagicMock()
        mock_smtp.sendmail.side_effect = Exception("SMTP failure")

        conn = EmailConnector(smtp_host="smtp.test.com", username="u@test.com", password="p")
        conn._connected = True
        conn._smtp = mock_smtp

        with pytest.raises(ConnectorError, match="Failed to send email"):
            await conn.send_message("to@test.com", "text")


class TestEmailConnectorIMAP:
    """Test IMAP message fetching."""

    @pytest.mark.asyncio
    async def test_fetch_imap_no_host_returns_empty(self) -> None:
        """No IMAP host configured → empty list."""
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        conn._connected = True
        result = await conn._fetch_imap_messages()
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_imap_messages(self) -> None:
        """Fetch unseen messages from IMAP."""
        # Build a simple RFC822 email
        raw_email = (
            b"From: sender@test.com\r\n"
            b"To: agent@test.com\r\n"
            b"Subject: Test\r\n"
            b"\r\n"
            b"Check pump P-201\r\n"
        )

        mock_imap = MagicMock()
        mock_imap.login.return_value = ("OK", [])
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.search.return_value = ("OK", [b"1"])
        mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {100})", raw_email)])
        mock_imap.logout.return_value = ("BYE", [])

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
            conn = EmailConnector(
                smtp_host="smtp.test.com",
                imap_host="imap.test.com",
                username="agent@test.com",
                password="p",
            )
            conn._connected = True
            messages = await conn._fetch_imap_messages()

        assert len(messages) == 1
        assert messages[0].text == "Check pump P-201"
        assert messages[0].chat_id == "sender@test.com"
        assert messages[0].channel == "email"

    @pytest.mark.asyncio
    async def test_fetch_imap_no_unseen(self) -> None:
        """No unseen messages → empty list."""
        mock_imap = MagicMock()
        mock_imap.login.return_value = ("OK", [])
        mock_imap.select.return_value = ("OK", [b"0"])
        mock_imap.search.return_value = ("OK", [b""])
        mock_imap.logout.return_value = ("BYE", [])

        with patch("imaplib.IMAP4_SSL", return_value=mock_imap):
            conn = EmailConnector(
                smtp_host="smtp.test.com",
                imap_host="imap.test.com",
                username="u@test.com",
                password="p",
            )
            conn._connected = True
            messages = await conn._fetch_imap_messages()

        assert messages == []

    @pytest.mark.asyncio
    async def test_fetch_imap_error_returns_empty(self) -> None:
        """IMAP error is logged but returns empty list."""
        with patch("imaplib.IMAP4_SSL", side_effect=OSError("Connection refused")):
            conn = EmailConnector(
                smtp_host="smtp.test.com",
                imap_host="imap.test.com",
                username="u@test.com",
                password="p",
            )
            conn._connected = True
            messages = await conn._fetch_imap_messages()

        assert messages == []


class TestEmailConnectorGmail:
    """Test Gmail API backend."""

    @pytest.mark.asyncio
    async def test_connect_gmail_import_error(self) -> None:
        """Gmail connect raises ImportError when google libs missing."""
        conn = EmailConnector(gmail_credentials_file="/path/creds.json")

        with (
            patch.dict(
                sys.modules,
                {
                    "google.oauth2.credentials": None,
                    "google_auth_oauthlib.flow": None,
                    "googleapiclient.discovery": None,
                },
            ),
            pytest.raises(ImportError, match="Google API libraries"),
        ):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_gmail_success(self) -> None:
        """Gmail connect authenticates and builds service."""
        mock_creds = MagicMock()
        mock_flow_cls = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow_cls
        mock_flow_cls.run_local_server.return_value = mock_creds

        mock_service = MagicMock()
        mock_build = MagicMock(return_value=mock_service)

        mock_google_creds = MagicMock()
        mock_google_creds.Credentials = MagicMock
        mock_google_flow = MagicMock()
        mock_google_flow.InstalledAppFlow = mock_flow_cls
        mock_google_discovery = MagicMock()
        mock_google_discovery.build = mock_build

        conn = EmailConnector(gmail_credentials_file="/path/creds.json")

        with patch.dict(
            sys.modules,
            {
                "google": MagicMock(),
                "google.oauth2": MagicMock(),
                "google.oauth2.credentials": mock_google_creds,
                "google_auth_oauthlib": MagicMock(),
                "google_auth_oauthlib.flow": mock_google_flow,
                "googleapiclient": MagicMock(),
                "googleapiclient.discovery": mock_google_discovery,
            },
        ):
            await conn.connect()

        assert conn._connected is True
        assert conn._gmail_service is mock_service

    @pytest.mark.asyncio
    async def test_send_gmail(self) -> None:
        """Send email via Gmail API."""
        mock_execute = MagicMock()
        mock_send = MagicMock()
        mock_send.execute = mock_execute
        mock_messages = MagicMock()
        mock_messages.send.return_value = mock_send
        mock_users = MagicMock()
        mock_users.messages.return_value = mock_messages
        mock_service = MagicMock()
        mock_service.users.return_value = mock_users

        conn = EmailConnector(gmail_credentials_file="/path/creds.json")
        conn._connected = True
        conn._gmail_service = mock_service

        await conn.send_message("tech@test.com", "WO created", subject="Alert")

        mock_messages.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_gmail_no_service_raises(self) -> None:
        """Gmail send with no service raises error."""
        conn = EmailConnector(gmail_credentials_file="/path/creds.json")
        conn._connected = True
        conn._gmail_service = None

        with pytest.raises(ConnectorError, match="Gmail service not initialised"):
            await conn.send_message("to@test.com", "text")


class TestEmailConnectorHealth:
    """Test health check."""

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self) -> None:
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"

    @pytest.mark.asyncio
    async def test_health_check_connected(self) -> None:
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")
        conn._connected = True
        health = await conn.health_check()
        assert health.status.value == "healthy"


class TestEmailConnectorListen:
    """Test listen (polling) behaviour."""

    @pytest.mark.asyncio
    async def test_listen_not_connected_raises(self) -> None:
        conn = EmailConnector(smtp_host="smtp.test.com", username="u", password="p")

        async def handler(msg: IncomingMessage) -> str:
            return "ok"

        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.listen(handler)

    @pytest.mark.asyncio
    async def test_listen_polls_and_dispatches(self) -> None:
        """Listen fetches messages and dispatches to handler."""
        received: list[IncomingMessage] = []
        call_count = 0

        conn = EmailConnector(
            smtp_host="smtp.test.com",
            imap_host="imap.test.com",
            username="u@test.com",
            password="p",
            poll_interval=0,
        )
        conn._connected = True

        test_msg = IncomingMessage(
            text="Check pump",
            chat_id="sender@test.com",
            user_id="sender@test.com",
            user_name="sender@test.com",
            channel="email",
        )

        async def mock_fetch() -> list[IncomingMessage]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [test_msg]
            # Cancel after first poll
            raise asyncio.CancelledError

        conn._fetch_new_messages = mock_fetch  # type: ignore[assignment]
        # Mock send_message to avoid SMTP call for the reply
        conn.send_message = AsyncMock()  # type: ignore[assignment]

        async def handler(msg: IncomingMessage) -> str:
            received.append(msg)
            return "Response"

        await conn.listen(handler)

        assert len(received) == 1
        assert received[0].text == "Check pump"

    @pytest.mark.asyncio
    async def test_listen_cancellation(self) -> None:
        """Listen exits cleanly on CancelledError."""
        conn = EmailConnector(
            smtp_host="smtp.test.com",
            username="u@test.com",
            password="p",
            poll_interval=0,
        )
        conn._connected = True

        async def mock_fetch() -> list[IncomingMessage]:
            raise asyncio.CancelledError

        conn._fetch_new_messages = mock_fetch  # type: ignore[assignment]

        async def handler(msg: IncomingMessage) -> str:
            return "ok"

        # Should not raise
        await conn.listen(handler)


# ---------------------------------------------------------------------------
# IMAP connection reuse
# ---------------------------------------------------------------------------


class TestEmailConnectorIMAPReuse:
    """Test IMAP connection reuse and reconnection logic."""

    @pytest.mark.asyncio
    async def test_imap_connection_reused_across_polls(self) -> None:
        """IMAP connection is reused when noop succeeds."""
        conn = EmailConnector(
            smtp_host="smtp.test.com",
            imap_host="imap.test.com",
            username="u@test.com",
            password="p",
        )
        conn._connected = True

        mock_imap = MagicMock()
        mock_imap.noop.return_value = ("OK", [])
        mock_imap.select.return_value = ("OK", [])
        mock_imap.search.return_value = ("OK", [b""])
        conn._imap = mock_imap

        messages = await conn._fetch_imap_messages()
        assert messages == []
        # Should reuse existing connection, not create a new one
        mock_imap.noop.assert_called_once()

    @pytest.mark.asyncio
    async def test_imap_reconnects_on_failure(self) -> None:
        """IMAP reconnects when noop raises an exception."""
        conn = EmailConnector(
            smtp_host="smtp.test.com",
            imap_host="imap.test.com",
            username="u@test.com",
            password="p",
        )
        conn._connected = True

        # Existing connection that fails noop
        old_imap = MagicMock()
        old_imap.noop.side_effect = Exception("Connection lost")
        old_imap.logout.return_value = ("BYE", [])
        conn._imap = old_imap

        # New connection that succeeds
        new_imap = MagicMock()
        new_imap.select.return_value = ("OK", [])
        new_imap.search.return_value = ("OK", [b""])

        import imaplib

        with patch.object(imaplib, "IMAP4_SSL", return_value=new_imap):
            messages = await conn._fetch_imap_messages()

        assert messages == []
        assert conn._imap is new_imap

    @pytest.mark.asyncio
    async def test_imap_no_host_returns_empty(self) -> None:
        """No IMAP host returns empty list."""
        conn = EmailConnector(
            smtp_host="smtp.test.com",
            username="u@test.com",
            password="p",
        )
        conn._connected = True

        messages = await conn._fetch_imap_messages()
        assert messages == []

    @pytest.mark.asyncio
    async def test_imap_fetch_error_resets_connection(self) -> None:
        """IMAP errors reset the connection for next poll."""
        conn = EmailConnector(
            smtp_host="smtp.test.com",
            imap_host="imap.test.com",
            username="u@test.com",
            password="p",
        )
        conn._connected = True

        mock_imap = MagicMock()
        mock_imap.noop.return_value = ("OK", [])
        mock_imap.select.side_effect = Exception("Mailbox unavailable")
        conn._imap = mock_imap

        messages = await conn._fetch_imap_messages()
        assert messages == []
        # Connection should be reset for next poll
        assert conn._imap is None

    @pytest.mark.asyncio
    async def test_disconnect_closes_imap(self) -> None:
        """Disconnect properly closes IMAP connection."""
        conn = EmailConnector(
            smtp_host="smtp.test.com",
            imap_host="imap.test.com",
            username="u@test.com",
            password="p",
        )
        conn._connected = True
        mock_imap = MagicMock()
        conn._imap = mock_imap

        await conn.disconnect()
        mock_imap.logout.assert_called_once()
        assert conn._imap is None

    @pytest.mark.asyncio
    async def test_imap_multipart_email_parsing(self) -> None:
        """Multipart emails have plain text body extracted."""
        import email.mime.multipart
        import email.mime.text

        conn = EmailConnector(
            smtp_host="smtp.test.com",
            imap_host="imap.test.com",
            username="u@test.com",
            password="p",
        )
        conn._connected = True

        # Build a real multipart email
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["From"] = "sender@test.com"
        msg["To"] = "u@test.com"
        msg["Subject"] = "Test"
        msg.attach(email.mime.text.MIMEText("Plain text body", "plain"))
        msg.attach(email.mime.text.MIMEText("<b>HTML body</b>", "html"))

        mock_imap = MagicMock()
        mock_imap.noop.return_value = ("OK", [])
        mock_imap.select.return_value = ("OK", [])
        mock_imap.search.return_value = ("OK", [b"1"])
        mock_imap.fetch.return_value = ("OK", [(b"1", msg.as_bytes())])
        conn._imap = mock_imap

        messages = await conn._fetch_imap_messages()
        assert len(messages) == 1
        assert messages[0].text == "Plain text body"
        assert messages[0].channel == "email"


# ---------------------------------------------------------------------------
# Gmail API backend
# ---------------------------------------------------------------------------


class TestEmailConnectorGmailExtended:
    """Extended Gmail API backend tests."""

    @pytest.mark.asyncio
    async def test_gmail_send(self) -> None:
        """Gmail send_message encodes and sends via the API."""
        conn = EmailConnector(
            gmail_credentials_file="/fake/creds.json",
            username="agent@gmail.com",
        )
        conn._connected = True

        mock_service = MagicMock()
        mock_users = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.return_value = {"id": "sent-1"}
        mock_messages.send.return_value = mock_send
        mock_users.messages.return_value = mock_messages
        mock_service.users.return_value = mock_users
        conn._gmail_service = mock_service

        await conn.send_message("tech@test.com", "WO created", subject="Alert")
        mock_messages.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_gmail_send_not_initialized_raises(self) -> None:
        """Gmail send raises when service is None."""
        conn = EmailConnector(
            gmail_credentials_file="/fake/creds.json",
            username="agent@gmail.com",
        )
        conn._connected = True
        conn._gmail_service = None

        with pytest.raises(ConnectorError, match="Gmail service not initialised"):
            await conn._send_gmail("to@test.com", "text", subject="Test")

    @pytest.mark.asyncio
    async def test_gmail_send_failure_raises(self) -> None:
        """Gmail send wraps API errors in ConnectorError."""
        conn = EmailConnector(
            gmail_credentials_file="/fake/creds.json",
            username="agent@gmail.com",
        )
        conn._connected = True

        mock_service = MagicMock()
        mock_users = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.side_effect = RuntimeError("API quota exceeded")
        mock_messages.send.return_value = mock_send
        mock_users.messages.return_value = mock_messages
        mock_service.users.return_value = mock_users
        conn._gmail_service = mock_service

        with pytest.raises(ConnectorError, match="Gmail send failed"):
            await conn.send_message("to@test.com", "text")

    @pytest.mark.asyncio
    async def test_gmail_fetch_messages(self) -> None:
        """Gmail fetch returns parsed messages and marks them as read."""
        import base64

        conn = EmailConnector(
            gmail_credentials_file="/fake/creds.json",
            username="agent@gmail.com",
        )
        conn._connected = True

        body_b64 = base64.urlsafe_b64encode(b"Check pump P-201").decode()
        mock_service = MagicMock()
        mock_users = MagicMock()
        mock_messages = MagicMock()

        mock_list = MagicMock()
        mock_list.execute.return_value = {"messages": [{"id": "msg-1"}]}
        mock_messages.list.return_value = mock_list

        mock_get = MagicMock()
        mock_get.execute.return_value = {
            "payload": {
                "headers": [{"name": "From", "value": "tech@test.com"}],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": body_b64}},
                ],
            }
        }
        mock_messages.get.return_value = mock_get

        mock_modify = MagicMock()
        mock_modify.execute.return_value = {}
        mock_messages.modify.return_value = mock_modify

        mock_users.messages.return_value = mock_messages
        mock_service.users.return_value = mock_users
        conn._gmail_service = mock_service

        messages = await conn._fetch_gmail_messages()
        assert len(messages) == 1
        assert messages[0].text == "Check pump P-201"
        assert messages[0].chat_id == "tech@test.com"
        # Verify marked as read
        mock_messages.modify.assert_called_once()

    @pytest.mark.asyncio
    async def test_gmail_fetch_body_without_parts(self) -> None:
        """Gmail messages without parts use direct body data."""
        import base64

        conn = EmailConnector(
            gmail_credentials_file="/fake/creds.json",
            username="agent@gmail.com",
        )
        conn._connected = True

        body_b64 = base64.urlsafe_b64encode(b"Simple body").decode()
        mock_service = MagicMock()
        mock_users = MagicMock()
        mock_messages = MagicMock()

        mock_list = MagicMock()
        mock_list.execute.return_value = {"messages": [{"id": "msg-2"}]}
        mock_messages.list.return_value = mock_list

        mock_get = MagicMock()
        mock_get.execute.return_value = {
            "payload": {
                "headers": [{"name": "From", "value": "mgr@test.com"}],
                "body": {"data": body_b64},
            }
        }
        mock_messages.get.return_value = mock_get

        mock_modify = MagicMock()
        mock_modify.execute.return_value = {}
        mock_messages.modify.return_value = mock_modify

        mock_users.messages.return_value = mock_messages
        mock_service.users.return_value = mock_users
        conn._gmail_service = mock_service

        messages = await conn._fetch_gmail_messages()
        assert len(messages) == 1
        assert messages[0].text == "Simple body"

    @pytest.mark.asyncio
    async def test_gmail_fetch_no_service_returns_empty(self) -> None:
        """Gmail fetch with no service returns empty."""
        conn = EmailConnector(gmail_credentials_file="/fake/creds.json")
        conn._connected = True
        conn._gmail_service = None

        messages = await conn._fetch_gmail_messages()
        assert messages == []

    @pytest.mark.asyncio
    async def test_gmail_fetch_error_returns_empty(self) -> None:
        """Gmail fetch that errors returns empty and logs warning."""
        conn = EmailConnector(gmail_credentials_file="/fake/creds.json")
        conn._connected = True

        mock_service = MagicMock()
        mock_users = MagicMock()
        mock_messages = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.side_effect = RuntimeError("Network error")
        mock_messages.list.return_value = mock_list
        mock_users.messages.return_value = mock_messages
        mock_service.users.return_value = mock_users
        conn._gmail_service = mock_service

        messages = await conn._fetch_gmail_messages()
        assert messages == []

    @pytest.mark.asyncio
    async def test_fetch_new_messages_routes_to_gmail(self) -> None:
        """_fetch_new_messages routes to Gmail when credentials are set."""
        conn = EmailConnector(gmail_credentials_file="/fake/creds.json")
        conn._connected = True
        conn._gmail_service = None

        messages = await conn._fetch_new_messages()
        assert messages == []

    @pytest.mark.asyncio
    async def test_gmail_connect(self) -> None:
        """Gmail connect flow with mocked Google libraries."""
        mock_flow_cls = MagicMock()
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        mock_oauthlib = MagicMock()
        mock_oauthlib.flow.InstalledAppFlow = mock_flow_cls

        mock_build = MagicMock(return_value=MagicMock())

        with patch.dict(
            sys.modules,
            {
                "google_auth_oauthlib": mock_oauthlib,
                "google_auth_oauthlib.flow": mock_oauthlib.flow,
                "googleapiclient": MagicMock(),
                "googleapiclient.discovery": MagicMock(build=mock_build),
            },
        ):
            conn = EmailConnector(
                gmail_credentials_file="/fake/creds.json",
                username="agent@gmail.com",
            )
            await conn.connect()
            assert conn._connected is True

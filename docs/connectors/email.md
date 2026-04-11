# Email

The `EmailConnector` provides email-based communication using standard **SMTP/IMAP** (zero external dependencies) with an optional **Gmail API** backend for Google Workspace environments.

## Prerequisites

### SMTP/IMAP (default)

- SMTP server hostname and port (e.g. `smtp.gmail.com:465`)
- IMAP server hostname and port (e.g. `imap.gmail.com:993`) — for receiving
- Email credentials (username + password or app-specific password)

### Gmail API (optional)

- A Google Cloud project with Gmail API enabled
- OAuth 2.0 credentials JSON file (Desktop app type)
- Install the Gmail extra: `pip install machina-ai[gmail]`

## Installation

```bash
# SMTP/IMAP — no extra dependencies (uses Python stdlib)
pip install machina-ai

# Gmail API backend
pip install machina-ai[gmail]
```

## Configuration

=== "Python (SMTP/IMAP)"

    ```python
    from machina.connectors import Email

    email = Email(
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        imap_host="imap.gmail.com",
        imap_port=993,
        username="agent@example.com",
        password="app-specific-password",
    )
    ```

=== "Python (Gmail API)"

    ```python
    from machina.connectors import Email

    email = Email(
        gmail_credentials_file="credentials.json",
        from_address="agent@example.com",
    )
    ```

=== "YAML"

    ```yaml
    connectors:
      email:
        type: email
        smtp_host: smtp.gmail.com
        smtp_port: 465
        imap_host: imap.gmail.com
        imap_port: 993
        username: ${EMAIL_USERNAME}
        password: ${EMAIL_PASSWORD}
    ```

## Capabilities

| Capability | Method | Description |
|-----------|--------|-------------|
| `send_message` | `send_message(to, text, subject=...)` | Send an email |
| `receive_message` | `listen(handler)` | Poll IMAP inbox for new messages |

## Usage with Agent

```python
from machina import Agent
from machina.connectors import Email

email = Email(
    smtp_host="smtp.example.com",
    imap_host="imap.example.com",
    username="agent@example.com",
    password="${EMAIL_PASSWORD}",
)

agent = Agent(
    llm="openai:gpt-4o",
    connectors=[email],
)
await agent.start()
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `smtp_host` | `str` | `""` | SMTP server hostname |
| `smtp_port` | `int` | `465` | SMTP server port |
| `imap_host` | `str` | `""` | IMAP server hostname (for receiving) |
| `imap_port` | `int` | `993` | IMAP server port |
| `username` | `str` | `""` | Email account username |
| `password` | `str` | `""` | Email account password |
| `use_tls` | `bool` | `True` | Use TLS/SSL (`True`=SMTP_SSL, `False`=STARTTLS) |
| `from_address` | `str` | *username* | Sender address for outgoing mail |
| `gmail_credentials_file` | `str \| None` | `None` | Path to Gmail OAuth credentials (activates Gmail API mode) |
| `poll_interval` | `int` | `30` | Seconds between inbox polls |

## Known Limitations

- HTML email rendering is not supported (plain text only).
- Attachments (e.g. PDF work orders) are not yet supported.
- Gmail API mode requires a one-time OAuth browser flow on first use.
- IMAP polling is not real-time — use `poll_interval` to control frequency.

## API Reference

::: machina.connectors.comms.email.EmailConnector

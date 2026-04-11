# Slack

The `SlackConnector` integrates Machina with Slack using the [Bolt SDK](https://slack.dev/bolt-python/) in **Socket Mode** — no public URL or ingress required.

## Prerequisites

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** in the app settings
3. Generate an **App-Level Token** (`xapp-…`) with `connections:write` scope
4. Install the app to your workspace and note the **Bot User OAuth Token** (`xoxb-…`)
5. Add the following **Bot Token Scopes**: `chat:write`, `channels:read`, `channels:history`

## Installation

```bash
pip install machina-ai[slack]
```

## Configuration

=== "Python"

    ```python
    from machina.connectors import Slack

    slack = Slack(
        bot_token="xoxb-...",
        app_token="xapp-...",
        allowed_channel_ids=["C0123456789"],  # optional whitelist
    )
    ```

=== "YAML"

    ```yaml
    connectors:
      messaging:
        type: slack
        bot_token: ${SLACK_BOT_TOKEN}
        app_token: ${SLACK_APP_TOKEN}
        allowed_channel_ids:
          - C0123456789
    ```

## Capabilities

| Capability | Method | Description |
|-----------|--------|-------------|
| `send_message` | `send_message(channel, text)` | Send a message to a channel or DM |
| `receive_message` | `listen(handler)` | Receive messages via Socket Mode |

## Usage with Agent

```python
from machina import Agent
from machina.connectors import Slack

agent = Agent(
    llm="openai:gpt-4o",
    connectors=[Slack(bot_token="xoxb-...", app_token="xapp-...")],
)
await agent.start()
```

## Known Limitations

- Slash commands and interactive messages (buttons, modals) are not yet exposed as agent tools.
- Thread replies are sent as top-level messages (threading support planned).
- File uploads are not supported yet.

## API Reference

::: machina.connectors.comms.slack.SlackConnector

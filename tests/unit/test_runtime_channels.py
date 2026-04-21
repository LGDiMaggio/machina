"""Tests for channel/registry unification and sandbox channel gating.

Regression tests for issue #31: channels passed via ``Agent(channels=[...])``
must be discoverable by the workflow engine's capability-based dispatch, and
``sandbox=True`` must prevent channels from performing outbound I/O in
``Agent.start()`` / ``Agent.stop()``.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from machina.agent.runtime import Agent
from machina.connectors.capabilities import Capability
from machina.workflows.models import Step, Workflow


class _FakeChannel:
    """Channel stub: declares send_message capability and records I/O."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.SEND_MESSAGE})

    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False
        self.last_channel: str | None = None
        self.last_message: str | None = None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def health_check(self) -> bool:
        return True

    async def send_message(self, channel: str, message: str, **kwargs: Any) -> None:
        self.last_channel = channel
        self.last_message = message


class _SilentChannel:
    """Channel without send_message capability (like CliChannel)."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    async def connect(self) -> None:  # pragma: no cover - trivial
        pass

    async def disconnect(self) -> None:  # pragma: no cover - trivial
        pass

    async def health_check(self) -> bool:  # pragma: no cover - trivial
        return True


# ---------------------------------------------------------------------------
# Unit 1 — channel registration
# ---------------------------------------------------------------------------


class TestChannelRegistration:
    def test_channel_is_registered_under_send_message_capability(self) -> None:
        channel = _FakeChannel()
        agent = Agent(channels=[channel])
        found = agent._registry.find_by_capability(Capability.SEND_MESSAGE)
        assert len(found) == 1
        assert found[0][1] is channel

    def test_connector_and_channel_both_registered(self) -> None:
        conn = _FakeChannel()
        chan = _FakeChannel()
        agent = Agent(connectors=[conn], channels=[chan])
        found = agent._registry.find_by_capability(Capability.SEND_MESSAGE)
        assert len(found) == 2
        assert {id(c) for _, c in found} == {id(conn), id(chan)}

    def test_same_instance_in_both_lists_deduped(self) -> None:
        shared = _FakeChannel()
        agent = Agent(connectors=[shared], channels=[shared])
        found = agent._registry.find_by_capability(Capability.SEND_MESSAGE)
        assert len(found) == 1

    def test_channel_without_send_message_not_returned(self) -> None:
        agent = Agent(channels=[_SilentChannel()])
        found = agent._registry.find_by_capability(Capability.SEND_MESSAGE)
        assert found == []

    @pytest.mark.asyncio
    async def test_workflow_dispatch_reaches_channel(self) -> None:
        channel = _FakeChannel()
        agent = Agent(channels=[channel])
        wf = Workflow(
            name="NotifyViaChannel",
            steps=[
                Step(
                    "notify",
                    action="channels.send_message",
                    template="Alert: {trigger.asset_id}",
                    inputs={"channel": "ops"},
                ),
            ],
        )
        agent.register_workflow(wf)
        result = await agent.trigger_workflow("NotifyViaChannel", {"asset_id": "P-201"})
        assert result.success is True
        assert result.step_results[0].output["sent"] is True
        assert channel.last_channel == "ops"
        assert "P-201" in (channel.last_message or "")


# ---------------------------------------------------------------------------
# Unit 2 — sandbox gates channel connect/disconnect
# ---------------------------------------------------------------------------


class TestSandboxChannelGate:
    @pytest.mark.asyncio
    async def test_sandbox_skips_channel_connect(self) -> None:
        channel = _FakeChannel()
        agent = Agent(channels=[channel], sandbox=True)
        await agent.start()
        assert channel.connected is False

    @pytest.mark.asyncio
    async def test_live_mode_connects_channel(self) -> None:
        channel = _FakeChannel()
        agent = Agent(channels=[channel], sandbox=False)
        await agent.start()
        assert channel.connected is True

    @pytest.mark.asyncio
    async def test_sandbox_skips_channel_disconnect(self) -> None:
        channel = _FakeChannel()
        agent = Agent(channels=[channel], sandbox=True)
        await agent.start()
        await agent.stop()
        assert channel.disconnected is False

    @pytest.mark.asyncio
    async def test_channel_not_double_connected(self) -> None:
        """Channel registered + in _channels list must connect only once."""
        channel = _FakeChannel()
        call_count = 0

        async def counting_connect() -> None:
            nonlocal call_count
            call_count += 1
            channel.connected = True

        channel.connect = counting_connect  # type: ignore[method-assign]
        agent = Agent(channels=[channel], sandbox=False)
        await agent.start()
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_channel_not_double_disconnected(self) -> None:
        channel = _FakeChannel()
        call_count = 0

        async def counting_disconnect() -> None:
            nonlocal call_count
            call_count += 1

        channel.disconnect = counting_disconnect  # type: ignore[method-assign]
        agent = Agent(channels=[channel], sandbox=False)
        await agent.start()
        await agent.stop()
        assert call_count == 1

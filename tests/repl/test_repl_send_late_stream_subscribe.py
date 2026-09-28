"""``send()`` must not lose an approval prompt to a late ``/stream`` subscribe. The
stream has no replay, so these tests use a fake client whose stream attaches late or
never and check that the POST waits for the ack and snapshot prompts surface once."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from omnigent_client import StreamHooks
from omnigent_client._events import ElicitationRequest
from omnigent_client._sessions import Session

from omnigent.repl import _repl
from omnigent.repl._repl import _server_event_to_sdk_event, _SessionsChatReplAdapter
from omnigent.server.schemas import SessionHeartbeatEvent

_SESSION_ID = "conv_abc"
_ELICITATION_ID = "elicit_late"
_FAST_GRACE_S = 0.5


@pytest.fixture(autouse=True)
def fast_subscribe_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the hung-connect cap short so never-attaching fakes finish quickly."""
    monkeypatch.setattr(_repl, "_STREAM_SUBSCRIBE_GRACE_S", _FAST_GRACE_S, raising=False)


def _pending_prompt() -> dict[str, Any]:
    """The ``response.elicitation_request`` payload a snapshot replays."""
    return {
        "type": "response.elicitation_request",
        "elicitation_id": _ELICITATION_ID,
        "params": {
            "mode": "form",
            "message": "Confirm this message before I process it.",
            "requestedSchema": {"type": "object", "properties": {}},
            "phase": "request",
            "policy_name": "always_ask_on_input",
        },
    }


def _snapshot(status: str, pending: list[dict[str, Any]]) -> Session:
    return Session.from_dict(
        {
            "id": _SESSION_ID,
            "agent_id": "ag_ask_demo",
            "status": status,
            "created_at": 0,
            "pending_elicitations": pending,
        }
    )


async def _never_attached(_session_id: str) -> AsyncIterator[object]:
    """A subscribe request that never reaches the server."""
    await asyncio.Event().wait()
    yield  # pragma: no cover - keeps this an async generator


def _make_adapter(client: MagicMock, hook: AsyncMock | None = None) -> _SessionsChatReplAdapter:
    return _SessionsChatReplAdapter(
        client=client,
        agent_name="ask-demo",
        hooks=StreamHooks(on_elicitation_request=hook),
        session_id=_SESSION_ID,
        attach_only=True,
    )


async def _drain(turn: AsyncIterator[object]) -> None:
    async for _ in turn:
        pass


@pytest.mark.parametrize(
    "start_turn",
    [
        pytest.param(lambda adapter: adapter.send("Hello there"), id="message"),
        pytest.param(
            lambda adapter: adapter.send_skill_slash_command("review", ""),
            id="skill-slash-command",
        ),
    ],
)
async def test_send_surfaces_prompt_the_stream_never_delivered(
    start_turn: Callable[[_SessionsChatReplAdapter], AsyncIterator[object]],
) -> None:
    """A prompt parked in the snapshot reaches the approval hook once, even
    though it stays in every later snapshot until answered."""
    client = MagicMock()
    client.sessions.stream = _never_attached
    client.sessions.post_event = AsyncMock()
    client.sessions.resolve_elicitation = AsyncMock()
    client.sessions.get = AsyncMock(
        side_effect=[
            _snapshot("idle", []),  # hydration on first send
            _snapshot("running", [_pending_prompt()]),
            _snapshot("running", [_pending_prompt()]),
            _snapshot("idle", []),
        ]
    )
    hook = AsyncMock(return_value=True)
    adapter = _make_adapter(client, hook)
    elicitation_tasks: list[asyncio.Task[None]] = []

    def render(event: object) -> None:
        # The elicitation branch of run_repl's push renderer.
        sdk_event = _server_event_to_sdk_event(event)
        if isinstance(sdk_event, ElicitationRequest):
            elicitation_tasks.append(
                asyncio.create_task(adapter._handle_elicitation(_SESSION_ID, sdk_event))
            )

    adapter._on_event = render
    try:
        await asyncio.wait_for(_drain(start_turn(adapter)), timeout=15)
        await asyncio.gather(*elicitation_tasks)
    finally:
        await adapter.aclose()

    assert hook.await_count == 1, "prompt parked before the subscription must surface once"
    ctx = hook.await_args.args[0]
    assert ctx.elicitation_id == _ELICITATION_ID
    assert ctx.policy_name == "always_ask_on_input"
    client.sessions.resolve_elicitation.assert_awaited_once()
    session_id, elicitation_id, verdict = client.sessions.resolve_elicitation.await_args.args
    assert (session_id, elicitation_id, verdict["action"]) == (
        _SESSION_ID,
        _ELICITATION_ID,
        "accept",
    )


async def test_send_waits_for_the_subscription_ack_before_posting() -> None:
    """The message is posted only after the server acks the subscriber."""
    order: list[str] = []

    async def acks_after_delay(_session_id: str) -> AsyncIterator[object]:
        await asyncio.sleep(0.2)
        order.append("subscribed")
        yield SessionHeartbeatEvent.model_validate({"type": "session.heartbeat"})
        await asyncio.Event().wait()

    client = MagicMock()
    client.sessions.stream = acks_after_delay
    client.sessions.post_event = AsyncMock(side_effect=lambda *_a, **_k: order.append("post"))
    client.sessions.get = AsyncMock(return_value=_snapshot("idle", []))
    adapter = _make_adapter(client)
    try:
        await asyncio.wait_for(_drain(adapter.send("Hello there")), timeout=15)
    finally:
        await adapter.aclose()

    assert order == ["subscribed", "post"]


async def test_send_posts_without_waiting_when_the_subscribe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect failure settles the wait immediately instead of running out the cap."""
    cap_s = 5.0
    monkeypatch.setattr(_repl, "_STREAM_SUBSCRIBE_GRACE_S", cap_s, raising=False)

    async def refuses_connection(_session_id: str) -> AsyncIterator[object]:
        raise httpx.ConnectError("connection refused")
        yield  # pragma: no cover - keeps this an async generator

    client = MagicMock()
    client.sessions.stream = refuses_connection
    client.sessions.post_event = AsyncMock()
    client.sessions.get = AsyncMock(return_value=_snapshot("idle", []))
    adapter = _make_adapter(client)
    started = time.monotonic()
    try:
        await asyncio.wait_for(_drain(adapter.send("Hello there")), timeout=15)
    finally:
        await adapter.aclose()

    client.sessions.post_event.assert_awaited_once()
    assert time.monotonic() - started < cap_s / 2


async def test_send_still_posts_when_no_subscription_ack_arrives() -> None:
    """A hung subscribe gives up after the grace cap rather than stalling the turn."""
    client = MagicMock()
    client.sessions.stream = _never_attached
    client.sessions.post_event = AsyncMock()
    client.sessions.get = AsyncMock(return_value=_snapshot("idle", []))
    adapter = _make_adapter(client)
    try:
        await asyncio.wait_for(_drain(adapter.send("Hello there")), timeout=15)
    finally:
        await adapter.aclose()

    client.sessions.post_event.assert_awaited_once()


async def test_handle_elicitation_resolves_a_repeated_prompt_once() -> None:
    """The same prompt arriving live and via a snapshot yields one verdict."""
    client = MagicMock()
    client.sessions.resolve_elicitation = AsyncMock()
    adapter = _make_adapter(client, AsyncMock(return_value=True))
    event = SimpleNamespace(
        elicitation_id=_ELICITATION_ID,
        message="approve?",
        requested_schema={},
        mode="form",
        phase="request",
        policy_name="always_ask_on_input",
        content_preview="",
        url=None,
    )

    await adapter._handle_elicitation(_SESSION_ID, event)
    await adapter._handle_elicitation(_SESSION_ID, event)

    client.sessions.resolve_elicitation.assert_awaited_once()


async def test_send_ignores_snapshots_without_a_usable_prompt() -> None:
    """Snapshots lacking the field, or carrying junk, never reach the renderer."""
    client = MagicMock()
    client.sessions.stream = _never_attached
    client.sessions.post_event = AsyncMock()
    client.sessions.get = AsyncMock(
        side_effect=[
            _snapshot("idle", []),  # hydration on first send
            SimpleNamespace(status="running"),
            _snapshot("running", [{"type": "response.elicitation_request"}]),
            _snapshot("idle", []),
        ]
    )
    adapter = _make_adapter(client)
    rendered: list[object] = []
    adapter._on_event = rendered.append
    try:
        await asyncio.wait_for(_drain(adapter.send("Hello there")), timeout=15)
    finally:
        await adapter.aclose()

    assert rendered == []

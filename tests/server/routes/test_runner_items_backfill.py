"""Tests for runner items backfill on relay reconnect after a mid-turn restart.

The runner's ``GET /v1/sessions/{id}`` returns its harness LLM-input history
(``_session_histories``): ``message`` entries shaped ``{"type","role","content"}``
and ``function_call`` / ``function_call_output`` entries — NOT store
ConversationItems. The backfill maps those into conversation items and dedups by
CONTENT (the live relay persists items without a stable id, so ids never match).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.db.utils import generate_conversation_id
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.entities.pagination import PagedList
from omnigent.server.routes._sessions.orchestration import (
    _backfill_runner_items_on_reconnect,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _runner_client_returning(items: list[dict[str, Any]]) -> AsyncMock:
    """A fake runner HTTP client whose GET returns *items* as session history."""
    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"items": items}
    fake_runner.get.return_value = response
    return fake_runner


def _sig(item: Any) -> tuple[str, ...]:
    """Content signature of a stored item, mirroring the backfill's own."""
    data = item.data
    if item.type == "message":
        text = "\n".join(
            block.get("text", "")
            for block in getattr(data, "content", [])
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
        )
        return ("message", data.role, text)
    if item.type == "function_call":
        return ("function_call", data.call_id, data.name, data.arguments)
    return ("function_call_output", data.call_id, data.output)


@pytest.mark.asyncio
async def test_backfill_recovers_missing_assistant_message_without_duplicates(
    db_uri: str,
) -> None:
    """
    Store has the user message (persisted the relay way, random id) but is missing
    the assistant response lost during the server-down gap. The runner history has
    both. Backfill appends ONLY the assistant message; the user message is not
    duplicated.
    """
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)
    store.create_conversation(kind="default", conversation_id=session_id)

    # Pre-seed the way the live relay does: NO stable_id -> random store id.
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hello there"}],
                ),
            )
        ],
    )

    runner_history = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello there"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "general kenobi"}],
        },
    ]

    await _backfill_runner_items_on_reconnect(
        session_id, _runner_client_returning(runner_history), store
    )

    page = store.list_items(session_id, limit=100, order="asc")
    assert len(page.data) == 2, "user msg must not be duplicated; assistant must be added"
    roles = [item.data.role for item in page.data if item.type == "message"]
    assert roles == ["user", "assistant"]
    assistant = next(i for i in page.data if i.data.role == "assistant")
    # Assistant messages require a non-None agent (validator).
    assert assistant.data.agent
    assert _sig(assistant) == ("message", "assistant", "general kenobi")


@pytest.mark.asyncio
async def test_backfill_is_idempotent_across_reconnects(db_uri: str) -> None:
    """Running backfill twice appends the missing item once — no duplicates."""
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)
    store.create_conversation(kind="default", conversation_id=session_id)

    runner_history = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q"}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "a"}],
        },
    ]
    client = _runner_client_returning(runner_history)

    await _backfill_runner_items_on_reconnect(session_id, client, store)
    first = store.list_items(session_id, limit=100, order="asc")
    assert len(first.data) == 2

    await _backfill_runner_items_on_reconnect(session_id, client, store)
    second = store.list_items(session_id, limit=100, order="asc")
    assert len(second.data) == 2, "second reconnect must not re-append"
    assert {_sig(i) for i in first.data} == {_sig(i) for i in second.data}


@pytest.mark.asyncio
async def test_backfill_resolves_assistant_agent_from_existing_item(db_uri: str) -> None:
    """
    A recovered assistant message inherits the agent from the newest existing
    assistant item so the message-data validator is satisfied with the real agent.
    """
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)
    store.create_conversation(kind="default", conversation_id=session_id)

    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="assistant",
                    agent="claude-sonnet",
                    content=[{"type": "output_text", "text": "earlier reply"}],
                ),
            )
        ],
    )

    runner_history = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "earlier reply"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "new lost reply"}],
        },
    ]

    await _backfill_runner_items_on_reconnect(
        session_id, _runner_client_returning(runner_history), store
    )

    page = store.list_items(session_id, limit=100, order="asc")
    assert len(page.data) == 2
    recovered = next(i for i in page.data if _sig(i) == ("message", "assistant", "new lost reply"))
    assert recovered.data.agent == "claude-sonnet"


@pytest.mark.asyncio
async def test_backfill_recovers_tool_call_and_output_grouped(db_uri: str) -> None:
    """function_call and its function_call_output are recovered under one response id."""
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)
    store.create_conversation(kind="default", conversation_id=session_id)

    runner_history = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "search please"}],
        },
        {"type": "function_call", "call_id": "c1", "name": "search.web", "arguments": '{"q":"x"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "results"},
    ]

    await _backfill_runner_items_on_reconnect(
        session_id, _runner_client_returning(runner_history), store
    )

    page = store.list_items(session_id, limit=100, order="asc")
    call = next(i for i in page.data if i.type == "function_call")
    output = next(i for i in page.data if i.type == "function_call_output")
    assert call.data.call_id == output.data.call_id == "c1"
    assert call.response_id == output.response_id
    assert call.response_id.startswith("recovery_")


@pytest.mark.asyncio
async def test_backfill_skips_unmappable_history_item(db_uri: str, caplog: Any) -> None:
    """Unknown history entry types are skipped; valid items still recovered."""
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)
    store.create_conversation(kind="default", conversation_id=session_id)

    runner_history = [
        {"type": "reasoning", "summary": "internal only"},  # unmappable
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "kept"}],
        },
    ]

    await _backfill_runner_items_on_reconnect(
        session_id, _runner_client_returning(runner_history), store
    )

    page = store.list_items(session_id, limit=100, order="asc")
    assert len(page.data) == 1
    assert _sig(page.data[0]) == ("message", "assistant", "kept")
    assert any(
        "Skipping unmappable runner history item" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


@pytest.mark.asyncio
async def test_backfill_no_items_early_return(db_uri: str) -> None:
    """Empty runner history returns early without touching the store."""
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)
    store.create_conversation(kind="default", conversation_id=session_id)

    await _backfill_runner_items_on_reconnect(session_id, _runner_client_returning([]), store)
    assert len(store.list_items(session_id, limit=100).data) == 0


@pytest.mark.asyncio
async def test_backfill_best_effort_on_runner_http_error(db_uri: str, caplog: Any) -> None:
    """A non-200 from the runner is logged and does not raise."""
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)
    store.create_conversation(kind="default", conversation_id=session_id)

    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 500
    fake_runner.get.return_value = response

    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    assert any(
        "Failed to fetch runner items" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )
    assert len(store.list_items(session_id, limit=100).data) == 0


@pytest.mark.asyncio
async def test_backfill_best_effort_on_runner_network_error(db_uri: str, caplog: Any) -> None:
    """A network exception fetching runner items is logged and does not raise."""
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)
    store.create_conversation(kind="default", conversation_id=session_id)

    fake_runner = AsyncMock()
    fake_runner.get.side_effect = httpx.ConnectError("boom")

    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    assert any("Item backfill" in r.message for r in caplog.records if r.levelname == "WARNING")


@pytest.mark.asyncio
async def test_backfill_best_effort_on_store_list_failure(caplog: Any) -> None:
    """A store list_items failure skips reconciliation without raising."""
    session_id = generate_conversation_id()
    store = MagicMock()
    store.list_items.side_effect = RuntimeError("db down")

    runner_history = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "x"}],
        },
    ]

    await _backfill_runner_items_on_reconnect(
        session_id, _runner_client_returning(runner_history), store
    )

    assert any(
        "Failed to fetch existing items" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )
    store.append.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_best_effort_on_store_append_failure(caplog: Any) -> None:
    """A store append failure is logged and does not raise."""
    session_id = generate_conversation_id()
    store = MagicMock()
    store.list_items.return_value = PagedList(data=[], first_id=None, last_id=None, has_more=False)
    store.append.side_effect = RuntimeError("append failed")

    runner_history = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "x"}],
        },
    ]

    await _backfill_runner_items_on_reconnect(
        session_id, _runner_client_returning(runner_history), store
    )

    assert any(
        "Failed to append backfilled items" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )

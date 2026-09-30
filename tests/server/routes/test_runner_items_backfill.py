"""Tests for runner items backfill on relay reconnect."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.db.utils import generate_conversation_id
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.routes._sessions.orchestration import (
    _backfill_runner_items_on_reconnect,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.mark.asyncio
async def test_backfill_appends_missing_runner_items_to_store(
    db_uri: str,
) -> None:
    """
    Runner holds items the store is missing (e.g., from a server downtime gap)
    → backfill appends them to the conversation store.

    When the server crashes mid-turn, the relay subscription may have missed SSE
    events emitted while the server was down. On reconnect, the backfill function
    should fetch items from the runner and append any that the store is missing.
    """
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)

    # Set up the conversation
    store.create_conversation(kind="default", conversation_id=session_id)

    # Use proper 32-char hex strings for stable IDs
    stable_id_1 = "01234567890abcdef01234567890abcd"
    stable_id_2 = "11234567890abcdef01234567890abcd"

    # Create some items to pre-seed the store
    initial_items = [
        NewConversationItem(
            type="message",
            response_id="resp_1",
            data=MessageData(
                role="user",
                content=[{"type": "input_text", "text": "initial message"}],
            ),
            stable_id=stable_id_1,
        ),
    ]
    store.append(session_id, initial_items)

    # Verify initial state
    initial_page = store.list_items(session_id, limit=100)
    assert len(initial_page.data) == 1
    assert initial_page.data[0].id == stable_id_1

    # Create a fake runner client that returns items, including one the store
    # already has and one new one
    runner_items = [
        {
            "id": stable_id_1,
            "type": "message",
            "response_id": "resp_1",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "initial message"}],
            },
            "created_by": None,
        },
        {
            "id": stable_id_2,
            "type": "message",
            "response_id": "resp_2",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "new item during downtime"}],
            },
            "created_by": None,
        },
    ]

    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"items": runner_items}
    fake_runner.get.return_value = response

    # Run backfill
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Verify that the new item was appended
    final_page = store.list_items(session_id, limit=100, order="asc")
    assert len(final_page.data) == 2
    # Find the new item
    new_item = next(item for item in final_page.data if item.id == stable_id_2)
    assert new_item.type == "message"
    assert new_item.response_id == "resp_2"

    # Verify the runner was called with the correct URL
    fake_runner.get.assert_called_once()
    call_args = fake_runner.get.call_args
    assert f"/v1/sessions/{session_id}" in call_args[0]
    assert call_args[1]["timeout"] == 5.0


@pytest.mark.asyncio
async def test_backfill_idempotency_no_duplicate_items_on_retry(
    db_uri: str,
) -> None:
    """
    Running backfill twice (or when items already exist) does NOT create duplicates.

    Items are deduplicated by stable_id: the runner's item id is used as stable_id,
    so repeated backfills or items already in the store are not re-inserted.
    """
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)

    # Set up the conversation
    store.create_conversation(kind="default", conversation_id=session_id)

    # Runner items that will be returned on both calls
    stable_id_x = "21234567890abcdef01234567890abcd"
    runner_items = [
        {
            "id": stable_id_x,
            "type": "message",
            "response_id": "resp_x",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "message x"}],
            },
            "created_by": None,
        },
    ]

    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"items": runner_items}
    fake_runner.get.return_value = response

    # First backfill
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Verify item was added
    page1 = store.list_items(session_id, limit=100)
    assert len(page1.data) == 1

    # Second backfill (simulating a retry)
    fake_runner.reset_mock()
    fake_runner.get.return_value = response
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Verify item was NOT duplicated
    page2 = store.list_items(session_id, limit=100)
    assert len(page2.data) == 1
    assert page2.data[0].id == stable_id_x


@pytest.mark.asyncio
async def test_backfill_best_effort_handles_runner_fetch_failure(
    db_uri: str,
    caplog: Any,
) -> None:
    """
    Runner fetch failure: backfill logs and does NOT raise (best-effort recovery).

    If the runner fetch fails (connection error, timeout, HTTP error), the backfill
    should handle it gracefully without breaking the reconnect flow.
    """
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)

    # Set up the conversation
    store.create_conversation(kind="default", conversation_id=session_id)

    # Create a fake runner that returns an HTTP error
    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 500
    fake_runner.get.return_value = response

    # Backfill should not raise, even with a 500 error
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Verify warning was logged
    assert any(
        "Failed to fetch runner items" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )

    # Store should remain empty (no items added on failure)
    page = store.list_items(session_id, limit=100)
    assert len(page.data) == 0


@pytest.mark.asyncio
async def test_backfill_best_effort_handles_exception_in_runner_get(
    db_uri: str,
    caplog: Any,
) -> None:
    """
    Runner HTTP exception (e.g., network error): backfill logs and does NOT raise.

    Network failures during the runner fetch should not break the reconnect.
    """
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)

    # Set up the conversation
    store.create_conversation(kind="default", conversation_id=session_id)

    # Create a fake runner that raises an exception
    fake_runner = AsyncMock()
    fake_runner.get.side_effect = httpx.ConnectError("Connection failed")

    # Backfill should not raise, even with a network error
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Verify warning was logged
    assert any(
        "Item backfill" in record.message or "backfill" in record.message.lower()
        for record in caplog.records
        if record.levelname == "WARNING"
    )

    # Store should remain empty
    page = store.list_items(session_id, limit=100)
    assert len(page.data) == 0


@pytest.mark.asyncio
async def test_backfill_handles_malformed_runner_items(
    db_uri: str,
    caplog: Any,
) -> None:
    """
    Malformed runner items (missing required fields) are skipped with a warning.

    Items that cannot be deserialized should be skipped, but backfill should
    continue and append valid items.
    """
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)

    # Set up the conversation
    store.create_conversation(kind="default", conversation_id=session_id)

    # Mix of valid and malformed items
    stable_id_valid = "31234567890abcdef01234567890abcd"
    stable_id_invalid = "41234567890abcdef01234567890abcd"
    stable_id_valid2 = "51234567890abcdef01234567890abcd"

    runner_items = [
        {
            "id": stable_id_valid,
            "type": "message",
            "response_id": "resp_valid",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "valid"}],
            },
            "created_by": None,
        },
        {
            "id": stable_id_invalid,
            # Missing "type" field - this will fail deserialization
            "response_id": "resp_invalid",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "invalid"}],
            },
            "created_by": None,
        },
    ]

    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"items": runner_items}
    fake_runner.get.return_value = response

    # Run backfill
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Verify that only the valid item was appended (1 valid item)
    page = store.list_items(session_id, limit=100)
    assert len(page.data) == 1

    # Find the valid item
    item_ids = {item.id for item in page.data}
    assert stable_id_valid in item_ids
    assert stable_id_invalid not in item_ids

    # Verify warning was logged for malformed item
    assert any(
        "Failed to deserialize runner item" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


@pytest.mark.asyncio
async def test_backfill_no_items_early_return(
    db_uri: str,
) -> None:
    """
    Runner returns empty items list → backfill returns early without error.
    """
    session_id = generate_conversation_id()
    store = SqlAlchemyConversationStore(db_uri)

    # Set up the conversation
    store.create_conversation(kind="default", conversation_id=session_id)

    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"items": []}
    fake_runner.get.return_value = response

    # Backfill should return early without error
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Store should remain empty
    page = store.list_items(session_id, limit=100)
    assert len(page.data) == 0


@pytest.mark.asyncio
async def test_backfill_handles_store_list_failure(
    db_uri: str,
    caplog: Any,
) -> None:
    """
    Store failure during list_items: backfill skips reconciliation with a warning.

    If we cannot fetch existing items from the store, we should skip the
    reconciliation rather than proceeding with incomplete deduplication.
    """
    session_id = generate_conversation_id()
    store = AsyncMock()

    # Mock the store to raise on list_items
    store.list_items.side_effect = RuntimeError("Database error")

    runner_items = [
        {
            "id": "item_should_skip",
            "type": "message",
            "response_id": "resp_1",
            "data": {"role": "user", "content": "msg"},
            "created_by": None,
        },
    ]

    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"items": runner_items}
    fake_runner.get.return_value = response

    # Backfill should not raise, even if store fails
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Verify warning was logged
    assert any(
        "Failed to fetch existing items" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


@pytest.mark.asyncio
async def test_backfill_handles_store_append_failure(
    db_uri: str,
    caplog: Any,
) -> None:
    """
    Store failure during append: backfill logs and does NOT raise.

    If the append fails, we should log a warning but not break reconnect.
    """
    session_id = generate_conversation_id()
    store = AsyncMock()

    # Mock list_items to succeed, but append to fail
    from omnigent.entities.pagination import PagedList
    from unittest.mock import MagicMock

    # list_items is synchronous, so use MagicMock instead of AsyncMock
    mock_store = MagicMock()
    mock_store.list_items.return_value = PagedList(data=[], first_id=None, last_id=None)
    mock_store.append.side_effect = RuntimeError("Database error during append")
    store = mock_store

    stable_id_append_fail = "61234567890abcdef01234567890abcd"
    runner_items = [
        {
            "id": stable_id_append_fail,
            "type": "message",
            "response_id": "resp_1",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "msg"}],
            },
            "created_by": None,
        },
    ]

    fake_runner = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"items": runner_items}
    fake_runner.get.return_value = response

    # Backfill should not raise
    await _backfill_runner_items_on_reconnect(session_id, fake_runner, store)

    # Verify warning was logged
    assert any(
        "Failed to append backfilled items" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )

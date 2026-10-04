"""Runner-scoped stop intent, rollback, and sessions without a local relay."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from omnigent.runtime import session_stream
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import ErrorDetail
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_RUNNER = "runner-stopped"


@pytest.fixture
def family(db_uri: str) -> Iterator[tuple[SqlAlchemyConversationStore, dict[str, str]]]:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    ids = {"parent": parent.id}
    store.set_runner_id(parent.id, _RUNNER)
    for name, status in (
        ("active", "running"),
        ("cold", "waiting"),
        ("finished", "idle"),
        ("failed", "failed"),
        ("elsewhere", "running"),
        ("grandchild", "running"),
    ):
        row = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=ids["active"] if name == "grandchild" else parent.id,
        )
        ids[name] = row.id
        store.set_runner_id(row.id, "runner-other" if name == "elsewhere" else _RUNNER)
        store.set_session_live_status(row.id, status)
        if name != "cold":
            sessions._session_status_cache[row.id] = status
    store.set_labels(ids["failed"], {"omnigent.last_task_error_code": "native_turn_error"})
    try:
        yield store, ids
    finally:
        for session_id in ids.values():
            sessions._intentional_stop_sessions.discard(session_id)
            sessions._session_status_cache.pop(session_id, None)
            sessions._runner_relay_tasks.pop(session_id, None)
            session_stream.close(session_id)


@pytest.mark.parametrize("outcome", ["delivered", "offline", "error", "cancelled"])
async def test_stop_marks_only_affected_active_sessions_and_rolls_back(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    store, ids = family
    expected = {ids[name] for name in ("parent", "active", "cold", "grandchild")}

    async def teardown(*_args):
        assert set(sessions._intentional_stop_sessions) == expected
        if outcome == "error":
            raise RuntimeError("host stop failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return outcome == "delivered"

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    call = orchestration._stop_host_runner_intentionally(
        ids["parent"], "host", _RUNNER, None, store
    )
    if outcome == "error":
        with pytest.raises(RuntimeError, match="host stop failed"):
            await call
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        assert await call is (outcome == "delivered")
    assert set(sessions._intentional_stop_sessions) == (
        expected if outcome == "delivered" else set()
    )
    assert (
        store.get_conversation(ids["failed"]).labels["omnigent.last_task_error_code"]
        == "native_turn_error"
    )
    assert sessions._session_status_cache[ids["elsewhere"]] == "running"


async def test_disconnect_sweep_settles_children_without_relays_and_consumes_intent(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ids = family

    async def teardown(*_args):
        return True

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    assert await orchestration._stop_host_runner_intentionally(
        ids["parent"], "host", _RUNNER, None, store
    )
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    await sessions._mark_runner_sessions_offline(
        store.list_conversations_by_runner_id(_RUNNER), error, store
    )
    for name in ("active", "cold", "grandchild"):
        assert sessions._session_status_cache[ids[name]] == "idle"
        assert (
            sessions._last_task_error_from_labels(store.get_conversation(ids[name]).labels) is None
        )
    assert not set(sessions._intentional_stop_sessions)
    assert sessions._session_status_cache[ids["finished"]] == "idle"
    assert sessions._session_status_cache[ids["failed"]] == "failed"

    # A later active turn losing its runner must still report the genuine failure.
    sessions._session_status_cache[ids["active"]] = "running"
    await sessions._mark_runner_sessions_offline(
        [store.get_conversation(ids["active"])], error, store
    )
    assert sessions._session_status_cache[ids["active"]] == "failed"
    assert (
        sessions._last_task_error_from_labels(store.get_conversation(ids["active"]).labels)["code"]
        == "runner_disconnected"
    )


async def test_stop_uses_live_relay_binding_when_row_lookup_fails(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ids = family
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    sessions._runner_relay_tasks[ids["cold"]] = sessions._RelayHandle(_RUNNER, task, gate)
    sessions._runner_relay_tasks[ids["elsewhere"]] = sessions._RelayHandle(
        "runner-other", task, gate
    )

    def unavailable(_runner_id):
        raise RuntimeError("store temporarily unavailable")

    async def teardown(*_args):
        assert set(sessions._intentional_stop_sessions) == {ids["parent"], ids["cold"]}
        return False

    monkeypatch.setattr(store, "list_conversations_by_runner_id", unavailable)
    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    try:
        assert not await orchestration._stop_host_runner_intentionally(
            ids["parent"], "host", _RUNNER, None, store
        )
        assert not set(sessions._intentional_stop_sessions)
    finally:
        gate.set()
        await task

"""Runner-scoped stop intent, rollback, and sessions without a local relay."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from omnigent.runtime import session_stream
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import common, orchestration
from omnigent.server.schemas import ErrorDetail
from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_RUNNER = "runner-stopped"


@pytest.fixture
def family(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SqlAlchemyConversationStore, dict[str, str]]]:
    monkeypatch.setattr(orchestration, "_RUNNER_STOP_STATUS_BATCH_SIZE", 2)
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
            sessions._intentional_stop_sessions.pop(session_id, None)
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


async def test_sweep_leaves_intent_for_a_matching_live_relay(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
) -> None:
    store, ids = family
    child_id = ids["active"]
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    sessions._runner_relay_tasks[child_id] = sessions._RelayHandle(_RUNNER, task, gate)
    sessions._intentional_stop_sessions[child_id] = _RUNNER
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    try:
        await sessions._mark_runner_sessions_offline(
            [store.get_conversation(child_id)], error, store
        )
        assert sessions._intentional_stop_sessions.get(child_id) == _RUNNER
        assert sessions._session_status_cache[child_id] == "running"
        assert (
            sessions._last_task_error_from_labels(store.get_conversation(child_id).labels) is None
        )
        gate.set()
        await task
        await sessions._mark_runner_sessions_offline(
            [store.get_conversation(child_id)], error, store
        )
        assert child_id not in sessions._intentional_stop_sessions
        assert sessions._session_status_cache[child_id] == "idle"
    finally:
        gate.set()
        await task


@pytest.mark.parametrize("delivered", [True, False])
async def test_stop_uses_live_relay_binding_when_row_lookup_fails(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    delivered: bool,
) -> None:
    store, ids = family
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    sessions._runner_relay_tasks[ids["cold"]] = sessions._RelayHandle(_RUNNER, task, gate)
    sessions._runner_relay_tasks[ids["elsewhere"]] = sessions._RelayHandle(
        "runner-other", task, gate
    )

    def unavailable(_runner_id, **_kwargs):
        raise RuntimeError("store temporarily unavailable")

    async def teardown(*_args):
        assert set(sessions._intentional_stop_sessions) == {ids["parent"], ids["cold"]}
        return delivered

    monkeypatch.setattr(store, "list_runner_session_statuses", unavailable)
    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    try:
        result = await orchestration._stop_host_runner_intentionally(
            ids["parent"], "host", _RUNNER, None, store
        )
        assert result is delivered
        assert set(sessions._intentional_stop_sessions) == (
            {ids["parent"], ids["cold"]} if delivered else set()
        )
    finally:
        gate.set()
        await task


async def test_unconsumed_stop_expires_before_a_later_disconnect(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ids = family
    now = time.monotonic()
    monkeypatch.setattr(common, "time", SimpleNamespace(monotonic=lambda: now))

    async def teardown(*_args):
        return True

    monkeypatch.setattr(sessions, "_stop_session_host_runner", teardown)
    assert await orchestration._stop_host_runner_intentionally(
        ids["parent"], "host", _RUNNER, None, store
    )
    # No local relay or sweep consumes these markers after the runner moves away.
    now += RUNNER_LIVENESS_TTL_S + 1
    assert sessions._intentional_stop_sessions.get(ids["cold"]) == _RUNNER
    now += RUNNER_LIVENESS_TTL_S
    assert ids["cold"] not in sessions._intentional_stop_sessions

    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    await sessions._mark_runner_sessions_offline(
        [store.get_conversation(ids["cold"])], error, store
    )
    assert sessions._session_status_cache[ids["cold"]] == "failed"
    assert (
        sessions._last_task_error_from_labels(store.get_conversation(ids["cold"]).labels)["code"]
        == "runner_disconnected"
    )


@pytest.mark.parametrize("stopped_runner", [_RUNNER, "runner-replacement"])
async def test_old_runner_sweep_preserves_rebound_session(
    family: tuple[SqlAlchemyConversationStore, dict[str, str]],
    stopped_runner: str,
) -> None:
    store, ids = family
    child_id = ids["active"]
    old_row = store.get_conversation(child_id)
    store.replace_runner_id(child_id, "runner-replacement")
    sessions._intentional_stop_sessions[child_id] = stopped_runner
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    sessions._runner_relay_tasks[child_id] = sessions._RelayHandle(
        "runner-replacement", task, gate
    )
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    try:
        await sessions._mark_runner_sessions_offline([old_row], error, store)
        assert sessions._session_status_cache[child_id] == "running"
        assert (
            sessions._last_task_error_from_labels(store.get_conversation(child_id).labels) is None
        )
        if stopped_runner == _RUNNER:
            assert child_id not in sessions._intentional_stop_sessions
            # The old stop must not suppress a real crash of the replacement.
            await sessions._mark_runner_sessions_offline(
                [store.get_conversation(child_id)], error, store
            )
            assert sessions._session_status_cache[child_id] == "failed"
        else:
            assert sessions._intentional_stop_sessions.get(child_id) == "runner-replacement"
    finally:
        gate.set()
        await task

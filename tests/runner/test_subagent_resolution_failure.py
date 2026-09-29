"""An unresolved child must fail before dispatch and remain retryable after repair."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.test_runner_dispatch import (
    _CONTRACT_ADAPTERS,
    _INSTRUCTION_WARN_CHUNKS,
    _await_bg_turn_task,
    _contract_root_spec,
    _contract_run_background,
    _ContractSnapshotClient,
    _FakeProcessManager,
    _RecordingHarnessClient,
    _runner_test_client,
)


class _RecordingManager(_FakeProcessManager):
    def __init__(self, harness: Any) -> None:
        super().__init__(harness)
        self.spawns: list[tuple[str, str]] = []

    async def get_client(
        self, conversation_id: str, harness_name: str, *, env: dict[str, str] | None = None
    ) -> Any:
        self.spawns.append((conversation_id, harness_name))
        return await super().get_client(conversation_id, harness_name, env=env)


def _statuses(app: Any, session_id: str) -> list[dict[str, Any]]:
    queue = app.state.session_event_queues[session_id]
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.get("type") == "session.status":
            events.append(event)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id_in_body", [False, True])
async def test_cold_child_resolution_survives_multiple_turns(agent_id_in_body: bool) -> None:
    """A cached child is already selected, including when create was on another runner."""
    conv = "conv_cold_child"
    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    calls = []

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        calls.append(agent_id)
        return _contract_root_spec(with_child=True)

    app = create_runner_app(
        process_manager=_RecordingManager(recording),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=_ContractSnapshotClient(conv),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        for _ in range(2):
            body: dict[str, Any] = {"type": "message", "role": "user", "content": "hi"}
            if agent_id_in_body:
                body["agent_id"] = "ag_contract_root"
            response = await http.post(f"/v1/sessions/{conv}/events", json=body)
            assert response.status_code == 202
            await _await_bg_turn_task(conv)
            assert _statuses(app, conv)[-1]["status"] == "idle"
    assert len(calls) == 1
    assert len(recording.posted_bodies) == 2
    assert all("Worker instructions." in body["instructions"] for body in recording.posted_bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["background", "known_harness", "no_harness"])
async def test_renamed_child_fails_notifies_parent_and_recovers(path: str) -> None:
    """A bundle rename invalidates an old child instead of substituting its parent."""
    conv = "conv_renamed_child"
    parent = "conv_renamed_parent"
    spec = _contract_root_spec(with_child=True)
    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    manager = _RecordingManager(recording)

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=_ContractSnapshotClient(conv),  # type: ignore[arg-type]
    )
    try:
        async with _runner_test_client(app) as http:
            await _contract_run_background(http, conv, recording)
            assert _statuses(app, conv)[-1]["status"] == "idle"
            assert "Worker instructions." in recording.posted_bodies[-1]["instructions"]

            spec.sub_agents[0].name = "worker_renamed"
            reset = await http.post(
                f"/v1/sessions/{conv}/agent-cache/reset", json={"agent_id": "ag_contract_root"}
            )
            assert reset.status_code == 200
            manager.spawns.clear()
            recording.posted_bodies.clear()
            inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            runner_app._session_inboxes_ref[parent] = inbox
            runner_app.register_subagent_work(
                parent_session_id=parent, child_session_id=conv, agent="worker", title="identity"
            )

            result = await _CONTRACT_ADAPTERS[path](http, conv, recording)
            if path == "background":
                assert result["status"] == 202
            else:
                assert result["status"] == 410
                assert result["error"]["code"] == "sub_agent_unresolved"
                assert "worker" in result["error"]["message"]

            failure = _statuses(app, conv)[-1]
            assert failure["status"] == "failed"
            assert failure["error"]["code"] == "sub_agent_unresolved"
            completion = await asyncio.wait_for(inbox.get(), timeout=2)
            assert completion["status"] == "failed"
            assert "worker" in completion["output"]
            assert not manager.spawns
            assert not recording.posted_bodies
            # Rejected lookups must not leave the parent cached or the turn active.
            resources = await http.get(f"/v1/sessions/{conv}/resources")
            assert resources.status_code == 410
            result = await _CONTRACT_ADAPTERS["known_harness"](http, conv, recording)
            assert result["status"] == 410
            assert not manager.spawns
            assert not recording.posted_bodies

            spec.sub_agents[0].name = "worker"
            await _contract_run_background(http, conv, recording)
            assert _statuses(app, conv)[-1]["status"] == "idle"
            assert "Worker instructions." in recording.posted_bodies[-1]["instructions"]
    finally:
        runner_app.unregister_subagent_work(conv)
        runner_app._session_inboxes_ref.pop(parent, None)

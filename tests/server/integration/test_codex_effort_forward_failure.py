"""Public effort updates preserve applied settings when the native runner refuses them."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.harnesses.codex_native import app_server, bridge
from omnigent.runner import app as runner_app
from omnigent.runner.native_controls import NativeControls, build_native_controls
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.runner.conftest import _build_app_for_spec, _runner_client
from tests.runner.native_helpers import _harness_spec
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


class _CodexClient:
    """Inject catalog failures at the Codex RPC boundary, below both HTTP apps."""

    def __init__(self) -> None:
        self.failure = "missing_default"
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.catalog_entered = asyncio.Event()
        self.release_catalog: asyncio.Event | None = None
        self.catalog_cancelled = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "model/list":
            self.catalog_entered.set()
            if self.release_catalog is not None:
                await self.release_catalog.wait()
            if self.failure == "timeout":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.catalog_cancelled = True
                    raise
            return {
                "result": {
                    "data": [{"id": "gpt-5.4", "supportedReasoningEfforts": []}],
                    "nextCursor": None,
                }
            }
        raise AssertionError(f"Rejected reset must not send {method}")


@dataclass
class _NativeSession:
    session_id: str
    store: SqlAlchemyConversationStore
    bridge_dir: Path
    codex: _CodexClient
    remembered_efforts: dict[str, str]
    runner: httpx.AsyncClient


@pytest.fixture
async def native_session(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_NativeSession]:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "labels": {"omnigent.wrapper": "codex-native-ui"},
            "model_override": "gpt-5.4",
            "reasoning_effort": "xhigh",
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    monkeypatch.setattr(app_server, "_effort_catalog_cache", {})
    monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 0.1)
    bridge_dir = bridge.bridge_dir_for_bridge_id(session_id)
    codex_home = bridge.codex_home_for_bridge_dir(bridge_dir)
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.4"\nmodel_reasoning_effort = "xhigh"\n'
    )
    bridge.write_bridge_state(
        bridge_dir,
        bridge.CodexNativeBridgeState(
            session_id=session_id,
            thread_id="thread_codex",
            socket_path=str(tmp_path / "codex.sock"),
            codex_home=str(codex_home),
        ),
    )
    codex = _CodexClient()
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: codex)
    remembered_efforts: dict[str, str] = {}

    def capture_controls(**kwargs: Any) -> NativeControls:
        nonlocal remembered_efforts
        remembered_efforts = kwargs["_session_reasoning_effort"]
        return build_native_controls(**kwargs)

    monkeypatch.setattr(runner_app, "build_native_controls", capture_controls)
    app, _ = await _build_app_for_spec(_harness_spec("codex-native", model="gpt-5.4"))
    async with _runner_client(app) as runner:
        initialized = await runner.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": agent["id"]}
        )
        assert initialized.status_code == 201, initialized.text
        remembered_efforts[session_id] = "xhigh"
        monkeypatch.setattr(
            "omnigent.server.routes.sessions._get_runner_client", AsyncMock(return_value=runner)
        )
        yield _NativeSession(session_id, store, bridge_dir, codex, remembered_efforts, runner)


async def test_reset_without_current_model_rejected_before_codex_connection(
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsatisfiable native reset is invalid input and must not open a connection."""
    session = native_session
    config = bridge.codex_home_for_bridge_dir(session.bridge_dir) / "config.toml"
    config.write_text('model_reasoning_effort = "xhigh"\n')
    factory = Mock(return_value=session.codex)
    monkeypatch.setattr(app_server, "client_for_transport", factory)

    response = await session.runner.post(
        f"/v1/sessions/{session.session_id}/events", json={"type": "effort_change", "effort": None}
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_input"
    assert "requires a current model" in response.json()["detail"]
    factory.assert_not_called()
    assert session.remembered_efforts[session.session_id] == "xhigh"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "xhigh"


@pytest.mark.parametrize("failure", ["missing_default", "timeout"])
@pytest.mark.parametrize("combined_model_change", [False, True])
async def test_rejected_reset_returns_error_and_preserves_applied_settings(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    failure: str,
    combined_model_change: bool,
) -> None:
    """The public PATCH must expose real runner discovery failures without saving Default."""
    session = native_session
    session.codex.failure = failure
    body = {"reasoning_effort": "default"}
    if combined_model_change:
        body["model_override"] = "gpt-6-sol"

    response = await client.patch(f"/v1/sessions/{session.session_id}", json=body)

    assert response.status_code == 503, response.text
    snapshot = await client.get(f"/v1/sessions/{session.session_id}")
    assert snapshot.json()["reasoning_effort"] == "xhigh"
    assert snapshot.json()["model_override"] == "gpt-5.4"
    assert bridge.read_codex_config_effort(session.bridge_dir) == "xhigh"
    assert bridge.read_codex_config_model(session.bridge_dir) == "gpt-5.4"
    assert session.remembered_efforts[session.session_id] == "xhigh"
    assert [method for method, _ in session.codex.requests] == ["model/list"]
    assert session.codex.catalog_cancelled == (failure == "timeout")


@pytest.mark.parametrize("newer_effort", [None, "high"])
async def test_rejected_reset_preserves_concurrent_selection_and_sibling_settings(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    newer_effort: str | None,
) -> None:
    """A delayed rejection restores only fields still holding this request's values."""
    session = native_session
    monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 5.0)
    session.codex.release_catalog = asyncio.Event()
    pending = asyncio.create_task(
        client.patch(
            f"/v1/sessions/{session.session_id}",
            json={"reasoning_effort": "default", "model_override": "gpt-6-sol"},
        )
    )
    try:
        await asyncio.wait_for(session.codex.catalog_entered.wait(), timeout=5.0)
        newer = await client.patch(
            f"/v1/sessions/{session.session_id}",
            json={
                "reasoning_effort": newer_effort,
                "model_override": "gpt-5.5",
                "cost_control_mode_override": "off",
                "title": "A newer title",
                "silent": True,
            },
        )
        assert newer.status_code == 200, newer.text
    finally:
        session.codex.release_catalog.set()
        response = await asyncio.wait_for(pending, timeout=5.0)

    assert response.status_code == 503, response.text
    saved = session.store.get_conversation(session.session_id)
    assert saved is not None
    assert saved.reasoning_effort == (newer_effort or "xhigh")
    assert saved.model_override == "gpt-5.5"
    assert saved.cost_control_mode_override == "off"
    assert saved.title == "A newer title"


@pytest.mark.parametrize("silent", [False, True])
async def test_offline_or_silent_effort_change_is_saved_for_resume(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    silent: bool,
) -> None:
    """No runner response is different from an explicit refusal by a live runner."""
    if not silent:
        monkeypatch.setattr(
            "omnigent.server.routes.sessions._get_runner_client", AsyncMock(return_value=None)
        )
    response = await client.patch(
        f"/v1/sessions/{native_session.session_id}",
        json={"reasoning_effort": "default", "silent": silent},
    )
    assert response.status_code == 200, response.text
    saved = native_session.store.get_conversation(native_session.session_id)
    assert saved is not None and saved.reasoning_effort is None
    assert native_session.codex.requests == []

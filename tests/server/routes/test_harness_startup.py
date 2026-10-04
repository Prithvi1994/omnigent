"""The harness startup route is owner-only and fails fast for older hosts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    CAP_HARNESS_STARTUP,
    HostHarnessStartupFrame,
    HostHarnessStartupResultFrame,
    HostHelloFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes.harness_startup import (
    create_harness_startup_router,
    request_host_harness_startup,
)
from omnigent.stores.host_store import HostStore

_HOST_ID = "a828988dc0b441fb8d04dad3761773b9"
_URL = f"/v1/hosts/{_HOST_ID}/harnesses/claude-native/startup"
_STARTUP = {
    "harness": "claude-native",
    "command": "claude",
    "command_source": "default",
    "env_var": "OMNIGENT_CLAUDE_PATH",
    "resolved_path": "/usr/local/bin/claude",
    "arg_count": 0,
    "env_vars": None,
}


class _Auth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


def _register(registry: HostRegistry, *capabilities: str) -> HostConnection:
    return registry.register(
        _HOST_ID,
        AsyncMock(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="laptop",
            capabilities=list(capabilities),
        ),
        owner="owner",
    )


@pytest.fixture
def startup_app(db_uri: str) -> tuple[FastAPI, HostRegistry, HostStore]:
    registry = HostRegistry()
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(_HOST_ID, "laptop", "owner")
    app = FastAPI()
    app.include_router(
        create_harness_startup_router(registry, hosts, auth_provider=_Auth()), prefix="/v1"
    )

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    return app, registry, hosts


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    "result,status",
    [
        (HostHarnessStartupResultFrame("", "ok", startup=_STARTUP), 200),
        (HostHarnessStartupResultFrame("", "failed", error="lookup failed"), 502),
        # A malformed reply (no ``env_var``, unknown source) is a host failure.
        (
            HostHarnessStartupResultFrame(
                "", "ok", startup={**_STARTUP, "env_var": None, "command_source": "bogus"}
            ),
            502,
        ),
    ],
)
async def test_owner_gets_harness_startup(
    startup_app, result: HostHarnessStartupResultFrame, status: int
) -> None:
    app, registry, _ = startup_app
    conn = _register(registry, CAP_HARNESS_STARTUP)
    async with _client(app) as client:
        task = asyncio.create_task(client.get(_URL, headers={"x-test-user": "owner"}))
        frame = decode_host_frame(await asyncio.wait_for(conn.outbound_queue.get(), 2))
        assert isinstance(frame, HostHarnessStartupFrame)
        assert frame.harness == "claude-native"
        result.request_id = frame.request_id
        conn.pending_harness_startup[frame.request_id].set_result(result)
        response = await task
    assert response.status_code == status
    if status == 200:
        assert response.json() == _STARTUP
    assert not conn.pending_harness_startup


@pytest.mark.parametrize("user,status", [("stranger", 403), (None, 401)])
async def test_non_owner_does_not_reach_the_host(
    startup_app, user: str | None, status: int
) -> None:
    app, registry, _ = startup_app
    conn = _register(registry, CAP_HARNESS_STARTUP)
    async with _client(app) as client:
        response = await client.get(_URL, headers={"x-test-user": user} if user else {})
    assert response.status_code == status
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize("harness", ["pi-native", "antigravity-native", "opencode-native"])
async def test_unsupported_harness_does_not_reach_the_host(startup_app, harness: str) -> None:
    app, registry, _ = startup_app
    conn = _register(registry, CAP_HARNESS_STARTUP)
    async with _client(app) as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/harnesses/{harness}/startup", headers={"x-test-user": "owner"}
        )
    assert response.status_code == 404
    assert conn.outbound_queue.empty()


async def test_offline_host_is_a_conflict(startup_app) -> None:
    app, _, hosts = startup_app
    hosts.set_offline(_HOST_ID)
    async with _client(app) as client:
        response = await client.get(_URL, headers={"x-test-user": "owner"})
    assert response.status_code == 409


async def test_older_host_fails_fast_without_a_frame(startup_app) -> None:
    app, registry, _ = startup_app
    conn = _register(registry)
    async with _client(app) as client:
        response = await client.get(_URL, headers={"x-test-user": "owner"})
    assert response.status_code == 501
    assert "update the host" in response.json()["detail"]
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize("outcome,status", [("timeout", 504), ("stale", 502)])
async def test_proxy_cleans_up_unanswered_requests(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int
) -> None:
    registry = HostRegistry()
    conn = _register(registry, CAP_HARNESS_STARTUP)
    if outcome == "timeout":
        monkeypatch.setattr(
            "omnigent.server.routes.harness_startup._HARNESS_STARTUP_TIMEOUT_S", 0.01
        )
    else:
        registry.deregister(conn.host_id)
    with pytest.raises(HTTPException) as exc_info:
        await request_host_harness_startup(
            host_registry=registry, host_conn=conn, harness="claude-native"
        )
    assert exc_info.value.status_code == status
    assert conn.pending_harness_startup == {}


async def test_replacing_the_connection_after_sending_fails_fast() -> None:
    """A host reconnecting mid-request fails the old request (502) at once."""
    registry = HostRegistry()
    conn = _register(registry, CAP_HARNESS_STARTUP)
    task = asyncio.create_task(
        request_host_harness_startup(host_registry=registry, host_conn=conn, harness="claude")
    )
    await asyncio.wait_for(conn.outbound_queue.get(), 2)  # the request went out
    _register(registry, CAP_HARNESS_STARTUP)  # the same host reconnects
    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(task, 2)
    assert exc_info.value.status_code == 502
    assert conn.pending_harness_startup == {}


async def test_disconnect_after_sending_fails_fast() -> None:
    """A host dropping mid-request is a lost connection (502) now, not a 504 later."""
    registry = HostRegistry()
    conn = _register(registry, CAP_HARNESS_STARTUP)
    task = asyncio.create_task(
        request_host_harness_startup(host_registry=registry, host_conn=conn, harness="claude")
    )
    await asyncio.wait_for(conn.outbound_queue.get(), 2)  # the request went out
    registry.deregister(conn.host_id)
    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(task, 2)  # well under the 15s timeout
    assert exc_info.value.status_code == 502
    assert conn.pending_harness_startup == {}

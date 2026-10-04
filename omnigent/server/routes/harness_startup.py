"""How a harness launches on a connected host: its binary and base args."""

from __future__ import annotations

import asyncio
import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from omnigent.host.frames import (
    CAP_HARNESS_STARTUP,
    HostHarnessStartupFrame,
    HostHarnessStartupResultFrame,
    encode_host_frame,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores.host_store import HostStore

_HARNESS_STARTUP_TIMEOUT_S = 15.0


class HarnessStartupResponse(BaseModel):
    """The command a harness launch uses on a host, and how many base args it passes.

    :param harness: Canonical harness id, e.g. ``"claude-native"``.
    :param command: Command a launch runs, e.g. ``"claude"`` or
        ``"/opt/bin/claude"``; ``None`` for a harness without a CLI.
    :param command_source: Where *command* comes from: the ``env`` var, the
        host's ``config``, or the harness ``default``.
    :param env_var: Env var that overrides the command, e.g.
        ``"OMNIGENT_CLAUDE_PATH"``.
    :param resolved_path: Executable *command* resolves to on the host, e.g.
        ``"/opt/homebrew/bin/claude"``; ``None`` when it isn't found.
    :param arg_count: How many base launch args the host's config sets; the
        args themselves never leave the host.
    :param env_vars: Names an ``env`` wrapper sets before *command*, e.g.
        ``["FOO"]`` for ``env FOO=1 claude``; ``None`` when there's no wrapper.
    :param reads_config: Whether the harness's launch reads
        ``harness.<name>.command`` / ``args`` from config at all.
    """

    harness: str
    command: str | None = None
    command_source: Literal["env", "config", "default"] | None = None
    env_var: str
    resolved_path: str | None = None
    arg_count: int = 0
    env_vars: list[str] | None = None
    reads_config: bool = False


def create_harness_startup_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the harness startup route, mounted under ``/v1``."""
    router = APIRouter()

    @router.get("/hosts/{host_id}/harnesses/{harness}/startup")
    async def get_harness_startup(
        request: Request, host_id: str, harness: str
    ) -> HarnessStartupResponse:
        """Describe the binary *harness* launches with on a host, and its arg count.

        Read-only; the args themselves stay on the host. The caller must own the host.
        """
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_HARNESS_STARTUP not in conn.hello.capabilities:
            raise HTTPException(
                status_code=501, detail="update the host to see its harness launch settings"
            )
        result = await request_host_harness_startup(
            host_registry=host_registry, host_conn=conn, harness=harness
        )
        if result.status != "ok" or result.startup is None:
            raise HTTPException(
                status_code=502, detail=result.error or "host harness startup lookup failed"
            )
        try:
            return HarnessStartupResponse.model_validate(result.startup)
        except ValidationError as exc:
            # A malformed host reply is a host failure; don't echo its payload.
            raise HTTPException(
                status_code=502, detail="host sent a malformed harness startup reply"
            ) from exc

    return router


async def request_host_harness_startup(
    *, host_registry: HostRegistry, host_conn: HostConnection, harness: str
) -> HostHarnessStartupResultFrame:
    """Request a harness's startup over the host tunnel, with bounded waiting and cleanup."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[HostHarnessStartupResultFrame] = (
        asyncio.get_running_loop().create_future()
    )
    host_conn.pending_harness_startup[request_id] = future
    try:
        host_registry.send_text(
            host_conn,
            encode_host_frame(HostHarnessStartupFrame(request_id=request_id, harness=harness)),
        )
        return await asyncio.wait_for(future, timeout=_HARNESS_STARTUP_TIMEOUT_S)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502, detail=f"host '{host_conn.host_id}' connection lost"
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"host '{host_conn.host_id}' did not report harness startup "
                f"within {_HARNESS_STARTUP_TIMEOUT_S:.0f}s"
            ),
        ) from exc
    finally:
        host_conn.pending_harness_startup.pop(request_id, None)

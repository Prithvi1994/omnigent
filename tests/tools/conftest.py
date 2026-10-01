"""Shared fixtures for tools tests."""

from __future__ import annotations

import socket
import time

import pytest

from omnigent.tools.base import ToolContext


@pytest.fixture()
def tool_ctx() -> ToolContext:
    """
    Dummy :class:`ToolContext` for tool tests that don't
    depend on specific task/agent identity.

    :returns: A :class:`ToolContext` with placeholder IDs.
    """
    return ToolContext(task_id="task_test", agent_id="agent_test")


def _free_port() -> int:
    """Reserve an ephemeral localhost port and return it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_listen(port: int, timeout_s: float = 30.0) -> None:
    """Poll until ``127.0.0.1:port`` accepts a TCP connection."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout_s}s")


@pytest.fixture()
def _no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep loopback traffic direct, off any corporate HTTP(S) proxy.

    CI sandboxes export ``HTTP_PROXY``/``HTTPS_PROXY``; httpx honors
    them even for 127.0.0.1, which would route local MCP test traffic
    through the proxy and distort the failure mode under test.
    """
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

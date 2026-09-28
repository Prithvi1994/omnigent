"""REPL approval prompt must survive a slow ``/stream`` subscribe: attach through a
proxy that delays only the first ``GET .../stream``, send a message, and expect the
``approval required`` banner although the subscription lands after the prompt."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import configure_mock_llm, find_free_port, reset_mock_llm

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASK_DEMO_YAML = _REPO_ROOT / "tests" / "resources" / "agents" / "ask-demo" / "ask-demo.yaml"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_REPLY_MARKER = "SLOW_STREAM_SUBSCRIBE_OK_MARKER"
_STREAM_DELAY_S = 4.0


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences before substring search."""
    return _ANSI_RE.sub("", text)


class _SlowStreamProxy:
    """TCP proxy that holds the first client chunk carrying ``GET /v1/sessions/{id}/stream``
    for ``delay_s`` before forwarding; everything else passes straight through."""

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        listen_port: int,
        *,
        delay_s: float,
    ) -> None:
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.listen_port = listen_port
        self.delay_s = delay_s
        self.delays = 0
        self._armed = True
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def _should_delay(self, chunk: bytes) -> bool:
        if b"/stream" not in chunk or b"GET /v1/sessions/" not in chunk:
            return False
        with self._lock:
            if not self._armed:
                return False
            self._armed = False
            self.delays += 1
            return True

    async def _handle(
        self,
        creader: asyncio.StreamReader,
        cwriter: asyncio.StreamWriter,
    ) -> None:
        try:
            ureader, uwriter = await asyncio.open_connection(
                self.upstream_host, self.upstream_port
            )
        except OSError:
            cwriter.close()
            return

        async def pump_c2s() -> None:
            try:
                while True:
                    data = await creader.read(65536)
                    if not data:
                        break
                    if self._should_delay(data):
                        await asyncio.sleep(self.delay_s)
                    uwriter.write(data)
                    await uwriter.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(OSError):
                    uwriter.write_eof()

        async def pump_s2c() -> None:
            try:
                while True:
                    data = await ureader.read(65536)
                    if not data:
                        break
                    cwriter.write(data)
                    await cwriter.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(OSError):
                    cwriter.close()

        await asyncio.gather(pump_c2s(), pump_s2c(), return_exceptions=True)

    async def _serve(self) -> None:
        server = await asyncio.start_server(self._handle, "127.0.0.1", self.listen_port)
        self._ready.set()
        async with server:
            await server.serve_forever()

    def start(self) -> None:
        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            with contextlib.suppress(asyncio.CancelledError):
                self._loop.run_until_complete(self._serve())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("slow-stream proxy did not start listening in time")

    def stop(self) -> None:
        loop = self._loop
        if loop is not None:
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)
        if self._thread is not None:
            self._thread.join(timeout=10)


class _ProxiedSession:
    """A live gated session reachable through the slow-stream proxy."""

    def __init__(
        self,
        proxy: _SlowStreamProxy,
        proxy_url: str,
        server_url: str,
        session_id: str,
    ) -> None:
        self.proxy = proxy
        self.proxy_url = proxy_url
        self.server_url = server_url
        self.session_id = session_id


@pytest.fixture
def proxied_gated_session(
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[_ProxiedSession]:
    """Boot server + runner with the always-ask agent, create a session,
    and put the slow-stream proxy in front of the server.
    """
    from omnigent.chat import _start_local_server, _stop_local_server, _wait_for_server

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": _REPLY_MARKER}] * 3, key="default")

    saved_env = {
        k: os.environ.get(k)
        for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "PYTHONPATH", "NO_PROXY", "no_proxy")
    }
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
    existing_pp = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        os.pathsep.join([str(_REPO_ROOT), existing_pp]) if existing_pp else str(_REPO_ROOT)
    )
    for proxy_key in ("NO_PROXY", "no_proxy"):
        parts = [p for p in os.environ.get(proxy_key, "").split(",") if p]
        for host in ("127.0.0.1", "localhost"):
            if host not in parts:
                parts.append(host)
        os.environ[proxy_key] = ",".join(parts)

    server_port = find_free_port()
    server = _start_local_server(_ASK_DEMO_YAML, server_port, ephemeral=True)
    proxy: _SlowStreamProxy | None = None
    try:
        _wait_for_server(server_port, server)
        base_url = f"http://127.0.0.1:{server_port}"

        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            resp = client.get("/v1/agents", params={"limit": 100})
            resp.raise_for_status()
            agents = resp.json().get("data", [])
            agent_id = next((str(a["id"]) for a in agents if a.get("name") == "ask-demo"), None)
            assert agent_id, f"ask-demo not registered: {[a.get('name') for a in agents]}"

            resp = client.post(
                "/v1/sessions",
                json={"agent_id": agent_id},
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            )
            resp.raise_for_status()
            session_id = str(resp.json()["id"])

            resp = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": server.runner_id})
            resp.raise_for_status()

            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                snap = client.get(f"/v1/sessions/{session_id}").json()
                if snap.get("runner_online") in (True, None):
                    break
                time.sleep(0.5)
            else:
                pytest.fail("runner never came online for the gated session")

        proxy_port = find_free_port()
        proxy = _SlowStreamProxy("127.0.0.1", server_port, proxy_port, delay_s=_STREAM_DELAY_S)
        proxy.start()

        yield _ProxiedSession(
            proxy=proxy,
            proxy_url=f"http://127.0.0.1:{proxy_port}",
            server_url=base_url,
            session_id=session_id,
        )
    finally:
        if proxy is not None:
            proxy.stop()
        _stop_local_server(server)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _build_repl_env(tmp_home: Path) -> dict[str, str]:
    """Env for the spawned ``omnigent attach`` REPL (pure client)."""
    from tests.e2e.omnigent._pexpect_harness import ensure_repl_test_theme_env

    sdk_paths = [
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    existing_pp = os.environ.get("PYTHONPATH", "")
    merged_pp = (
        os.pathsep.join([*sdk_paths, existing_pp]) if existing_pp else os.pathsep.join(sdk_paths)
    )
    config_home = tmp_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "PYTHONPATH": merged_pp,
            "TERM": "xterm-256color",
            "LINES": "40",
            "COLUMNS": "120",
            "PROMPT_TOOLKIT_NO_CPR": "1",
        }
    )
    for k in list(env):
        if k.startswith("DATABRICKS_"):
            env.pop(k, None)
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE", "CLAUDECODE", "CODEX"):
        env.pop(k, None)
    return ensure_repl_test_theme_env(env)


def _read_pending(child: Any, seconds: float) -> str:
    """Non-blocking read of buffered PTY output, ANSI-stripped."""
    collected = ""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(pexpect.EOF):
            child.expect(pexpect.TIMEOUT, timeout=min(0.5, max(0.05, deadline - time.monotonic())))
        chunk = child.before or ""
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        collected += chunk
        child.buffer = child.string_type()
        child.before = child.string_type()
    return _strip_ansi(collected)


def _clean_exit(child: Any) -> None:
    """Best-effort clean exit of the REPL."""
    try:
        child.sendcontrol("d")
        child.expect(pexpect.EOF, timeout=10)
    except pexpect.ExceptionPexpect:
        pass
    if child.isalive():
        child.terminate(force=True)


def test_approval_prompt_surfaces_after_slow_stream_subscribe(
    proxied_gated_session: _ProxiedSession,
    tmp_path: Path,
) -> None:
    """The approval prompt renders even when the SSE subscription lands after
    the elicitation was published: attach through the slow proxy, send a
    message, expect ``approval required``, answer ``y``, expect the reply."""
    sess = proxied_gated_session
    env = _build_repl_env(tmp_path / "home")

    child = pexpect.spawn(
        sys.executable,
        [
            "-m",
            "omnigent.cli",
            "attach",
            sess.session_id,
            "--server",
            sess.proxy_url,
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        timeout=120,
        dimensions=(40, 120),
    )
    try:
        child.expect(r"·\s*ready", timeout=90)
        child.send("Hello there\r")
        try:
            child.expect("approval required", timeout=30)
        except pexpect.TIMEOUT:
            assert sess.proxy.delays == 1, (
                f"stream subscribe was not delayed (delays={sess.proxy.delays}); "
                "the REPL never opened /stream through the proxy — test harness issue."
            )
            with httpx.Client(base_url=sess.server_url, timeout=10.0) as client:
                snap = client.get(f"/v1/sessions/{sess.session_id}").json()
            pytest.fail(
                "approval prompt never surfaced in the REPL after a "
                f"{_STREAM_DELAY_S:.0f}s-late /stream subscribe, although the server "
                f"gated the turn (status={snap.get('status')!r}, "
                f"pending_elicitations={len(snap.get('pending_elicitations') or [])}).\n"
                f"Buffer:\n{_strip_ansi(child.before or '')[-2000:]}"
            )
        assert sess.proxy.delays == 1, (
            f"stream subscribe was not delayed (delays={sess.proxy.delays}); "
            "the race was not exercised — test harness issue."
        )
        child.send("y\r")
        child.expect("approved", timeout=15)
        buffered = _read_pending(child, seconds=15.0)
        assert _REPLY_MARKER in buffered, (
            f"Turn never completed after approval.\nBuffer:\n{buffered[-3000:]}"
        )
    except pexpect.EOF:
        buf = _strip_ansi(child.before or "")
        pytest.fail(f"REPL exited early. Full buffer:\n{buf[-3000:]}")
    finally:
        _clean_exit(child)

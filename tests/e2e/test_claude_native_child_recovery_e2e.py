"""Real Claude child history must recover, in order, after a prolonged outage.

Opt in with OMNIGENT_E2E_CLAUDE_RECOVERY=1 and an authenticated Claude CLI.
OMNIGENT_E2E_CLAUDE_RECOVERY_REPO selects the checkout for every product process,
so this exact test can compare main and a fix. No product code, retry constants,
transcripts, or model responses are patched. Child event HTTP delivery is
faulted; the restart case holds the existing bridge lifecycle lock to preserve
its checkpoint during runner-exit cleanup. The cases take about 6, 10, and 6 minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from filelock import FileLock

pytestmark = [pytest.mark.live, pytest.mark.posix_only, pytest.mark.timeout(1000)]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RECOVERY_TIMEOUT_S = 90.0


def _wait(check: Callable[[], Any], timeout: float, what: str) -> Any:
    """Poll a real observable condition without changing the product clock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {what}")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _assistant_said(items: list[dict[str, Any]], text: str) -> bool:
    return any(
        block.get("text", "").strip() == text
        for item in items
        if item.get("role") == "assistant"
        for block in item.get("content", [])
    )


class _DeliveryProxy:
    """Pass HTTP/WebSockets; fault only the first real child's item POSTs."""

    def __init__(self, upstream_port: int, root: Path, mode: str):
        self.upstream_port = upstream_port
        self.port = _port()
        self.root = root
        self.mode = mode
        self.parent_id: str | None = None
        self.child_id: str | None = None
        self.outage = True
        self.failures: list[dict[str, Any]] = []
        self.events: dict[str, dict[str, dict[str, Any]]] = {}
        self.connections: set[asyncio.Task[Any]] = set()
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.connections.add(task)
        upstream = None
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
            lines = headers.decode("latin1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            fields = {
                key.lower(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            body = await reader.readexactly(int(fields.get("content-length", "0")))
            parts = path.split("/")
            if method == "POST" and len(parts) == 5 and parts[1:3] == ["v1", "sessions"]:
                sid = parts[3]
                if parts[4] == "events" and self.parent_id and sid != self.parent_id:
                    decoded = json.loads(body)
                    rows = decoded if isinstance(decoded, list) else [decoded]
                    items = [e for e in rows if e.get("type") == "external_conversation_item"]
                    if items:
                        self.child_id = self.child_id or sid
                        observed = self.events.setdefault(sid, {})
                        for event in items:
                            observed.setdefault(event["data"]["source_id"], event)
                    if items and self.outage and sid == self.child_id:
                        failure = {
                            "at": time.time(),
                            "source_ids": [e["data"]["source_id"] for e in items],
                            "batch": isinstance(decoded, list),
                        }
                        self.failures.append(failure)
                        with (self.root / "faults.jsonl").open("a") as output:
                            output.write(json.dumps(failure) + "\n")
                        if self.mode == "disconnect":
                            writer.transport.abort()
                        else:
                            payload = b'{"error":{"code":"unavailable","message":"test outage"}}'
                            writer.write(
                                b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\n"
                                + f"Content-Length: {len(payload)}\r\n".encode()
                                + b"Connection: close\r\n\r\n"
                                + payload
                            )
                            await writer.drain()
                        return

            up_reader, upstream = await asyncio.open_connection("127.0.0.1", self.upstream_port)
            websocket = fields.get("upgrade", "").lower() == "websocket"
            if not websocket:
                lines = [
                    line for line in lines if line and not line.lower().startswith("connection:")
                ]
                headers = ("\r\n".join([*lines, "Connection: close", "", ""])).encode("latin1")
            upstream.write(headers + body)
            await upstream.drain()

            async def pipe(source: asyncio.StreamReader, dest: asyncio.StreamWriter) -> None:
                try:
                    while data := await source.read(65536):
                        dest.write(data)
                        await dest.drain()
                except (ConnectionError, OSError):
                    pass

            if websocket:
                tasks = [
                    asyncio.create_task(pipe(reader, upstream)),
                    asyncio.create_task(pipe(up_reader, writer)),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for pipe_task in tasks:
                        pipe_task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await pipe(up_reader, writer)
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, OSError):
            pass
        finally:
            if upstream is not None:
                upstream.close()
            writer.close()
            self.connections.discard(task)

    async def _serve(self) -> None:
        server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.ready.set()
        async with server:
            await server.serve_forever()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            with contextlib.suppress(asyncio.CancelledError):
                self.loop.run_until_complete(self._serve())
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()

    def start(self) -> None:
        self.thread.start()
        assert self.ready.wait(10), "HTTP fault proxy did not start"

    def stop(self) -> None:
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(
                lambda: [task.cancel() for task in asyncio.all_tasks(self.loop)]
            )
            self.thread.join(timeout=10)


class _RecoveryStack:
    """Own all local processes and observe only this test's native bridge."""

    def __init__(self, root: Path, native_tmp: Path, mode: str):
        self.root = root
        self.native_tmp = native_tmp
        self.repo = Path(os.environ.get("OMNIGENT_E2E_CLAUDE_RECOVERY_REPO", _REPO_ROOT)).resolve()
        self.port = _port()
        self.proxy = _DeliveryProxy(self.port, root, mode)
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=20
        )
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[Any] = []
        self.sockets: set[str] = set()
        self.parent_id = ""
        self.bridge = native_tmp
        self.restart_evidence: dict[str, Any] | None = None
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OMNIGENT_", "DATABRICKS_"))
            and key
            not in {
                "CLAUDECODE",
                "RUNNER_SERVER_URL",
                "OMNIGENT",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            }
        }
        config = root / "config"
        config.mkdir()
        (config / "config.yaml").write_text(
            "providers:\n  claude:\n    kind: subscription\n    cli: claude\n    default: true\n"
        )
        self.env.update(
            {
                "OMNIGENT_CONFIG_HOME": str(config),
                "OMNIGENT_DATA_DIR": str(root / "data"),
                "OMNIGENT_AUTH_PROVIDER": "header",
                "OMNIGENT_LOCAL_SINGLE_USER": "1",
                "OMNIGENT_LOG_TO_STDERR": "1",
                "TMPDIR": str(native_tmp),
                "PYTHONPATH": os.pathsep.join(
                    str(self.repo / part) for part in ("", "sdks/python-client", "sdks/ui")
                ),
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )

    def spawn(self, name: str, *args: str) -> subprocess.Popen[bytes]:
        log = (self.root / f"{name}.log").open("ab")
        self.logs.append(log)
        proc = subprocess.Popen(
            [sys.executable, *args],
            cwd=self.repo,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(proc)
        return proc

    def get(self, path: str, **params: Any) -> Any:
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: Any) -> Any:
        response = self.client.post(path, json=body, timeout=120)
        response.raise_for_status()
        return response.json()

    def snapshot(self) -> dict[str, Any]:
        return self.get(f"/v1/sessions/{self.parent_id}")

    def items(self, sid: str) -> list[dict[str, Any]]:
        return self.get(f"/v1/sessions/{sid}/items", limit=200, order="asc")["data"]

    def send(self, text: str) -> None:
        self.post(
            f"/v1/sessions/{self.parent_id}/events",
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
        )

    def approve_test_inbox(self) -> None:
        for event in self.snapshot().get("pending_elicitations", []):
            tool_name = event.get("params", {}).get("tool_name", "")
            assert tool_name == "mcp__omnigent__sys_read_inbox", (
                f"Unexpected test permission prompt: {tool_name}"
            )
            self.post(
                f"/v1/sessions/{self.parent_id}/elicitations/{event['elicitation_id']}/resolve",
                {"action": "accept"},
            )

    def replied(self, marker: str) -> bool:
        self.approve_test_inbox()
        return _assistant_said(self.items(self.parent_id), marker)

    def remember_pane(self) -> bool:
        info = _json(self.bridge / "tmux.json")
        if info and info.get("socket_path"):
            self.sockets.add(info["socket_path"])
            return True
        return False

    def native_ready(self) -> bool:
        info = _json(self.bridge / "tmux.json")
        if not info:
            return False
        pane = subprocess.run(
            ["tmux", "-S", info["socket_path"], "capture-pane", "-p", "-t", "main"],
            capture_output=True,
            text=True,
            check=False,
        )
        (self.root / "last-pane.txt").write_text(pane.stdout)
        return (
            pane.returncode == 0
            and any(line.lstrip().startswith("❯") for line in pane.stdout.splitlines())
            and self.snapshot().get("terminal_pending") is False
        )

    def start(self) -> None:
        server = self.spawn(
            "server",
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--database-uri",
            f"sqlite:///{self.root / 'server.db'}",
            "--artifact-location",
            str(self.root / "artifacts"),
        )

        def healthy() -> bool:
            assert server.poll() is None, f"Server exited; see {self.root / 'server.log'}"
            try:
                return self.client.get("/health").status_code == 200
            except httpx.HTTPError:
                return False

        _wait(healthy, 90, "real server health")
        self.proxy.start()
        self.spawn(
            "host",
            "-m",
            "omnigent.host._daemon_entry",
            "--server",
            f"http://127.0.0.1:{self.proxy.port}",
        )
        host = _wait(
            lambda: next(
                (host for host in self.get("/v1/hosts")["hosts"] if host["status"] == "online"),
                None,
            ),
            90,
            "real host registration",
        )
        agent = next(a for a in self.get("/v1/agents")["data"] if a["name"] == "claude-native-ui")
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.fixture = workspace / "fixture.txt"
        self.fixture.write_text("The transcript recovery verification word is kestrel.\n")
        session = self.post(
            "/v1/sessions",
            {
                "agent_id": agent["id"],
                "host_id": host["host_id"],
                "workspace": str(workspace),
                "terminal_launch_args": [
                    "--dangerously-skip-permissions",
                    "--tools",
                    "Agent,Read",
                ],
            },
        )
        self.parent_id = session["id"]
        self.proxy.parent_id = self.parent_id
        bridge_id = (
            session.get("labels", {}).get("omnigent.claude_native.bridge_id") or self.parent_id
        )
        digest = hashlib.sha256(bridge_id.encode()).hexdigest()[:32]
        self.bridge = self.native_tmp / f"omnigent-{os.getuid()}" / "claude-native" / digest
        _wait(self.remember_pane, 90, "real Claude native pane")
        _wait(self.native_ready, 90, "Claude's interactive prompt")
        time.sleep(1)
        (self.root / "identity.json").write_text(
            json.dumps(
                {
                    "repo": str(self.repo),
                    "sha": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
                    ).strip(),
                    "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "forwarder_sha256": hashlib.sha256(
                        (self.repo / "omnigent/harnesses/claude_native/forwarder.py").read_bytes()
                    ).hexdigest(),
                    "parent_id": self.parent_id,
                    "server": str(self.client.base_url),
                    "bridge_dir": str(self.bridge),
                },
                indent=2,
            )
        )

    def child_prompt(self, marker: str) -> str:
        return (
            "Use Claude Code's built-in Agent tool to launch exactly one child. "
            "Do not do the task yourself. Ask the child to use Read on exactly "
            f"fixture.txt in the current directory and reply exactly '{marker} kestrel'. "
            "Do not read other files, write files, run commands, access the network, "
            f"or launch additional agents. After the child returns, reply PARENT_{marker}."
        )

    def restart_runner(self) -> None:
        checkpoint_path = self.bridge / "subagent_forwarder.json"
        before = _json(checkpoint_path)
        assert before and before["subagents"]
        inode = self.bridge.stat().st_ino
        old_runner = self.snapshot()["runner_id"]
        owner_pid = int((self.bridge / "owner.pid").read_text())
        assert owner_pid > 1 and owner_pid != os.getpid()
        # Keep the genuine checkpoint through the host's runner-exit sweep.
        # Release the production lifecycle lock before the new runner prepares it.
        lock = self.bridge.parent / ".locks" / f"{self.bridge.name}.lock"
        with ThreadPoolExecutor(max_workers=1) as executor:
            with FileLock(str(lock), timeout=10):
                os.kill(owner_pid, signal.SIGTERM)
                _wait(lambda: not self.snapshot()["runner_online"], 45, "old runner disconnect")
                restart = executor.submit(
                    self.post,
                    f"/v1/sessions/{self.parent_id}/events",
                    {"type": "retry_session", "data": {}},
                )
                _wait(
                    lambda: (
                        self.get(
                            f"/v1/sessions/{self.parent_id}",
                            include_liveness="false",
                            include_usage="false",
                        )["runner_id"]
                        not in (None, old_runner)
                    ),
                    90,
                    "new runner registration",
                )
            restart.result(timeout=120)
        _wait(
            lambda: int((self.bridge / "owner.pid").read_text()) != owner_pid,
            90,
            "new runner bridge ownership",
        )
        self.remember_pane()
        _wait(self.native_ready, 90, "resumed Claude's interactive prompt")
        after = _json(checkpoint_path)
        assert self.bridge.stat().st_ino == inode, "Restart rebuilt the bridge; invalid test case"
        assert after is not None
        for sid, entry in before["subagents"].items():
            for field in ("child_conversation_id", "byte_offset", "seen_source_ids"):
                assert after["subagents"][sid][field] == entry[field], (
                    f"Restart reset {sid}.{field}; checkpoint preservation was not exercised"
                )
        self.restart_evidence = {
            "old_runner": old_runner,
            "new_runner": self.snapshot()["runner_id"],
            "bridge_inode_before": inode,
            "bridge_inode_after": self.bridge.stat().st_ino,
            "checkpoint_before": before,
            "checkpoint_after": after,
        }
        time.sleep(1)
        marker = f"RESTARTED_{uuid.uuid4().hex}"
        self.send(f"Reply exactly {marker}. Do not use any tools.")
        _wait(lambda: self.replied(marker), 120, "new runner's real parent reply")

    def close(self) -> None:
        self.remember_pane()
        for proc in reversed(self.processes):
            _stop(proc)
        for socket_path in self.sockets:
            subprocess.run(
                ["tmux", "-S", socket_path, "kill-server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        self.proxy.stop()
        self.client.close()
        for log in self.logs:
            log.close()


@pytest.fixture
def recovery_stack(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_RecoveryStack]:
    if os.environ.get("OMNIGENT_E2E_CLAUDE_RECOVERY") != "1":
        pytest.skip("set OMNIGENT_E2E_CLAUDE_RECOVERY=1; requires real authenticated Claude")
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary} is required for live Claude recovery")
    # Short socket paths and a private native-bridge root for every stack.
    with tempfile.TemporaryDirectory(
        prefix="ocr-", dir="/tmp", ignore_cleanup_errors=True
    ) as native_tmp:
        stack = _RecoveryStack(tmp_path, Path(native_tmp), request.param)
        try:
            stack.start()
            yield stack
        finally:
            stack.close()


@pytest.mark.parametrize(
    ("recovery_stack", "outage_seconds", "restart_runner"),
    [
        pytest.param("502", 240, False, id="http502-live"),
        pytest.param("disconnect", 480, False, id="connection-loss-live"),
        pytest.param("502", 240, True, id="http502-preserved-checkpoint"),
    ],
    indirect=["recovery_stack"],
)
def test_real_child_history_recovers_in_order(
    recovery_stack: _RecoveryStack, outage_seconds: int, restart_runner: bool
) -> None:
    stack = recovery_stack
    marker = f"CHILD_{uuid.uuid4().hex[:12]}"
    stack.send(stack.child_prompt(marker))
    _wait(lambda: stack.proxy.failures, 180, "first real child transcript delivery failure")
    _wait(lambda: stack.replied(f"PARENT_{marker}"), 180, "real child's completion")

    # A second genuine child and its parent must remain usable during the outage.
    control = f"CONTROL_{uuid.uuid4().hex[:12]}"
    stack.send(stack.child_prompt(control))
    _wait(lambda: stack.replied(f"PARENT_{control}"), 180, "healthy sibling completion")
    child = stack.proxy.child_id
    assert child is not None
    sibling = _wait(
        lambda: next((sid for sid in stack.proxy.events if sid != child), None),
        30,
        "healthy sibling transcript",
    )
    _wait(
        lambda: _assistant_said(stack.items(sibling), f"{control} kestrel"),
        30,
        "healthy sibling's completed answer in the server transcript",
    )

    # The oracle comes from Claude's real source file, not HTTP delivery order.
    from omnigent.harnesses.claude_native.bridge import read_transcript_items_from_offset

    checkpoint = _json(stack.bridge / "subagent_forwarder.json")
    subagent_id = next(
        sid
        for sid, entry in checkpoint["subagents"].items()
        if entry["child_conversation_id"] == child
    )
    parent_transcript = Path(_json(stack.bridge / "state.json")["transcript_path"])
    source_path = parent_transcript.with_suffix("") / "subagents" / f"agent-{subagent_id}.jsonl"
    source_items = read_transcript_items_from_offset(
        source_path,
        0,
        start_line=0,
        agent_name="claude-native-ui",
        include_sidechains=True,
    ).items
    assert _assistant_said([item.data for item in source_items], f"{marker} kestrel")
    assert {"function_call", "function_call_output", "message"} <= {
        item.item_type for item in source_items
    }
    source_ids = list(dict.fromkeys(item.source_id for item in source_items))
    expected = [
        uuid.uuid5(uuid.NAMESPACE_URL, f"omnigent-external-item:{child}:{source.strip()}").hex
        for source in source_ids
    ]

    outage_ends = stack.proxy.failures[0]["at"] + outage_seconds
    while time.time() < outage_ends:
        stack.approve_test_inbox()
        time.sleep(0.5)
    assert len(stack.proxy.failures) >= 12, "Did not exercise the real default retry budget"
    if stack.proxy.mode == "disconnect":
        assert sum(not failure["batch"] for failure in stack.proxy.failures) >= 12

    if restart_runner:
        stack.restart_runner()
    stack.proxy.outage = False
    restored_marker = f"HEALTHY_{uuid.uuid4().hex}"
    stack.send(f"Reply exactly {restored_marker}. Do not use any tools.")
    _wait(lambda: stack.replied(restored_marker), 120, "healthy parent after restoring delivery")

    deadline = time.monotonic() + _RECOVERY_TIMEOUT_S
    actual: list[str] = []
    while time.monotonic() < deadline:
        actual = [item["id"] for item in stack.items(child)]
        if actual == expected:
            break
        time.sleep(0.5)
    checkpoint = _json(stack.bridge / "subagent_forwarder.json")
    report = {
        "child": child,
        "restart_runner": restart_runner,
        "restart_evidence": stack.restart_evidence,
        "outage_seconds": outage_seconds,
        "faults": stack.proxy.failures,
        "source_path": str(source_path),
        "source_ids": source_ids,
        "expected_item_ids": expected,
        "actual_item_ids": actual,
        "missing": sorted(set(expected) - set(actual)),
        "checkpoint": checkpoint,
    }
    (stack.root / "result.json").write_text(json.dumps(report, indent=2))
    assert actual == expected, (
        f"Real child history did not recover exactly once in source order: "
        f"{len(report['missing'])} missing; evidence: {stack.root / 'result.json'}"
    )

    # Let the real idle observation run; delivery recovery must not report a fatal gap.
    time.sleep(8)
    status = stack.get(f"/v1/sessions/{child}")
    checkpoint = _json(stack.bridge / "subagent_forwarder.json")
    assert checkpoint is not None
    entry = next(
        entry
        for entry in checkpoint["subagents"].values()
        if entry["child_conversation_id"] == child
    )
    assert entry.get("delivery_error") is None
    assert status["status"] != "failed"
    assert [item["id"] for item in stack.items(child)] == expected

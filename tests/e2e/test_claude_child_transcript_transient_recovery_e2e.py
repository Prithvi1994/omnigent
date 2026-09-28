"""E2E regression tests: Claude child transcripts must recover from a transient
delivery outage that outlasts the forwarder's batch retry budget.

Journey: a Claude-native parent spawns an Agent child. While the child's
transcript is being mirrored into its Omnigent child session, delivery of the
child's items fails transiently (a fixed HTTP 502 outage, or abrupt connection
loss) for long enough to exhaust the forwarder's per-item retry budget. Delivery
then recovers, the parent produces another reply and the child keeps working.
The child session must eventually show every child item, in transcript order,
and must not stay marked failed, both while the same forwarder keeps running
and after a forwarder restart that keeps its checkpoint.

Drives the real ``omnigent server`` subprocess, a real claude-native session
created exactly like ``omnigent claude`` does, and the real
``forward_claude_transcript_to_session`` loop tailing a seeded Claude transcript
tree. The outage is injected at an HTTP proxy between the forwarder and the
server that faults only child ``external_conversation_item`` POSTs; the
production 12-attempt budget is kept and only the backoff schedule is
compressed so the outage fits a test.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_child_transcript_transient_recovery_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
_http = httpx.Client(trust_env=False)

_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)
_SERVER_BOOTSTRAP = "from omnigent.cli import main\n\nmain()\n"
_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.25

_EVENTS_PATH = re.compile(r"^/v1/sessions/([^/?]+)/events(?:\?.*)?$")
_FAULT_MODES = ("http_502", "connection_loss")

_SUBAGENT_ID = "b7e1d2c3a4f5e6d70"
_TOOL_USE_ID = "toolu_01ChildTransientRecovery"
_SUBAGENT_TYPE = "general-purpose"
_SUBAGENT_DESCRIPTION = "read the fixture and summarize it"
_AGENT_NAME = "claude-native-ui"

# Six child transcript items; each is mirrored as one external_conversation_item.
_CHILD_MARKERS = tuple(f"child-transcript-item-{index}" for index in range(1, 7))
_FRESH_CHILD_MARKER = "child-output-after-delivery-recovered"
_PARENT_REPLY_MARKER = "parent-reply-after-delivery-recovered"

# The outage lasts until the forwarder dead-letters a child item or the faulted
# POSTs cover one production batch budget plus as many individual re-drive
# attempts, whichever comes first. The wall-clock cap is only a safety net: under
# pytest each failed attempt's exc_info log line takes over a second to render.
_BATCH_ATTEMPT_BUDGET = 12
_OUTAGE_FAULTED_POSTS = 2 * _BATCH_ATTEMPT_BUDGET
_OUTAGE_CAP_S = 120.0
_RECOVERY_WINDOW_S = 15.0
_MIRROR_TIMEOUT_S = 30.0


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env() -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy/credentials in the way."""
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    for name in list(env):
        if (
            name.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or name.endswith("_SECRET")
            or name
            in (
                "ANTHROPIC_API_KEY",
                "OMNIGENT_AUTH_ENABLED",
                "OMNIGENT_RUNNER_TUNNEL_TOKEN",
            )
        ):
            env.pop(name, None)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``."""
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat translator.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _assistant_text_record(uuid: str, text: str, *, sidechain: bool) -> dict[str, Any]:
    return {
        "isSidechain": sidechain,
        "type": "assistant",
        "uuid": uuid,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _append_record(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _seed_claude_task_spawn(bridge_dir: Path) -> tuple[Path, Path]:
    """Lay out the on-disk tree Claude Code writes for an Agent spawn.

    :returns: ``(parent transcript path, child transcript path)``.
    """
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"
    parent_records = [
        {
            "isSidechain": False,
            "type": "user",
            "uuid": "parent-user-1",
            "message": {"role": "user", "content": "Delegate reading the fixture to a sub-agent."},
        },
        {
            "isSidechain": False,
            "type": "assistant",
            "uuid": f"spawn-{_SUBAGENT_ID}",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": _TOOL_USE_ID,
                        "name": "Agent",
                        "input": {"description": _SUBAGENT_DESCRIPTION},
                    }
                ],
            },
        },
    ]
    transcript_path.write_text(
        "".join(json.dumps(record) + "\n" for record in parent_records), encoding="utf-8"
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session-child-transient-recovery",
            "transcript_path": str(transcript_path),
        },
    )

    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    (subagents_dir / f"agent-{_SUBAGENT_ID}.meta.json").write_text(
        json.dumps(
            {
                "agentType": _SUBAGENT_TYPE,
                "description": _SUBAGENT_DESCRIPTION,
                "toolUseId": _TOOL_USE_ID,
            }
        ),
        encoding="utf-8",
    )
    child_records: list[dict[str, Any]] = [
        {
            "isSidechain": True,
            "type": "user",
            "uuid": "child-user-1",
            "message": {"role": "user", "content": f"Read the fixture. {_CHILD_MARKERS[0]}"},
        }
    ]
    child_records.extend(
        _assistant_text_record(f"child-assistant-{index}", marker, sidechain=True)
        for index, marker in enumerate(_CHILD_MARKERS[1:], start=1)
    )
    child_path = subagents_dir / f"agent-{_SUBAGENT_ID}.jsonl"
    child_path.write_text(
        "".join(json.dumps(record) + "\n" for record in child_records), encoding="utf-8"
    )
    return transcript_path, child_path


class _ChildDeliveryOutage:
    """Reverse proxy that faults only child transcript POSTs while ``active`` is set.

    ``http_502`` answers each faulted POST with a 502; ``connection_loss`` drops
    the connection without a response. Every other request is relayed unchanged.
    """

    def __init__(self, upstream: str, parent_session_id: str, mode: str) -> None:
        self.upstream = upstream
        self.parent_session_id = parent_session_id
        self.mode = mode
        self.active = threading.Event()
        self.faulted_posts = 0
        self._lock = threading.Lock()
        self._relay_client = httpx.Client(trust_env=False, timeout=60.0)
        outage = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def _relay(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if outage._should_fault(self.command, self.path, body):
                    with outage._lock:
                        outage.faulted_posts += 1
                    if outage.mode == "connection_loss":
                        self.close_connection = True
                        return
                    payload = b'{"error":"bad gateway"}'
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower()
                    not in {"host", "content-length", "connection", "accept-encoding"}
                }
                upstream = outage._relay_client.request(
                    self.command, f"{outage.upstream}{self.path}", content=body, headers=headers
                )
                self.send_response(upstream.status_code)
                content_type = upstream.headers.get("content-type")
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(upstream.content)))
                self.end_headers()
                self.wfile.write(upstream.content)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _relay

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _should_fault(self, method: str, path: str, body: bytes) -> bool:
        if not self.active.is_set() or method != "POST":
            return False
        match = _EVENTS_PATH.match(path)
        return (
            match is not None
            and match.group(1) != self.parent_session_id
            and b"external_conversation_item" in body
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._relay_client.close()


def _count_marker(base_url: str, session_id: str, marker: str) -> int:
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return sum(1 for item in resp.json()["data"] if marker in json.dumps(item))


def _ordered_child_markers(base_url: str, session_id: str) -> list[str]:
    """Return the known markers in the child session's committed item order."""
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    ordered: list[str] = []
    for item in resp.json()["data"]:
        text = json.dumps(item)
        for marker in (*_CHILD_MARKERS, _FRESH_CHILD_MARKER):
            if marker in text:
                ordered.append(marker)
    return ordered


def _session_status(base_url: str, session_id: str) -> str | None:
    resp = _http.get(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)
    resp.raise_for_status()
    status = resp.json().get("status")
    return status if isinstance(status, str) else None


def _dead_letters_for(bridge_dir: Path, session_id: str) -> list[dict[str, Any]]:
    path = bridge_dir / "dead_letter.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            record = json.loads(line)
            if isinstance(record, dict) and record.get("session_id") == session_id:
                records.append(record)
    return records


def _registered_child_id(bridge_dir: Path) -> str | None:
    import omnigent.harnesses.claude_native.forwarder as fwd

    entry = fwd._read_subagent_forward_state(bridge_dir).subagents.get(_SUBAGENT_ID)
    return entry.child_conversation_id or None if entry is not None else None


@dataclass
class _Observation:
    """What the journey observed on the running build."""

    parent_session_id: str
    child_session_id: str
    faulted_posts: int
    dead_letters_at_restore: int
    parent_reply_mirrored: bool
    fresh_child_item_mirrored: bool
    present_markers: list[str]
    ordered_markers: list[str]
    child_status: str | None
    present_after_restart: list[str] | None = None
    child_status_after_restart: str | None = None
    server_log_tail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def missing_markers(self) -> list[str]:
        return [marker for marker in _CHILD_MARKERS if marker not in self.present_markers]

    @property
    def missing_after_restart(self) -> list[str]:
        present = self.present_after_restart or []
        return [marker for marker in _CHILD_MARKERS if marker not in present]


async def _wait_until(
    predicate: Callable[[], bool],
    timeout_s: float,
    *,
    forwarder: asyncio.Task[Any],
) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if forwarder.done() and not forwarder.cancelled():
            forwarder.result()
        if await asyncio.to_thread(predicate):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_POLL_S)


def _present_markers(base_url: str, child_id: str) -> list[str]:
    return [marker for marker in _CHILD_MARKERS if _count_marker(base_url, child_id, marker) >= 1]


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _drive_journey(
    *,
    base_url: str,
    outage: _ChildDeliveryOutage,
    parent_id: str,
    bridge_dir: Path,
    transcript_path: Path,
    child_path: Path,
    restart: bool,
) -> _Observation:
    """Run the real forwarder loop through the outage, the recovery and an optional restart."""
    import omnigent.harnesses.claude_native.forwarder as fwd

    real_tracker = fwd._PostRetryTracker

    class _CompressedBackoffTracker(real_tracker):  # type: ignore[misc,valid-type]
        # Same attempt budgets as production; only the sleeps between attempts shrink.
        def __init__(self, **kwargs: Any) -> None:
            kwargs.setdefault("base_delay_s", 0.05)
            kwargs.setdefault("max_delay_s", 0.25)
            super().__init__(**kwargs)

    def _start_forwarder() -> asyncio.Task[Any]:
        return asyncio.create_task(
            fwd.forward_claude_transcript_to_session(
                base_url=outage.url,
                headers={},
                session_id=parent_id,
                bridge_dir=bridge_dir,
                agent_name=_AGENT_NAME,
                start_at_end=False,
                poll_interval_s=0.05,
            )
        )

    fwd._PostRetryTracker = _CompressedBackoffTracker
    try:
        outage.active.set()
        forwarder = _start_forwarder()
        try:
            registered = await _wait_until(
                lambda: _registered_child_id(bridge_dir) is not None,
                _MIRROR_TIMEOUT_S,
                forwarder=forwarder,
            )
            assert registered, "forwarder never registered the on-disk sub-agent"
            child_id = _registered_child_id(bridge_dir)
            assert child_id is not None

            await _wait_until(
                lambda: (
                    bool(_dead_letters_for(bridge_dir, child_id))
                    or outage.faulted_posts >= _OUTAGE_FAULTED_POSTS
                ),
                _OUTAGE_CAP_S,
                forwarder=forwarder,
            )
            dead_letters_at_restore = len(_dead_letters_for(bridge_dir, child_id))
            outage.active.clear()

            _append_record(
                transcript_path,
                _assistant_text_record(
                    "parent-assistant-2", _PARENT_REPLY_MARKER, sidechain=False
                ),
            )
            _append_record(
                child_path,
                _assistant_text_record(
                    "child-assistant-fresh", _FRESH_CHILD_MARKER, sidechain=True
                ),
            )
            parent_reply_mirrored = await _wait_until(
                lambda: _count_marker(base_url, parent_id, _PARENT_REPLY_MARKER) >= 1,
                _MIRROR_TIMEOUT_S,
                forwarder=forwarder,
            )
            fresh_child_item_mirrored = await _wait_until(
                lambda: _count_marker(base_url, child_id, _FRESH_CHILD_MARKER) >= 1,
                _MIRROR_TIMEOUT_S,
                forwarder=forwarder,
            )
            await _wait_until(
                lambda: len(_present_markers(base_url, child_id)) == len(_CHILD_MARKERS),
                _RECOVERY_WINDOW_S,
                forwarder=forwarder,
            )
            observation = _Observation(
                parent_session_id=parent_id,
                child_session_id=child_id,
                faulted_posts=outage.faulted_posts,
                dead_letters_at_restore=dead_letters_at_restore,
                parent_reply_mirrored=parent_reply_mirrored,
                fresh_child_item_mirrored=fresh_child_item_mirrored,
                present_markers=_present_markers(base_url, child_id),
                ordered_markers=_ordered_child_markers(base_url, child_id),
                child_status=_session_status(base_url, child_id),
            )
        finally:
            await _cancel(forwarder)

        if restart:
            forwarder = _start_forwarder()
            try:
                await _wait_until(
                    lambda: len(_present_markers(base_url, child_id)) == len(_CHILD_MARKERS),
                    _RECOVERY_WINDOW_S,
                    forwarder=forwarder,
                )
                observation.present_after_restart = _present_markers(base_url, child_id)
                observation.child_status_after_restart = _session_status(base_url, child_id)
            finally:
                await _cancel(forwarder)
        return observation
    finally:
        fwd._PostRetryTracker = real_tracker


@contextlib.contextmanager
def _run_journey(
    tmp_path: Path, fault: str, *, restart: bool
) -> Iterator[tuple[str, _Observation]]:
    """Spawn the server, drive the journey, and yield ``(base_url, observation)`` pre-teardown."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server_log_path = tmp_path / "server.log"
    server_log = server_log_path.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    outage: _ChildDeliveryOutage | None = None
    bridge_dir: Path | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SERVER_BOOTSTRAP,
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{tmp_path / 'chat.db'}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env(),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        parent_id = _create_claude_native_session(base_url)
        from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

        bridge_dir = prepare_bridge_dir(parent_id, workspace=workspace)
        transcript_path, child_path = _seed_claude_task_spawn(bridge_dir)

        outage = _ChildDeliveryOutage(base_url, parent_id, fault)
        outage.start()
        observation = asyncio.run(
            _drive_journey(
                base_url=base_url,
                outage=outage,
                parent_id=parent_id,
                bridge_dir=bridge_dir,
                transcript_path=transcript_path,
                child_path=child_path,
                restart=restart,
            )
        )
        observation.server_log_tail = server_log_path.read_text()[-2000:]
        observation.extra["dead_letters"] = _dead_letters_for(
            bridge_dir, observation.child_session_id
        )
        yield base_url, observation
    finally:
        if outage is not None:
            outage.stop()
        _terminate(server_proc)
        server_log.close()
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)


def _assert_journey_healthy(observation: _Observation, fault: str) -> None:
    """Fail for a harness problem rather than misreport it as the bug."""
    assert observation.faulted_posts >= 1, (
        f"the {fault} outage never faulted a child transcript POST; "
        f"child={observation.child_session_id} server log tail:\n{observation.server_log_tail}"
    )
    assert observation.parent_reply_mirrored, (
        "after the outage ended the parent's new reply was not mirrored, so delivery "
        f"never recovered; server log tail:\n{observation.server_log_tail}"
    )
    assert observation.fresh_child_item_mirrored, (
        "after the outage ended fresh child output was not mirrored, so child delivery "
        f"never recovered; server log tail:\n{observation.server_log_tail}"
    )


@pytest.mark.timeout(300)
@pytest.mark.parametrize("fault", _FAULT_MODES)
def test_child_transcript_recovers_after_transient_delivery_outage(
    tmp_path: Path, fault: str
) -> None:
    """Every child item must reach the child session once delivery recovers, in order."""
    with _run_journey(tmp_path, fault, restart=False) as (_base_url, observation):
        _assert_journey_healthy(observation, fault)
        assert not observation.missing_markers, (
            f"After a transient {fault} outage exhausted the forwarder's retries "
            f"({observation.faulted_posts} faulted child POSTs, "
            f"{observation.dead_letters_at_restore} child items dead-lettered) and delivery "
            "recovered (the parent's new reply and fresh child output were mirrored), the "
            f"child session {observation.child_session_id} is still missing "
            f"{len(observation.missing_markers)}/{len(_CHILD_MARKERS)} transcript items: "
            f"{observation.missing_markers}; child status={observation.child_status!r}"
        )
        assert observation.ordered_markers == [*_CHILD_MARKERS, _FRESH_CHILD_MARKER], (
            f"child items were recovered out of transcript order: {observation.ordered_markers}"
        )
        assert observation.child_status != "failed", (
            "a temporary delivery loss left the child session permanently marked failed"
        )


@pytest.mark.timeout(300)
@pytest.mark.parametrize("fault", _FAULT_MODES)
def test_checkpoint_preserving_restart_recovers_child_transcript(
    tmp_path: Path, fault: str
) -> None:
    """A forwarder restart that keeps its checkpoint must still fill the child gap."""
    with _run_journey(tmp_path, fault, restart=True) as (_base_url, observation):
        _assert_journey_healthy(observation, fault)
        assert not observation.missing_after_restart, (
            f"After a transient {fault} outage, recovery, and a forwarder restart that kept "
            f"its checkpoint, the child session {observation.child_session_id} is still "
            f"missing {len(observation.missing_after_restart)}/{len(_CHILD_MARKERS)} "
            f"transcript items: {observation.missing_after_restart}; "
            f"child status={observation.child_status_after_restart!r}"
        )

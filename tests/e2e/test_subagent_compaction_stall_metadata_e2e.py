"""An orchestrator must be able to tell a compaction-stalled sub-agent from a healthy one.

A mock ``openai-agents`` orchestrator dispatches a real ``claude-native`` child
through ``sys_session_send`` (purpose ``implement``) with a large read-phase
prompt. The child's Claude Code reads files until the mock model reports a
near-full context, auto-compacts, reads again, auto-compacts a second time, and
then its next model reply is held on the mock gate so the turn never advances
(no further tool calls, no writes). The orchestrator then polls
``sys_session_get_info`` / ``sys_session_list`` / ``sys_session_get_history``
twice, at least ten seconds apart.

Claude Code only enforces a context window it knows; a third-party base URL
leaves the window unenforced, so the child's HOME carries
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (also exported to the runner) and the mock
reports ``input_tokens`` above the window minus Claude's 13k buffer.

The model APIs are mocked; the server, runner, native Claude CLI, hooks, tmux
pane and the runner-dispatched ``sys_session_*`` tools are real::

    uv run --no-sync pytest -o addopts='' \\
        tests/e2e/test_subagent_compaction_stall_metadata_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import _mock_llm_server_process, find_free_port

pytestmark = pytest.mark.timeout(900, method="signal")
_REPO = Path(__file__).resolve().parents[2]
_PARENT_MODEL = "mock-compaction-stall-parent"
_CHILD_MODEL = "claude-sonnet-4-20250514"
# Claude Code compacts once the last reply's prompt usage reaches the enforced
# window minus its 13k buffer.
_AUTO_COMPACT_WINDOW = 100_000
_NEAR_FULL_USAGE = {
    "input_tokens": 95_000,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}
_SMALL_USAGE = {
    "input_tokens": 1_200,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}
_DOC_COUNT = 6
_HELD_REPLY = "HELD-REPLY: never delivered while the gate is closed."
_POLL_GAP_S = 10.0


def _write_docs(workspace: Path) -> None:
    docs = workspace / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for i in range(1, _DOC_COUNT + 1):
        lines = [f"# Guide {i}", ""]
        lines += [
            f"Section {n}: guide {i} explains step {n} of the workflow in detail."
            for n in range(300)
        ]
        (docs / f"guide-{i}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Rig:
    client: httpx.Client
    runner_id: str
    mock_url: str
    workspace: Path
    tmp: Path
    base_url: str


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Rig]:
    """Run an isolated server/runner; the child uses the real Claude CLI + mock API."""
    for binary in ("claude", "tmux", "git"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} binary")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip("machine-managed Claude settings override mock auth; run in a clean container")
    tmp_path = tmp_path_factory.mktemp("compaction-stall")
    (tmp_path / "mock-logs").mkdir()
    mock_iter = _mock_llm_server_process(tmp_path / "mock-logs")
    mock_url = next(mock_iter)
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "config"
    native_home = tmp_path / "home"
    claude_home = native_home / ".claude"
    for directory in (workspace, config_dir, claude_home):
        directory.mkdir(parents=True)
    _write_docs(workspace)
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "-c", "user.email=repro@example.com", "-c", "user.name=repro", "add", ".")
    _git(
        workspace,
        "-c",
        "user.email=repro@example.com",
        "-c",
        "user.name=repro",
        "commit",
        "-q",
        "-m",
        "docs baseline",
    )
    (native_home / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace.resolve()): {"hasTrustDialogAccepted": True}},
            }
        ),
        encoding="utf-8",
    )
    (claude_home / "settings.json").write_text(
        json.dumps({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(_AUTO_COMPACT_WINDOW)}}),
        encoding="utf-8",
    )
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "repro-claude": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": mock_url,
                            "api_key": "mock-key",
                            "models": {"default": _CHILD_MODEL},
                        },
                    },
                    "repro-openai": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _PARENT_MODEL},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    token = uuid.uuid4().hex
    runner_id = token_bound_runner_id(token)
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "LANG",
                "LC_ALL",
                "TMPDIR",
                "TMP",
                "TEMP",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE",
                "NODE_EXTRA_CA_CERTS",
            }
        },
        "HOME": str(native_home),
        "OMNIGENT_CONFIG_HOME": str(config_dir),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_CLAUDE_PATH": str(shutil.which("claude")),
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(_AUTO_COMPACT_WINDOW),
        "PYTHONPATH": str(_REPO),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    # The web SPA is only needed when a recording driver attaches a browser.
    if not os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        env["OMNIGENT_SKIP_WEB_UI"] = "true"
    processes: list[subprocess.Popen[bytes]] = []
    with (
        (tmp_path / "server.log").open("w") as server_log,
        (tmp_path / "runner.log").open("w") as runner_log,
        httpx.Client(
            base_url=base_url,
            timeout=30,
            trust_env=False,
            headers={
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                "x-omnigent-background-session-titles": "off",
            },
        ) as client,
    ):
        try:
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "omnigent.cli",
                        "server",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--database-uri",
                        f"sqlite:///{tmp_path / 'test.db'}",
                        "--artifact-location",
                        str(tmp_path / "artifacts"),
                    ],
                    cwd=_REPO,
                    env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": token},
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                )
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "omnigent.runner._entry"],
                    cwd=_REPO,
                    env={
                        **env,
                        "OMNIGENT_RUNNER_ID": runner_id,
                        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                        "RUNNER_SERVER_URL": base_url,
                    },
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                )
            )
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                assert all(proc.poll() is None for proc in processes), "server/runner exited"
                with contextlib.suppress(httpx.HTTPError):
                    response = client.get(f"/v1/runners/{runner_id}/status", timeout=2)
                    if response.status_code == 200 and response.json().get("online"):
                        break
                time.sleep(0.5)
            else:
                pytest.fail("server/runner did not become ready")
            yield Rig(
                client=client,
                runner_id=runner_id,
                mock_url=mock_url,
                workspace=workspace,
                tmp=tmp_path,
                base_url=base_url,
            )
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
            with contextlib.suppress(StopIteration):
                next(mock_iter)


def _mock_post(mock_url: str, path: str, body: dict[str, Any]) -> None:
    httpx.post(f"{mock_url}{path}", json=body, timeout=10, trust_env=False).raise_for_status()


def _read_call(workspace: Path, index: int, usage: dict[str, int]) -> dict[str, Any]:
    return {
        "tool_calls": [
            {
                "call_id": f"toolu_read_guide_{index}",
                "name": "Read",
                "arguments": json.dumps(
                    {"file_path": str(workspace / "docs" / f"guide-{index}.md")}
                ),
            }
        ],
        "usage": usage,
    }


def _configure_child_model(mock_url: str, workspace: Path) -> None:
    """Script the read phase: reads, compaction, read, compaction, then a held reply."""
    responses = [_read_call(workspace, i, _SMALL_USAGE) for i in range(1, _DOC_COUNT - 1)]
    responses += [
        _read_call(workspace, _DOC_COUNT - 1, _NEAR_FULL_USAGE),
        {"text": "Summary after the first compaction: guides 1-5 were read verbatim."},
        _read_call(workspace, _DOC_COUNT, _NEAR_FULL_USAGE),
        {"text": "Summary after the second compaction: guides 1-6 were read verbatim."},
    ]
    responses += [{"text": _HELD_REPLY, "block": True}] * 6
    # Claude's title/background requests advertise no tools; keep them off this queue.
    _mock_post(
        mock_url,
        "/mock/configure",
        {"key": _CHILD_MODEL, "required_tools": ["Read"], "responses": responses},
    )
    _mock_post(mock_url, "/mock/set_fallback", {"key": _CHILD_MODEL, "text": _HELD_REPLY})
    _mock_post(mock_url, "/mock/set_fallback", {"key": "default", "text": "ok"})


def _dispatch_prompt() -> str:
    files = ", ".join(f"docs/guide-{i}.md" for i in range(1, _DOC_COUNT + 1))
    return (
        "Restructure the docs information architecture. Before writing anything, read "
        f"each of these files verbatim and in full: {files}. Then move their content "
        "into docs/new/ preserving every sentence, update cross references, and "
        "commit when done."
    )


def _configure_parent_dispatch(mock_url: str) -> None:
    _mock_post(
        mock_url,
        "/mock/configure",
        {
            "key": _PARENT_MODEL,
            "responses": [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_dispatch_implementer",
                            "name": "sys_session_send",
                            "arguments": json.dumps(
                                {
                                    "agent": "implementer",
                                    "title": "docs-ia",
                                    "args": {"input": _dispatch_prompt(), "purpose": "implement"},
                                }
                            ),
                        }
                    ]
                },
                {"text": "Implementer dispatched; waiting for its result."},
            ],
        },
    )
    _mock_post(mock_url, "/mock/set_fallback", {"key": _PARENT_MODEL, "text": "Acknowledged."})


def _configure_parent_status_check(mock_url: str, child_id: str, n: int) -> None:
    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_calls": [{"call_id": f"{name}_{n}", "name": name, "arguments": json.dumps(args)}]
        }

    _mock_post(
        mock_url,
        "/mock/configure",
        {
            "key": _PARENT_MODEL,
            "responses": [
                call("sys_session_get_info", {"session_id": child_id}),
                call("sys_session_list", {}),
                call("sys_session_get_history", {"conversation_id": child_id, "tail_items": 5}),
                call("sys_read_inbox", {}),
                {"text": f"Status check {n} recorded."},
            ],
        },
    )


def _register_parent(client: httpx.Client, mock_url: str) -> str:
    name = f"compaction-stall-orchestrator-{uuid.uuid4().hex[:8]}"
    spec = {
        "name": name,
        "prompt": (
            "You are an orchestrator. Dispatch the implementer sub-agent via "
            "sys_session_send when asked, and check on it with sys_session_get_info "
            "and sys_session_list when asked for a status check."
        ),
        "executor": {
            "harness": "openai-agents",
            "model": _PARENT_MODEL,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock_url}/v1"},
        },
        "tools": {
            "implementer": {
                "type": "agent",
                "description": "Claude Code implementer sub-agent for multi-file docs work.",
                "executor": {"harness": "claude-native"},
                "prompt": (
                    "You are a coding sub-agent. Read the referenced files verbatim "
                    "before writing."
                ),
            }
        },
    }
    bundle_bytes = bundle_files({f"{name}.yaml": yaml.safe_dump(spec).encode()})
    resp = post_session_bundle(client.post, "/v1/sessions", bundle_bytes)
    assert resp.status_code in (200, 201, 409), f"{resp.status_code} {resp.text[:400]}"
    listing = client.get(
        "/v1/sessions", params={"visibility": "all", "agent_name": name, "limit": 1}
    )
    listing.raise_for_status()
    return str(listing.json()["data"][0]["agent_id"])


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    resp = client.get(
        f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 1000}, timeout=10
    )
    resp.raise_for_status()
    return resp.json()["data"]


def _item_type(item: dict[str, Any]) -> str | None:
    return item.get("type") or (item.get("data") or {}).get("type")


def _tool_outputs(items: list[dict[str, Any]]) -> dict[str, str]:
    """Map ``call_id`` to the persisted output of every function call in *items*."""
    out: dict[str, str] = {}
    for item in items:
        data = item.get("data") or {}
        if _item_type(item) == "function_call_output":
            cid = item.get("call_id") or data.get("call_id")
            text = item.get("output") or data.get("output")
            if cid and text:
                out[str(cid)] = str(text)
    return out


def _compaction_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    return [item for item in _items(client, session_id) if _item_type(item) == "compaction"]


def _gate_pending(mock_url: str) -> bool:
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5, trust_env=False)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def _wait_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _send_user_message(client: httpx.Client, session_id: str, text: str) -> None:
    send = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    assert send.status_code == 202, f"{send.status_code} {send.text}"


def _stop(client: httpx.Client, *session_ids: str) -> None:
    for sid in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            client.post(f"/v1/sessions/{sid}/events", json={"type": "stop_session"}, timeout=5)


@dataclass
class StallObservation:
    parent_id: str
    child_id: str
    get_info: list[dict[str, Any]]
    session_list: list[dict[str, Any]]
    history: list[str]
    inbox: list[str]
    child_snapshots: list[dict[str, Any]]
    activity_during_reads: list[int | None]
    compaction_items: list[dict[str, Any]]
    git_status_before: str
    git_status_after: str
    head_before: str
    head_after: str
    gate_pending: bool
    evidence: dict[str, Any] = field(default_factory=dict)


def _child_snapshot(client: httpx.Client, child_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/sessions/{child_id}", params={"include_items": "false"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="module")
def stalled_subagent(rig: Rig) -> Iterator[StallObservation]:
    """Drive the journey once and keep the orchestrator's observations."""
    client, mock_url, workspace = rig.client, rig.mock_url, rig.workspace
    _configure_child_model(mock_url, workspace)
    _configure_parent_dispatch(mock_url)
    agent_id = _register_parent(client, mock_url)
    create = client.post("/v1/sessions", json={"agent_id": agent_id})
    create.raise_for_status()
    parent_id = str(create.json()["id"])
    client.patch(f"/v1/sessions/{parent_id}", json={"runner_id": rig.runner_id}).raise_for_status()
    git_status_before = _git(workspace, "status", "--porcelain")
    head_before = _git(workspace, "rev-parse", "HEAD")

    _send_user_message(
        client, parent_id, "Dispatch the implementer sub-agent to restructure the docs."
    )

    child_id: str | None = None

    def child_seen() -> bool:
        nonlocal child_id
        resp = client.get(f"/v1/sessions/{parent_id}/child_sessions")
        if resp.status_code == 200 and resp.json().get("data"):
            row = resp.json()["data"][0]
            child_id = str(row.get("session_id") or row.get("id"))
            return True
        return False

    try:
        assert _wait_until(child_seen, timeout=120), "orchestrator never dispatched the child"
        assert child_id is not None
        activity: list[int | None] = []

        def compacted_twice_and_parked() -> bool:
            activity.append(_child_snapshot(client, child_id).get("updated_at"))
            return len(_compaction_items(client, child_id)) >= 2 and _gate_pending(mock_url)

        assert _wait_until(compacted_twice_and_parked, timeout=240, interval=2.0), (
            f"child did not compact twice and park; compaction items: "
            f"{len(_compaction_items(client, child_id))}, gate pending: {_gate_pending(mock_url)}"
        )
        # Let the forwarder persist anything still in flight before polling.
        time.sleep(5)
        snapshots = [_child_snapshot(client, child_id)]
        get_info: list[dict[str, Any]] = []
        session_list: list[dict[str, Any]] = []
        history: list[str] = []
        inbox: list[str] = []
        for n in (1, 2):
            if n == 2:
                time.sleep(_POLL_GAP_S)
            _configure_parent_status_check(mock_url, child_id, n)
            _send_user_message(
                client, parent_id, f"Status check {n}: how is the implementer doing?"
            )
            done_marker = f"sys_read_inbox_{n}"
            assert _wait_until(
                lambda done=done_marker: done in _tool_outputs(_items(client, parent_id)),
                timeout=120,
            ), f"orchestrator status check {n} did not complete"
            outputs = _tool_outputs(_items(client, parent_id))
            get_info.append(json.loads(outputs[f"sys_session_get_info_{n}"]))
            session_list.append(json.loads(outputs[f"sys_session_list_{n}"]))
            history.append(outputs[f"sys_session_get_history_{n}"])
            inbox.append(outputs[f"sys_read_inbox_{n}"])
            snapshots.append(_child_snapshot(client, child_id))
        observation = StallObservation(
            parent_id=parent_id,
            child_id=child_id,
            get_info=get_info,
            session_list=session_list,
            history=history,
            inbox=inbox,
            child_snapshots=snapshots,
            activity_during_reads=activity,
            compaction_items=_compaction_items(client, child_id),
            git_status_before=git_status_before,
            git_status_after=_git(workspace, "status", "--porcelain"),
            head_before=head_before,
            head_after=_git(workspace, "rev-parse", "HEAD"),
            gate_pending=_gate_pending(mock_url),
        )
        observation.evidence = {
            "parent_session_id": parent_id,
            "child_session_id": child_id,
            "get_info_polls": get_info,
            "session_list_polls": session_list,
            "session_list_child_rows": [_child_row(sl, child_id) for sl in session_list],
            "history_tails": history,
            "inbox_polls": inbox,
            "child_updated_at_series": activity,
            "child_snapshots": [
                {
                    k: s.get(k)
                    for k in ("status", "updated_at", "runner_online", "harness", "workspace")
                }
                for s in snapshots
            ],
            "compaction_items": observation.compaction_items,
            "git_status_after": observation.git_status_after,
            "head_unchanged": observation.head_before == observation.head_after,
            "gate_pending_after_polls": observation.gate_pending,
        }
        print("STALL_OBSERVATION " + json.dumps(observation.evidence, default=str)[:20000])
        yield observation
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_url}/gate/release", timeout=5, trust_env=False)
        if child_id is not None:
            _stop(client, child_id)
        _stop(client, parent_id)


def _child_row(listing: dict[str, Any], child_id: str) -> dict[str, Any] | None:
    """The child's global ``sessions`` row, falling back to its ``sub_agents`` entry."""
    for row in listing.get("sessions", []):
        if row.get("session_id") == child_id:
            return row
    for row in listing.get("sub_agents", []):
        if row.get("conversation_id") == child_id:
            return row
    return None


def _assert_parked_after_two_compactions(obs: StallObservation) -> None:
    """The reported stall state, cross-checked the way the reporter had to."""
    assert len(obs.compaction_items) == 2, obs.compaction_items
    assert obs.gate_pending, "child turn should still be held open"
    assert obs.history[0] == obs.history[1], "transcript tail changed between polls"
    # The runner leaves its own startup probe dir in the workspace; only new paths count.
    new_paths = set(obs.git_status_after.splitlines()) - set(obs.git_status_before.splitlines())
    assert not new_paths, f"sub-agent wrote to the workspace: {sorted(new_paths)}"
    assert obs.head_before == obs.head_after
    for info in obs.get_info:
        assert info.get("status") == "running", info
        assert info.get("runner_online") is True, info
        assert info.get("pending_elicitation_count") == 0, info
    for text in obs.inbox:
        assert "docs-ia" not in text and obs.child_id not in text, text


def test_parked_subagent_heartbeat_freezes(stalled_subagent: StallObservation) -> None:
    """``last_activity_at`` is present and stops advancing once the child parks."""
    obs = stalled_subagent
    _assert_parked_after_two_compactions(obs)
    first, second = (info.get("last_activity_at") for info in obs.get_info)
    assert isinstance(first, int) and first == second, obs.get_info
    reads = [t for t in obs.activity_during_reads if t is not None]
    assert len(set(reads)) >= 2, f"heartbeat never advanced during the read phase: {reads}"
    assert first >= max(reads), f"heartbeat {first} older than observed activity {max(reads)}"


def test_parked_subagent_metadata_reports_compactions(stalled_subagent: StallObservation) -> None:
    """``sys_session_get_info`` must expose the two compactions, not just lifecycle status."""
    obs = stalled_subagent
    _assert_parked_after_two_compactions(obs)
    info = obs.get_info[-1]
    compaction_fields = {k: v for k, v in info.items() if "compaction" in k}
    assert compaction_fields, (
        "sys_session_get_info reports no compaction signal for a sub-agent that compacted "
        f"twice and stopped advancing; keys: {sorted(info)}"
    )
    counts = [
        v for v in compaction_fields.values() if isinstance(v, int) and not isinstance(v, bool)
    ]
    assert 2 in counts, compaction_fields

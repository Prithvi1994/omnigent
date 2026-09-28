"""UI journey: a runner booting in another PID namespace must not wipe a live Claude session.

A native Claude session keeps its hook, MCP and token state in a per-session
bridge directory under the shared temp root, marked with the owning runner's
pid. Every standalone runner boot sweeps that root and deletes bridge
directories whose owner is dead. When two runners share the root from
different PID namespaces (a sandboxed or containerised runner beside a plain
one), the live runner's pid is not visible to the booting one.

The journey: answer one composer turn on a live native Claude session, boot a
second runner from a fresh PID namespace that shares ``/tmp``, then check the
live session still has its bridge state and answers the next composer turn.
Skips where the kernel or sandbox forbids creating a PID namespace.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _REPO_ROOT, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

# A fresh user + PID namespace that keeps this process's uid, so the second
# runner resolves the same per-user bridge root and can delete what it finds.
_UNSHARE = ["unshare", "--map-current-user", "--pid", "--fork", "--kill-child", "--mount-proc"]
_BRIDGE_FILE = "bridge.json"
_OWNER_FILE = "owner.pid"
_BRIDGE_READY_TIMEOUT_S = 60.0
_SECOND_RUNNER_BOOT_TIMEOUT_S = 150.0
# Each line is logged only after the runner's startup sweep has finished.
_STARTUP_DONE_MARKERS = ("runner tunnel disconnected", " connected to ", "runner exiting")
_CONNECTION_ERROR = "The connection to the agent dropped mid-turn."

_PROBE_OWNER = (
    "import os, sys\n"
    "try:\n"
    "    os.kill(int(sys.argv[1]), 0)\n"
    "except ProcessLookupError:\n"
    "    sys.exit(3)\n"
)


def _pid_namespace_unavailable() -> str | None:
    if shutil.which("unshare") is None:
        return "unshare is not installed"
    probe = subprocess.run(
        [*_UNSHARE, "true"], check=False, capture_output=True, text=True, timeout=30
    )
    if probe.returncode != 0:
        return f"cannot create a PID namespace: {probe.stderr.strip() or probe.returncode}"
    return None


def _bridge_dir(base_url: str, session_id: str) -> Path:
    from omnigent.harnesses.claude_native.bridge import (
        BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )

    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    bridge_id = (session.get("labels") or {}).get(BRIDGE_ID_LABEL_KEY) or session_id
    return bridge_dir_for_bridge_id(bridge_id)


def _wait_for_owner_pid(bridge_dir: Path) -> int:
    deadline = time.monotonic() + _BRIDGE_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if (bridge_dir / _BRIDGE_FILE).exists():
            try:
                return int((bridge_dir / _OWNER_FILE).read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                pass
        time.sleep(0.5)
    raise AssertionError(
        f"live session never advertised {_BRIDGE_FILE}/{_OWNER_FILE} in {bridge_dir}"
    )


def _owner_visible_in_fresh_namespace(pid: int) -> bool:
    probe = subprocess.run(
        [*_UNSHARE, sys.executable, "-c", _PROBE_OWNER, str(pid)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode not in (0, 3):
        raise AssertionError(f"owner probe failed in the fresh namespace: {probe.stderr}")
    return probe.returncode == 0


def _boot_runner_in_fresh_namespace(
    base_url: str, log_path: Path, stdout_path: Path
) -> subprocess.Popen[bytes]:
    from omnigent.runner.identity import token_bound_runner_id

    binding_token = secrets.token_urlsafe(32)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
    }
    env.update(
        PYTHONPATH=f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        OMNIGENT_RUNNER_ID=token_bound_runner_id(binding_token),
        OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN=binding_token,
        OMNIGENT_PROCESS_LOG_FILE=str(log_path),
        RUNNER_SERVER_URL=base_url,
    )
    with stdout_path.open("wb") as stdout_handle:
        return subprocess.Popen(
            [*_UNSHARE, sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            stdout=stdout_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _wait_for_startup_sweep(
    proc: subprocess.Popen[bytes], log_path: Path, bridge_json: Path
) -> str:
    deadline = time.monotonic() + _SECOND_RUNNER_BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not bridge_json.exists():
            return "bridge.json vanished while the second runner was booting"
        log_text = (
            log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        )
        if any(marker in log_text for marker in _STARTUP_DONE_MARKERS):
            return "second runner finished its startup sweep"
        if proc.poll() is not None:
            return f"second runner exited with code {proc.returncode}"
        time.sleep(0.5)
    return "second runner did not report finishing startup in time"


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _composer_turn(page: Page, mock_llm_server_url: str, index: int, nonce: str) -> str:
    token = f"ast-{index}-{nonce}"
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)
    _send(page, _turn_prompt(index, f"usr-{index}-{nonce}", token))
    return token


@pytest.mark.timeout(600)
def test_runner_boot_in_other_pid_namespace_preserves_live_claude_bridge(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A live Claude session keeps its bridge state and keeps answering across the sweep."""
    if reason := _pid_namespace_unavailable():
        pytest.skip(reason)
    base_url, session_id = native_claude_mock_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    reset_mock_llm(mock_llm_server_url)
    nonce = uuid.uuid4().hex[:8]

    first_token = _composer_turn(page, mock_llm_server_url, 1, nonce)
    expect(page.locator(_ASSISTANT, has_text=first_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    bridge_dir = _bridge_dir(base_url, session_id)
    owner_pid = _wait_for_owner_pid(bridge_dir)
    os.kill(owner_pid, 0)
    if _owner_visible_in_fresh_namespace(owner_pid):
        pytest.skip(f"owner pid {owner_pid} collides with a pid inside the fresh namespace")
    _log.info(
        "live bridge dir %s owned by pid %s: %s",
        bridge_dir,
        owner_pid,
        sorted(os.listdir(bridge_dir)),
    )

    runner_log = tmp_path / "second-runner.log"
    second_runner = _boot_runner_in_fresh_namespace(
        base_url, runner_log, tmp_path / "second-runner.stdout"
    )
    try:
        outcome = _wait_for_startup_sweep(second_runner, runner_log, bridge_dir / _BRIDGE_FILE)
    finally:
        _stop(second_runner)
    _log.info("second runner: %s", outcome)
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        pytest.fail(f"the live session's own runner (pid {owner_pid}) died during the test")
    bridge_survived = (bridge_dir / _BRIDGE_FILE).exists()
    _log.info(
        "bridge dir after the second runner booted: %s",
        sorted(os.listdir(bridge_dir)) if bridge_dir.exists() else "<gone>",
    )

    second_token = _composer_turn(page, mock_llm_server_url, 2, nonce)
    reply = page.locator(_ASSISTANT, has_text=second_token).first
    connection_error = page.get_by_role("button", name=_CONNECTION_ERROR)
    expect(reply.or_(connection_error).first).to_be_visible(timeout=_MOCK_TURN_TIMEOUT_MS)

    assert bridge_survived, (
        f"a runner booting in another PID namespace deleted the live session's "
        f"{bridge_dir / _BRIDGE_FILE} while its owner pid {owner_pid} was still running "
        f"({outcome}); the next composer turn then failed with {_CONNECTION_ERROR!r}"
    )
    expect(reply).to_be_visible(timeout=_MOCK_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

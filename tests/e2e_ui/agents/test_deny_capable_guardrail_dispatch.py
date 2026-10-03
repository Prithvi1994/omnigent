"""UI journey: a deny-capable guardrail must not break sub-agent dispatch.

The session's agent bundle declares a function-type ``guardrails`` policy
whose CEL expression *can* return DENY: it ALLOWs an allowlist of tool
names (including ``sys_session_send``) and DENYs everything else. The
allowlist covers the dispatch the parent actually makes, so the policy
never denies the real ``sys_session_send`` call. The user asks the
orchestrator to dispatch its worker; after the turn settles the Agents
rail must list the worker sub-agent row.

The defect this guards against: before dispatching, the runner evaluates
a synthetic ``sys_agent_start`` start-probe gate against the same policy.
A deny-capable policy's terminal DENY branch denies that probe, so the
session's inbox is never created, and the parent's ``sys_session_send``
then fails with ``Error: sys_session_send requires parent session inbox``.
On the buggy build no worker spawns and the Agents rail stays empty, so
``rows.first`` never appears and this test FAILS; allowlisting
``sys_agent_start`` (a byte-identical bundle except the terminal branch)
restores dispatch.

Registration and mock scripting mirror ``test_spawn_bounds_fanout_cap``:
the strict ``config.yaml`` bundle parser that honors ``guardrails`` plus
the two-file parent+child bundle shape driven over openai-agents.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_SUBAGENT_ROW = '[data-testid="subagent-row"]'

# Sentinel ending the parent's dispatch turn so the test can wait on it.
# It renders on both the buggy and fixed builds (a failed dispatch returns
# an error string and the turn still runs to its scripted end), so the
# worker row — not the sentinel — is the discriminating signal.
_PARENT_TURN_DONE = "PARENT_DISPATCH_TURN_DONE"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# The dispatch turn runs the child turn and a parent auto-wake over the
# mock LLM, so give the terminal bubble a generous budget.
_TURN_TIMEOUT_MS = 180_000

_PARENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are an orchestrator. When asked to run, dispatch the worker
  sub-agent once with sys_session_send, then finish.

executor:
  model: {parent_model}
  config:
    harness: openai-agents

tools:
  agents:
    - worker

guardrails:
  policies:
    allowlist_then_deny:
      type: function
      "on": [tool_call]
      function:
        path: omnigent.policies.builtins.cel.cel_policy
        arguments:
          expression: >
            event.type != "tool_call"
              ? {{"result": "ALLOW"}}
              : has(event.data.name)
                && type(event.data.name) == string
                && event.data.name.matches("^(ToolSearch|sys_session_send|sys_read_inbox)$")
                ? {{"result": "ALLOW"}}
                : {{"result": "DENY"}}

os_env:
  type: caller_process
  cwd: .
"""

_WORKER_YAML = """\
spec_version: 1
name: worker
prompt: |
  You are a worker. Acknowledge the task you were given and finish.

executor:
  model: {child_model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
"""


@dataclass(frozen=True)
class DenyCapableSession:
    """Handle for the deny-capable-guardrail orchestrator session fixture.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound parent session id.
    """

    base_url: str
    session_id: str


@pytest.fixture
def deny_capable_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DenyCapableSession]:
    """Create a runner-bound session for the deny-capable-guardrail orchestrator.

    Scripts the parent queue to dispatch the worker once with
    ``sys_session_send`` then end the turn; the worker child draws a canned
    acknowledgement. Unique per-run model keys isolate the queues.

    :param live_server: Spawned server fixture from the parent conftest.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: A :class:`DenyCapableSession` handle.
    """
    uid = uuid.uuid4().hex[:8]
    agent_name = f"deny_capable_probe_{uid}"
    parent_model = f"denycap-parent-{uid}"
    child_model = f"denycap-child-{uid}"

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_worker",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": "deny-capable-dispatch",
                                "args": "Acknowledge the task and finish.",
                            }
                        ),
                    },
                ],
            },
            {"text": _PARENT_TURN_DONE},
        ],
        key=parent_model,
    )
    # Absorb the parent's inbox auto-wake continuation (fired when the child
    # actually runs) so queue exhaustion can't fail the wake turn.
    set_fallback_mock_llm(mock_llm_server_url, parent_model, "PARENT_WAKE_DONE")
    set_fallback_mock_llm(mock_llm_server_url, child_model, "WORKER_ACK_DONE")

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    parent_yaml = _PARENT_YAML.format(name=agent_name, parent_model=parent_model).encode()
    worker_yaml = _WORKER_YAML.format(child_model=child_model).encode()
    # config.yaml selects the strict parser that honors guardrails.
    bundle = bundle_files({"config.yaml": parent_yaml, "agents/worker/config.yaml": worker_yaml})
    create_resp = post_session_bundle(
        httpx.post, f"{live_server}/v1/sessions", bundle, timeout=30.0
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)

    try:
        yield DenyCapableSession(base_url=live_server, session_id=session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


@pytest.mark.timeout(600)
def test_deny_capable_guardrail_allows_subagent_dispatch(
    request: pytest.FixtureRequest,
    deny_capable_session: DenyCapableSession,
) -> None:
    """A deny-capable guardrail that ALLOWs the dispatch still spawns the worker."""
    chat = deny_capable_session
    # Create the recorded page only after the session is set up and bound,
    # so recording starts at the session load, not blank setup time.
    page = request.getfixturevalue("page")
    assert isinstance(page, Page)
    page.goto(f"{chat.base_url}/c/{chat.session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Please dispatch the worker sub-agent now, then finish.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The dispatch turn finished (scripted terminal sentinel rendered). This
    # renders even when the dispatch failed, so it only paces the test.
    expect(page.locator(_ASSISTANT, has_text=_PARENT_TURN_DONE).first).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
    agents_tab.click()
    rows = rail.locator(_SUBAGENT_ROW)

    # The sole discriminating signal: the allowed dispatch must materialize a
    # worker row. On the buggy build the start-probe gate denied the session's
    # inbox, sys_session_send errored, and no worker ever spawned.
    expect(rows.first).to_be_visible(timeout=60_000)

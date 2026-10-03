"""Verify deny-capable tool policies neither block nor weaken session initialization.

Before spawning the harness, the runner probes the spec's tool policies with a
synthetic ``sys_agent_start`` call. A policy that allowlists tool names and
DENYs everything else rejects that probe name. Session init must still
succeed, register the session inbox, and apply the sandbox transform of an
``enforce_sandbox`` policy declared in the same bundle, in either order.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.policies import FunctionPolicy
from omnigent.runner import app as runner_app_module
from omnigent.runner import create_runner_app
from omnigent.runner.policy import (
    AGENT_START_TOOL,
    AgentStartPolicyError,
    RunnerToolPolicyGate,
    _GatedPolicy,
)
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    Phase,
    PhaseSelector,
)
from tests.runner.helpers import NullServerClient

_DENY_CAPABLE_EXPRESSION = (
    'event.type != "tool_call"\n'
    '  ? {"result": "ALLOW"}\n'
    "  : has(event.data.name)\n"
    "    && type(event.data.name) == string\n"
    '    && event.data.name.matches("^(ToolSearch|sys_session_send|sys_read_inbox)$")\n'
    '    ? {"result": "ALLOW"}\n'
    '    : {"result": "DENY"}\n'
)

_START_PROBE_ARGS: dict[str, object] = {
    "agent_name": "deny-capable-policy-agent",
    "harness": "claude-sdk",
    "sandbox": {"type": "none"},
}


def _allowlist_then_deny() -> FunctionPolicySpec:
    """Tool-call allowlist whose terminal branch DENYs every other tool name."""
    return FunctionPolicySpec(
        name="allowlist_then_deny",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
        function=FunctionRef(
            path="omnigent.policies.builtins.cel.cel_policy",
            arguments={"expression": _DENY_CAPABLE_EXPRESSION},
        ),
    )


def _force_bwrap() -> FunctionPolicySpec:
    """``enforce_sandbox`` forcing bwrap without network on agent start."""
    return FunctionPolicySpec(
        name="force_bwrap",
        on=None,
        function=FunctionRef(
            path="omnigent.policies.builtins.safety.enforce_sandbox",
            arguments={"sandbox_type": "linux_bwrap", "allow_network": False},
        ),
    )


def _unresolvable_policy() -> FunctionPolicySpec:
    """A tool-call policy whose function path cannot be imported."""
    return FunctionPolicySpec(
        name="unresolvable_sandbox",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
        function=FunctionRef(
            path="omnigent.policies.builtins.does_not_exist",
            arguments={},
        ),
    )


# A leading DENY skips enforce_sandbox entirely; a trailing one discards the
# transform enforce_sandbox already produced. Both must keep the sandbox.
_POLICY_ORDERS = {
    "sandbox_then_allowlist": lambda: [_force_bwrap(), _allowlist_then_deny()],
    "allowlist_then_sandbox": lambda: [_allowlist_then_deny(), _force_bwrap()],
}


class _ScriptedHarnessClient:
    """Minimal harness client stub — session init only spawns, never calls."""

    async def close(self) -> None:
        """No-op close."""


class _FakeProcessManager:
    """Captures ``get_client`` calls so tests can assert the harness spawned."""

    handles_tool_dispatch = True

    def __init__(self) -> None:
        self._client = _ScriptedHarnessClient()
        self._sessions: set[str] = set()
        self.get_client_calls: list[tuple[str, str, dict[str, str] | None]] = []

    async def get_client(
        self, conversation_id: str, harness: str, env: Any = None
    ) -> _ScriptedHarnessClient:
        """Record the spawn and return the stub client."""
        self.get_client_calls.append((conversation_id, harness, env))
        self._sessions.add(conversation_id)
        return self._client

    def has_session(self, conversation_id: str) -> bool:
        """Return whether the session spawned."""
        return conversation_id in self._sessions

    async def forward_cancel(self, conversation_id: str) -> bool:
        """Accept cancellation."""
        del conversation_id
        return True

    async def release(self, conversation_id: str) -> None:
        """Forget a released session."""
        self._sessions.discard(conversation_id)

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub."""
        del conversation_id


@contextlib.asynccontextmanager
async def _runner_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an ASGI client for the runner app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


def _spec(*policies: FunctionPolicySpec) -> AgentSpec:
    """Build a claude-sdk spec declaring ``sandbox.type: none`` plus *policies*."""
    return AgentSpec(
        spec_version=1,
        name="deny-capable-policy-agent",
        executor=ExecutorSpec(
            config={"harness": "claude-sdk"},
            model="databricks-claude-sonnet-4-6",
        ),
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
        guardrails=GuardrailsSpec(policies=list(policies)),
    )


async def _create_session(
    spec: AgentSpec, session_id: str
) -> tuple[httpx.Response, _FakeProcessManager]:
    """POST the runner's session init for *spec* and return the response and spawns."""
    pm = _FakeProcessManager()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "ag_test"},
        )
    return resp, pm


def _spawned_sandbox(pm: _FakeProcessManager) -> dict[str, Any]:
    """Return the sandbox block the runner serialized into the harness spawn env."""
    assert pm.get_client_calls, "harness was not spawned"
    _conv_id, _harness, env = pm.get_client_calls[-1]
    assert env is not None, "spawn env was None"
    os_env_json = env.get("HARNESS_CLAUDE_SDK_OS_ENV")
    assert os_env_json is not None, "HARNESS_CLAUDE_SDK_OS_ENV missing from spawn env"
    return json.loads(os_env_json).get("sandbox", {})


@pytest.mark.asyncio
async def test_deny_capable_policy_does_not_block_session_init() -> None:
    """A fail-closed tool policy still permits session initialization."""
    session_id = "conv_deny_capable_init"
    try:
        resp, pm = await _create_session(_spec(_allowlist_then_deny()), session_id)

        assert resp.status_code == 201, (
            f"Session init must succeed despite the deny-capable policy; "
            f"got {resp.status_code}: {resp.text}"
        )
        assert pm.has_session(session_id), "harness was not spawned"
        assert session_id in runner_app_module._session_inboxes_ref, (
            "session inbox was not created — sub-agent dispatch would fail "
            "with 'requires parent session inbox'"
        )
    finally:
        runner_app_module._session_inboxes_ref.pop(session_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", sorted(_POLICY_ORDERS))
async def test_deny_capable_policy_does_not_suppress_enforce_sandbox(order: str) -> None:
    """``enforce_sandbox`` still forces bwrap when an allowlist DENYs the start probe."""
    session_id = f"conv_deny_capable_sandbox_{order}"
    try:
        resp, pm = await _create_session(_spec(*_POLICY_ORDERS[order]()), session_id)

        assert resp.status_code == 201, f"got {resp.status_code}: {resp.text}"
        sandbox = _spawned_sandbox(pm)
        assert sandbox.get("type") == "linux_bwrap", (
            f"[{order}] enforce_sandbox should force linux_bwrap but the spawn env "
            f"carries {sandbox!r}: the allowlist's DENY on the sys_agent_start probe "
            "suppressed the sandbox transform"
        )
        assert sandbox.get("allow_network") is False
        assert session_id in runner_app_module._session_inboxes_ref
    finally:
        runner_app_module._session_inboxes_ref.pop(session_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", sorted(_POLICY_ORDERS))
async def test_start_probe_keeps_sandbox_transform_past_a_deny(order: str) -> None:
    """The gate composes start transforms even when another policy DENYs the probe."""
    gate = RunnerToolPolicyGate.from_spec(_spec(*_POLICY_ORDERS[order]()))

    data = await gate.evaluate_agent_start(dict(_START_PROBE_ARGS))

    assert isinstance(data, dict), f"[{order}] expected a composed payload, got {data!r}"
    assert data["arguments"]["sandbox"]["type"] == "linux_bwrap"
    assert data["arguments"]["sandbox"]["allow_network"] is False


@pytest.mark.asyncio
async def test_start_probe_without_transforms_keeps_real_tool_enforcement() -> None:
    """An allowlist alone yields no start transform and still gates real tool calls."""
    gate = RunnerToolPolicyGate.from_spec(_spec(_allowlist_then_deny()))

    assert await gate.evaluate_agent_start(dict(_START_PROBE_ARGS)) is None

    allowed = await gate.evaluate_tool_call("sys_session_send", {"agent": "child"})
    assert allowed.action == "allow"
    denied = await gate.evaluate_tool_call("shell", {"command": "ls"})
    assert denied.action == "deny"
    assert denied.policy_name == "allowlist_then_deny"


@pytest.mark.asyncio
async def test_start_probe_fails_closed_when_a_start_policy_raises() -> None:
    """A policy that raises aborts the launch instead of dropping its transform."""

    def _boom(_event: object) -> dict[str, object]:
        raise RuntimeError("transform policy bug")

    gated = _GatedPolicy(
        name="force_bwrap",
        policy=FunctionPolicy(_force_bwrap(), _boom),
        phases=frozenset([Phase.TOOL_CALL]),
    )
    gate = RunnerToolPolicyGate([gated])

    with pytest.raises(AgentStartPolicyError):
        await gate.evaluate_agent_start(dict(_START_PROBE_ARGS))


@pytest.mark.asyncio
async def test_start_probe_drops_ask_verdict_transform() -> None:
    """A resolved ASK does not contribute its transform, unlike a real tool call."""

    def _ask_with_sandbox(_event: object) -> dict[str, object]:
        return {
            "result": "ASK",
            "data": {
                "name": AGENT_START_TOOL,
                "arguments": {"sandbox": {"type": "none"}},
            },
        }

    gated = _GatedPolicy(
        name="ask_with_sandbox",
        policy=FunctionPolicy(_force_bwrap(), _ask_with_sandbox),
        phases=frozenset([Phase.TOOL_CALL]),
    )
    gate = RunnerToolPolicyGate([gated])

    # evaluate_tool_call would compose this ASK's data; the start probe drops it,
    # so a non-ALLOW verdict produces no launch transform.
    assert await gate.evaluate_agent_start(dict(_START_PROBE_ARGS)) is None


@pytest.mark.asyncio
async def test_start_probe_fails_closed_on_malformed_transform() -> None:
    """An ALLOW payload that drops the probe shape fails the launch closed."""

    def _malformed(_event: object) -> dict[str, object]:
        # ALLOW, but the data drops the name/arguments shape the next policy and
        # the sandbox override both read, so the restriction would silently
        # vanish if the chain accepted it.
        return {"result": "ALLOW", "data": {"sandbox": {"type": "none"}}}

    gated = _GatedPolicy(
        name="malformed_transform",
        policy=FunctionPolicy(_force_bwrap(), _malformed),
        phases=frozenset([Phase.TOOL_CALL]),
    )
    gate = RunnerToolPolicyGate([gated])

    with pytest.raises(AgentStartPolicyError):
        await gate.evaluate_agent_start(dict(_START_PROBE_ARGS))


@pytest.mark.asyncio
async def test_start_probe_fails_closed_on_unresolved_policy_sentinel() -> None:
    """An unresolvable configured policy fails the probe closed, not open."""
    gate = RunnerToolPolicyGate.from_spec(_spec(_unresolvable_policy()))

    with pytest.raises(AgentStartPolicyError):
        await gate.evaluate_agent_start(dict(_START_PROBE_ARGS))

    # The sentinel keeps denying real tool calls — fail-closed posture intact.
    denied = await gate.evaluate_tool_call("sys_session_send", {"agent": "child"})
    assert denied.action == "deny"


@pytest.mark.asyncio
async def test_session_init_fails_closed_when_a_start_policy_cannot_resolve() -> None:
    """Session init refuses to spawn when a configured tool policy cannot resolve."""
    session_id = "conv_unresolvable_policy_init"
    try:
        resp, pm = await _create_session(_spec(_unresolvable_policy()), session_id)

        assert resp.status_code == 403, (
            f"an unresolvable policy must fail session init closed; "
            f"got {resp.status_code}: {resp.text}"
        )
        assert not pm.has_session(session_id), "harness spawned despite the unevaluable policy"
        assert session_id not in runner_app_module._session_inboxes_ref
    finally:
        runner_app_module._session_inboxes_ref.pop(session_id, None)

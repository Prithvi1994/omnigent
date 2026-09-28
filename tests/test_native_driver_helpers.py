"""Failure-path checks for reusable native journey observations."""

import json

import httpx
import pytest

from tests.e2e_ui.native_driver import (
    DriverSetupError,
    LogWindow,
    inject_child_start,
    send_message,
    wait_child_task,
    wait_native_delegation,
)


def test_rejected_message_fails_before_any_wait():
    def respond(request):
        body = json.loads(request.content)
        assert body["data"]["content"] == [{"type": "input_text", "text": "hello"}]
        return httpx.Response(400, json={"error": "invalid event"})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DriverSetupError, match="400"):
            send_message(client, "session", "hello")


@pytest.mark.parametrize("response", [{"queued": False}, {"queued": True}])
def test_message_requires_accepted_identity(response):
    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    ) as client:
        with pytest.raises(DriverSetupError, match="accepted input id"):
            send_message(client, "session", "hello")


def test_typed_synthetic_child_requires_returned_link():
    def respond(request):
        body = json.loads(request.content)
        assert body["type"] == "external_subagent_start"
        assert body["data"]["tool_use_id"] == "call"
        return httpx.Response(202, json={"child_session_id": "child"})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        assert (
            inject_child_start(
                client,
                "parent",
                subagent_id="agent",
                agent_type="researcher",
                description="inspect",
                tool_use_id="call",
            )
            == "child"
        )


@pytest.mark.parametrize("missing", ["invocation", "result", "child", None])
def test_native_proof_requires_exact_call_and_link(missing):
    items = [
        {"type": "function_call", "name": "Agent", "call_id": "call"},
        {"type": "function_call_output", "call_id": "call", "output": "done"},
    ]
    child = {
        "id": "child",
        "parent_session_id": "parent",
        "labels": {"omnigent.claude_native.tool_use_id": "call"},
    }
    if missing == "invocation":
        items[0]["call_id"] = "earlier-call"
    if missing == "result":
        items[1]["call_id"] = "earlier-call"
    if missing == "child":
        child["labels"]["omnigent.claude_native.tool_use_id"] = "earlier-call"

    def respond(request):
        return httpx.Response(
            200, json={"data": [child] if request.url.path.endswith("child_sessions") else items}
        )

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        if missing:
            with pytest.raises(AssertionError, match="not observed"):
                wait_native_delegation(
                    client, "parent", call_id="call", tool_name="Agent", timeout=0
                )
        else:
            assert (
                wait_native_delegation(
                    client, "parent", call_id="call", tool_name="Agent", timeout=0
                ).child_id
                == "child"
            )


@pytest.mark.parametrize(
    "task,status,busy,passes",
    [
        ("old-task", "completed", False, False),
        ("task", None, False, False),
        ("task", "in_progress", False, False),
        ("task", "failed", False, False),
        ("task", "completed", True, False),
        ("task", "completed", False, True),
    ],
)
def test_completion_requires_exact_successful_task(task, status, busy, passes):
    child = {
        "id": "child",
        "parent_session_id": "parent",
        "current_task_id": task,
        "current_task_status": status,
        "busy": busy,
    }
    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [child]})),
    ) as client:
        if passes:
            assert wait_child_task(client, "parent", "child", "task", timeout=0) == child
        else:
            with pytest.raises(AssertionError):
                wait_child_task(client, "parent", "child", "task", timeout=0)


def test_logs_require_attempt_window_and_all_exact_identifiers(tmp_path):
    path = tmp_path / "runner.log"
    path.write_text("session=child turn=turn old warning\n")
    window = LogWindow.begin(path)
    with path.open("a") as handle:
        handle.write(
            "session=child-other turn=turn warning\n"
            "session=child turn=turn-old warning\n"
            "session=child turn=turn warning\n"
            "agent=general-purpose unrelated warning\n"
        )
    assert window.finish(session_id="child", turn_id="turn") == ["session=child turn=turn warning"]
    path.write_text("")
    with pytest.raises(AssertionError, match="truncated"):
        window.finish(session_id="child")

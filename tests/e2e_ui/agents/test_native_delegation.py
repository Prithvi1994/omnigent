"""Real native delegation through Claude's advertised tool and transcript bridge."""

import json
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _CLAUDE_MOCK_MODEL,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)
from tests.e2e_ui.native_driver import (
    navigate_to_child,
    send_composer_message,
    wait_claude_completion,
    wait_native_delegation,
)
from tests.server.integration.mock_llm_server import MockState


@pytest.mark.nightly
@pytest.mark.timeout(300)
@pytest.mark.parametrize("agent_type", ["general-purpose", "Explore"])
def test_native_claude_delegation(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    agent_type: str,
    tmp_path: Path,
) -> None:
    base_url, parent_id = native_claude_mock_session
    nonce = uuid.uuid4().hex
    probe = f"probe-{nonce}"
    parent_marker = f"parent-{nonce}"
    child_marker = f"worker-request-{nonce}"
    child_reply = f"worker-finished-{nonce}"
    parent_reply = f"parent-finished-{nonce}"
    call_id = f"toolu_{nonce}"
    page.goto(f"{base_url}/c/{parent_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", probe)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, probe)
    send_composer_message(page, parent_id, probe)
    expect(page.locator(_ASSISTANT, has_text=probe).last).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    with httpx.Client(base_url=mock_llm_server_url, timeout=10) as mock:
        requests = mock.get("/mock/requests").raise_for_status().json()["requests"]
        tools = [
            tool
            for request in requests
            if probe in MockState._user_input_text(request)
            for tool in request.get("tools", [])
            if tool.get("name") in {"Task", "Agent"}
        ]
        assert tools, "Native CLI did not advertise a delegation tool"
        tool = tools[-1]
        tool_name = tool["name"]
        properties = tool["input_schema"]["properties"]
        assert {"subagent_type", "prompt", "description"} <= properties.keys(), tool
        arguments = {
            "subagent_type": agent_type,
            "description": f"Inspect {nonce[:8]}",
            "prompt": child_marker,
        }
        if "run_in_background" in properties:
            arguments["run_in_background"] = False
        parent_key = configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": call_id,
                            "name": tool_name,
                            "arguments": json.dumps(arguments),
                        }
                    ]
                },
                {"text": parent_reply},
            ],
            match=parent_marker,
            required_tools=[tool_name],
        )
        child_key = configure_mock_llm(
            mock_llm_server_url,
            [{"text": child_reply}],
            match=child_marker,
            required_tools=["Read"],
        )
        queues = mock.get("/mock/queues").raise_for_status().json()["queues"]
        assert parent_key != child_key
        assert queues[parent_key]["remaining"] == 2
        assert queues[child_key]["remaining"] == 1
        # Background title requests carry the user's nonce but no native tools.
        title = mock.post(
            "/v1/messages",
            json={
                "model": "title-model",
                "messages": [
                    {"role": "user", "content": parent_marker},
                ],
            },
        )
        title.raise_for_status()
        queues_after_title = mock.get("/mock/queues").raise_for_status().json()["queues"]
        assert queues_after_title[parent_key]["remaining"] == 2, queues_after_title
        send_composer_message(page, parent_id, parent_marker)
        expect(page.locator(_ASSISTANT, has_text=parent_reply).last).to_be_visible(timeout=90_000)
        expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
        with httpx.Client(base_url=base_url, timeout=10) as client:
            proof = wait_native_delegation(client, parent_id, call_id=call_id, tool_name=tool_name)
        completion = wait_claude_completion(mock, call_id=call_id, expected_text=child_reply)
        (tmp_path / "native-delegation.json").write_text(
            json.dumps({"delegation": asdict(proof), "completion": completion}, indent=2)
        )
        selections = mock.get("/mock/selections").raise_for_status().json()["selections"]
        assert any(
            selection["key"] == parent_key
            and selection["response"]["tool_calls"]
            and selection["response"]["tool_calls"][0]["call_id"] == call_id
            for selection in selections
        )
    navigate_to_child(page, proof.child_id)
    _ensure_chat_view(page)
    expect(page.locator(_ASSISTANT, has_text=child_reply).last).to_be_visible(timeout=30_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)

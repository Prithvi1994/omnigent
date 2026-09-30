"""Tests for the claude-native reconnect transcript re-forward.

A mid-turn server restart drops the live forwarder's in-flight POSTs; its poll
loop advances past them and never retries, so those items land in the terminal
transcript but not the chat view. ``reforward_transcript_items_on_reconnect``
re-reads the transcript from the live-portion boundary and re-POSTs each item
under its original ``source_id``; the server derives the item id from that
source id and the append is idempotent, so already-persisted items come back
deduplicated (no-ops) and only the gap items are inserted — no duplicates.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import (
    prepare_bridge_dir,
    read_transcript_items_from_offset,
    record_hook_event,
)
from omnigent.harnesses.claude_native.forwarder import (
    reforward_transcript_items_on_reconnect,
)

_SESSION = "conv_reforward"
_AGENT = "claude-native-ui"


def _stable_id(source_id: str) -> str:
    """Mirror the server's source_id -> stable_id derivation."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"omnigent-external-item:{_SESSION}:{source_id}").hex


def _write_transcript(path: Path) -> None:
    """Write a small real-shape Claude transcript: a user turn + assistant reply."""
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "message": {"role": "user", "content": "explain TCP slow start"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Slow start grows cwnd..."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _bridge_with_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    bridge_dir = prepare_bridge_dir(_SESSION, bridge_id="bridge_reforward", workspace=tmp_path)
    transcript_path = tmp_path / "session.jsonl"
    _write_transcript(transcript_path)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "claude-uuid-1",
            "transcript_path": str(transcript_path),
        },
    )
    return bridge_dir


class _FakeServer:
    """Simulates the server's source_id->stable_id dedup on POST /events."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.posted_source_ids: list[str] = []
        self.insert_count = 0
        self.fail = fail

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if self.fail:
                return httpx.Response(500, json={"error": "server down"})
            body = json.loads(request.content.decode("utf-8"))
            assert body["type"] == "external_conversation_item"
            source_id = body["data"]["source_id"]
            self.posted_source_ids.append(source_id)
            sid = _stable_id(source_id)
            deduplicated = sid in self.store
            if not deduplicated:
                self.store[sid] = body["data"]
                self.insert_count += 1
            return httpx.Response(200, json={"item_id": sid, "deduplicated": deduplicated})

        return httpx.MockTransport(handler)


def _patch_open_client(monkeypatch: pytest.MonkeyPatch, server: _FakeServer) -> None:
    transport = server.transport()

    @asynccontextmanager
    async def _fake_open(
        base_url: str, *, headers: Any = None, auth: Any = None, timeout: Any = None
    ) -> Any:
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            yield client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", _fake_open)


def _expected_source_ids(bridge_dir: Path, offset: int = 0) -> list[str]:
    from omnigent.harnesses.claude_native.bridge import read_transcript_path

    path = read_transcript_path(bridge_dir)
    assert path is not None
    result = read_transcript_items_from_offset(path, offset, start_line=0, agent_name=_AGENT)
    return [item.source_id for item in result.items]


@pytest.mark.asyncio
async def test_reforward_recovers_gap_items_without_duplicating_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assistant reply lost during the outage is delivered; the already-persisted
    user message comes back deduplicated (no duplicate)."""
    bridge_dir = _bridge_with_transcript(monkeypatch, tmp_path)
    source_ids = _expected_source_ids(bridge_dir)
    assert len(source_ids) >= 2, "transcript should yield a user + assistant item"
    user_sid, assistant_sid = source_ids[0], source_ids[-1]

    server = _FakeServer()
    # Simulate: the user message WAS persisted before the crash (relay/forwarder
    # got it through), but the assistant reply's POST was lost during the outage.
    server.store[_stable_id(user_sid)] = {"pre": "existing"}
    server.insert_count = 1
    _patch_open_client(monkeypatch, server)

    delivered = await reforward_transcript_items_on_reconnect(
        base_url="http://ap",
        headers={},
        session_id=_SESSION,
        bridge_dir=bridge_dir,
        agent_name=_AGENT,
        start_at_offset=None,
        auth=None,
    )

    assert delivered == len(source_ids), "every live-portion item is re-POSTed"
    # No duplicate: user message stays a single row (server deduped its re-post),
    # only the missing assistant reply was inserted.
    assert _stable_id(assistant_sid) in server.store
    assert server.insert_count == 2, "exactly one new row (the recovered assistant reply)"


@pytest.mark.asyncio
async def test_reforward_is_idempotent_across_reconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the re-forward twice inserts nothing new the second time."""
    bridge_dir = _bridge_with_transcript(monkeypatch, tmp_path)
    server = _FakeServer()
    _patch_open_client(monkeypatch, server)

    async def _run() -> int:
        return await reforward_transcript_items_on_reconnect(
            base_url="http://ap",
            headers={},
            session_id=_SESSION,
            bridge_dir=bridge_dir,
            agent_name=_AGENT,
            start_at_offset=None,
            auth=None,
        )

    await _run()
    inserts_after_first = server.insert_count
    await _run()

    assert inserts_after_first > 0, "first pass inserts the transcript items"
    assert server.insert_count == inserts_after_first, "second pass adds no duplicates"


@pytest.mark.asyncio
async def test_reforward_skips_resume_prefix_below_start_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE TRAP: starting at a resume-prefix offset must NOT re-post the prefix
    (its synthesized source_ids differ from the committed items → would dup)."""
    bridge_dir = _bridge_with_transcript(monkeypatch, tmp_path)
    from omnigent.harnesses.claude_native.bridge import read_transcript_path

    transcript_path = read_transcript_path(bridge_dir)
    assert transcript_path is not None
    # Offset past the first (user) record = the live-portion boundary.
    first_line_bytes = len(
        (transcript_path.read_text(encoding="utf-8").splitlines(keepends=True)[0]).encode("utf-8")
    )
    prefix_source_ids = _expected_source_ids(bridge_dir, offset=0)
    live_source_ids = _expected_source_ids(bridge_dir, offset=first_line_bytes)
    assert prefix_source_ids[0] not in live_source_ids, "first item is below the boundary"

    server = _FakeServer()
    _patch_open_client(monkeypatch, server)
    await reforward_transcript_items_on_reconnect(
        base_url="http://ap",
        headers={},
        session_id=_SESSION,
        bridge_dir=bridge_dir,
        agent_name=_AGENT,
        start_at_offset=first_line_bytes,
        auth=None,
    )
    # The prefix (first record) item was never posted; only live-portion items were.
    assert prefix_source_ids[0] not in server.posted_source_ids
    assert server.posted_source_ids, "live-portion items are still re-forwarded"


@pytest.mark.asyncio
async def test_reforward_best_effort_on_server_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing server does not raise out of the re-forward (best-effort)."""
    bridge_dir = _bridge_with_transcript(monkeypatch, tmp_path)
    server = _FakeServer(fail=True)
    _patch_open_client(monkeypatch, server)

    delivered = await reforward_transcript_items_on_reconnect(
        base_url="http://ap",
        headers={},
        session_id=_SESSION,
        bridge_dir=bridge_dir,
        agent_name=_AGENT,
        start_at_offset=None,
        auth=None,
    )
    assert delivered == 0, "no items counted as delivered when every POST fails"


@pytest.mark.asyncio
async def test_reforward_no_transcript_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No transcript reported yet → clean no-op (0 delivered, no client opened)."""
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    bridge_dir = prepare_bridge_dir(_SESSION, bridge_id="bridge_empty", workspace=tmp_path)

    delivered = await reforward_transcript_items_on_reconnect(
        base_url="http://ap",
        headers={},
        session_id=_SESSION,
        bridge_dir=bridge_dir,
        agent_name=_AGENT,
        start_at_offset=None,
        auth=None,
    )
    assert delivered == 0

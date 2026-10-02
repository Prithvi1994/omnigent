"""A forked Claude clone defers to a clone id another launch already recorded.

Two launches racing on a fresh fork each write a clone transcript and try to
record its id; the server keeps the first. The later launch must resume that
recorded clone so the live Claude session matches ``external_session_id``.

Usage::

    pytest tests/runner/test_claude_fork_clone_external_id.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent.runner.native.orchestration import _preset_fork_clone_external_session_id

_SESSION_ID = "9d2c4b6a8e0f4a1c3e5b7d9f1a3c5e70"
_OURS = "5f3c264a-5585-4e8e-8025-f4873be3117b"
_RECORDED = "1ac75d5c-3dc8-46fe-90d6-649efeacdc1e"


def _client(patch_status: int, recorded: str | None) -> httpx.AsyncClient:
    """
    Build an AP client whose PATCH returns *patch_status* and GET reports *recorded*.

    :param patch_status: Status for ``PATCH /v1/sessions/{id}``, e.g. ``400``.
    :param recorded: ``external_session_id`` the session GET returns.
    :returns: Client backed by a mock transport.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{_SESSION_ID}"
        if request.method == "PATCH":
            assert json.loads(request.content) == {"external_session_id": _OURS}
            return httpx.Response(patch_status, json={})
        return httpx.Response(200, json={"external_session_id": recorded})

    return httpx.AsyncClient(base_url="http://ap.test", transport=httpx.MockTransport(_handler))


def _write(path: Path) -> Path:
    """
    Write a one-record transcript at *path*.

    :param path: Transcript path, e.g. ``tmp/<uuid>.jsonl``.
    :returns: *path*.
    """
    path.write_text('{"type": "user"}\n')
    return path


@pytest.mark.asyncio
async def test_recorded_id_keeps_our_clone(tmp_path: Path) -> None:
    """A successful PATCH resumes the clone this launch wrote."""
    ours = _write(tmp_path / f"{_OURS}.jsonl")
    async with _client(200, None) as client:
        result = await _preset_fork_clone_external_session_id(client, _SESSION_ID, _OURS, ours)
    assert result == (_OURS, ours)
    assert ours.is_file()


@pytest.mark.asyncio
async def test_rejected_id_resumes_the_already_recorded_clone(tmp_path: Path) -> None:
    """A 400 resumes the clone the server already records and drops ours."""
    ours = _write(tmp_path / f"{_OURS}.jsonl")
    recorded = _write(tmp_path / f"{_RECORDED}.jsonl")
    async with _client(400, _RECORDED) as client:
        result = await _preset_fork_clone_external_session_id(client, _SESSION_ID, _OURS, ours)
    assert result == (_RECORDED, recorded)
    assert not ours.exists()


@pytest.mark.asyncio
async def test_rejected_id_keeps_our_clone_when_recorded_transcript_is_missing(
    tmp_path: Path,
) -> None:
    """Without the recorded clone on disk, this launch keeps resuming its own."""
    ours = _write(tmp_path / f"{_OURS}.jsonl")
    async with _client(400, _RECORDED) as client:
        result = await _preset_fork_clone_external_session_id(client, _SESSION_ID, _OURS, ours)
    assert result == (_OURS, ours)
    assert ours.is_file()

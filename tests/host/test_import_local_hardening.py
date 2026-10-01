"""Host side of local-session import: failure codes and hosts without SQLite."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from omnigent.host.frames import (
    HostImportLocalDoneFrame,
    HostImportLocalFrame,
)
from omnigent.session_import import local as local_import
from omnigent.session_import.errors import (
    MISSING_SQLITE_FIX_COMMANDS,
    MISSING_SQLITE_MESSAGE,
    ImportErrorCode,
    mentions_missing_sqlite,
)
from tests.server.import_tunnel_harness import (
    RecordingWs,
    local_session,
    make_host,
    serve_local_sessions,
)

_MISSING_SQLITE = "No module named '_sqlite3'"


def _done(ws: RecordingWs) -> HostImportLocalDoneFrame:
    (done,) = [f for f in ws.frames() if isinstance(f, HostImportLocalDoneFrame)]
    return done


class _HeartbeatFailingWs(RecordingWs):
    """Records frames, but heartbeats sent from the background task raise ``exc``."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc
        self.handler_task: asyncio.Task[Any] | None = None
        self.heartbeat_attempts = 0

    async def send(self, text: str) -> None:
        is_progress = json.loads(text)["kind"] == "host.import_local_progress"
        if is_progress and asyncio.current_task() is not self.handler_task:
            self.heartbeat_attempts += 1
            raise self.exc
        await super().send(text)


async def test_unexpected_session_error_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected per-session error reports a generic reason without its text or a code."""
    sessions: dict[str, Any] = {"bad": RuntimeError("/secret/path"), "ok": local_session("ok")}
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
    )
    assert _done(ws).failures == [
        {
            "external_session_id": "bad",
            "source": "claude",
            "reason": "This session could not be read.",
        }
    ]


async def test_missing_sqlite_session_failure_carries_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that fails on a missing SQLite module is reported with the fix and its code."""
    sessions: dict[str, Any] = {
        "bad": ModuleNotFoundError(_MISSING_SQLITE),
        "ok": local_session("ok"),
    }
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
    )
    done = _done(ws)
    assert done.status == "ok"
    assert done.failures == [
        {
            "external_session_id": "bad",
            "source": "claude",
            "reason": MISSING_SQLITE_MESSAGE,
            "code": ImportErrorCode.HOST_PYTHON_MISSING_SQLITE,
        }
    ]


async def _list_failing_with(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> RecordingWs:
    """Import one harness whose session listing raises ``exc``."""

    def _broken(_source: str, *, limit: int) -> list[str]:
        raise exc

    monkeypatch.setattr(local_import, "list_recent_local_session_ids", _broken)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="codex", limit=5)
    )
    return ws


async def test_listing_missing_sqlite_passes_its_text_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing-SQLite listing error keeps its text so the server can name the fix."""
    ws = await _list_failing_with(monkeypatch, ModuleNotFoundError(_MISSING_SQLITE))
    done = _done(ws)
    assert done.status == "failed"
    assert done.error == _MISSING_SQLITE


@pytest.mark.parametrize(
    "exc",
    [
        OSError("permission denied: /Users/alice/.codex/sessions"),
        ImportError("No module named 'yaml' (/Users/alice/venv)"),
        RuntimeError("index corrupt at /Users/alice/.codex/session_index.jsonl"),
    ],
    ids=["os-error", "other-import-error", "unexpected"],
)
async def test_listing_error_is_reported_without_its_text(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """Any other listing error fails the import with a generic message, never local paths."""
    ws = await _list_failing_with(monkeypatch, exc)
    done = _done(ws)
    assert done.status == "failed"
    assert done.error == "Local sessions could not be listed on the host."
    assert not any("/Users/alice" in text for text in ws.sent)


def test_missing_sqlite_message_is_actionable_and_short() -> None:
    """The missing-SQLite message names the module and comes with pasteable per-OS fixes."""
    assert len(MISSING_SQLITE_MESSAGE) < 450
    assert "_sqlite3" in MISSING_SQLITE_MESSAGE
    assert "omnigent host" in MISSING_SQLITE_MESSAGE
    labels = [fix["label"] for fix in MISSING_SQLITE_FIX_COMMANDS]
    assert labels[0].startswith("macOS")
    assert labels[1] == "Linux"
    # Each command is pasteable as-is: no label or prose inside it.
    for fix in MISSING_SQLITE_FIX_COMMANDS:
        assert ":" not in fix["command"]
        assert "(" not in fix["command"]
    assert MISSING_SQLITE_FIX_COMMANDS[0]["command"] == (
        "brew install sqlite && pyenv install --force 3.12"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ModuleNotFoundError: No module named '_sqlite3'", True),
        ("No module named 'sqlite3'", True),
        ("No module named 'yaml'", False),
        (None, False),
    ],
)
def test_mentions_missing_sqlite_detects_both_spellings(text: object, expected: bool) -> None:
    """Both spellings of the missing-SQLite import error are recognized."""
    assert mentions_missing_sqlite(text) is expected


def test_host_import_modules_load_without_sqlite(tmp_path: Path) -> None:
    """The host daemon and transcript readers import on a Python built without SQLite."""
    Path(tmp_path, "state_5.sqlite").write_text("not a db")
    # A fresh interpreter, because this one already has sqlite3 loaded.
    script = textwrap.dedent(
        f"""
        import sys
        sys.modules["_sqlite3"] = None
        sys.modules["sqlite3"] = None
        from pathlib import Path
        import omnigent.host.connect
        from omnigent.session_import import local
        assert local._codex_native_title(Path({str(tmp_path)!r}), "thread-1") is None
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert result.stdout.strip().endswith("ok")

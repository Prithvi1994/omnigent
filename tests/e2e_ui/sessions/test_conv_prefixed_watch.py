"""E2E regression — watching a ``conv_``-prefixed session id crashes
``WS /v1/sessions/updates``.

The CLI prints and opens session links verbatim as ``/c/<id>`` (see
``conversation_browser.conversation_url``), so an id kept in its legacy
``conv_<hex>`` spelling reaches the browser unchanged. When the open session is
not in the sidebar's loaded list (a directly-opened link, or a filtered-out
sub-agent), the SPA's watch-set for ``WS /v1/sessions/updates`` carries only
that ``conv_``-prefixed id (``SessionUpdatesProvider`` unions the active id in;
the bare-hex row is absent from every cached page), and ``session_ids`` reaches
the server verbatim.

Three bulk store readers seed their result dict with the ids the caller passed
but file each row under the id read back from the ``Uuid16`` column (always bare
hex). The ``IN`` filter coerces the bind param, so the row comes back — under a
key spelled differently from the seeded one:

- ``PermissionStore.list_for_sessions`` raises ``KeyError(<bare hex>)``, so the
  stream task crashes on every snapshot; the socket closes and reconnects once
  per second, and the sidebar silently loses live updates.
- ``ConversationStore.get_conversations`` does not raise but drops the row, so
  even without the crash the snapshot would list the session under zero items.

This drives the real SPA journey — open ``/c/conv_<hex>`` for an off-sidebar
session — and observes the SPA's own updates socket. It asserts the corrected
outcome (a snapshot that includes the session, a stream that stays open), which
guards both readers at once: today the crash delivers no snapshot and the socket
crash-loops, so it fails.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from playwright.sync_api import Browser, Route, WebSocket

from tests.e2e_ui.conftest import _build_hello_world_bundle


def _snapshot_contains(frames: list[str], session_id: str) -> bool:
    """True if any received ``snapshot``/``changed`` frame lists *session_id*."""
    for raw in frames:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if frame.get("type") in ("snapshot", "changed"):
            if any(item.get("id") == session_id for item in frame.get("items", [])):
                return True
    return False


@pytest.mark.compat_smoke
def test_conv_prefixed_watch_delivers_snapshot(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    base_url, sidebar_id = seeded_session

    # A second session opened by its conv_-prefixed link and kept off the
    # sidebar list, so the SPA watches it under conv_<hex> alone — the exact
    # shape that carries the legacy spelling into the server's watch-set.
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    offsidebar_id = create.json()["session_id"]
    conv_link_id = f"conv_{offsidebar_id}"

    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    context = (
        browser.new_context(record_video_dir=record_dir) if record_dir else browser.new_context()
    )
    page = context.new_page()

    def _strip_offsidebar(route: Route) -> None:
        resp = route.fetch()
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                body["data"] = [r for r in body["data"] if r.get("id") != offsidebar_id]
            route.fulfill(response=resp, json=body)
        except (ValueError, KeyError):
            route.fulfill(response=resp)

    page.route("**/v1/sessions?**", _strip_offsidebar)

    received: list[str] = []
    closes: list[int] = []

    def _on_ws(ws: WebSocket) -> None:
        if "sessions/updates" not in ws.url:
            return
        ws.on(
            "framereceived",
            lambda p: received.append(p["payload"] if isinstance(p, dict) else str(p)),
        )
        ws.on("close", lambda _p: closes.append(1))

    page.on("websocket", _on_ws)

    try:
        page.goto(f"{base_url}/c/{conv_link_id}")
        # Sidebar row for the visible session renders from the HTTP list ⇒ the
        # SPA has connected the updates socket and is watching [sidebar_id,
        # conv_<offsidebar_id>].
        page.locator(f'a[href="/c/{sidebar_id}"]').first.wait_for(state="visible")

        # Poll until the corrected outcome arrives (fast once fixed) or the
        # observation window closes; on the buggy build the socket crash-loops
        # here and no snapshot ever arrives.
        deadline, step, waited = 15_000, 500, 0
        while waited < deadline and not _snapshot_contains(received, offsidebar_id):
            page.wait_for_timeout(step)
            waited += step

        assert _snapshot_contains(received, offsidebar_id), (
            "WS /v1/sessions/updates never delivered a snapshot containing the "
            f"conv_-prefixed session {offsidebar_id!r}; the stream crash-looped "
            f"({len(closes)} socket closes) instead of filing the row. "
            f"Received frames: {received[:5]}"
        )
        assert len(closes) <= 2, (
            f"the updates socket crash-looped ({len(closes)} closes) while "
            f"watching conv_-prefixed id {conv_link_id!r}"
        )
    finally:
        context.close()
        httpx.delete(f"{base_url}/v1/sessions/{offsidebar_id}", timeout=10.0)

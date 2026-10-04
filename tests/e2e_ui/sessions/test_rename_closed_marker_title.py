"""E2E: a user title containing ``:closed:`` must not close the session.

``sys_session_close`` tombstones a closed child's title as ``:closed:<child id>``
and the server reads that suffix back as closed state. A sidebar rename
round-trips the user's title verbatim through ``PATCH /v1/sessions/{id}``, so a
title that merely *contains* ``:closed:`` must stay user text: no truncation, no
synthesized ``omnigent.closed=true`` label, and no ``409 Session is closed``.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, expect

_TITLE = "notes about a :closed: door"
_CLOSED_LABEL = "omnigent.closed"
_FOLLOWUP = "still here after the rename"


def _rename_via_sidebar(page: Page, base_url: str, session_id: str, title: str) -> None:
    """Rename *session_id* to *title* through the sidebar row kebab.

    Waits for the rename ``PATCH`` to round-trip and requires it to have
    been accepted, so the assertions that follow observe persisted
    server state rather than an optimistic cache paint.

    :param page: Playwright page fixture (fresh context per test).
    :param base_url: Live server base URL.
    :param session_id: Session to rename.
    :param title: Exact title to commit.
    """
    page.goto(f"{base_url}/c/{session_id}")
    row = page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    expect(row).to_be_visible()

    # Hover first so the desktop hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("rename-conversation").click()
    edit = page.get_by_test_id("rename-conversation-input")
    expect(edit).to_be_visible()
    edit.fill(title)
    with page.expect_response(
        lambda r: (
            r.request.method == "PATCH" and urlparse(r.url).path == f"/v1/sessions/{session_id}"
        )
    ) as patch_info:
        edit.press("Enter")
    assert patch_info.value.status == 200, (
        f"rename PATCH should be accepted, got {patch_info.value.status}"
    )


def test_rename_with_closed_infix_keeps_title_and_session_open(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The renamed title persists as written and the session stays writable.

    A substring match on ``:closed:`` would truncate the row, header and
    snapshot to ``notes about a``, synthesize ``omnigent.closed=true``,
    disable the composer and make the events route refuse messages with 409.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _rename_via_sidebar(page, base_url, session_id, _TITLE)

    # Reload so the sidebar and snapshot render server state, not the optimistic paint.
    page.reload()
    link = page.locator(f'a[href="/c/{session_id}"]')
    expect(link).to_be_visible()
    expect(link).to_contain_text(_TITLE)
    expect(page.get_by_test_id("header-title")).to_have_text(_TITLE)

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_editable()
    composer.fill(_FOLLOWUP)
    with page.expect_response(
        lambda r: (
            r.request.method == "POST"
            and urlparse(r.url).path == f"/v1/sessions/{session_id}/events"
        )
    ) as post_info:
        page.get_by_role("button", name="Send", exact=True).click()
    assert post_info.value.status < 400, (
        f"message after the rename should be accepted, got {post_info.value.status}: "
        f"{post_info.value.text()[:200]}"
    )
    expect(page.get_by_text(_FOLLOWUP).first).to_be_visible()

    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    body = snap.json()
    assert body.get("title") == _TITLE, (
        f"server should return the title as written {_TITLE!r}, got {body.get('title')!r}"
    )
    assert (body.get("labels") or {}).get(_CLOSED_LABEL) is None, (
        f"a user rename must not mark the session closed; labels: {body.get('labels')!r}"
    )

"""E2E coverage for what a session open is allowed to fetch.

Opening a session used to render one small page and then keep paging older
history from the transcript's layout effect until it found the previous user
prompt. The reader watched history land for seconds after the page had already
settled, with the content shifting under them, having never scrolled.

Opening loads one history window and may check for newer committed items once.
Older pages load only when the reader scrolls toward the top.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from playwright.sync_api import Page, expect


def _seed_message(
    client: httpx.Client,
    session_id: str,
    *,
    response_id: str,
    role: str,
    text: str,
) -> None:
    """Persist one user or assistant message through the native event route."""
    content_type = "input_text" if role == "user" else "output_text"
    item_data: dict[str, object] = {
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }
    if role == "assistant":
        item_data["agent"] = "e2e-history"

    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": item_data,
                "response_id": response_id,
            },
        },
    )
    assert response.status_code == 202, response.text


def _track_items_requests(page: Page, session_id: str) -> None:
    """Record every `/items` request the page makes, in order."""
    endpoint = f"/v1/sessions/{session_id}/items"
    page.add_init_script(
        f"""
        (() => {{
          const endpoint = {json.dumps(endpoint)};
          window.__itemsUrls = [];
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {{
            const url = typeof input === "string" ? input : input.url;
            if (url.includes(endpoint)) window.__itemsUrls.push(url);
            return originalFetch(input, init);
          }};
        }})();
        """
    )


def _history_requests(page: Page) -> list[str]:
    """Separate older-history reads from the initial forward catch-up."""
    urls: list[str] = page.evaluate("window.__itemsUrls")
    return [url for url in urls if parse_qs(urlsplit(url).query).get("order") != ["asc"]]


def _assert_initial_history_requests(page: Page) -> None:
    """Allow one newest window and at most one catch-up for this static transcript."""
    urls: list[str] = page.evaluate("window.__itemsUrls")
    history = _history_requests(page)
    assert len(history) == 1, urls
    assert "after" not in parse_qs(urlsplit(history[0]).query), urls
    catchup = [url for url in urls if url not in history]
    assert len(catchup) <= 1, urls
    assert all("after" in parse_qs(urlsplit(url).query) for url in catchup), urls


def _seed_long_transcript(base_url: str, session_id: str, latest_prompt: str) -> None:
    """Seed more items than one window holds, so older history remains."""
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        _seed_message(
            client, session_id, response_id="resp_old", role="user", text="oldest prompt"
        )
        # Comfortably beyond the initial window, so `has_more` stays true and
        # a regression that resumes paging has something to page into.
        for index in range(130):
            _seed_message(
                client,
                session_id,
                response_id=f"resp_fill_{index:03d}",
                role="assistant",
                text=f"filler {index}",
            )
        _seed_message(
            client, session_id, response_id="resp_latest", role="user", text=latest_prompt
        )


@pytest.mark.compat_smoke
def test_opening_a_session_fetches_history_once_and_then_stops(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Open one history window without automatically paging into older items."""
    base_url, session_id = seeded_session
    latest_prompt = "history latest prompt"
    _seed_long_transcript(base_url, session_id, latest_prompt)
    _track_items_requests(page, session_id)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")

    conversation = page.get_by_role("log")
    expect(conversation.get_by_text(latest_prompt, exact=True)).to_be_visible(timeout=20_000)

    # Sit still, as a reader who has not scrolled. Background paging showed up
    # here as extra requests seconds after the transcript had settled.
    page.wait_for_timeout(4_000)

    _assert_initial_history_requests(page)
    # The "Loading earlier messages…" row belongs to reader-driven paging, so
    # it must never have appeared during the open either.
    expect(page.get_by_text("Loading earlier messages", exact=False)).to_have_count(0)


def test_scrolling_to_the_top_still_pages_older_history(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Keep scroll-up paging working; only the automatic build is gone."""
    base_url, session_id = seeded_session
    latest_prompt = "history latest prompt"
    _seed_long_transcript(base_url, session_id, latest_prompt)
    _track_items_requests(page, session_id)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")

    conversation = page.get_by_role("log")
    expect(conversation.get_by_text(latest_prompt, exact=True)).to_be_visible(timeout=20_000)
    page.wait_for_timeout(2_000)
    _assert_initial_history_requests(page)

    # Now the reader wheels up to the top, which IS a request for older
    # history. Only reader input counts: a programmatic scroll to the top (a
    # jump, a restore) loads nothing on its own.
    box = conversation.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(12):
        page.mouse.wheel(0, -600)
        page.wait_for_timeout(16)
    page.wait_for_function(
        """window.__itemsUrls.filter(url =>
            new URL(url, location.href).searchParams.get('order') !== 'asc'
        ).length > 1""",
        timeout=20_000,
    )

    urls = _history_requests(page)
    assert len(urls) >= 2, urls
    # Every page after the first is cursored off the window's oldest item.
    assert all("after=" in url for url in urls[1:]), urls

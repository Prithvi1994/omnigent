"""UI journey: the Automations dialog submits a 300-character name, longer than the
``scheduled_tasks.name`` column; the server must answer 400 and the dialog must show
that message inline, leaving no over-long row behind."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress

import httpx
import pytest
from playwright.sync_api import Locator, Page, Request, Response, expect

NAME_COLUMN_LIMIT = 256


def _over_long_name(tag: str) -> str:
    return (f"Over-long automation {tag} " + "nightly-triage-report " * 20)[:300]


def _list_names(base_url: str) -> list[str]:
    resp = httpx.get(f"{base_url}/v1/scheduled-tasks", timeout=10.0)
    resp.raise_for_status()
    return [t["name"] for t in resp.json()["scheduled_tasks"]]


def _list_task_ids(base_url: str) -> set[str]:
    resp = httpx.get(f"{base_url}/v1/scheduled-tasks", timeout=10.0)
    resp.raise_for_status()
    return {t["id"] for t in resp.json()["scheduled_tasks"]}


@pytest.fixture(autouse=True)
def _delete_scheduled_tasks_created_by_test(live_server: str) -> Iterator[None]:
    before = _list_task_ids(live_server)
    yield
    for task_id in _list_task_ids(live_server) - before:
        with suppress(httpx.HTTPError):
            httpx.delete(f"{live_server}/v1/scheduled-tasks/{task_id}", timeout=10.0)


def _row_by_name(page: Page, name: str) -> Locator:
    return page.locator('[data-testid="scheduled-task-row"]').filter(has_text=name)


def _is_scheduled_task_write(method: str) -> Callable[[Response], bool]:
    def _match(response: Response) -> bool:
        request: Request = response.request
        return "/v1/scheduled-tasks" in response.url and request.method == method

    return _match


def _open_new_automation_dialog(page: Page) -> Locator:
    page.get_by_test_id("new-task-button").click()
    dialog = page.get_by_test_id("create-scheduled-task-dialog")
    expect(dialog).to_be_visible(timeout=30_000)
    agent_trigger = page.get_by_test_id("task-agent-picker").get_by_test_id(
        "new-chat-landing-agent-select"
    )
    expect(agent_trigger).to_contain_text("Claude Code", timeout=30_000)
    return dialog


def _settle_after_submit(page: Page, submitted_name: str) -> None:
    """Wait for the dialog error or the new row so the outcome is on screen."""
    outcome = page.get_by_test_id("create-error").or_(_row_by_name(page, submitted_name))
    expect(outcome.first).to_be_visible(timeout=15_000)


def _expect_inline_length_error(page: Page) -> None:
    error = page.get_by_test_id("create-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text(str(NAME_COLUMN_LIMIT))


def test_create_dialog_rejects_name_longer_than_column(page: Page, live_server: str) -> None:
    long_name = _over_long_name(uuid.uuid4().hex[:8])
    assert len(long_name) > NAME_COLUMN_LIMIT

    page.goto(f"{live_server}/tasks")
    _open_new_automation_dialog(page)
    page.get_by_test_id("task-name-input").fill(long_name)
    page.get_by_test_id("task-prompt-input").fill("Summarize the day.")
    with page.expect_response(_is_scheduled_task_write("POST")) as created:
        page.get_by_test_id("create-scheduled-task-submit").click()
    response = created.value
    _settle_after_submit(page, long_name)

    stored = _list_names(live_server)
    assert response.status == 400, (
        f"POST /v1/scheduled-tasks answered HTTP {response.status} for a "
        f"{len(long_name)}-character name; stored name lengths: {[len(n) for n in stored]}"
    )
    _expect_inline_length_error(page)
    assert long_name not in stored


def test_edit_dialog_rejects_name_longer_than_column(page: Page, live_server: str) -> None:
    tag = uuid.uuid4().hex[:8]
    original_name = f"Rename me {tag}"
    long_name = _over_long_name(tag)

    page.goto(f"{live_server}/tasks")
    _open_new_automation_dialog(page)
    page.get_by_test_id("task-name-input").fill(original_name)
    page.get_by_test_id("task-prompt-input").fill("Summarize the day.")
    page.get_by_test_id("create-scheduled-task-submit").click()
    row = _row_by_name(page, original_name)
    expect(row).to_be_visible(timeout=30_000)

    row.hover()
    row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-edit").click()
    expect(page.get_by_test_id("create-scheduled-task-dialog")).to_be_visible(timeout=30_000)
    name_input = page.get_by_test_id("task-name-input")
    expect(name_input).to_have_value(original_name)
    name_input.fill(long_name)
    with page.expect_response(_is_scheduled_task_write("PATCH")) as patched:
        page.get_by_test_id("create-scheduled-task-submit").click()
    response = patched.value
    _settle_after_submit(page, long_name)

    stored = _list_names(live_server)
    assert response.status == 400, (
        f"PATCH /v1/scheduled-tasks answered HTTP {response.status} for a "
        f"{len(long_name)}-character name; stored name lengths: {[len(n) for n in stored]}"
    )
    _expect_inline_length_error(page)
    assert original_name in stored
    assert long_name not in stored

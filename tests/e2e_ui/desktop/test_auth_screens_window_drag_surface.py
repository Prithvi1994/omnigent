"""The desktop window must stay movable on the screens shown outside the AppShell.

On macOS the Electron shell hides the native title bar (``titleBarStyle:
"hiddenInset"``), so the page is the window's only drag surface: a screen with
no visible ``-webkit-app-region: drag`` element leaves the window impossible to
move. The signed-in AppShell renders ``.electron-drag-strip``; ``/login``,
``/register`` and ``/approve`` mount outside it (``web/src/App.tsx``).

The frameless window itself needs macOS, so these tests pin the observable
invariant behind the symptom against a real accounts-mode server, presenting
as the mac shell through the two signals ``isMacElectronShell()`` sniffs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e_ui.auth._accounts_server import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    AccountsServer,
    spawn_accounts_server,
)

_MAC_ELECTRON_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) omnigent-desktop/1.0.0 Chrome/126.0.0.0 "
    "Electron/31.0.0 Safari/537.36"
)

_ELECTRON_BRIDGE_STUB = """
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount() {},
  notify() { return Promise.resolve(true); },
  onOpenPath() { return () => {}; },
};
"""

_APP_REGION_JS = """
(el) => {
  const style = getComputedStyle(el);
  return (style.getPropertyValue("app-region") || style.webkitAppRegion || "none").trim();
}
"""

# Zero-sized drag regions are excluded: a collapsed strip cannot be grabbed.
_VISIBLE_DRAG_REGIONS_JS = f"""
() => Array.from(document.querySelectorAll("*"))
  .filter((el) => ({_APP_REGION_JS})(el) === "drag")
  .filter((el) => {{ const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }})
  .map((el) => `${{el.tagName.toLowerCase()}}.${{el.className}}`)
"""

_GRAB_POINT = (400, 10)


@pytest.fixture(scope="module")
def accounts_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AccountsServer]:
    """An accounts-mode server: the shared single-user ``live_server`` has no ``/login`` route."""
    yield from spawn_accounts_server(
        mock_llm_server_url, tmp_path_factory.mktemp("e2e_ui_window_drag")
    )


@pytest.fixture
def mac_desktop_page(browser: Browser, browser_context_args: dict[str, Any]) -> Iterator[Page]:
    context_args: dict[str, Any] = {
        **browser_context_args,
        "user_agent": _MAC_ELECTRON_USER_AGENT,
        "viewport": {"width": 1280, "height": 860},
    }
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_args["record_video_dir"] = record_dir
    context = browser.new_context(**context_args)
    page = context.new_page()
    page.add_init_script(_ELECTRON_BRIDGE_STUB)
    yield page
    context.close()


def _visible_drag_regions(page: Page) -> list[str]:
    return page.evaluate(_VISIBLE_DRAG_REGIONS_JS)


def _attempt_window_drag(page: Page) -> str:
    """Grab the window's top band and pull, paced like a real gesture.

    :returns: The computed app-region under the grab point.
    """
    x, y = _GRAB_POINT
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(x + 20, 700, 40):
        page.mouse.move(step, y + 4)
        page.wait_for_timeout(80)
    page.mouse.up()
    page.wait_for_timeout(1_200)
    return page.evaluate(
        f"() => ({_APP_REGION_JS})(document.elementFromPoint({x}, {y}) || document.body)"
    )


def _snapshot(page: Page, name: str) -> None:
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        page.screenshot(path=str(Path(record_dir) / f"{name}.png"))


def _sign_in(page: Page, server: AccountsServer) -> None:
    page.goto(f"{server.public_url}/login")
    page.wait_for_selector("#login-username", timeout=30_000)
    page.fill("#login-username", ADMIN_USERNAME)
    page.fill("#login-password", ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page).not_to_have_url(re.compile(r"/login"), timeout=30_000)


def _assert_drag_surface(page: Page, screen: str) -> None:
    grab_region = _attempt_window_drag(page)
    regions = _visible_drag_regions(page)
    _snapshot(page, f"{screen}-after-drag")
    assert regions, (
        f"{screen} renders no visible `-webkit-app-region: drag` element "
        f"(app-region under the grab point: {grab_region!r}); with the native title "
        "bar hidden on the macOS shell the window cannot be moved from this screen."
    )


def test_sign_in_screen_offers_window_drag_surface(
    accounts_server: AccountsServer, mac_desktop_page: Page
) -> None:
    page = mac_desktop_page
    page.goto(f"{accounts_server.public_url}/login")
    page.wait_for_selector("#login-username", timeout=30_000)
    _assert_drag_surface(page, "sign-in")


def test_register_screen_offers_window_drag_surface(
    accounts_server: AccountsServer, mac_desktop_page: Page
) -> None:
    page = mac_desktop_page
    page.goto(f"{accounts_server.public_url}/register")
    page.wait_for_selector("[role=alert]", timeout=30_000)
    _assert_drag_surface(page, "register")


def test_approve_screen_offers_window_drag_surface(
    accounts_server: AccountsServer, mac_desktop_page: Page
) -> None:
    page = mac_desktop_page
    _sign_in(page, accounts_server)
    page.goto(f"{accounts_server.public_url}/approve/no-such-session/no-such-elicitation")
    page.wait_for_selector("[role=alert]", timeout=30_000)
    _assert_drag_surface(page, "approve")


def test_signed_in_shell_offers_window_drag_surface(
    accounts_server: AccountsServer, mac_desktop_page: Page
) -> None:
    """Control: the AppShell's own strip is detected by the same probe."""
    page = mac_desktop_page
    _sign_in(page, accounts_server)
    page.wait_for_selector(".electron-drag-strip", state="attached", timeout=30_000)
    _attempt_window_drag(page)
    regions = _visible_drag_regions(page)
    _snapshot(page, "signed-in-shell-after-drag")
    assert any("electron-drag-strip" in region for region in regions), regions

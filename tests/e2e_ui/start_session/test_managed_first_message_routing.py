"""A managed session's first message follows its newly provisioned host."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.conftest import _find_free_port, configure_mock_llm
from tests.e2e_ui.start_session.test_start_session import _run_in_fresh_loop

_WEB_DIR = Path(__file__).resolve().parents[3] / "web"
_HOST_ID = "managed-routing-test-host"
_SLICE_KEY = "x-databricks-omnigent-slice-key"
_PROMPT = "Confirm this first managed message arrived once."
_REPLY = "The first managed message arrived once."


@pytest.fixture
def managed_routing_ui(live_server: str, tmp_path: Path) -> Iterator[str]:
    """Serve the real UI sources so its managed transport can be installed before boot."""
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "managed-routing-vite.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            ["pnpm", "exec", "vite", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=_WEB_DIR,
            env={**os.environ, "OMNIGENT_URL": live_server, "OMNIGENT_AUTH_TOKEN": ""},
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            with httpx.Client(timeout=1.0) as client:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        pytest.fail(f"Vite exited during startup:\n{log_path.read_text()}")
                    try:
                        if client.get(url).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail(f"Vite did not start:\n{log_path.read_text()}")
            yield url
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def test_managed_first_message_refreshes_host_and_retries_once(
    seeded_session: tuple[str, str],
    managed_routing_ui: str,
    mock_llm_server_url: str,
) -> None:
    """Recover the original hostless POST without a reload, duplicate turn, or lost draft."""
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY}],
        key="managed-first-message-routing",
        match=_PROMPT,
    )
    _run_in_fresh_loop(_drive(managed_routing_ui, *seeded_session))


async def _drive(ui_url: str, server_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        first_post = asyncio.Event()
        finish_provisioning = asyncio.Event()
        provisioned = False
        attempts: list[tuple[str | None, str]] = []
        network: list[tuple[str, str | None]] = []
        forwarded = 0
        try:

            async def bootstrap(route: Route) -> None:
                response = await route.fetch()
                html = await response.text()
                # Install the real embed transport seam, retaining the standalone UI.
                # The transform adds only the provider's ready event to the live SSE.
                entry = f"""<script type="module">
                  import {{ setOmnigentHostConfig }} from '/src/lib/host.ts';
                  setOmnigentHostConfig({{
                    fetcher: async (path, init) => {{
                      const response = await fetch(path, init);
                      if (!path.includes('/{session_id}/stream') || !response.body) {{
                        return response;
                      }}
                      const stream = new TransformStream({{
                        start(controller) {{ window.__managedRoutingStream = controller; }}
                      }});
                      return new Response(response.body.pipeThrough(stream), {{
                        status: response.status, headers: response.headers
                      }});
                    }}
                  }});
                  await import('/src/main.tsx');
                </script>"""
                html, replaced = re.subn(
                    r'<script\b[^>]*src="/src/main\.tsx(?:\?[^"]*)?"[^>]*></script>',
                    lambda _match: entry,
                    html,
                )
                assert replaced == 1, "Vite's UI entry was not replaced"
                await route.fulfill(response=response, body=html)

            async def snapshot(route: Route) -> None:
                response = await route.fetch()
                body = await response.json()
                host_id = _HOST_ID if provisioned else None
                network.append(("snapshot", host_id))
                body.update(
                    host_id=host_id,
                    host_online=provisioned,
                    host_managed=True,
                    runner_id=body["runner_id"] if provisioned else None,
                    runner_online=provisioned,
                    sandbox_status=None if provisioned else {"stage": "provisioning"},
                )
                await route.fulfill(response=response, json=body)

            async def events(route: Route) -> None:
                nonlocal provisioned, forwarded
                if route.request.post_data_json["type"] != "message":
                    await route.continue_()
                    return
                key = route.request.headers.get(_SLICE_KEY)
                attempts.append((key, route.request.post_data or ""))
                network.append(("message", key))
                if len(attempts) == 1:
                    first_post.set()
                    await finish_provisioning.wait()
                    provisioned = True
                    network.append(("wrong_replica", None))
                    await route.fulfill(
                        status=400,
                        json={
                            "error": {
                                "code": "wrong_replica",
                                "message": "session runner is on another replica; retry",
                            }
                        },
                    )
                    return
                assert key == _HOST_ID, "The retry must use the newly provisioned host"
                forwarded += 1
                await route.continue_()

            await page.route(f"{ui_url}/c/{session_id}", bootstrap)
            await page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), snapshot)
            await page.route(f"**/v1/sessions/{session_id}/events", events)
            await page.goto(f"{ui_url}/c/{session_id}")
            await page.wait_for_function("window.__managedRoutingStream !== undefined")

            composer = page.get_by_label("Message the agent")
            await expect(composer).to_be_visible()
            await composer.fill(_PROMPT)
            await page.get_by_role("button", name="Send", exact=True).click()
            await asyncio.wait_for(first_post.wait(), timeout=10)
            await expect(page.get_by_test_id("runner-starting-indicator")).to_be_visible()
            assert attempts[0][0] is None

            record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
            if record_dir:
                await page.screenshot(
                    path=str(Path(record_dir) / "managed-routing-provisioning.png")
                )
            await page.evaluate(
                """sessionId => {
                  const payload = {
                    type: 'session.sandbox_status', conversation_id: sessionId, stage: 'ready'
                  };
                  const frame = `event: session.sandbox_status\n`
                    + `data: ${JSON.stringify(payload)}\n\n`;
                  window.__managedRoutingStream.enqueue(new TextEncoder().encode(frame));
                }""",
                session_id,
            )
            finish_provisioning.set()

            user = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
                has_text=_PROMPT
            )
            assistant = page.locator(
                '[data-testid="message-bubble"][data-role="assistant"]'
            ).filter(has_text=_REPLY)
            try:
                await expect(assistant).to_be_visible(timeout=30_000)
            except AssertionError as exc:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(f"{server_url}/v1/sessions/{session_id}")
                raise AssertionError(
                    f"No assistant response after routing recovery: {response.text}"
                ) from exc
            await expect(user).to_have_count(1)
            await expect(assistant).to_have_count(1)
            await expect(page.get_by_test_id("runner-starting-indicator")).to_have_count(0)
            await expect(
                page.get_by_text("session runner is on another replica; retry")
            ).to_have_count(0)
            assert [key for key, _body in attempts] == [None, _HOST_ID]
            assert attempts[0][1] == attempts[1][1], "Retries must preserve the original stable_id"
            assert forwarded == 1
            rejected = network.index(("wrong_replica", None))
            assert network[rejected + 1 :].index(("snapshot", _HOST_ID)) < network[
                rejected + 1 :
            ].index(("message", _HOST_ID))

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{server_url}/v1/sessions/{session_id}")
                response.raise_for_status()
                items = response.json()["items"]
            for role, text in (("user", _PROMPT), ("assistant", _REPLY)):
                matching = [
                    item
                    for item in items
                    if item.get("data", {}).get("role") == role
                    and text in json.dumps(item["data"].get("content", []))
                ]
                assert len(matching) == 1, f"Expected one persisted {role} message: {matching}"
            if record_dir:
                await page.screenshot(path=str(Path(record_dir) / "managed-routing-recovered.png"))
        finally:
            finish_provisioning.set()
            await browser.close()

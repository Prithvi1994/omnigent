"""Security regression tests for the OIDC CLI login ticket flow."""

from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import jwt
import pytest
from fastapi import FastAPI

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.routes.auth import create_auth_router
from tests.server.integration.oidc_fixtures import TEST_SIGNING_KEY, make_oidc_config
from tests.server.integration.test_oidc_auth_e2e import (
    _build_oidc_app,
    _mint_state_cookie,
    _mock_httpx_client_for_github,
    _pkce_pair,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _authenticated_origin_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)


def _build_app(store: DeviceGrantStore | None = None) -> httpx.ASGITransport:
    config = make_oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    router = create_auth_router(
        auth_provider=provider,
        permission_store=None,
        admin_list=AdminList(Path("/tmp/nonexistent-admin-list.txt")),
        device_grant_store=store,
    )
    app = FastAPI()
    app.include_router(router, prefix="/auth")
    return httpx.ASGITransport(app=app)


async def _create_ticket(client: httpx.AsyncClient) -> tuple[str, str]:
    verifier, challenge = _pkce_pair()
    response = await client.post(
        "/auth/cli-login",
        json={"code_challenge": challenge, "code_challenge_method": "S256"},
    )
    assert response.status_code == 200
    return response.json()["ticket"], verifier


async def _complete_callback(
    client: httpx.AsyncClient, ticket: str, *, state: str = "state"
) -> httpx.Response:
    state_cookie = _mint_state_cookie(state, ticket=ticket)
    mock_cm = _mock_httpx_client_for_github()
    with patch("omnigent.server.routes.auth.httpx.AsyncClient", return_value=mock_cm):
        return await client.get(
            "/auth/callback",
            params={"code": "auth-code", "state": state},
            cookies={"ap_auth_state": state_cookie},
        )


async def test_cli_ticket_requires_browser_consent_and_issues_grant(tmp_path: Path) -> None:
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    transport = _build_app(store)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        callback = await _complete_callback(client, ticket)
        assert callback.status_code == 302
        assert callback.headers["location"] == f"/auth/cli-consent?ticket={ticket}"
        assert "ap_session" in callback.cookies

        pending = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        assert pending.status_code == 202

        consent = await client.get(f"/auth/cli-consent?ticket={ticket}")
        assert consent.status_code == 200
        assert re.search(r"Code: [A-Z2-9]{4}-[A-Z2-9]{4}", consent.text)
        assert "alice@example.com" in consent.text
        assert "Authorize CLI login" in consent.text

        approved = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert approved.status_code == 200
        assert "Approved" in approved.text

        fulfilled = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        assert fulfilled.status_code == 200
        body = fulfilled.json()
        payload = jwt.decode(body["token"], TEST_SIGNING_KEY, algorithms=["HS256"])
        assert payload["sub"] == "alice@example.com"
        assert body["user_id"] == "alice@example.com"
        assert body["refresh_token"]


async def test_cli_ticket_rejects_wrong_verifier_without_consuming() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        approved = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert approved.status_code == 200

        wrong = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={'b' * 64}")
        assert wrong.status_code == 403
        correct = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        assert correct.status_code == 200


async def test_cli_ticket_poll_requires_matching_verifier() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ticket, verifier = await _create_ticket(client)
        missing = await client.get(f"/auth/cli-poll?ticket={ticket}")
        assert missing.status_code == 400
        assert "Upgrade" in missing.json()["error"]
        wrong = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={'b' * 64}")
        assert wrong.status_code == 403
        assert "does not match" in wrong.json()["error"]
        assert (
            await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        ).status_code == 202


async def test_cli_ticket_deny_is_terminal() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        denied = await client.post(
            "/auth/cli-deny",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert denied.status_code == 200
        first = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        second = await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        assert first.status_code == 410
        assert second.status_code == 410


async def test_cli_approve_requires_origin_and_session() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, _ = await _create_ticket(client)
        await _complete_callback(client, ticket)
        no_origin = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": ""},
        )
        assert no_origin.status_code == 403

        client.cookies.delete("ap_session")
        no_session = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert no_session.status_code == 401


async def test_cli_approve_rejects_stale_session() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, verifier = await _create_ticket(client)
        await _complete_callback(client, ticket)
        with patch("omnigent.server.oidc.time.time", return_value=time.time() - 10):
            stale = mint_session_cookie("alice@example.com", TEST_SIGNING_KEY, 8, "github")
        client.cookies.set("ap_session", stale)
        approved = await client.post(
            "/auth/cli-approve",
            data={"ticket": ticket},
            headers={"Origin": "http://test"},
        )
        assert approved.status_code == 200
        assert "session is too old" in approved.text
        assert (
            await client.get(f"/auth/cli-poll?ticket={ticket}&code_verifier={verifier}")
        ).status_code == 202


async def test_cli_consent_unauthenticated_bounces_to_login() -> None:
    transport = _build_app()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        ticket, _ = await _create_ticket(client)
        response = await client.get(f"/auth/cli-consent?ticket={ticket}")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/auth/login?")
    assert "reauth=1" in response.headers["location"]


async def test_cli_login_response_contains_consent_code() -> None:
    transport = _build_oidc_app()
    _, challenge = _pkce_pair()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/cli-login",
            json={"code_challenge": challenge, "code_challenge_method": "S256"},
        )
    body = response.json()
    assert "reauth=1" in body["login_url"]
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", body["user_code"])


async def test_legacy_login_opens_upgrade_instructions_and_stops_polling() -> None:
    """Old clients open login_url on 200 and stop polling on 410."""
    async with httpx.AsyncClient(transport=_build_app(), base_url="http://test") as client:
        response = await client.post("/auth/cli-login")
        assert response.status_code == 200
        body = response.json()
        page = await client.get(body["login_url"])
        assert page.status_code == 200
        assert "Update Omnigent to sign in" in page.text
        assert "omni upgrade" in page.text
        assert "iOS or Android" in page.text
        assert "Slack integration" in page.text
        assert "<form" not in page.text
        assert "set-cookie" not in page.headers
        assert page.headers["cache-control"] == "no-store"
        poll = await client.get("/auth/cli-poll", params={"ticket": body["ticket"]})
        assert poll.status_code == 410
        assert "omni upgrade" in poll.json()["error"]


async def test_legacy_upgrade_notice_cannot_issue_credentials(tmp_path: Path) -> None:
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/grants.db")
    async with httpx.AsyncClient(
        transport=_build_app(store), base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.post("/auth/cli-login")
        ticket = response.json()["ticket"]
        with patch("omnigent.server.routes.auth.issue_login_grant") as issue_grant:
            await _complete_callback(client, ticket)
            consent = await client.get("/auth/cli-consent", params={"ticket": ticket})
            assert "invalid or has expired" in consent.text
            approval = await client.post(
                "/auth/cli-approve", data={"ticket": ticket}, headers={"Origin": "http://test"}
            )
            assert "invalid or has expired" in approval.text
            poll = await client.get("/auth/cli-poll", params={"ticket": ticket})
            assert poll.status_code == 410
            assert "token" not in poll.json()
            assert "refresh_token" not in poll.json()
            issue_grant.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"null",
        b"[]",
        b"{}",
        b'{"code_challenge": "short"}',
        b'{"code_challenge_method": "plain"}',
    ],
)
async def test_malformed_pkce_is_not_treated_as_a_legacy_request(body: bytes) -> None:
    async with httpx.AsyncClient(transport=_build_app(), base_url="http://test") as client:
        response = await client.post(
            "/auth/cli-login", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert "login_url" not in response.json()

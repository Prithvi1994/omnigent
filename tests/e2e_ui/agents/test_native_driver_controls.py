"""Browser-level acceptance checks for the shared composer driver."""

import json

import pytest
from playwright.sync_api import Page

from tests.e2e_ui.native_driver import DriverSetupError, send_composer_message


@pytest.mark.parametrize("status", [200, 400])
def test_composer_driver_requires_acceptance(page: Page, seeded_session, status):
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    body = (
        {"queued": True, "item_id": "accepted-input"} if status == 200 else {"error": "rejected"}
    )
    page.route(
        f"**/v1/sessions/{session_id}/events",
        lambda route: route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(body),
        ),
    )
    if status == 400:
        with pytest.raises(DriverSetupError, match="rejected: 400"):
            send_composer_message(page, session_id, "driver submission")
    else:
        accepted = send_composer_message(page, session_id, "driver submission")
        assert accepted.session_id == session_id
        assert accepted.input_id == "accepted-input"

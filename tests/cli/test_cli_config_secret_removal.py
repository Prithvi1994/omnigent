"""Tests for the remove-key status line in the setup menus."""

from __future__ import annotations

from omnigent.cli_config import _secret_removal_status


def test_secret_removal_status_confirms_a_clean_removal() -> None:
    assert _secret_removal_status("Cursor API key", "") == "\u2713 Removed Cursor API key"


def test_secret_removal_status_warns_when_a_copy_may_remain() -> None:
    assert _secret_removal_status("Cursor API key", "the OS keychain still holds the secret") == (
        "\u26a0 Removed Cursor API key from config; the OS keychain still holds the secret"
    )

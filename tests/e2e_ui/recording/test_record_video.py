"""The shared recorder captures direct sync Playwright callers too."""

from pathlib import Path

import pytest
from playwright.sync_api import Browser

from tests.e2e_ui.conftest import _record_video


@pytest.mark.parametrize("explicit_context", [False, True])
@pytest.mark.parametrize("explicit_directory", [False, True])
def test_sync_recorder_writes_video(
    browser: Browser,
    monkeypatch,
    tmp_path,
    explicit_context,
    explicit_directory,
):
    record_dir = tmp_path / "automatic"
    custom_dir = tmp_path / "custom"
    monkeypatch.setenv("OMNIGENT_E2E_RECORD_DIR", str(record_dir))
    recording = _record_video.__wrapped__(monkeypatch)
    next(recording)
    options = {"record_video_dir": str(custom_dir)} if explicit_directory else {}
    try:
        if explicit_context:
            context = browser.new_context(**options)
            page = context.new_page()
        else:
            page = browser.new_page(**options)
            context = page.context
        try:
            page.set_content("<h1>Recorded sync journey</h1>")
            page.get_by_role("heading").wait_for()
            video = page.video
            assert video is not None
        finally:
            context.close()
        path = Path(video.path())
        assert path.parent == (custom_dir if explicit_directory else record_dir)
        assert path.stat().st_size > 0
    finally:
        recording.close()

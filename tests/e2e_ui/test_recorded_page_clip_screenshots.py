"""Clipped screenshots polled on a video-recorded page must leave the footage intact,
and the crop the e2e_ui conftest substitutes for Chromium's view-resizing clip path
must match Playwright's native clip pixel for pixel."""

from __future__ import annotations

import asyncio
import glob
import io
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from playwright.async_api import async_playwright
from playwright.sync_api import Browser

# A flat blue page with a red block inside the probe clip and a ticking counter
# far away from it, so the screencast keeps emitting frames like a live pane.
_PAGE = """<html><body style="margin:0;background:#1d4ed8;color:#fff;font:32px monospace">
<div style="position:absolute;left:60px;top:50px;width:100px;height:40px;background:#dc2626"></div>
<div id="tick" style="position:absolute;left:400px;top:300px">0</div>
<script>let n=0;
setInterval(()=>{document.getElementById('tick').textContent=String(++n)},50)</script>
</body></html>"""
_CLIP = {"x": 40, "y": 40, "width": 160, "height": 48}
_PAGE_BLUE = (0x1D, 0x4E, 0xD8)
# Playwright pads screencast frames smaller than the video onto this grey.
_PAD_GREY = (128, 128, 128)


def _ffmpeg() -> str:
    """Playwright's bundled ffmpeg (the one that wrote the video), else one on PATH."""
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "~/.cache/ms-playwright")
    bundled = sorted(
        candidate
        for candidate in glob.glob(str(root.expanduser() / "ffmpeg-*" / "ffmpeg-*"))
        if os.access(candidate, os.X_OK)
    )
    found = bundled[-1] if bundled else shutil.which("ffmpeg")
    assert found, "no ffmpeg available to decode the recording"
    return found


def _fraction_near(frame: Path, color: tuple[int, int, int], tolerance: int = 8) -> float:
    data = Image.open(frame).convert("RGB").reduce(8).tobytes()
    pixels = [data[i : i + 3] for i in range(0, len(data), 3)]
    near = sum(
        1
        for p in pixels
        if all(abs(c - want) <= tolerance for c, want in zip(p, color, strict=True))
    )
    return near / len(pixels)


def _extract_frames(video: Path, into: Path) -> list[Path]:
    into.mkdir()
    subprocess.run(
        [
            _ffmpeg(),
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            "fps=10",
            str(into / "%04d.png"),
        ],
        check=True,
        timeout=120,
    )
    frames = sorted(into.glob("*.png"))
    assert frames, f"no frames decoded from {video}"
    return frames


def _skip_unless_chromium(browser: Browser) -> None:
    if browser.browser_type.name != "chromium":
        pytest.skip("only Chromium resizes the view for clipped screenshots")


def _assert_same_pixels(native: bytes, cropped: bytes, scale: int) -> None:
    expected = Image.open(io.BytesIO(native)).convert("RGB")
    actual = Image.open(io.BytesIO(cropped)).convert("RGB")
    assert actual.size == expected.size == (_CLIP["width"] * scale, _CLIP["height"] * scale)
    assert ImageChops.difference(expected, actual).getbbox() is None


def test_clip_probes_keep_the_recording_intact(browser: Browser, tmp_path: Path) -> None:
    _skip_unless_chromium(browser)
    record_dir = tmp_path / "video"
    context = browser.new_context(record_video_dir=str(record_dir))
    page = context.new_page()
    page.set_content(_PAGE)
    page.wait_for_timeout(500)

    # Poll the probe back-to-back, the way a renderer-corruption test watches a
    # region of the terminal for the whole flood.
    probes = 0
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        shot = Image.open(io.BytesIO(page.screenshot(clip=_CLIP)))
        assert shot.size == (_CLIP["width"], _CLIP["height"])
        probes += 1
    page.wait_for_timeout(500)
    context.close()
    assert probes > 20, (
        f"only {probes} probes in 3s; the probe loop is not exercising the recorder"
    )

    video = next(record_dir.glob("*.webm"))
    frames = _extract_frames(video, tmp_path / "frames")
    assert any(_fraction_near(f, _PAGE_BLUE) > 0.5 for f in frames), (
        "the video never shows the page"
    )
    grey = {f.name: round(_fraction_near(f, _PAD_GREY), 2) for f in frames}
    distorted = {name: share for name, share in grey.items() if share > 0.5}
    assert not distorted, (
        f"{len(distorted)}/{len(frames)} recorded frames are mostly pad-grey while the "
        f"clip probe polled: {distorted}"
    )


@pytest.mark.parametrize("device_scale_factor", [1, 2])
def test_recorded_clip_matches_native_clip(
    browser: Browser, tmp_path: Path, device_scale_factor: int
) -> None:
    _skip_unless_chromium(browser)
    plain = browser.new_context(device_scale_factor=device_scale_factor)
    page = plain.new_page()
    page.set_content(_PAGE)
    native = page.screenshot(clip=_CLIP)
    plain.close()

    recorded = browser.new_context(
        record_video_dir=str(tmp_path / "video"), device_scale_factor=device_scale_factor
    )
    page = recorded.new_page()
    page.set_content(_PAGE)
    saved = tmp_path / "out" / "probe.png"
    cropped = page.screenshot(clip=_CLIP, path=str(saved))
    recorded.close()

    _assert_same_pixels(native, cropped, device_scale_factor)
    assert saved.read_bytes() == cropped


def test_async_recorded_clip_matches_native_clip(tmp_path: Path) -> None:
    shots: dict[str, bytes] = {}

    async def drive() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(_PAGE)
                shots["native"] = await page.screenshot(clip=_CLIP)
                await page.context.close()

                page = await browser.new_page(record_video_dir=str(tmp_path / "video"))
                await page.set_content(_PAGE)
                shots["cropped"] = await page.screenshot(clip=_CLIP)
                await page.context.close()
            finally:
                await browser.close()

    # The sync Playwright fixtures keep a loop running on the main thread, so
    # the async API gets its own loop in a worker thread.
    failure: list[BaseException] = []

    def worker() -> None:
        try:
            asyncio.run(drive())
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    _assert_same_pixels(shots["native"], shots["cropped"], scale=1)

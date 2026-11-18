"""ffprobe and ffmpeg: reading their answers, and running them on a generated clip if installed."""

import asyncio
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from app.pipeline.media import FfmpegMediaTools, MediaToolError, MediaToolStartError
from app.pipeline.storage import LocalMediaStore

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg and ffprobe are not installed, so the tests that run them are skipped",
)


async def script_tools(tmp_path: Path, output: str, exit_code: int = 0) -> FfmpegMediaTools:
    """Tools whose 'ffprobe' is a small script that prints `output`."""
    script = tmp_path / "fake_ffprobe.py"
    script.write_text(
        f"import sys\nsys.stdout.write({output!r})\nsys.exit({exit_code})\n", encoding="utf-8"
    )
    wrapper = tmp_path / "fake_ffprobe"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} -I {script} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return FfmpegMediaTools(ffmpeg_path="unused", ffprobe_path=str(wrapper), timeout_seconds=10)


async def test_probe_reads_audio_and_video_streams_and_duration(tmp_path: Path) -> None:
    report = (
        '{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],"format":{"duration":"9.5"}}'
    )
    tools = await script_tools(tmp_path, report)

    probe = await tools.probe(tmp_path / "clip.mp4")

    assert probe.has_video and probe.has_audio
    assert probe.duration_seconds == 9.5


async def test_probe_without_an_audio_stream_reports_no_audio_track(tmp_path: Path) -> None:
    tools = await script_tools(tmp_path, '{"streams":[{"codec_type":"video"}],"format":{}}')

    probe = await tools.probe(tmp_path / "clip.mp4")

    assert probe.has_video and not probe.has_audio
    assert probe.duration_seconds is None


async def test_a_failing_program_raises_without_its_output(tmp_path: Path) -> None:
    tools = await script_tools(tmp_path, "secret path /users/someone/clip.mp4", exit_code=1)

    with pytest.raises(MediaToolError) as raised:
        await tools.probe(tmp_path / "clip.mp4")

    assert "someone" not in str(raised.value)


async def test_a_missing_program_is_a_start_error(tmp_path: Path) -> None:
    tools = FfmpegMediaTools(
        ffmpeg_path=str(tmp_path / "no-ffmpeg"),
        ffprobe_path=str(tmp_path / "no-ffprobe"),
        timeout_seconds=5,
    )

    with pytest.raises(MediaToolStartError):
        await tools.probe(tmp_path / "clip.mp4")


async def generate_clip(path: Path, *, with_audio: bool) -> None:
    assert FFMPEG is not None
    inputs = ["-f", "lavfi", "-i", "testsrc=size=640x480:rate=10:duration=2"]
    if with_audio:
        inputs += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    process = await asyncio.create_subprocess_exec(
        FFMPEG, "-nostdin", "-loglevel", "error", "-y", *inputs, "-shortest", str(path)
    )
    assert await process.wait() == 0


@needs_ffmpeg
@pytest.mark.parametrize("with_audio", [True, False])
async def test_real_ffmpeg_copies_a_clip_and_reports_its_audio_track(
    tmp_path: Path, with_audio: bool
) -> None:
    assert FFMPEG is not None and FFPROBE is not None
    source = tmp_path / "source.mp4"
    await generate_clip(source, with_audio=with_audio)
    tools = FfmpegMediaTools(ffmpeg_path=FFMPEG, ffprobe_path=FFPROBE, timeout_seconds=60)

    probe = await tools.probe(source)
    await tools.make_video_copy(source, tmp_path / "copy.mp4", 360)
    await tools.make_still(source, tmp_path / "still.jpg", 360)
    copy_probe = await tools.probe(tmp_path / "copy.mp4")

    assert probe.has_audio is with_audio
    assert copy_probe.has_audio is with_audio
    assert (tmp_path / "still.jpg").stat().st_size > 0
    if with_audio:
        assert (await tools.extract_audio(source)).startswith(b"RIFF")


async def test_the_local_store_refuses_names_and_keys_outside_it(tmp_path: Path) -> None:
    store = LocalMediaStore(tmp_path / "media")

    with pytest.raises(ValueError, match="plain file name"):
        await store.put(uuid.uuid4(), uuid.uuid4(), "../x", b"x")
    with pytest.raises(ValueError, match="inside the store"):
        await store.get("../../etc/hosts")

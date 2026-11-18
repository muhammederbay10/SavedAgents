"""ffprobe and ffmpeg: reading their answers, and running them on a generated clip if installed."""

import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

from app.pipeline.media import FfmpegMediaTools, MediaToolError, MediaToolStartError
from app.pipeline.storage import LocalMediaStore

# How long a stopped tool gets to exit after SIGTERM, and again after SIGKILL.
STOP_TIMEOUT_SECONDS = 0.5
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
    return FfmpegMediaTools(
        ffmpeg_path="unused",
        ffprobe_path=str(wrapper),
        timeout_seconds=10,
        stop_timeout_seconds=STOP_TIMEOUT_SECONDS,
    )


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
        stop_timeout_seconds=STOP_TIMEOUT_SECONDS,
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
    tools = FfmpegMediaTools(
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        timeout_seconds=60,
        stop_timeout_seconds=STOP_TIMEOUT_SECONDS,
    )

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


# --- Cancellation and timeouts stop and reap the exact child ---------------------

SLOW_TOOL_SECONDS = 3
DEADLINE_SECONDS = 0.5


def slow_tool(tmp_path: Path, *, ignore_terminate: bool = False) -> tuple[Path, Path, Path]:
    """A stand-in for ffprobe that records its pid, works for a while, then leaves a marker."""
    pid_file = tmp_path / "tool.pid"
    marker = tmp_path / "tool.finished"
    ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_terminate else ""
    script = tmp_path / "slow_tool.py"
    script.write_text(
        "import os, pathlib, signal, time\n"
        f"{ignore}"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"time.sleep({SLOW_TOOL_SECONDS})\n"
        f"pathlib.Path({str(marker)!r}).write_text('done')\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "slow_tool"
    # `exec` replaces the shell, so the pid the script records is the child's own.
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} -I {script} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper, pid_file, marker


def is_running(pid: int) -> bool:
    """Whether a process with this pid still exists. A child not yet reaped still does."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def read_pid(pid_file: Path) -> int:
    """Wait for the tool to record its pid, then return it."""
    give_up_at = time.monotonic() + SLOW_TOOL_SECONDS
    while not pid_file.exists() or not pid_file.read_text():
        if time.monotonic() > give_up_at:
            raise TimeoutError("the tool never recorded its pid")
        time.sleep(0.01)
    return int(pid_file.read_text())


async def wait_for_pid(pid_file: Path) -> int:
    return await asyncio.to_thread(read_pid, pid_file)


@pytest.mark.parametrize("ignore_terminate", [False, True], ids=["terminates", "needs-kill"])
async def test_a_run_cancelled_by_its_deadline_leaves_no_live_tool(
    tmp_path: Path, ignore_terminate: bool
) -> None:
    program, pid_file, marker = slow_tool(tmp_path, ignore_terminate=ignore_terminate)
    tools = tool_runner(program, timeout_seconds=60)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(DEADLINE_SECONDS):
            await tools.probe(tmp_path / "clip.mp4")

    pid = await wait_for_pid(pid_file)
    assert not is_running(pid)
    await asyncio.sleep(SLOW_TOOL_SECONDS - DEADLINE_SECONDS + 0.5)
    assert not await asyncio.to_thread(marker.exists)


async def test_a_cancelled_task_stops_the_tool_and_stays_cancelled(tmp_path: Path) -> None:
    program, pid_file, _ = slow_tool(tmp_path)
    tools = tool_runner(program, timeout_seconds=60)
    task = asyncio.create_task(tools.probe(tmp_path / "clip.mp4"))
    pid = await wait_for_pid(pid_file)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not is_running(pid)


async def test_a_run_past_the_tool_timeout_stops_the_tool(tmp_path: Path) -> None:
    program, pid_file, _ = slow_tool(tmp_path, ignore_terminate=True)
    tools = tool_runner(program, timeout_seconds=DEADLINE_SECONDS)

    with pytest.raises(MediaToolError, match="took longer"):
        await tools.probe(tmp_path / "clip.mp4")

    assert not is_running(await wait_for_pid(pid_file))


def tool_runner(program: Path, *, timeout_seconds: float) -> FfmpegMediaTools:
    return FfmpegMediaTools(
        ffmpeg_path=str(program),
        ffprobe_path=str(program),
        timeout_seconds=timeout_seconds,
        stop_timeout_seconds=STOP_TIMEOUT_SECONDS,
    )

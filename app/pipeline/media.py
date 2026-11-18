"""ffprobe and ffmpeg: what a media file holds, and the copies the pipeline keeps.

The pipeline runs the two programs; no Python media package is used. Every run
has a time limit, its arguments are passed as a list (never through a shell),
and its error output is never logged or raised, because it can name the file.

    tools = FfmpegMediaTools(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", timeout_seconds=60)
    probe = await tools.probe(Path("clip.mp4"))
    if probe.has_audio:
        audio = await tools.extract_audio(Path("clip.mp4"))
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

logger = logging.getLogger(__name__)

# What speech-to-text gets: mono WAV at 16 kHz. Every ffmpeg build has this
# encoder, every speech-to-text model accepts it, and speech needs no more.
AUDIO_FORMAT: Final = "wav"
_AUDIO_SAMPLE_RATE_HZ: Final = "16000"
_AUDIO_CHANNELS: Final = "1"
# The stored video copy: H.264 and AAC in MP4, readable by every model and player.
_VIDEO_CODEC: Final = "libx264"
_VIDEO_PRESET: Final = "veryfast"
_AUDIO_CODEC: Final = "aac"
# The thumbnail is the frame at this time, or the first frame of a shorter clip.
_THUMBNAIL_SECOND: Final = "1"


class MediaToolError(Exception):
    """ffprobe or ffmpeg failed, timed out, or could not be started.

    The message names the program and the reason, never the file or the
    program's own error output.
    """


class MediaToolStartError(MediaToolError):
    """ffprobe or ffmpeg could not be started, for example because it is not installed.

    This is a problem of the machine, not of the file, so it is never taken to
    mean that a file is unreadable.
    """


@dataclass(frozen=True)
class MediaProbe:
    """What a media file holds, as ffprobe reports it.

    Attributes:
        has_video: Whether it has a video stream. An image counts as one.
        has_audio: Whether it has an audio track. Whether the track holds
            speech is decided by speech-to-text, not here.
        duration_seconds: The file's length; `None` when ffprobe reports none,
            as for an image.
    """

    has_video: bool
    has_audio: bool
    duration_seconds: float | None


class MediaTools(Protocol):
    """Inspects media files and makes the copies the pipeline keeps."""

    async def probe(self, source: Path) -> MediaProbe:
        """Report the streams and length of a file.

        Args:
            source: The file.

        Returns:
            What the file holds.

        Raises:
            MediaToolError: If the file cannot be read as media.
        """
        ...

    async def make_video_copy(self, source: Path, target: Path, max_height: int) -> None:
        """Write a smaller MP4 copy of a video, with its audio track if it has one.

        Args:
            source: The downloaded video.
            target: Where to write the copy.
            max_height: The copy's height in pixels; a smaller video keeps its size.

        Raises:
            MediaToolError: If the copy could not be made.
        """
        ...

    async def make_still(self, source: Path, target: Path, max_height: int) -> None:
        """Write a JPEG of an image, or of one frame of a video.

        Args:
            source: The downloaded image or video.
            target: Where to write the JPEG.
            max_height: Its height in pixels; a smaller picture keeps its size.

        Raises:
            MediaToolError: If the picture could not be made.
        """
        ...

    async def extract_audio(self, source: Path) -> bytes:
        """Return a video's audio track as WAV (`AUDIO_FORMAT`).

        Args:
            source: A video with an audio track.

        Returns:
            The audio file's bytes.

        Raises:
            MediaToolError: If the audio could not be extracted.
        """
        ...


class FfmpegMediaTools(MediaTools):
    """`MediaTools` on the `ffmpeg` and `ffprobe` programs."""

    def __init__(self, *, ffmpeg_path: str, ffprobe_path: str, timeout_seconds: float) -> None:
        """Create the tools.

        Args:
            ffmpeg_path: The `ffmpeg` program, a name on `PATH` or a path.
            ffprobe_path: The `ffprobe` program.
            timeout_seconds: Time limit of one run.
        """
        self._ffmpeg = ffmpeg_path
        self._ffprobe = ffprobe_path
        self._timeout_seconds = timeout_seconds

    async def probe(self, source: Path) -> MediaProbe:
        """Report the streams and length of a file.

        Args:
            source: The file.

        Returns:
            What the file holds.

        Raises:
            MediaToolError: If ffprobe fails or its answer is not what it documents.
        """
        output = await self._run(
            self._ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            str(source),
        )
        return _parse_probe(output)

    async def make_video_copy(self, source: Path, target: Path, max_height: int) -> None:
        """Write a smaller MP4 copy of a video, with its audio track if it has one.

        Args:
            source: The downloaded video.
            target: Where to write the copy.
            max_height: The copy's height in pixels; a smaller video keeps its size.

        Raises:
            MediaToolError: If ffmpeg fails.
        """
        await self._run(
            self._ffmpeg,
            *_FFMPEG_QUIET,
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            _scale_filter(max_height),
            "-c:v",
            _VIDEO_CODEC,
            "-preset",
            _VIDEO_PRESET,
            "-c:a",
            _AUDIO_CODEC,
            # The index goes first, so a reader can start before the end of the file.
            "-movflags",
            "+faststart",
            str(target),
        )

    async def make_still(self, source: Path, target: Path, max_height: int) -> None:
        """Write a JPEG of an image, or of one frame of a video.

        Args:
            source: The downloaded image or video.
            target: Where to write the JPEG.
            max_height: Its height in pixels; a smaller picture keeps its size.

        Raises:
            MediaToolError: If ffmpeg fails.
        """
        probe = await self.probe(source)
        seek = (
            ["-ss", _THUMBNAIL_SECOND]
            if probe.duration_seconds and probe.duration_seconds > float(_THUMBNAIL_SECOND)
            else []
        )
        await self._run(
            self._ffmpeg,
            *_FFMPEG_QUIET,
            *seek,
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            _scale_filter(max_height),
            str(target),
        )

    async def extract_audio(self, source: Path) -> bytes:
        """Return a video's audio track as 16 kHz mono WAV.

        Args:
            source: A video with an audio track.

        Returns:
            The WAV file's bytes.

        Raises:
            MediaToolError: If ffmpeg fails.
        """
        return await self._run(
            self._ffmpeg,
            *_FFMPEG_QUIET,
            "-i",
            str(source),
            "-vn",
            "-ac",
            _AUDIO_CHANNELS,
            "-ar",
            _AUDIO_SAMPLE_RATE_HZ,
            "-f",
            AUDIO_FORMAT,
            "pipe:1",
        )

    async def _run(self, program: str, *arguments: str) -> bytes:
        name = Path(program).name
        try:
            process = await asyncio.create_subprocess_exec(
                program,
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            raise MediaToolStartError(
                f"{name} could not be started ({type(error).__name__})"
            ) from error
        try:
            output, _ = await asyncio.wait_for(process.communicate(), self._timeout_seconds)
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise MediaToolError(f"{name} took longer than {self._timeout_seconds} s") from error
        if process.returncode != 0:
            raise MediaToolError(f"{name} failed with exit code {process.returncode}")
        return output


# `-y` overwrites a target in the job's own temporary folder; `-nostdin` keeps
# ffmpeg from waiting for keyboard input.
_FFMPEG_QUIET: Final = ("-nostdin", "-hide_banner", "-loglevel", "error", "-y")


def _scale_filter(max_height: int) -> str:
    # Scale down to the height, never up; -2 keeps the width even, as H.264 needs.
    return f"scale=-2:'min({max_height},ih)'"


def _parse_probe(output: bytes) -> MediaProbe:
    try:
        report = json.loads(output)
    except json.JSONDecodeError as error:
        raise MediaToolError("ffprobe returned no JSON") from error
    if not isinstance(report, dict):
        raise MediaToolError("ffprobe returned an unexpected report")
    streams = report.get("streams", [])
    if not isinstance(streams, list):
        raise MediaToolError("ffprobe returned an unexpected stream list")
    codec_types = {stream.get("codec_type") for stream in streams if isinstance(stream, dict)}
    return MediaProbe(
        has_video="video" in codec_types,
        has_audio="audio" in codec_types,
        duration_seconds=_duration(report.get("format")),
    )


def _duration(media_format: object) -> float | None:
    if not isinstance(media_format, dict):
        return None
    value = media_format.get("duration")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

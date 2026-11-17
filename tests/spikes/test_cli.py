import asyncio
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.spikes.model_choices import cli, stt
from scripts.spikes.model_choices.cli import REPOSITORY_ROOT, main
from scripts.spikes.model_choices.settings import (
    API_KEY_ENVIRONMENT_VARIABLE,
    SPEECH_TO_TEXT_MODELS,
)
from tests.spikes.clip_set import (
    CLIP_SECONDS,
    LANGUAGES,
    SENTENCE_IDS,
    wav_bytes,
    write_clip_set,
)

FAKE_KEY = "sk-test-not-a-real-key"
PRIVATE_WORDS = "words spoken in a private reel"
TRANSCRIPTION_COST = 0.0001
EMBEDDING_COST = 0.00002
FULL_SIZES = {"openai/text-embedding-3-large": 3072, "qwen/qwen3-embedding-8b": 4096}
STT_MODEL_COUNT = 4
FLEURS_CLIP_COUNT = len(LANGUAGES) * len(SENTENCE_IDS)
# Requests go clip by clip, so the fourth one is the first clip with the fourth model.
COMPLETED_BEFORE_THE_BREAK = 3
INTERRUPTED_REQUEST_ESTIMATE = stt.estimate_transcription_usd(
    SPEECH_TO_TEXT_MODELS[COMPLETED_BEFORE_THE_BREAK], CLIP_SECONDS
)


class FakeOpenRouter:
    """Answers like OpenRouter, from memory, and keeps the requests it saw."""

    def __init__(
        self,
        transcription_cost: float | str = TRANSCRIPTION_COST,
        fail_after: int | None = None,
        failure: BaseException | None = None,
    ) -> None:
        """Create the fake.

        Args:
            transcription_cost: The charge for a transcription, or a raw JSON literal such as "NaN".
            fail_after: Fail on the paid request after this many, to imitate a broken run.
            failure: What that request raises; a crash by default.
        """
        self.transcription_cost = str(transcription_cost)
        self.fail_after = fail_after
        self.failure = failure or RuntimeError("the run broke")
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"usage": 0.08541}})
        if self.fail_after is not None and len(self.paid_requests()) > self.fail_after:
            raise self.failure
        body = json.loads(request.content)
        if request.url.path.endswith("/audio/transcriptions"):
            answer = (
                f'{{"text": {json.dumps(PRIVATE_WORDS)}, '
                f'"usage": {{"cost": {self.transcription_cost}}}}}'
            )
            return httpx.Response(200, content=answer.encode("utf-8"))
        size = body.get("dimensions", FULL_SIZES[body["model"]])
        data = [
            {"index": index, "embedding": self._vector(text, size)}
            for index, text in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": data, "usage": {"cost": EMBEDDING_COST}})

    @staticmethod
    def _vector(text: str, size: int) -> list[float]:
        """Point along the axis of the sentence id found in the synthetic reference."""
        match = re.search(r"sentence (\d+)", text)
        assert match is not None
        vector = [0.01] * size
        vector[int(match.group(1))] = 1.0
        return vector

    def paid_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]


@pytest.fixture
def clip_set(tmp_path: Path) -> tuple[Path, Path]:
    """A media folder with synthetic clips, and the provenance file that matches it."""
    media_directory = tmp_path / "media"
    provenance = write_clip_set(media_directory)
    provenance_path = tmp_path / "provenance.json"
    provenance_path.write_text(provenance.model_dump_json(), encoding="utf-8")
    return media_directory, provenance_path


def run(
    arguments: list[str], provenance_path: Path, api: FakeOpenRouter | None = None
) -> tuple[int, FakeOpenRouter]:
    fake_api = api or FakeOpenRouter()
    exit_code = main(arguments, httpx.MockTransport(fake_api), provenance_path)
    return exit_code, fake_api


def load_report(output_directory: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads((output_directory / "report.json").read_text("utf-8"))
    return report


def test_dry_run_sends_nothing_and_needs_no_key(
    clip_set: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.delenv(API_KEY_ENVIRONMENT_VARIABLE, raising=False)

    exit_code, api = run(["--media-dir", str(media_directory)], provenance_path)

    assert exit_code == 0
    assert api.requests == []
    plan = next(
        json.loads(line) for line in capsys.readouterr().out.splitlines() if '"plan"' in line
    )
    assert plan["transcription_requests"] == FLEURS_CLIP_COUNT * STT_MODEL_COUNT
    assert plan["embedding_requests"] == len(FULL_SIZES) * 2
    assert plan["worst_case_usd"] > 0


@pytest.mark.parametrize(
    "missing",
    [
        pytest.param("--spent-so-far", id="no spent-so-far"),
        pytest.param("--output-dir", id="no output-dir"),
    ],
)
def test_live_run_needs_the_earlier_spend_and_an_output_folder(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    options = {"--spent-so-far": "0.08541", "--output-dir": str(tmp_path / "out")}
    del options[missing]
    arguments = ["--media-dir", str(media_directory), "--live"]
    for name, value in options.items():
        arguments += [name, value]

    exit_code, api = run(arguments, provenance_path)

    assert exit_code == 1
    assert api.requests == []


def live_arguments(media_directory: Path, output_directory: Path, *extra: str) -> list[str]:
    return [
        "--media-dir",
        str(media_directory),
        "--output-dir",
        str(output_directory),
        "--live",
        "--spent-so-far",
        "0.08541",
        *extra,
    ]


def test_live_run_without_a_key_sends_nothing(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.delenv(API_KEY_ENVIRONMENT_VARIABLE, raising=False)

    exit_code, api = run(live_arguments(media_directory, tmp_path / "out"), provenance_path)

    assert exit_code == 1
    assert api.requests == []


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(("--spend-cap", "2.01"), id="cap above the owner's limit"),
        pytest.param(("--spend-cap", "0"), id="cap of zero"),
        pytest.param(("--spend-cap", "-1"), id="negative cap"),
        pytest.param(("--spend-cap", "nan"), id="cap is NaN"),
        pytest.param(("--spend-cap", "inf"), id="infinite cap"),
        pytest.param(("--spent-so-far", "-1"), id="negative earlier spend"),
        pytest.param(("--spent-so-far", "2.00"), id="cap already reached"),
        pytest.param(("--spent-so-far", "nan"), id="earlier spend is NaN"),
        pytest.param(("--spent-so-far", "inf"), id="infinite earlier spend"),
        pytest.param(("--spent-so-far=-inf",), id="minus infinite earlier spend"),
    ],
)
def test_invalid_spending_options_send_nothing(
    clip_set: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    arguments = [*live_arguments(media_directory, tmp_path / "out"), *extra]

    exit_code, api = run(arguments, provenance_path)

    assert exit_code == 1
    assert api.requests == []


def test_run_stops_when_real_charges_reach_the_cap_and_keeps_what_it_measured(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    expensive_api = FakeOpenRouter(transcription_cost=1.0)
    output_directory = tmp_path / "out"

    exit_code, api = run(
        live_arguments(media_directory, output_directory), provenance_path, expensive_api
    )

    assert exit_code == 0
    assert len(api.paid_requests()) == 2
    report = load_report(output_directory)
    assert report["spend"]["stopped_at_spend_cap"]
    assert report["spend"]["cumulative_usd"] == pytest.approx(2.08541)
    assert len(report["speech_to_text"]["requests"]) == 2
    assert report["embeddings"] == []


def test_plan_that_does_not_fit_under_the_cap_sends_nothing(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    arguments = live_arguments(media_directory, tmp_path / "out", "--spend-cap", "0.0855")

    exit_code, api = run(arguments, provenance_path)

    assert exit_code == 1
    assert api.requests == []
    assert not (tmp_path / "out").exists()


def test_results_inside_the_repository_are_refused(
    clip_set: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    inside = REPOSITORY_ROOT / "spike-output-that-must-not-exist"

    exit_code, api = run(live_arguments(media_directory, inside), provenance_path)

    assert exit_code == 1
    assert api.requests == []
    assert not inside.exists()


def test_changed_clip_stops_the_run_before_any_request(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    (media_directory / "en_us-11.wav").write_bytes(wav_bytes(seconds=0.2))

    exit_code, api = run(live_arguments(media_directory, tmp_path / "out"), provenance_path)

    assert exit_code == 1
    assert api.requests == []


def test_live_run_writes_a_report_without_the_key_or_private_transcripts(
    clip_set: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    private_directory = tmp_path / "private"
    private_directory.mkdir()
    (private_directory / "restaurant-name.wav").write_bytes(wav_bytes(seconds=2.0))
    output_directory = tmp_path / "out"
    arguments = live_arguments(
        media_directory, output_directory, "--unreferenced-dir", str(private_directory)
    )

    exit_code, api = run(arguments, provenance_path)

    assert exit_code == 0
    clip_count = FLEURS_CLIP_COUNT + 1
    assert len(api.paid_requests()) == clip_count * STT_MODEL_COUNT + len(FULL_SIZES) * 2

    report = load_report(output_directory)
    requests = report["speech_to_text"]["requests"]
    private = [request for request in requests if request["group"] == "unreferenced"]
    assert len(private) == STT_MODEL_COUNT
    assert all(request["transcript"] is None and request["succeeded"] for request in private)
    assert all(request["transcript"] for request in requests if request["group"] == "fleurs")
    assert len(report["speech_to_text"]["by_language"]) == len(LANGUAGES) * STT_MODEL_COUNT
    assert {row["metric"] for row in report["speech_to_text"]["by_language"]} == {"WER", "CER"}

    assert [(run["expected_dimensions"], run["vectors_valid"]) for run in report["embeddings"]] == [
        (1024, True),
        (3072, True),
        (1024, True),
        (4096, True),
    ]
    assert all(
        run["retrieval"]["pair_correct"] == run["retrieval"]["pair_queries"]
        for run in report["embeddings"]
    )

    spend = report["spend"]
    expected_charges = clip_count * STT_MODEL_COUNT * TRANSCRIPTION_COST + 4 * EMBEDDING_COST
    assert spend["returned_this_run_usd"] == pytest.approx(expected_charges)
    assert spend["unconfirmed_this_run_usd"] == 0
    assert spend["cumulative_usd"] == pytest.approx(0.08541 + expected_charges)
    assert spend["key_usage_before_usd"] == pytest.approx(0.08541)
    assert not spend["stopped_at_spend_cap"]
    assert report["test_set"]["revision"]

    written = (output_directory / "report.json").read_text("utf-8") + capsys.readouterr().out
    assert FAKE_KEY not in written
    assert "restaurant-name" not in written


def test_invalid_amount_is_refused_in_a_dry_run_too(clip_set: tuple[Path, Path]) -> None:
    media_directory, provenance_path = clip_set
    arguments = ["--media-dir", str(media_directory), "--spent-so-far", "nan"]

    exit_code, api = run(arguments, provenance_path)

    assert exit_code == 1
    assert api.requests == []


def test_malformed_charges_are_counted_at_their_estimates_and_never_reach_the_report(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    output_directory = tmp_path / "out"

    exit_code, api = run(
        live_arguments(media_directory, output_directory),
        provenance_path,
        FakeOpenRouter(transcription_cost="NaN"),
    )

    assert exit_code == 0
    report_text = (output_directory / "report.json").read_text("utf-8")
    assert "NaN" not in report_text
    assert "Infinity" not in report_text
    report = load_report(output_directory)
    spend = report["spend"]
    assert spend["returned_this_run_usd"] == pytest.approx(4 * EMBEDDING_COST)
    assert spend["unconfirmed_this_run_usd"] > 0
    assert spend["cumulative_usd"] == pytest.approx(
        0.08541 + spend["returned_this_run_usd"] + spend["unconfirmed_this_run_usd"]
    )
    assert all(
        request["returned_cost_usd"] is None for request in report["speech_to_text"]["requests"]
    )
    assert len(api.paid_requests()) == FLEURS_CLIP_COUNT * STT_MODEL_COUNT + 4


def test_malformed_charges_cannot_carry_the_run_past_the_cap(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With NaN charges and NaN earlier spend, every request used to be let through."""
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    arguments = [
        "--media-dir",
        str(media_directory),
        "--output-dir",
        str(tmp_path / "out"),
        "--live",
        "--spent-so-far",
        "nan",
    ]

    exit_code, api = run(arguments, provenance_path, FakeOpenRouter(transcription_cost=1.0))

    assert exit_code == 1
    assert api.requests == []


def make_regular_file(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.write_text("not a folder", encoding="utf-8")
    return path


def make_folder_under_a_file(tmp_path: Path) -> Path:
    return make_regular_file(tmp_path) / "results"


def make_folder_with_an_earlier_report(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    (path / "report.json").write_text("an earlier run", encoding="utf-8")
    return path


def make_folder_where_the_report_name_is_a_folder(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    (path / "report.json").mkdir(parents=True)
    return path


@pytest.mark.parametrize(
    "make_output_path",
    [
        pytest.param(make_regular_file, id="the output folder is a regular file"),
        pytest.param(make_folder_under_a_file, id="the output folder is below a regular file"),
        pytest.param(make_folder_with_an_earlier_report, id="an earlier report is in the way"),
        pytest.param(make_folder_where_the_report_name_is_a_folder, id="report.json is a folder"),
    ],
)
def test_unusable_output_destination_sends_nothing(
    clip_set: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_output_path: Callable[[Path], Path],
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    output_path = make_output_path(tmp_path)

    exit_code, api = run(live_arguments(media_directory, output_path), provenance_path)

    assert exit_code == 1
    assert api.requests == []


def test_earlier_report_is_not_overwritten(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    output_directory = make_folder_with_an_earlier_report(tmp_path)

    run(live_arguments(media_directory, output_directory), provenance_path)

    assert (output_directory / "report.json").read_text("utf-8") == "an earlier run"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only folder")
def test_read_only_output_folder_sends_nothing(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    output_directory = tmp_path / "out"
    output_directory.mkdir()
    output_directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        exit_code, api = run(live_arguments(media_directory, output_directory), provenance_path)
    finally:
        output_directory.chmod(stat.S_IRWXU)

    assert exit_code == 1
    assert api.requests == []


def test_run_that_breaks_midway_still_writes_what_it_spent(
    clip_set: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    output_directory = tmp_path / "out"
    broken_api = FakeOpenRouter(fail_after=COMPLETED_BEFORE_THE_BREAK)

    with pytest.raises(RuntimeError, match="the run broke"):
        run(live_arguments(media_directory, output_directory), provenance_path, broken_api)

    report = load_report(output_directory)
    assert report["spend"]["run_finished"] is False
    assert report["spend"]["returned_this_run_usd"] == pytest.approx(3 * TRANSCRIPTION_COST)
    assert report["spend"]["unconfirmed_this_run_usd"] == pytest.approx(
        INTERRUPTED_REQUEST_ESTIMATE
    )
    assert len(report["speech_to_text"]["requests"]) == 3


def test_report_that_cannot_be_written_is_reported_with_the_spending(
    clip_set: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    output_directory = tmp_path / "out"

    def disk_is_full(path: Path, report: dict[str, object]) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(cli, "_write_report", disk_is_full)

    exit_code, api = run(live_arguments(media_directory, output_directory), provenance_path)

    assert exit_code == 1
    assert len(api.paid_requests()) == FLEURS_CLIP_COUNT * STT_MODEL_COUNT + 4
    spend_lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if '"spend"' in line
    ]
    expected = FLEURS_CLIP_COUNT * STT_MODEL_COUNT * TRANSCRIPTION_COST + 4 * EMBEDDING_COST
    assert spend_lines[-1]["returned_this_run_usd"] == pytest.approx(expected)


def spend_log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"message": "spend"' in line
    ]


def assert_interrupted_spend(spend: dict[str, Any]) -> None:
    """Three requests were charged; the fourth was cut off and counts once, as an estimate."""
    assert spend["run_finished"] is False
    assert spend["returned_this_run_usd"] == pytest.approx(
        COMPLETED_BEFORE_THE_BREAK * TRANSCRIPTION_COST
    )
    assert spend["unconfirmed_this_run_usd"] == pytest.approx(INTERRUPTED_REQUEST_ESTIMATE)
    assert spend["cumulative_usd"] == pytest.approx(
        0.08541 + COMPLETED_BEFORE_THE_BREAK * TRANSCRIPTION_COST + INTERRUPTED_REQUEST_ESTIMATE
    )


def test_cancelled_request_is_reserved_once_in_the_report_and_the_spending_log(
    clip_set: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    output_directory = tmp_path / "out"
    cancelled_api = FakeOpenRouter(
        fail_after=COMPLETED_BEFORE_THE_BREAK, failure=asyncio.CancelledError()
    )

    with pytest.raises(asyncio.CancelledError):
        run(live_arguments(media_directory, output_directory), provenance_path, cancelled_api)

    assert len(cancelled_api.paid_requests()) == COMPLETED_BEFORE_THE_BREAK + 1
    report = load_report(output_directory)
    assert_interrupted_spend(report["spend"])
    assert len(report["speech_to_text"]["requests"]) == COMPLETED_BEFORE_THE_BREAK
    (logged_spend,) = spend_log_lines(capsys)
    assert_interrupted_spend(logged_spend)


def test_cancelled_run_whose_report_cannot_be_written_still_logs_the_reservation(
    clip_set: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media_directory, provenance_path = clip_set
    monkeypatch.setenv(API_KEY_ENVIRONMENT_VARIABLE, FAKE_KEY)
    output_directory = tmp_path / "out"
    cancelled_api = FakeOpenRouter(
        fail_after=COMPLETED_BEFORE_THE_BREAK, failure=asyncio.CancelledError()
    )

    def disk_is_full(path: Path, report: dict[str, object]) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(cli, "_write_report", disk_is_full)

    # The cancellation must come through, not be replaced by the write error.
    with pytest.raises(asyncio.CancelledError):
        run(live_arguments(media_directory, output_directory), provenance_path, cancelled_api)

    output = capsys.readouterr().out
    (logged_spend,) = [
        json.loads(line) for line in output.splitlines() if '"message": "spend"' in line
    ]
    assert_interrupted_spend(logged_spend)
    assert '"message": "report_not_written"' in output

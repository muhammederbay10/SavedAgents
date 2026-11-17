import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.spikes.model_choices.cli import REPOSITORY_ROOT, main
from scripts.spikes.model_choices.settings import API_KEY_ENVIRONMENT_VARIABLE
from tests.spikes.clip_set import LANGUAGES, SENTENCE_IDS, wav_bytes, write_clip_set

FAKE_KEY = "sk-test-not-a-real-key"
PRIVATE_WORDS = "words spoken in a private reel"
TRANSCRIPTION_COST = 0.0001
EMBEDDING_COST = 0.00002
FULL_SIZES = {"openai/text-embedding-3-large": 3072, "qwen/qwen3-embedding-8b": 4096}
STT_MODEL_COUNT = 4
FLEURS_CLIP_COUNT = len(LANGUAGES) * len(SENTENCE_IDS)


class FakeOpenRouter:
    """Answers like OpenRouter, from memory, and keeps the requests it saw."""

    def __init__(self, transcription_cost: float = TRANSCRIPTION_COST) -> None:
        self.transcription_cost = transcription_cost
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"usage": 0.08541}})
        body = json.loads(request.content)
        if request.url.path.endswith("/audio/transcriptions"):
            answer = {"text": PRIVATE_WORDS, "usage": {"cost": self.transcription_cost}}
            return httpx.Response(200, json=answer)
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
        pytest.param(("--spent-so-far", "-1"), id="negative earlier spend"),
        pytest.param(("--spent-so-far", "2.00"), id="cap already reached"),
    ],
)
def test_invalid_spending_options_send_nothing(
    clip_set: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, str],
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

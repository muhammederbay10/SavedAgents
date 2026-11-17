"""Command line for the SAV-7 model comparison.

Without `--live` nothing is sent: the test clips are verified and the plan is
logged. With `--live` the same clips go to every speech-to-text model, the
reference sentences go to every embedding model, and a report is written to a
folder outside the repository.

Everything that can stop a live run is checked before the first request: the
amounts, the key, the plan against the cap, and that the report can be written.
Once requests have been sent, the report and its spending are written even if
the run breaks.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.logging_config import configure_logging
from scripts.spikes.model_choices import embeddings, stt
from scripts.spikes.model_choices.budget import (
    InvalidAmountError,
    SpendCapExceededError,
    SpendLedger,
    is_valid_amount,
)
from scripts.spikes.model_choices.fleurs import (
    PROVENANCE_PATH,
    ClipSetError,
    FleursSample,
    Provenance,
    load_provenance,
    load_samples,
)
from scripts.spikes.model_choices.openrouter import OpenRouterClient, build_http_client
from scripts.spikes.model_choices.settings import (
    API_KEY_ENVIRONMENT_VARIABLE,
    EMBEDDING_MODELS,
    SPEECH_TO_TEXT_MODELS,
    SPEND_CAP_USD,
)

logger = logging.getLogger(__name__)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REPORT_NAME = "report.json"
RUN_STARTED_NOTE = "the run started; this file is replaced by the report when it ends"
EXIT_OK = 0
EXIT_FAILED = 1
# Each embedding model runs at 1024 dimensions and at its full size.
EMBEDDING_SIZES_PER_MODEL = 2


class UsageError(ValueError):
    """Raised when the command line asks for something the spike does not allow."""


class ReportNotWrittenError(RuntimeError):
    """Raised when a live run finished but its report could not be saved."""


@dataclass(frozen=True)
class Plan:
    """What a live run would send.

    Attributes:
        transcription_requests: Clips x speech-to-text models.
        embedding_requests: Embedding models x vector sizes.
        worst_case_usd: The sum of every request's cost estimate.
    """

    transcription_requests: int
    embedding_requests: int
    worst_case_usd: float


@dataclass
class Measurements:
    """Results collected during a live run; partly filled if the run stops early.

    Attributes:
        transcriptions: One outcome per finished transcription request.
        embedding_runs: One entry per finished embedding request.
        key_usage_before_usd: The key's total spend before the run, if readable.
        key_usage_after_usd: The key's total spend after the run, if readable.
        stopped_at_spend_cap: The ledger refused a request mid-run.
        finished: The run reached its end; false if it broke part-way.
    """

    transcriptions: list[stt.TranscriptionOutcome] = field(default_factory=list)
    embedding_runs: list[embeddings.EmbeddingRun] = field(default_factory=list)
    key_usage_before_usd: float | None = None
    key_usage_after_usd: float | None = None
    stopped_at_spend_cap: bool = False
    finished: bool = False


def main(
    arguments: Sequence[str],
    transport: httpx.AsyncBaseTransport | None = None,
    provenance_path: Path = PROVENANCE_PATH,
) -> int:
    """Run the spike.

    Args:
        arguments: Command-line arguments without the program name.
        transport: A replacement HTTP transport; tests pass a mock here.
        provenance_path: The provenance file; tests pass one for synthetic clips.

    Returns:
        The process exit code: 0 on success, 1 when the run was refused or failed.
    """
    options = _parse(arguments)
    configure_logging("INFO", sys.stdout)
    try:
        _check_amounts(options)
        provenance = load_provenance(provenance_path)
        samples = load_samples(_outside_repository(options.media_dir), provenance)
        clips = stt.fleurs_clips(samples)
        if options.unreferenced_dir is not None:
            clips += stt.unreferenced_clips(_outside_repository(options.unreferenced_dir))
        plan = build_plan(clips, samples)
        logger.info("plan", extra=asdict(plan))
        if not options.live:
            logger.info("dry_run_finished", extra={"hint": "add --live to send the requests"})
            return EXIT_OK
        report_path = _run_live(options, provenance, samples, clips, plan, transport)
    except (ClipSetError, UsageError, InvalidAmountError, SpendCapExceededError) as error:
        logger.error("run_refused", extra={"reason": str(error)})
        return EXIT_FAILED
    except ReportNotWrittenError as error:
        logger.error("report_not_written", extra={"reason": str(error)})
        return EXIT_FAILED
    logger.info("report_written", extra={"path": str(report_path)})
    return EXIT_OK


def _parse(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.spikes.model_choices",
        description="SAV-7: compare speech-to-text and embedding models through OpenRouter.",
    )
    parser.add_argument(
        "--media-dir",
        type=Path,
        required=True,
        help="folder outside the repository with the FLEURS manifest.json and WAV clips",
    )
    parser.add_argument(
        "--unreferenced-dir",
        type=Path,
        help="optional folder outside the repository with extra PCM WAV clips (no references)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="folder outside the repository for report.json (needed with --live)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="send real, paid requests to OpenRouter; without it nothing is sent",
    )
    parser.add_argument(
        "--spent-so-far",
        type=float,
        help="US dollars already charged for SAV-7 in earlier runs (needed with --live)",
    )
    parser.add_argument(
        "--spend-cap",
        type=float,
        default=SPEND_CAP_USD,
        help=f"cumulative limit in US dollars; can be lowered, never above {SPEND_CAP_USD:.2f}",
    )
    return parser.parse_args(arguments)


def _check_amounts(options: argparse.Namespace) -> None:
    """Refuse amounts that are not real dollars, before anything else happens.

    `float("nan")` and `float("inf")` parse as numbers, but a NaN makes every
    cap comparison false, so they are rejected here as well as in the ledger.
    """
    if options.spent_so_far is not None and not is_valid_amount(options.spent_so_far):
        raise UsageError("--spent-so-far must be a finite number, zero or more")
    if not is_valid_amount(options.spend_cap) or not 0 < options.spend_cap <= SPEND_CAP_USD:
        raise UsageError(f"--spend-cap must be above 0 and at most {SPEND_CAP_USD:.2f}")


def build_plan(clips: Sequence[stt.Clip], samples: Sequence[FleursSample]) -> Plan:
    """Count the requests of a live run and add up their cost estimates."""
    texts = [sample.reference for sample in samples]
    transcription_usd = sum(
        stt.estimate_transcription_usd(model, clip.duration_seconds)
        for clip in clips
        for model in SPEECH_TO_TEXT_MODELS
    )
    embedding_usd = sum(
        embeddings.estimate_embedding_usd(model, texts) * EMBEDDING_SIZES_PER_MODEL
        for model in EMBEDDING_MODELS
    )
    return Plan(
        transcription_requests=len(clips) * len(SPEECH_TO_TEXT_MODELS),
        embedding_requests=len(EMBEDDING_MODELS) * EMBEDDING_SIZES_PER_MODEL,
        worst_case_usd=transcription_usd + embedding_usd,
    )


def _run_live(
    options: argparse.Namespace,
    provenance: Provenance,
    samples: Sequence[FleursSample],
    clips: Sequence[stt.Clip],
    plan: Plan,
    transport: httpx.AsyncBaseTransport | None,
) -> Path:
    if options.output_dir is None or options.spent_so_far is None:
        raise UsageError("--live needs --output-dir and --spent-so-far")
    output_directory = _outside_repository(options.output_dir)
    api_key = os.environ.get(API_KEY_ENVIRONMENT_VARIABLE)
    if not api_key:
        raise UsageError(f"{API_KEY_ENVIRONMENT_VARIABLE} is not set in the environment")
    ledger = SpendLedger(cap_usd=options.spend_cap, spent_before_usd=options.spent_so_far)
    # The whole plan must fit before the first request, not only each request on its own.
    ledger.authorize(plan.worst_case_usd)
    # Last check before paid work: a run whose report cannot be saved must not start.
    report_path = _reserve_report(output_directory)
    measurements = Measurements()
    try:
        asyncio.run(_measure(api_key, ledger, samples, clips, transport, measurements))
    finally:
        # Requests may have been charged, so the spending is kept even if the run broke.
        _save_report(report_path, build_report(provenance, plan, ledger, measurements))
    return report_path


def _reserve_report(output_directory: Path) -> Path:
    """Create the output folder and claim the report file, without overwriting anything.

    Args:
        output_directory: The resolved output folder, outside the repository.

    Returns:
        The path of the report, which now holds a short "run started" note.

    Raises:
        UsageError: If the folder cannot be created or written, or a report is already there.
    """
    report_path = output_directory / REPORT_NAME
    try:
        output_directory.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8") as placeholder:
            placeholder.write(json.dumps({"status": RUN_STARTED_NOTE}))
    except FileExistsError as error:
        raise UsageError(
            f"the output folder is a file, or already holds {REPORT_NAME}; use a new folder"
        ) from error
    except OSError as error:
        raise UsageError("the output folder cannot be created or written to") from error
    return report_path


def _save_report(report_path: Path, report: dict[str, object]) -> None:
    """Log the spending, then write the report over the "run started" note.

    The spending is logged first, so it is not lost if the file cannot be written.

    Raises:
        ReportNotWrittenError: If the report file cannot be written.
    """
    logger.info("spend", extra=_as_log_fields(report["spend"]))
    try:
        _write_report(report_path, report)
    except OSError as error:
        raise ReportNotWrittenError(
            "the report could not be written; the spending is in the log line before this one"
        ) from error


def _write_report(report_path: Path, report: dict[str, object]) -> None:
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def _as_log_fields(spend: object) -> dict[str, object]:
    return dict(spend) if isinstance(spend, dict) else {}


async def _measure(
    api_key: str,
    ledger: SpendLedger,
    samples: Sequence[FleursSample],
    clips: Sequence[stt.Clip],
    transport: httpx.AsyncBaseTransport | None,
    measurements: Measurements,
) -> None:
    """Run both comparisons, filling `measurements` as requests finish."""
    async with build_http_client(api_key, transport) as http_client:
        client = OpenRouterClient(http_client, ledger)
        measurements.key_usage_before_usd = await client.key_usage_usd()
        try:
            await stt.run_comparison(
                client, SPEECH_TO_TEXT_MODELS, clips, measurements.transcriptions
            )
            await embeddings.run_comparison(
                client, EMBEDDING_MODELS, samples, measurements.embedding_runs
            )
        except SpendCapExceededError as error:
            measurements.stopped_at_spend_cap = True
            logger.error("stopped_at_spend_cap", extra={"reason": str(error)})
        measurements.key_usage_after_usd = await client.key_usage_usd()
    measurements.finished = True


def build_report(
    provenance: Provenance, plan: Plan, ledger: SpendLedger, measurements: Measurements
) -> dict[str, object]:
    """Assemble the report: test-set provenance, spending and every measurement.

    Transcripts are included only for the public FLEURS clips.

    Args:
        provenance: The committed FLEURS provenance.
        plan: The plan that was authorized.
        ledger: The ledger after the run.
        measurements: What the run measured.

    Returns:
        A JSON-serializable report.
    """
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "test_set": provenance.model_dump(exclude={"samples"}),
        "plan": asdict(plan),
        "spend": {
            "cap_usd": ledger.cap_usd,
            "spent_before_usd": ledger.spent_before_usd,
            "returned_this_run_usd": ledger.returned_usd,
            "unconfirmed_this_run_usd": ledger.unconfirmed_usd,
            "cumulative_usd": ledger.cumulative_usd,
            "key_usage_before_usd": measurements.key_usage_before_usd,
            "key_usage_after_usd": measurements.key_usage_after_usd,
            "stopped_at_spend_cap": measurements.stopped_at_spend_cap,
            "run_finished": measurements.finished,
        },
        "speech_to_text": {
            "by_language": [
                asdict(row) for row in stt.summarize_by_language(measurements.transcriptions)
            ],
            "by_model": [
                asdict(row) for row in stt.summarize_by_model(measurements.transcriptions)
            ],
            "requests": [asdict(outcome) for outcome in measurements.transcriptions],
        },
        "embeddings": [asdict(run) for run in measurements.embedding_runs],
    }


def _outside_repository(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(REPOSITORY_ROOT):
        raise UsageError("test media and results must stay outside the repository")
    return resolved

"""Loads the FLEURS test clips and checks them against the committed provenance.

The clips and their reference sentences stay outside the repository. The
repository only holds `fleurs_provenance.json`: which sentences were used, from
which dataset revision, and the SHA-256 of each clip and reference. Loading
fails if a clip or a sentence differs from what was measured.

FLEURS: Conneau et al. (2022), Google, CC BY 4.0,
https://huggingface.co/datasets/google/fleurs
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from scripts.spikes.model_choices.settings import FLEURS_MANIFEST_NAME

PROVENANCE_PATH = Path(__file__).with_name("fleurs_provenance.json")


class ClipSetError(ValueError):
    """Raised when the test clips are missing, changed or not aligned."""


class SampleProvenance(BaseModel):
    """What the repository records about one FLEURS clip.

    Attributes:
        language: FLEURS language code, e.g. `tr_tr`.
        id: FLEURS sentence id; the same id is the same sentence in every language.
        original_file: The clip's file name in the dataset.
        sha256_pcm_wav: SHA-256 of the converted PCM16 WAV file.
        sha256_reference_utf8: SHA-256 of the reference sentence, UTF-8 encoded.
        duration_s: Clip length in seconds.
    """

    language: str
    id: int
    original_file: str
    sha256_pcm_wav: str
    sha256_reference_utf8: str
    duration_s: float


class Provenance(BaseModel):
    """Source, licence and expected hashes of the FLEURS clips used in SAV-7."""

    dataset: str
    source: str
    revision: str
    split: str
    license: str
    attribution: str
    audio_preprocessing: str
    aligned_ids: list[int]
    samples: list[SampleProvenance]


class _ManifestSample(BaseModel):
    language: str
    id: int
    reference_raw: str
    audio_file: str


class _Manifest(BaseModel):
    dataset: str
    revision: str
    split: str
    samples: list[_ManifestSample]


@dataclass(frozen=True)
class FleursSample:
    """One verified test clip with its reference sentence.

    Attributes:
        language: FLEURS language code.
        sentence_id: FLEURS sentence id, shared across languages.
        reference: The reference sentence as published.
        audio: The clip's bytes (PCM16 WAV).
        duration_seconds: Clip length.
    """

    language: str
    sentence_id: int
    reference: str
    audio: bytes = field(repr=False)
    duration_seconds: float


def load_provenance(path: Path = PROVENANCE_PATH) -> Provenance:
    """Read the provenance file and check that its sentences are aligned.

    Args:
        path: The provenance JSON file.

    Returns:
        The provenance record.

    Raises:
        ClipSetError: If the file is invalid, or a language lacks one of the
            shared sentence ids.
    """
    try:
        provenance = Provenance.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        raise ClipSetError(f"cannot read the provenance file {path.name}") from error
    languages = {sample.language for sample in provenance.samples}
    expected = {
        (language, sentence_id) for language in languages for sentence_id in provenance.aligned_ids
    }
    recorded = [(sample.language, sample.id) for sample in provenance.samples]
    if set(recorded) != expected or len(recorded) != len(expected):
        raise ClipSetError("every language must have exactly the shared sentence ids")
    return provenance


def load_samples(media_directory: Path, provenance: Provenance) -> list[FleursSample]:
    """Load the clips listed in the media folder's manifest and verify each one.

    Args:
        media_directory: Folder outside the repository with `manifest.json` and the WAV files.
        provenance: The committed provenance to verify against.

    Returns:
        The verified samples, in provenance order.

    Raises:
        ClipSetError: If the manifest is missing or from another dataset revision,
            or a clip or reference is missing or does not match its recorded hash.
    """
    manifest = _read_manifest(media_directory / FLEURS_MANIFEST_NAME)
    if (manifest.dataset, manifest.revision, manifest.split) != (
        provenance.dataset,
        provenance.revision,
        provenance.split,
    ):
        raise ClipSetError("the manifest is not from the recorded dataset revision and split")
    listed = {(sample.language, sample.id): sample for sample in manifest.samples}
    return [_load_sample(media_directory, listed, expected) for expected in provenance.samples]


def _read_manifest(path: Path) -> _Manifest:
    try:
        return _Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        raise ClipSetError(f"cannot read the test-set manifest {path.name}") from error


def _load_sample(
    media_directory: Path,
    listed: dict[tuple[str, int], _ManifestSample],
    expected: SampleProvenance,
) -> FleursSample:
    label = f"{expected.language}-{expected.id}"
    sample = listed.get((expected.language, expected.id))
    if sample is None:
        raise ClipSetError(f"the manifest has no clip {label}")
    if _sha256(sample.reference_raw.encode("utf-8")) != expected.sha256_reference_utf8:
        raise ClipSetError(f"the reference sentence of {label} differs from the recorded one")
    # The manifest stores absolute paths from the machine that built it; only the name is used.
    audio_path = media_directory / Path(sample.audio_file).name
    try:
        audio = audio_path.read_bytes()
    except OSError as error:
        raise ClipSetError(f"cannot read the clip {audio_path.name}") from error
    if _sha256(audio) != expected.sha256_pcm_wav:
        raise ClipSetError(f"the clip {audio_path.name} differs from the recorded one")
    return FleursSample(
        language=expected.language,
        sentence_id=expected.id,
        reference=sample.reference_raw,
        audio=audio,
        duration_seconds=expected.duration_s,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

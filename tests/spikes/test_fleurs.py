import json
from pathlib import Path

import pytest

from scripts.spikes.model_choices.fleurs import (
    PROVENANCE_PATH,
    ClipSetError,
    load_provenance,
    load_samples,
)
from tests.spikes.clip_set import LANGUAGES, SENTENCE_IDS, reference_for, wav_bytes, write_clip_set

SAV7_LANGUAGES = {"en_us", "tr_tr", "ar_eg", "es_419", "hi_in", "ja_jp"}
SAV7_SENTENCE_IDS = {1914, 1771, 1669}
ALLOWED_SAMPLE_KEYS = {
    "language",
    "id",
    "original_file",
    "sha256_pcm_wav",
    "sha256_reference_utf8",
    "duration_s",
}


def test_committed_provenance_is_the_aligned_sav7_set() -> None:
    provenance = load_provenance()
    assert provenance.dataset == "google/fleurs"
    assert provenance.split == "test"
    assert "CC BY 4.0" in provenance.license
    assert set(provenance.aligned_ids) == SAV7_SENTENCE_IDS
    assert {(sample.language, sample.id) for sample in provenance.samples} == {
        (language, sentence_id) for language in SAV7_LANGUAGES for sentence_id in SAV7_SENTENCE_IDS
    }


def test_committed_provenance_holds_hashes_but_no_sentences() -> None:
    samples = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))["samples"]
    assert all(set(sample) == ALLOWED_SAMPLE_KEYS for sample in samples)


def test_verified_clips_load_with_their_references(tmp_path: Path) -> None:
    provenance = write_clip_set(tmp_path)
    samples = load_samples(tmp_path, provenance)
    assert [(sample.language, sample.sentence_id) for sample in samples] == [
        (language, sentence_id) for language in LANGUAGES for sentence_id in SENTENCE_IDS
    ]
    assert samples[0].reference == reference_for(LANGUAGES[0], SENTENCE_IDS[0])


def test_changed_clip_is_rejected(tmp_path: Path) -> None:
    provenance = write_clip_set(tmp_path)
    (tmp_path / "tr_tr-22.wav").write_bytes(wav_bytes(seconds=0.5))
    with pytest.raises(ClipSetError, match=r"tr_tr-22\.wav differs"):
        load_samples(tmp_path, provenance)


def test_missing_clip_is_rejected(tmp_path: Path) -> None:
    provenance = write_clip_set(tmp_path)
    (tmp_path / "ja_jp-11.wav").unlink()
    with pytest.raises(ClipSetError, match="cannot read the clip"):
        load_samples(tmp_path, provenance)


def test_changed_reference_is_rejected(tmp_path: Path) -> None:
    provenance = write_clip_set(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["samples"][0]["reference_raw"] += " edited"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ClipSetError, match="reference sentence"):
        load_samples(tmp_path, provenance)


def test_other_dataset_revision_is_rejected(tmp_path: Path) -> None:
    provenance = write_clip_set(tmp_path)
    other_revision = provenance.model_copy(update={"revision": "1" * 40})
    with pytest.raises(ClipSetError, match="revision"):
        load_samples(tmp_path, other_revision)


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    provenance = write_clip_set(tmp_path / "clips")
    with pytest.raises(ClipSetError, match="manifest"):
        load_samples(tmp_path / "empty", provenance)


def test_provenance_with_a_sentence_missing_in_one_language_is_rejected(tmp_path: Path) -> None:
    provenance = write_clip_set(tmp_path)
    unaligned = provenance.model_copy(update={"samples": provenance.samples[:-1]})
    path = tmp_path / "provenance.json"
    path.write_text(unaligned.model_dump_json(), encoding="utf-8")
    with pytest.raises(ClipSetError, match="shared sentence ids"):
        load_provenance(path)

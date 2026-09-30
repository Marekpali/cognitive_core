"""T10: classifier sources are pinned to CLASSIFIER_SET_VERSION (ADR 7.2)."""

import hashlib
from pathlib import Path

from src.classifiers import (
    CLASSIFIER_SET_HISTORY, CLASSIFIER_SET_VERSION, CLASSIFIER_SOURCE_FILES,
)

CLASSIFIERS_DIR = Path(__file__).resolve().parent.parent / "src" / "classifiers"


def classifier_sources_sha256(directory: Path = CLASSIFIERS_DIR) -> str:
    """name NUL content NUL for each file, line endings normalised to LF so
    the Windows working tree and the deployed (LF) files hash the same."""
    digest = hashlib.sha256()
    for name in CLASSIFIER_SOURCE_FILES:
        content = (directory / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def test_classifier_sources_match_the_current_version():
    assert classifier_sources_sha256() == CLASSIFIER_SET_HISTORY[CLASSIFIER_SET_VERSION], (
        "Classifier source changed: add a NEW entry to CLASSIFIER_SET_HISTORY "
        "and bump CLASSIFIER_SET_VERSION (src/classifiers/__init__.py)."
    )


def test_every_classifier_file_is_covered():
    on_disk = {p.name for p in CLASSIFIERS_DIR.glob("*.py")} - {"__init__.py"}
    assert on_disk == set(CLASSIFIER_SOURCE_FILES)


def test_history_versions_have_distinct_hashes():
    assert len(set(CLASSIFIER_SET_HISTORY.values())) == len(CLASSIFIER_SET_HISTORY)


def test_editing_a_classifier_breaks_the_pin(tmp_path):
    for name in CLASSIFIER_SOURCE_FILES:
        (tmp_path / name).write_bytes((CLASSIFIERS_DIR / name).read_bytes())
    (tmp_path / "motion.py").write_bytes(
        (tmp_path / "motion.py").read_bytes() + b"\n# tweak\n")
    assert classifier_sources_sha256(tmp_path) != CLASSIFIER_SET_HISTORY[CLASSIFIER_SET_VERSION]

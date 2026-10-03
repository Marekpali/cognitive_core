"""STEP 7a `precedent-report`: three sections never merged, fractions,
maturity with n, and a review queue that stays blind."""

import inspect
import sqlite3

import pytest

from src import main as main_module
from src.learning.precedent import AMBIGUOUS, SUGGESTION
from src.precedent_report import build_report, match_class, ratio, render
from src.storage import Storage
from tests.precedent_helpers import decided, pending, start_layer, sweep


MOTION = "motion_sensor"


def _suggestion(outcome, category=None):
    return {"result": SUGGESTION, "suggested_outcome": outcome, "suggested_category": category}


@pytest.mark.parametrize("annotation, decision, corrected, expected", [
    (_suggestion("approved"), "approved", None, "matched"),
    (_suggestion("corrected", "occupancy"), "corrected", "occupancy", "matched"),
    (_suggestion("corrected", "occupancy"), "corrected", "environmental", "direction-only"),
    (_suggestion("rejected"), "corrected", "occupancy", "direction-only"),
    (_suggestion("corrected", "occupancy"), "rejected", None, "direction-only"),
    (_suggestion("approved"), "rejected", None, "mismatched"),
    (_suggestion("rejected"), "approved", None, "mismatched"),
    ({"result": AMBIGUOUS, "suggested_outcome": None, "suggested_category": None},
     "approved", None, "ambiguous"),
])
def test_match_class(annotation, decision, corrected, expected):
    assert match_class(annotation, decision, corrected) == expected


@pytest.mark.parametrize("n, d, text", [(0, 0, "0/0"), (1, 2, "1/2"), (4, 4, "4/4"),
                                        (3, 5, "3/5 (60%)"), (2, 6, "2/6 (33%)")])
def test_ratio_is_a_fraction_with_a_percentage_only_from_five(n, d, text):
    assert ratio(n, d) == text


def test_empty_database(tmp_path):
    storage = Storage(tmp_path / "core.db")
    report = build_report(storage.connect())
    assert report["layer_started_at"] is None
    assert report["annotation_failed"] == {"observation": 0, "bootstrap_pending": 0}
    text = render(report)
    assert "never (layer has not run)" in text and "bootstrap cohort's evidence" not in text
    storage.connection.close()


@pytest.fixture
def mixed(tmp_path):
    """dev0, dev00 decided and dev1 pending before the layer; then the layer
    starts, dev1 is resolved (bootstrap cohort) and a sweep observes new
    devices (normal cohort), one of which is then resolved."""
    path = tmp_path / "core.db"
    storage = Storage(path)
    for device in ("dev0", "dev00"):
        decided(storage, device, classifier=MOTION, category=MOTION)
    legacy = pending(storage, "dev1", classifier=MOTION, category=MOTION)
    storage.connection.close()
    storage = start_layer(path)
    storage.resolve_review_case(*legacy, "corrected", corrected_category="occupancy")
    sweep(storage)
    yield storage
    storage.connection.close()


def test_cohorts_are_reported_separately(mixed):
    report = build_report(mixed.connect())
    normal, bootstrap = (report["class_pattern"][k] for k in ("observation", "bootstrap_pending"))
    assert (bootstrap["generated"], bootstrap["verifiable"], bootstrap["mismatched"]) == (1, 1, 1)
    assert bootstrap["not_yet_verifiable"] == 0
    assert normal["generated"] >= 1 and normal["verifiable"] == 0
    assert normal["not_yet_verifiable"] == normal["cases"]
    assert report["device_precedent"]["generated"] == 0

    case = mixed.connect().execute(
        "SELECT rc.id, rc.last_observation_id FROM review_cases rc "
        "JOIN precedent_annotations a ON a.observation_id = rc.last_observation_id "
        "WHERE rc.status = 'pending' AND a.result = 'suggestion' LIMIT 1").fetchone()
    mixed.resolve_review_case(case[0], case[1], "approved")
    after = build_report(mixed.connect())["class_pattern"]
    assert after["observation"]["verifiable"] == 1
    assert after["bootstrap_pending"] == bootstrap          # the other cohort did not move


def test_render_shows_sections_fractions_and_maturity_with_n(mixed):
    text = render(build_report(mixed.connect()))
    for heading in ("DEVICE PRECEDENT - recall, counted, never scored",
                    "CLASS PATTERN - normal cohort (annotated when observed)",
                    "CLASS PATTERN - bootstrap cohort (annotated at decision time)",
                    "annotation_failed: observation 0, bootstrap_pending 0",
                    "never merged with the normal cohort"):
        assert heading in text
    assert "-> approved (evidence 2/2, corrections 0/2, insufficient n=2) " \
           "-> corrected:occupancy -> mismatched" in text
    assert "accuracy" not in text.lower() and "%" not in text     # denominators below 5


def test_failures_are_counted_per_source(mixed, monkeypatch):
    from src import precedent_store

    def boom(*args, **kwargs):
        raise RuntimeError("broken")
    monkeypatch.setattr(precedent_store, "insert_annotation", boom)
    case, obs = pending(mixed, "dev50", classifier=MOTION, category=MOTION)
    conn = mixed.connect()
    from src import precedent_annotator
    precedent_annotator.annotate_in_transaction(conn.cursor(), obs)
    conn.commit()
    assert build_report(conn)["annotation_failed"] == {"observation": 1, "bootstrap_pending": 0}


def test_report_is_read_only(mixed):
    path = mixed.db_path
    mixed.connection.commit()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert "PRECEDENT REPORT" in render(build_report(conn))
    conn.close()


def test_cli_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "core.db"))
    monkeypatch.setattr(main_module, "Storage", lambda: Storage(tmp_path / "core.db"))
    main_module.show_precedent_report()
    assert "PRECEDENT REPORT (STEP 7a - shadow: annotations have no effect)" in capsys.readouterr().out


# --- blind review ---------------------------------------------------------------

def test_review_queue_is_blind(mixed):
    """The reviewer never sees a suggestion: the queue has the same columns
    with or without annotations, and the review code never reads the
    precedent tables."""
    queue = mixed.get_pending_review_cases()
    assert queue and mixed.connect().execute(
        "SELECT COUNT(*) FROM precedent_annotations").fetchone()[0] > 0
    for case in queue:
        assert not [k for k in case if "precedent" in k or "suggest" in k or "maturity" in k]
    for function in (Storage.get_pending_review_cases, Storage.get_pending_reviews,
                     main_module.show_review_queue, main_module.show_review_count):
        assert "precedent" not in inspect.getsource(function).lower()
    from src import ha_sensor
    assert "precedent" not in inspect.getsource(ha_sensor).lower()

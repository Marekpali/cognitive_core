"""Classifier set identity (STEP 7P, docs/STEP7_OBSERVATION_SOURCES.md 7.2).

CLASSIFIER_SET_VERSION is part of every input fingerprint: bumping it makes
every device's next sweep produce a new input and fresh observations.

CLASSIFIER_SET_HISTORY pins each version to the SHA256 of the classifier
sources (see tests/test_classifier_version.py for the exact hashing). A
classifier change without a new version entry fails the test suite; add a
new entry instead of editing an existing one.
"""

CLASSIFIER_SOURCE_FILES = ("energy.py", "environmental.py", "motion.py")

CLASSIFIER_SET_HISTORY = {
    "1": "b52f83d901e1e5e044cbcdb45b4c634e5463420dc73820c562255b153cb81a45",
}

CLASSIFIER_SET_VERSION = "1"

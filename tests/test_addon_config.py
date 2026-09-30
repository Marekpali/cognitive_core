"""haos_addon/config.yaml exposes observation_mode consistently with src/options.py."""

import re
from pathlib import Path

from src.options import DEFAULT_OBSERVATION_MODE, OBSERVATION_MODES

CONFIG = (Path(__file__).resolve().parent.parent / "haos_addon" / "config.yaml").read_text(
    encoding="utf-8")


def _block(name: str) -> str:
    match = re.search(rf"^{name}:\n((?:  .*\n)+)", CONFIG, re.MULTILINE)
    assert match, f"no {name}: block"
    return match.group(1)


def test_default_option_is_shadow():
    assert re.search(r"^  observation_mode: (\S+)$", _block("options"), re.MULTILINE).group(1) \
        == DEFAULT_OBSERVATION_MODE == "shadow"


def test_schema_allows_exactly_the_code_modes():
    values = re.search(r"^  observation_mode: list\(([^)]*)\)$", _block("schema"),
                       re.MULTILINE).group(1).split("|")
    assert tuple(values) == OBSERVATION_MODES


def test_config_is_lf_only():
    raw = (Path(__file__).resolve().parent.parent / "haos_addon" / "config.yaml").read_bytes()
    assert b"\r" not in raw


def test_addon_requirements_are_fully_pinned():
    lines = [line.strip() for line in (
        Path(__file__).resolve().parent.parent / "haos_addon" / "requirements.txt"
    ).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.\-]+", l) for l in lines)
    assert "aiohttp==3.14.3" in lines

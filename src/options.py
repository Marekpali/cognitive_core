"""Add-on options (STEP 7P).

`observation_mode` comes from the Supervisor-written /data/options.json,
then the OBSERVATION_MODE environment variable, and defaults to "shadow".
Anything unreadable or unknown falls back to "shadow": activation must be
an explicit, valid choice (docs/STEP7_OBSERVATION_SOURCES.md section 8).
"""

import json
import os
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")
DEFAULT_OBSERVATION_MODE = "shadow"
OBSERVATION_MODES = ("shadow", "active")


def read_observation_mode(options_path: Path = OPTIONS_PATH) -> str:
    raw = None
    source = "default"
    try:
        if options_path.exists():
            raw = json.loads(options_path.read_text(encoding="utf-8")).get("observation_mode")
            source = str(options_path)
    except (OSError, ValueError, AttributeError) as exc:
        print(f"[OPTIONS] WARNING: cannot read {options_path}: {exc!r}")
    if raw is None and os.getenv("OBSERVATION_MODE"):
        raw, source = os.getenv("OBSERVATION_MODE"), "OBSERVATION_MODE"

    if raw is None:
        mode = DEFAULT_OBSERVATION_MODE
    elif raw in OBSERVATION_MODES:
        mode = raw
    else:
        print(f"[OPTIONS] WARNING: unknown observation_mode {raw!r} from {source}; "
              f"using {DEFAULT_OBSERVATION_MODE!r}")
        mode, source = DEFAULT_OBSERVATION_MODE, "fallback"
    print(f"[OPTIONS] observation_mode={mode} (source: {source})")
    return mode

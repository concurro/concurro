"""Concurro service: drives the engine's controller tick under fenced ownership."""

import sys
from pathlib import Path

# The engine is a flat module directory, importable on its own; the service builds on it.
ENGINE = Path(__file__).resolve().parents[1] / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

"""Shared fixtures: run engine tests as though inside the example target checkout."""

import json
import os
from pathlib import Path
import sys

ENGINE = Path(__file__).resolve().parents[1]
ROOT = ENGINE.parent
TARGET = ROOT / "examples/stacks-core"
CONFIG = json.loads((TARGET / ".concurro/config.json").read_text())
sys.path.insert(0, str(ENGINE))

_previous = []


def setUpModule():
    """The controller reads the target charter from its working directory, as in Actions."""
    _previous.append(os.getcwd())
    os.chdir(TARGET)


def tearDownModule():
    """Restore the caller's working directory."""
    os.chdir(_previous.pop())

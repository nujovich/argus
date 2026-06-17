"""Test setup — put the plugin dir on sys.path and isolate HERMES_HOME."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


@pytest.fixture
def tmp_hermes_home(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="argus_test_") as d:
        monkeypatch.setenv("HERMES_HOME", d)
        # Drop any cached per-thread DB connection so the next call reopens
        # against the fresh HERMES_HOME.
        import db
        db.reset_connection_for_tests()
        yield Path(d)
        db.reset_connection_for_tests()

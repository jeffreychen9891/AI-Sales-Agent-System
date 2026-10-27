"""Shared fixtures for the contract suite.

`load_runner` lived in two test modules verbatim -- same name, same path, same
`sys.modules` key, so the behaviour was already shared and only the source was
duplicated (independent review 2026-09-03, P3-5).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_runner() -> Any:
    if "replay_regression" in sys.modules:
        return sys.modules["replay_regression"]
    path = ROOT / "scripts" / "replay_regression.py"
    spec = importlib.util.spec_from_file_location("replay_regression", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_regression"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def runner() -> Any:
    """The replay runner, imported by path because `scripts/` is not a package."""

    return _load_runner()

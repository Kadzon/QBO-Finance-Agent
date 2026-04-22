"""Shared test configuration.

Forces the whole suite into a hermetic environment: no ``.env`` file is
loaded, so tests see only what ``monkeypatch`` injects.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from qbagent import config as config_module


@pytest.fixture(autouse=True)
def _isolate_settings() -> Iterator[None]:
    original = config_module.Settings.model_config.get("env_file")
    config_module.Settings.model_config["env_file"] = None  # type: ignore[typeddict-item]
    config_module._SETTINGS = None
    try:
        yield
    finally:
        config_module._SETTINGS = None
        config_module.Settings.model_config["env_file"] = original  # type: ignore[typeddict-item]

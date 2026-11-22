"""No test reads this machine's own NouGen mesh: every test sees a fresh runtime
home and no NOUGEN_* settings from the box running the suite."""
import os

import pytest


@pytest.fixture(autouse=True)
def _fresh_mesh(tmp_path_factory, monkeypatch):
    for key in list(os.environ):
        if key.startswith("NOUGEN_") or key == "NOUGENTRACKER_EXPECTED_MACHINES":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NOUGEN_ROOT", str(tmp_path_factory.mktemp("nougen-root")))

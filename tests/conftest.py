"""
conftest.py — shared fixtures and path setup for the stateful-loop test suite.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Ensure both src/ and scripts/ are importable from any test file.
_ROOT = os.path.dirname(os.path.dirname(__file__))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True, scope="session")
def _isolate_postmortem_dir():
    """workers.archivist._writable_postmortem_dir() defaults to the real repo docs/post-mortems/
    when OMNI_POSTMORTEM_DIR is unset. Some tests call write_incident_postmortem()/
    _archive_postmortem() for real (without patching the dir), which overwrites real incident
    post-mortem timestamps on every test run. Redirect the whole test session to a scratch dir
    so no test run mutates repo docs as a side effect."""
    scratch = tempfile.mkdtemp(prefix="omni-test-postmortems-")
    prev = os.environ.get("OMNI_POSTMORTEM_DIR")
    os.environ["OMNI_POSTMORTEM_DIR"] = scratch
    yield
    if prev is None:
        os.environ.pop("OMNI_POSTMORTEM_DIR", None)
    else:
        os.environ["OMNI_POSTMORTEM_DIR"] = prev

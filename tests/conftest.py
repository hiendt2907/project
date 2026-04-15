"""
conftest.py — shared fixtures and path setup for the stateful-loop test suite.
"""
from __future__ import annotations

import os
import sys

# Ensure both src/ and scripts/ are importable from any test file.
_ROOT = os.path.dirname(os.path.dirname(__file__))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

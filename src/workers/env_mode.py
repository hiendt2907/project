"""Environment mode helpers for governance behavior (prod/dev).

Moved to pkg/env_mode.py (WS1, dependency-direction fix — pkg/ callers must
not import workers/). Re-exported here unchanged so existing worker callers
are unaffected.
"""

from __future__ import annotations

from pkg.env_mode import env_mode, is_dev_mode, is_prod_mode, namespace_allowed, parse_allowed_namespaces

__all__ = [
    "env_mode",
    "is_dev_mode",
    "is_prod_mode",
    "namespace_allowed",
    "parse_allowed_namespaces",
]

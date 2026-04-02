"""query_points payload_filters SQL composition — key whitelist regex."""

from __future__ import annotations

import re


def test_filter_key_pattern() -> None:
    assert re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", "layer")
    assert re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", "doc_version")

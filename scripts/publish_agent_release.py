"""Publish the expected agent release manifest (Sprint NV-SRE IT-2).

Computes version + canonical bundle sha256 from ``src/remote_agent`` in the
repo (same algorithm the agent uses to self-hash at startup) and prints the
manifest JSON. Pipe it into Redis to publish:

    make publish-agent-release
    # ≙ kubectl -n multi-agent exec -i redis-0 -- redis-cli -x SET omni:agent:release_manifest

The gateway reads ``omni:agent:release_manifest`` and marks every registered
agent current | drifted | unknown on /webhook/agent/versions.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from remote_agent.bundle_hash import compute_bundle_hash  # noqa: E402


def build_manifest() -> dict:
    package_root = REPO_ROOT / "src" / "remote_agent"
    version = (package_root / "VERSION").read_text().strip()
    return {
        "version": version,
        "bundle_sha256": compute_bundle_hash(package_root),
        "published_at": int(time.time()),
    }


if __name__ == "__main__":
    print(json.dumps(build_manifest(), separators=(",", ":")))

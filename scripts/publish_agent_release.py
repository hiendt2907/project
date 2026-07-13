"""Publish the expected agent release manifest + bundle (Sprint NV-SRE IT-2/IT-5).

Computes version + canonical bundle sha256 from ``src/remote_agent`` and
``src/aoip`` in the repo (same algorithm the agent uses to self-hash) and
prints the manifest JSON. IT-5: also builds the release tarball (both
packages, deterministic tarfile — no macOS AppleDouble) so agents can
self-update via the durable command channel:

    make publish-agent-release
    # ≙ manifest  → redis SET omni:agent:release_manifest
    #   bundle    → redis SET omni:agent:release_bundle   (base64 tar.gz)

The gateway reads the manifest for drift classification on
/webhook/agent/versions and streams the bundle at /webhook/agent/release/bundle.

Usage:
    publish_agent_release.py                      # print manifest (includes tar sha256)
    publish_agent_release.py --bundle-b64 PATH    # also write base64 tarball to PATH
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import sys
import tarfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from remote_agent.bundle_hash import compute_bundle_hash  # noqa: E402

_PACKAGES = ("remote_agent", "aoip")  # phải khớp aoip.agent.updater._BUNDLE_PACKAGES


def build_release_tar() -> bytes:
    """Tarball 2 package, deterministic (mtime=0, uid/gid=0, sort tên) — sha256
    ổn định giữa các lần build cùng nội dung."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for pkg in _PACKAGES:
            root = REPO_ROOT / "src" / pkg
            for path in sorted(root.rglob("*")):
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                if not path.is_file():
                    continue
                info = tarfile.TarInfo(name=f"{pkg}/{path.relative_to(root)}")
                data = path.read_bytes()
                info.size = len(data)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    # gzip với mtime=0 để nén cũng deterministic
    return gzip.compress(buf.getvalue(), mtime=0)


def build_manifest(release_tar: bytes) -> dict:
    package_root = REPO_ROOT / "src" / "remote_agent"
    version = (package_root / "VERSION").read_text().strip()
    return {
        "version": version,
        "bundle_sha256": compute_bundle_hash(package_root),
        # IT-4: employee ship thêm package aoip — publish hash để gateway xét drift
        "aoip_bundle_sha256": compute_bundle_hash(REPO_ROOT / "src" / "aoip"),
        # IT-5: sha256 của tarball release — agent verify TRƯỚC khi cài
        "release_tar_sha256": hashlib.sha256(release_tar).hexdigest(),
        "published_at": int(time.time()),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--bundle-b64", default="", help="write base64 tarball to this path")
    args = p.parse_args()

    tar_bytes = build_release_tar()
    if args.bundle_b64:
        Path(args.bundle_b64).write_bytes(base64.b64encode(tar_bytes))
    print(json.dumps(build_manifest(tar_bytes), separators=(",", ":")))

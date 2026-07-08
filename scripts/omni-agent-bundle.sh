#!/usr/bin/env bash
# Build omni-agent tarball — self-contained installer package.
#
# Usage:
#   bash scripts/omni-agent-bundle.sh [--offline] [--version X.Y.Z]
#
# Output:
#   dist/omni-agent-<version>.tar.gz
#
# Tarball layout:
#   omni-agent-<version>/
#   ├── install.sh           ← run this on the target server
#   ├── omni-agent.service   ← systemd unit (install.sh uses it)
#   ├── aoip-agent.service   ← systemd unit employee (IT-4, deploy thủ công pilot)
#   ├── requirements.txt     ← Python deps list
#   ├── wheels/              ← pre-downloaded wheels (only with --offline)
#   ├── aoip/                ← Python package AOIP (employee + durable daemon)
#   └── remote_agent/        ← Python package
#       ├── __init__.py
#       ├── agent.py
#       ├── settings.py
#       ├── evidence.py
#       ├── emitter.py
#       └── collectors/
#           ├── system.py
#           ├── logs.py
#           └── k8s.py

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="1.0.0"
OFFLINE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)       OFFLINE=true; shift ;;
    --version)       VERSION="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

BUNDLE_NAME="omni-agent-${VERSION}"
DIST_DIR="$REPO_ROOT/dist"
STAGE_DIR="$DIST_DIR/$BUNDLE_NAME"
TARBALL="$DIST_DIR/${BUNDLE_NAME}.tar.gz"

echo "Building omni-agent bundle v${VERSION}..."
echo "  Offline wheels: $OFFLINE"
echo "  Output: $TARBALL"

# Clean stage
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

# Copy agent source (exclude __pycache__ and .pyc)
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
  "$REPO_ROOT/src/remote_agent" "$STAGE_DIR/"

# IT-4: ship FULL package aoip (employee entrypoint + daemon) — phải nguyên vẹn
# để aoip_self_bundle_hash() trên VM khớp aoip_bundle_sha256 của publisher.
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
  "$REPO_ROOT/src/aoip" "$STAGE_DIR/"

# Copy installer files
cp "$REPO_ROOT/scripts/omni-agent-install.sh" "$STAGE_DIR/install.sh"
cp "$REPO_ROOT/scripts/omni-agent.service"     "$STAGE_DIR/omni-agent.service"
cp "$REPO_ROOT/scripts/aoip-agent.service"     "$STAGE_DIR/aoip-agent.service"
cp "$REPO_ROOT/requirements-agent.txt"         "$STAGE_DIR/requirements.txt"
chmod +x "$STAGE_DIR/install.sh"

# Bundle .deb packages cho offline install (Ubuntu/Debian)
echo "  Bundling .deb packages..."
mkdir -p "$STAGE_DIR/debs"
DEB_SOURCES=(
  "http://archive.ubuntu.com/ubuntu/pool/universe/p/python3.8/python3.8-venv_3.8.10-0ubuntu1~20.04.18_amd64.deb"
  "http://archive.ubuntu.com/ubuntu/pool/main/p/python3-stdlib-extensions/python3-distutils_3.8.10-0ubuntu1~20.04_all.deb"
)
for url in "${DEB_SOURCES[@]}"; do
  fname=$(basename "$url")
  # Dùng cache local nếu có, tránh download lại
  if [[ -f "/tmp/omni-debs/$fname" ]]; then
    cp "/tmp/omni-debs/$fname" "$STAGE_DIR/debs/"
    echo "    (cached) $fname"
  else
    echo "    Downloading $fname..."
    curl -fsSL -o "$STAGE_DIR/debs/$fname" "$url" || echo "    WARNING: failed to download $fname"
  fi
done
echo "  Debs bundled: $(ls "$STAGE_DIR/debs" | wc -l | tr -d ' ') packages"

# Optional: pre-download wheels for offline install
if $OFFLINE; then
  echo "  Downloading wheels for offline install..."
  mkdir -p "$STAGE_DIR/wheels"
  python3 -m pip download \
    --dest "$STAGE_DIR/wheels" \
    --quiet \
    -r "$REPO_ROOT/requirements-agent.txt"
  echo "  Wheels: $(ls "$STAGE_DIR/wheels" | wc -l | tr -d ' ') packages"
fi

# Write version file at bundle root
cat > "$STAGE_DIR/VERSION" <<EOF
omni-agent ${VERSION}
built: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

# Also write plain version into remote_agent/ so settings.py can read it
echo "$VERSION" > "$STAGE_DIR/remote_agent/VERSION"

# Create tarball (COPYFILE_DISABLE loại bỏ Mac xattr/._* metadata)
cd "$DIST_DIR"
COPYFILE_DISABLE=1 tar -czf "${BUNDLE_NAME}.tar.gz" "$BUNDLE_NAME"
rm -rf "$STAGE_DIR"

SIZE=$(du -sh "$TARBALL" | cut -f1)
echo ""
echo "Done: $TARBALL ($SIZE)"
echo ""
echo "Deploy to target server:"
echo "  scp $TARBALL user@server:/tmp/"
echo "  ssh user@server 'cd /tmp && tar -xzf ${BUNDLE_NAME}.tar.gz && \\"
echo "    sudo bash ${BUNDLE_NAME}/install.sh \\"
echo "      --gateway-url http://omni.yourcompany.com:8080 \\"
echo "      --api-key <KEY>'"

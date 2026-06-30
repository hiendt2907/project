#!/usr/bin/env bash
# AOIP Remote Agent installer — chạy trên BẤT KỲ Linux nào (EPIC 2).
#
#   curl -fsSL https://<omni>/install.sh | sudo bash -s -- --tenant acme --omni https://omni.example.com
#
# DoD: cài được trên Ubuntu/Debian/RHEL/Rocky/Amazon Linux. Agent tự dựng identity,
# đăng ký Omni, giữ heartbeat. Không phụ thuộc OrbStack — OrbStack chỉ là lab để
# test chính script này.
set -euo pipefail

TENANT="${TENANT:-default}"
OMNI_URL="${OMNI_URL:-http://localhost:8090}"
INSTALL_DIR="/opt/aoip-agent"
PY="$(command -v python3 || true)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant) TENANT="$2"; shift 2 ;;
    --omni)   OMNI_URL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "[install] detecting OS..."
if [[ -r /etc/os-release ]]; then . /etc/os-release; echo "[install] $PRETTY_NAME"; fi

if [[ -z "$PY" ]]; then
  echo "[install] python3 missing — installing..."
  if   command -v apt-get >/dev/null; then sudo apt-get update -q && sudo apt-get install -y -q python3;
  elif command -v dnf >/dev/null;     then sudo dnf install -y python3;
  elif command -v yum >/dev/null;     then sudo yum install -y python3;
  else echo "[install] no known package manager" >&2; exit 1; fi
  PY="$(command -v python3)"
fi

echo "[install] staging agent into $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
# Trong deploy thật: tải agent bundle từ Omni. Ở lab: copy cây src hiện tại.
if [[ -d "$(dirname "$0")/../src/aoip" ]]; then
  sudo cp -r "$(dirname "$0")/../src/aoip" "$INSTALL_DIR/"
fi

echo "[install] registering systemd unit"
sudo tee /etc/systemd/system/aoip-agent.service >/dev/null <<EOF
[Unit]
Description=AOIP Remote Agent (tenant=$TENANT)
After=network-online.target
[Service]
Environment=PYTHONPATH=$INSTALL_DIR
ExecStart=$PY -m aoip.agent.main --tenant $TENANT
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now aoip-agent 2>/dev/null || true
echo "[install] done — tenant=$TENANT omni=$OMNI_URL"
echo "[install] status: systemctl status aoip-agent"

#!/usr/bin/env bash
# Omni Remote Agent Installer
# Usage: bash omni-agent-install.sh --gateway-url <URL> --api-key <KEY> [options]
#
# Options:
#   --gateway-url URL     Omni gateway URL (required)
#   --api-key KEY         Agent API key (required)
#   --agent-id ID         Agent identifier (default: hostname)
#   --log-paths PATHS     Comma-separated log paths (default: /var/log/syslog)
#   --namespace NS        K8s namespace filter (default: all)
#   --no-k8s              Disable K8s collector
#   --interval SEC        Collect interval in seconds (default: 60)
#   --dry-run             Print steps without executing
#   --force               Ignore existing install state and re-run all steps
#   --uninstall           Remove omni-agent from this system

set -euo pipefail

# ─── Constants ────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/omni-agent"
CONFIG_DIR="/etc/omni-agent"
DATA_DIR="/var/lib/omni-agent"
STATE_DIR="/var/lib/omni-agent/install-state"
SERVICE_FILE="/etc/systemd/system/omni-agent.service"
AGENT_USER="omni-agent"
PYTHON_MIN="3.8"
VERSION="1.0.0"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[omni-agent]${NC} $*"; }
warn() { echo -e "${YELLOW}[omni-agent]${NC} $*"; }
err()  { echo -e "${RED}[omni-agent]${NC} $*" >&2; }

# ─── Args ─────────────────────────────────────────────────────────────────────
GATEWAY_URL=""
API_KEY=""
AGENT_ID="$(hostname -s)"
LOG_PATHS="/var/log/syslog"
K8S_ENABLED="true"
K8S_NAMESPACE=""
COLLECT_INTERVAL=60
DRY_RUN=false
FORCE=false
UNINSTALL=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gateway-url) GATEWAY_URL="$2"; shift 2 ;;
    --api-key)     API_KEY="$2"; shift 2 ;;
    --agent-id)    AGENT_ID="$2"; shift 2 ;;
    --log-paths)   LOG_PATHS="$2"; shift 2 ;;
    --namespace)   K8S_NAMESPACE="$2"; shift 2 ;;
    --no-k8s)      K8S_ENABLED="false"; shift ;;
    --interval)    COLLECT_INTERVAL="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=true; shift ;;
    --force)       FORCE=true; shift ;;
    --uninstall)   UNINSTALL=true; shift ;;
    *) err "Unknown option: $1"; exit 1 ;;
  esac
done

run() {
  if $DRY_RUN; then echo "[DRY-RUN] $*"; else "$@"; fi
}

# ─── Uninstall ────────────────────────────────────────────────────────────────
if $UNINSTALL; then
  log "Uninstalling omni-agent..."
  run systemctl stop omni-agent 2>/dev/null || true
  run systemctl disable omni-agent 2>/dev/null || true
  run rm -f "$SERVICE_FILE"
  run systemctl daemon-reload
  run rm -rf "$INSTALL_DIR" "$CONFIG_DIR"
  log "Uninstall complete. Data dir $DATA_DIR preserved."
  exit 0
fi

# ─── Validate args ────────────────────────────────────────────────────────────
if [[ -z "$GATEWAY_URL" ]]; then err "Missing --gateway-url"; exit 1; fi
if [[ -z "$API_KEY" ]];     then err "Missing --api-key"; exit 1; fi

# ─── State helpers ────────────────────────────────────────────────────────────
step_done() { [[ -f "$STATE_DIR/$1" ]] && ! $FORCE; }
mark_done() { $DRY_RUN || { mkdir -p "$STATE_DIR"; touch "$STATE_DIR/$1"; }; }

# ─── Steps ────────────────────────────────────────────────────────────────────
log "Omni Remote Agent Installer v${VERSION}"
log "gateway=${GATEWAY_URL} agent_id=${AGENT_ID} k8s=${K8S_ENABLED}"
$DRY_RUN && warn "DRY-RUN mode — no changes will be made"

# Step 1: Preflight
if ! step_done "preflight"; then
  log "[1/7] Preflight checks..."
  if ! command -v python3 &>/dev/null; then
    err "python3 not found. Install Python ${PYTHON_MIN}+ first."; exit 1
  fi
  PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  if python3 -c "import sys; exit(0 if sys.version_info >= (3,8) else 1)" 2>/dev/null; then
    log "  Python ${PY_VER} ✓"
  else
    err "Python ${PYTHON_MIN}+ required. Found ${PY_VER}."; exit 1
  fi
  command -v curl &>/dev/null || { err "curl not found"; exit 1; }
  log "  curl ✓"
  # Gateway connectivity check
  if ! $DRY_RUN; then
    if curl -4sf --max-time 5 "${GATEWAY_URL}/healthz" &>/dev/null; then
      log "  Gateway reachable ✓"
    else
      warn "  Cannot reach ${GATEWAY_URL}/healthz — continuing anyway (offline install)"
    fi
  fi
  mark_done "preflight"
fi

# Step 2: Create user
if ! step_done "user"; then
  log "[2/7] Creating system user ${AGENT_USER}..."
  if id "$AGENT_USER" &>/dev/null; then
    log "  User already exists ✓"
  else
    run useradd --system --no-create-home --shell /sbin/nologin "$AGENT_USER"
  fi
  mark_done "user"
fi

# Step 3: Install package
if ! step_done "package"; then
  log "[3/7] Installing omni-agent package..."
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  # Detect install source:
  #   tarball context: install.sh sits next to remote_agent/ and requirements.txt
  #   repo context:    install.sh is in scripts/, code is in ../src/remote_agent/
  if [[ -d "$SCRIPT_DIR/remote_agent" ]]; then
    AGENT_SRC="$SCRIPT_DIR"                         # tarball: code next to script
  elif [[ -d "$SCRIPT_DIR/../src/remote_agent" ]]; then
    AGENT_SRC="$(cd "$SCRIPT_DIR/.." && pwd)/src"   # dev repo
  else
    err "Cannot find remote_agent/ package. Bundle may be corrupted."; exit 1
  fi
  log "  Source: $AGENT_SRC"

  # Đảm bảo python3-venv có sẵn — thử tạo venv test trước
  if ! python3 -m venv /tmp/omni-venv-test &>/dev/null; then
    rm -rf /tmp/omni-venv-test
    warn "  python3-venv thiếu — đang cài từ bundle..."
    DEB_DIR="$SCRIPT_DIR/debs"
    if [[ -d "$DEB_DIR" ]] && ls "$DEB_DIR"/*.deb &>/dev/null; then
      # Cài từ .deb bundled — không cần apt/internet
      run dpkg -i "$DEB_DIR"/*.deb 2>/dev/null || true
    elif command -v apt-get &>/dev/null; then
      warn "  Thử apt-get (cần network)..."
      apt-get install -y -qq \
        -o Acquire::https::Verify-Peer=false \
        -o Acquire::https::Verify-Host=false \
        python3-venv 2>/dev/null || true
    fi
    # Kiểm tra lại sau khi cài
    python3 -m venv /tmp/omni-venv-test &>/dev/null \
      || { err "Vẫn không tạo được venv. Cài thủ công: dpkg -i debs/*.deb"; exit 1; }
  fi
  rm -rf /tmp/omni-venv-test

  run mkdir -p "$INSTALL_DIR"
  run python3 -m venv "$INSTALL_DIR/venv"
  run "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip

  # Install Python deps (prefer offline wheel dir if bundled)
  if [[ -d "$SCRIPT_DIR/wheels" ]]; then
    log "  Installing deps from bundled wheels (offline)..."
    run "$INSTALL_DIR/venv/bin/pip" install --quiet --no-index \
      --find-links="$SCRIPT_DIR/wheels" \
      -r "$SCRIPT_DIR/requirements.txt"
  elif [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
    log "  Installing deps from requirements.txt..."
    run "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
  else
    run "$INSTALL_DIR/venv/bin/pip" install --quiet \
      "httpx>=0.27.0" "psutil>=5.9.0" "aiofiles>=23.0.0" "kubernetes-asyncio>=30.0.0"
  fi

  # Copy agent package into venv site-packages
  SITE_PKG=$("$INSTALL_DIR/venv/bin/python3" -c \
    "import sysconfig; print(sysconfig.get_paths()['purelib'])")
  run cp -r "$AGENT_SRC/remote_agent" "$SITE_PKG/"
  log "  Installed remote_agent → $SITE_PKG/remote_agent ✓"

  # pkg.diagnostics + pkg.domain: validator dùng chung với gateway. Không có nó,
  # command_executor import lỗi ngay lúc khởi động.
  if [[ -d "$AGENT_SRC/pkg" ]]; then
    run cp -r "$AGENT_SRC/pkg" "$SITE_PKG/"
    log "  Installed pkg (diagnostics/domain) → $SITE_PKG/pkg ✓"
  fi

  # Catalogue lệnh chẩn đoán. Layout site-packages KHÔNG giống repo, nên đường dẫn
  # mặc định của loader (<root>/config/…) không đúng ở đây — phải trỏ tường minh qua
  # OMNI_DIAG_CATALOG_FILE trong config.env (bước 4).
  CATALOG_SRC=""
  for cand in "$SCRIPT_DIR/config/diagnostic_commands.json" \
              "$SCRIPT_DIR/config/diagnostic_commands.yaml" \
              "$SCRIPT_DIR/../config/diagnostic_commands.yaml"; do
    [[ -f "$cand" ]] && { CATALOG_SRC="$cand"; break; }
  done
  if [[ -n "$CATALOG_SRC" ]]; then
    run mkdir -p "$INSTALL_DIR/config"
    run cp "$CATALOG_SRC" "$INSTALL_DIR/config/$(basename "$CATALOG_SRC")"
    CATALOG_DST="$INSTALL_DIR/config/$(basename "$CATALOG_SRC")"
    log "  Installed diagnostic catalogue → $CATALOG_DST ✓"
  else
    warn "  KHÔNG tìm thấy catalogue lệnh chẩn đoán — agent sẽ từ chối MỌI lệnh (fail-closed)"
  fi

  mark_done "package"
fi

# Step 4: Write config
if ! step_done "config"; then
  log "[4/7] Writing config to ${CONFIG_DIR}/config.env..."
  run mkdir -p "$CONFIG_DIR"
  run chmod 700 "$CONFIG_DIR"
  if ! $DRY_RUN; then
    cat > "$CONFIG_DIR/config.env" <<EOF
OMNI_AGENT_GATEWAY_URL=${GATEWAY_URL}
OMNI_AGENT_API_KEY=${API_KEY}
OMNI_AGENT_ID=${AGENT_ID}
OMNI_AGENT_HOSTNAME=$(hostname -f 2>/dev/null || hostname)
OMNI_AGENT_COLLECT_INTERVAL=${COLLECT_INTERVAL}
OMNI_AGENT_LOG_PATHS=${LOG_PATHS}
OMNI_AGENT_K8S_ENABLED=${K8S_ENABLED}
OMNI_AGENT_NAMESPACE=${K8S_NAMESPACE}
OMNI_DIAG_CATALOG_FILE=${CATALOG_DST:-$(ls "$INSTALL_DIR"/config/diagnostic_commands.* 2>/dev/null | head -1)}
EOF
    chmod 600 "$CONFIG_DIR/config.env"
    chown -R "$AGENT_USER:$AGENT_USER" "$CONFIG_DIR"
  else
    echo "[DRY-RUN] Would write config.env with GATEWAY_URL, API_KEY, etc."
  fi
  mark_done "config"
fi

# Step 5: Create data dir
if ! step_done "datadir"; then
  log "[5/7] Setting up data directory..."
  run mkdir -p "$DATA_DIR"
  run chown -R "$AGENT_USER:$AGENT_USER" "$DATA_DIR"
  mark_done "datadir"
fi

# Step 6: Install systemd service
if ! step_done "service"; then
  log "[6/7] Installing systemd service..."
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$SCRIPT_DIR/omni-agent.service" ]]; then
    run cp "$SCRIPT_DIR/omni-agent.service" "$SERVICE_FILE"
  else
    if ! $DRY_RUN; then
      cat > "$SERVICE_FILE" <<'SVCEOF'
[Unit]
Description=Omni Remote Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=omni-agent
Group=omni-agent
EnvironmentFile=/etc/omni-agent/config.env
ExecStart=/opt/omni-agent/venv/bin/python -m remote_agent.agent
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/omni-agent
ReadOnlyPaths=/var/log
StandardOutput=journal
StandardError=journal
SyslogIdentifier=omni-agent

[Install]
WantedBy=multi-user.target
SVCEOF
    fi
  fi

  # Ensure ExecStart points to our venv python
  if ! $DRY_RUN; then
    sed -i "s|ExecStart=.*|ExecStart=/opt/omni-agent/venv/bin/python -m remote_agent.agent|" "$SERVICE_FILE"
  fi

  run systemctl daemon-reload
  run systemctl enable omni-agent
  mark_done "service"
fi

# Step 7: Start + post-check
if ! step_done "start"; then
  log "[7/7] Starting omni-agent service..."
  run systemctl start omni-agent

  if ! $DRY_RUN; then
    sleep 5
    if systemctl is-active --quiet omni-agent; then
      log "  Service is active ✓"
    else
      err "Service failed to start. Check: journalctl -u omni-agent -n 50"
      exit 1
    fi

    # Verify registration reached gateway
    log "  Checking agent appears in Omni registry..."
    sleep 3
    REG_STATUS=$(curl -4sf --max-time 5 \
      -H "Authorization: Bearer ${API_KEY}" \
      "${GATEWAY_URL}/agents/remote" 2>/dev/null \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('online',0))" 2>/dev/null || echo "?")
    log "  Remote agents online: ${REG_STATUS}"
  fi
  mark_done "start"
fi

log ""
log "Installation complete!"
log "  Status:   systemctl status omni-agent"
log "  Logs:     journalctl -u omni-agent -f"
log "  Config:   ${CONFIG_DIR}/config.env"
log "  Registry: curl -H 'Authorization: Bearer <key>' ${GATEWAY_URL}/agents/remote"

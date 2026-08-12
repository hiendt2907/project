#!/usr/bin/env bash
# Gate 0 hardening migration — chuyển aoip-agent.service ĐANG CHẠY từ root sang
# non-root (docs/audit/SRE_READINESS_2026-08.md F-005/B7). KHÔNG phải fresh
# install (xem omni-agent-install.sh cho việc đó, mục đích khác: bundle
# remote_agent.agent độc lập, chưa dùng cho fleet thật). Script này migrate CÂY
# THƯ MỤC ĐÃ TỒN TẠI của một agent đang chạy — không tạo venv mới, không viết lại
# run.env, không đụng registry.
#
# Usage: bash aoip-agent-harden-migrate.sh [--dry-run] [--force]
# Chạy trên chính VM mục tiêu (root), SAU KHI đã backup unit + run.env hiện tại
# (xem trình tự cutover trong plan Gate 0 / docs/audit/backup-units/).
#
# Script CHỈ cài đặt (user, chown, sudoers, unit file) + daemon-reload — KHÔNG
# restart service. Restart/verify/drill/soak làm riêng, có kiểm chứng từng bước
# (xem plan Gate 0 mục 4, bước 4-8) — không gộp vào đây để tránh mù verify.

set -euo pipefail

INSTALL_DIR="/opt/omni-remote-agent"
SERVICE_FILE="/etc/systemd/system/aoip-agent.service"
SUDOERS_FILE="/etc/sudoers.d/omni-agent"
AGENT_USER="omni-agent"
STATE_DIR="/var/lib/aoip/harden-migrate-state"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[gate0-harden]${NC} $*"; }
warn() { echo -e "${YELLOW}[gate0-harden]${NC} $*"; }
err()  { echo -e "${RED}[gate0-harden]${NC} $*" >&2; }

DRY_RUN=false
FORCE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --force)   FORCE=true; shift ;;
    *) err "Unknown option: $1"; exit 1 ;;
  esac
done

run() { if $DRY_RUN; then echo "[DRY-RUN] $*"; else "$@"; fi; }
step_done() { [[ -f "$STATE_DIR/$1" ]] && ! $FORCE; }
mark_done() { $DRY_RUN || { mkdir -p "$STATE_DIR"; touch "$STATE_DIR/$1"; }; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Step 1: Preflight ────────────────────────────────────────────────────────
if ! step_done "preflight"; then
  log "[1/5] Preflight..."
  [[ $EUID -eq 0 ]] || { err "Phải chạy bằng root (dùng orb -m <vm> -u root)."; exit 1; }
  [[ -d "$INSTALL_DIR" ]] || { err "$INSTALL_DIR không tồn tại — đây là script MIGRATE, không phải fresh install."; exit 1; }
  [[ -f "$INSTALL_DIR/run.env" ]] || { err "$INSTALL_DIR/run.env không tồn tại — VM này chưa cài agent thật."; exit 1; }
  for bin in systemctl journalctl ss sudo visudo; do
    command -v "$bin" &>/dev/null || { err "Thiếu binary bắt buộc: $bin"; exit 1; }
  done
  [[ -f "$SCRIPT_DIR/aoip-agent.service" ]] || { err "Thiếu $SCRIPT_DIR/aoip-agent.service (unit mục tiêu)."; exit 1; }
  [[ -f "$SCRIPT_DIR/omni-agent.sudoers" ]] || { err "Thiếu $SCRIPT_DIR/omni-agent.sudoers."; exit 1; }
  log "  OK: root, $INSTALL_DIR tồn tại, binary đủ, artifact có sẵn."
  mark_done "preflight"
fi

# ─── Step 2: Create system user ───────────────────────────────────────────────
if ! step_done "user"; then
  log "[2/5] Tạo system user ${AGENT_USER}..."
  if id "$AGENT_USER" &>/dev/null; then
    log "  User đã tồn tại ✓"
  else
    run useradd --system --no-create-home --shell /sbin/nologin "$AGENT_USER"
  fi
  mark_done "user"
fi

# ─── Step 3: Resolve supplementary groups + chown existing tree ──────────────
if ! step_done "chown"; then
  log "[3/5] Chown cây thư mục hiện có + resolve supplementary groups..."

  RESOLVED_GROUPS=""
  for g in systemd-journal adm utmp; do
    if getent group "$g" &>/dev/null; then
      RESOLVED_GROUPS="${RESOLVED_GROUPS:+$RESOLVED_GROUPS }$g"
    else
      warn "  Group '$g' không tồn tại trên VM này — bỏ khỏi SupplementaryGroups (sửa unit tay nếu cần trước khi restart)."
    fi
  done
  log "  SupplementaryGroups thật sẽ dùng: ${RESOLVED_GROUPS:-<rỗng>}"
  if [[ -n "$RESOLVED_GROUPS" ]] && ! $DRY_RUN; then
    run usermod -aG "$(echo "$RESOLVED_GROUPS" | tr ' ' ',')" "$AGENT_USER"
  fi

  run chown -R "$AGENT_USER:$AGENT_USER" "$INSTALL_DIR"
  if [[ -d "$INSTALL_DIR/venv" ]]; then
    run chown -R root:root "$INSTALL_DIR/venv"
  fi
  run chmod 600 "$INSTALL_DIR/run.env"
  run chown "$AGENT_USER:$AGENT_USER" "$INSTALL_DIR/run.env"

  run touch /var/log/omni-agent.log
  run chown "$AGENT_USER:$AGENT_USER" /var/log/omni-agent.log
  run chmod 640 /var/log/omni-agent.log

  if [[ -d /var/lib/aoip ]]; then
    run chown -R "$AGENT_USER:$AGENT_USER" /var/lib/aoip
  fi

  # cảnh báo runtime path lệch layout mặc định — đọc plan Gate 0 mục 2 trước khi
  # restart nếu thấy cảnh báo dưới đây
  for var in AOIP_AUDIT_LOG_PATH AOIP_AGENT_INBOX AOIP_RELEASES_DIR OMNI_AGENT_LOG_PATHS; do
    val=$(grep -E "^${var}=" "$INSTALL_DIR/run.env" 2>/dev/null | head -1 | cut -d= -f2- || true)
    if [[ -n "$val" ]] && [[ "$val" != "$INSTALL_DIR"* ]] && [[ "$val" != "/var/lib/aoip"* ]]; then
      warn "  $var=$val nằm NGOÀI $INSTALL_DIR và /var/lib/aoip — có thể cần thêm vào ReadWritePaths= của unit trước khi restart."
    fi
  done

  mark_done "chown"
fi

# ─── Step 4: Install sudoers drop-in ──────────────────────────────────────────
if ! step_done "sudoers"; then
  log "[4/5] Cài sudoers drop-in..."
  run visudo -c -f "$SCRIPT_DIR/omni-agent.sudoers"
  run install -m 0440 -o root -g root "$SCRIPT_DIR/omni-agent.sudoers" "$SUDOERS_FILE"
  log "  Cài xong tại $SUDOERS_FILE (validate lại: visudo -c -f $SUDOERS_FILE)"
  mark_done "sudoers"
fi

# ─── Step 5: Install hardened unit + daemon-reload (KHÔNG restart) ───────────
if ! step_done "unit"; then
  log "[5/5] Cài unit hardened + daemon-reload (chưa restart)..."
  run cp "$SCRIPT_DIR/aoip-agent.service" "$SERVICE_FILE"
  run systemctl daemon-reload
  mark_done "unit"
fi

log ""
log "Migrate xong (user/chown/sudoers/unit) — service VẪN đang chạy dưới config CŨ cho tới khi"
log "'systemctl restart aoip-agent' được gọi tường minh. Trình tự verify trước/sau bắt buộc:"
log "  xem plan Gate 0 mục 4, bước 4-8 (baseline, restart, verify user, drill, soak >=25 phút)."

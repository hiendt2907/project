#!/usr/bin/env bash
# Cập nhật CODE của remote agent trên fleet VM OrbStack — giữ nguyên danh tính.
#
#   bash scripts/omni-agent-update-fleet.sh [--bundle dist/omni-agent-X.Y.Z.tar.gz] [machine...]
#
# Vì sao script này tồn tại thay vì dùng `install.sh` của bundle:
#   `install.sh` cài vào /opt/omni-agent và ghi run.env mới — nó là bộ CÀI MỚI.
#   Fleet lab đang sống ở /opt/omni-remote-agent với `run.env` chứa khoá enrollment
#   RIÊNG cho từng agent (IT-3, one-time token single-use). Chạy install.sh lên đây sẽ
#   tạo một bản cài SONG SONG và làm mất danh tính agent — phải enroll lại từ đầu.
#
# Nên đây là đường CẬP NHẬT: thay code, giữ `run.env` + `venv`.
#
# Không tự bật lại service: trạng thái chạy/dừng là quyết định vận hành, script chỉ
# đổi code. Bật lại bằng `systemctl start omni-remote-agent` khi đã sẵn sàng.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/omni-remote-agent"
BUNDLE=""
MACHINES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bundle) BUNDLE="$2"; shift 2 ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) MACHINES+=("$1"); shift ;;
    esac
done

[[ ${#MACHINES[@]} -eq 0 ]] && MACHINES=(cust-edge cust-app cust-db)

if [[ -z "$BUNDLE" ]]; then
    BUNDLE="$(ls -t "$REPO_ROOT"/dist/omni-agent-*.tar.gz 2>/dev/null | head -1 || true)"
fi
[[ -z "$BUNDLE" || ! -f "$BUNDLE" ]] && {
    echo "Không thấy bundle. Chạy 'make agent-bundle' trước, hoặc --bundle <path>." >&2
    exit 1
}

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar xzf "$BUNDLE" -C "$STAGE"
SRC="$(ls -d "$STAGE"/omni-agent-*/ | head -1)"
VERSION="$(cat "$SRC/VERSION" 2>/dev/null || echo unknown)"

# Chỉ những gì là CODE/CẤU HÌNH TĨNH. Cố ý KHÔNG có run.env, venv, debs, install.sh.
PAYLOAD=(remote_agent aoip pkg config)

echo "bundle=$(basename "$BUNDLE") version=$VERSION"
echo "payload=${PAYLOAD[*]}"
echo

# COPYFILE_DISABLE: macOS thêm xattr ._* vào tar, giải nén trên Linux sinh rác và
# cảnh báo 'unknown extended header keyword' (đã trả giá ở IT-4).
TARBALL="$STAGE/payload.tgz"
( cd "$SRC" && COPYFILE_DISABLE=1 tar czf "$TARBALL" "${PAYLOAD[@]}" )

rc=0
for m in "${MACHINES[@]}"; do
    printf '=== %s ===\n' "$m"

    if ! orb -m "$m" test -f "$INSTALL_DIR/run.env" 2>/dev/null; then
        echo "  BỎ QUA: không thấy $INSTALL_DIR/run.env — máy này chưa enroll."
        rc=1
        continue
    fi

    was_active="$(orb -m "$m" systemctl is-active omni-remote-agent.service 2>/dev/null || true)"
    [[ "$was_active" == "active" ]] && orb -m "$m" sudo systemctl stop omni-remote-agent.service

    # Xoá payload cũ trước khi giải nén: rsync-không-delete để lại file đã bị xoá ở
    # bản mới, và một module chết còn sót có thể vẫn import được.
    for p in "${PAYLOAD[@]}"; do
        orb -m "$m" sudo rm -rf "$INSTALL_DIR/$p"
    done
    orb -m "$m" sudo tar xzf - -C "$INSTALL_DIR" < "$TARBALL" 2>/dev/null

    orb -m "$m" sudo sh -c "printf '%s\n' '$VERSION' > $INSTALL_DIR/VERSION"

    # Kiểm THẬT: import trong chính venv của máy đó. `tar xzf` thành công KHÔNG
    # chứng minh agent chạy được — thiếu một subpackage của pkg/ là fail-closed lúc
    # load catalogue, và nó chỉ lộ ra lúc khởi động.
    if orb -m "$m" sh -c "cd $INSTALL_DIR && ./venv/bin/python -c '
import remote_agent.agent, remote_agent.exec_guard
from pkg.domain.taxonomy import lane_to_domain, CANONICAL_DOMAINS
from pkg.diagnostics.command_catalog import load_catalog
c = load_catalog()
doms = sorted({s.domain for s in c.specs.values()})
print(\"  OK import — catalogue\", len(c.specs), \"lenh /\", len(doms), \"domain\")
print(\"     nguon:\", c.source_files[0])
print(\"     domain:\", \", \".join(doms))
'" 2>&1; then
        :
    else
        echo "  LỖI: import thất bại sau khi cập nhật — xem log ở trên."
        rc=1
    fi

    if [[ "$was_active" == "active" ]]; then
        orb -m "$m" sudo systemctl start omni-remote-agent.service
        echo "  service: bật lại (trước đó đang chạy)"
    else
        echo "  service: để nguyên '$was_active' — script không tự bật"
    fi
    echo
done

exit $rc

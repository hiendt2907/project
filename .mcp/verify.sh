#!/usr/bin/env bash
# Xác minh cấu hình MCP của dự án bằng cách bắt tay THẬT với từng server.
#
# Không giả định, không đọc tài liệu — mỗi PASS dưới đây đều đến từ một
# lần chạy server thật qua stdio và đọc phản hồi JSON-RPC của nó.
#
#   bash .mcp/verify.sh
#
# Exit 0 = tất cả PASS. Exit 1 = có FAIL.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MCP_JSON="$PROJECT_ROOT/.mcp.json"

PASS=0
FAIL=0

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL + 1)); }
group() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Tool nào chạm được vào cluster hoặc DB thì coi là mutate. Danh sách này là bất
# biến của dự án: mutation chỉ đi qua executor, MCP không được mở đường vòng.
#
# Áp cho cả kubernetes lẫn postgres. Với postgres đặc biệt quan trọng vì
# @henkey/postgres-mcp-server@1.0.7 FAIL-OPEN: nếu không đọc được tools-config
# nó chỉ cảnh báo rồi bật TOÀN BỘ 18 tool, gồm pg_execute_sql và
# pg_manage_users, trên chính DB source-of-truth omni_admin. Cổng này là thứ
# phát hiện ra điều đó.
MUTATING_RE='apply|create|delete|patch|scale|rollout|exec_in_pod|helm|cleanup|node_management|generic|port_forward|execute_sql|execute_mutation|pg_manage_|import_table|copy_between'

# Server nào phải chịu cổng chống-mutate.
READONLY_SERVERS='kubernetes postgres'

group "A. Tiền đề môi trường"

bin_version() {
    case "$1" in
        kubectl) kubectl version --client -o json 2>/dev/null \
                     | python3 -c 'import json,sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])' 2>/dev/null ;;
        *) "$1" --version 2>&1 | head -1 ;;
    esac
}

for bin in node npx kubectl python3; do
    if command -v "$bin" >/dev/null 2>&1; then
        pass "$bin có sẵn ($(bin_version "$bin" | cut -c1-40))"
    else
        fail "$bin KHÔNG có — MCP server không chạy được"
    fi
done

if [ -r "$HOME/.kube/config" ]; then
    pass "kubeconfig đọc được"
else
    fail "kubeconfig không đọc được tại ~/.kube/config"
fi

CTX="$(kubectl config current-context 2>/dev/null || true)"
if [ "$CTX" = "orbstack" ]; then
    pass "kube context = orbstack (khớp K8S_CONTEXT trong .mcp.json)"
else
    fail "kube context = '${CTX:-<rỗng>}', .mcp.json khai orbstack"
fi

if kubectl get ns multi-agent >/dev/null 2>&1; then
    pass "namespace multi-agent tồn tại"
else
    fail "namespace multi-agent không truy cập được"
fi

group "B. File cấu hình"

if [ -f "$MCP_JSON" ]; then
    pass ".mcp.json tồn tại ở gốc repo (nơi Claude Code thật sự đọc)"
else
    fail ".mcp.json KHÔNG tồn tại tại $MCP_JSON"
    printf '\nTổng: %d PASS / %d FAIL\n' "$PASS" "$FAIL"
    exit 1
fi

if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$MCP_JSON" 2>/dev/null; then
    pass ".mcp.json parse được"
else
    fail ".mcp.json JSON hỏng"
    printf '\nTổng: %d PASS / %d FAIL\n' "$PASS" "$FAIL"
    exit 1
fi

SERVERS="$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
for name in d.get('mcpServers',{}):
    print(name)
" "$MCP_JSON")"

if [ -n "$SERVERS" ]; then
    pass "khai báo $(echo "$SERVERS" | wc -l | tr -d ' ') server: $(echo "$SERVERS" | tr '\n' ' ')"
else
    fail "không có server nào trong mcpServers"
fi

group "C. Bắt tay MCP thật với từng server"

for name in $SERVERS; do
    OUT="$(python3 "$SCRIPT_DIR/handshake.py" "$MCP_JSON" "$name" 2>&1)"
    RC=$?

    if [ $RC -ne 0 ]; then
        fail "$name: không bắt tay được"
        echo "$OUT" | sed 's/^/          /' | head -6
        continue
    fi

    SRV="$(echo "$OUT" | sed -n 's/^server=//p')"
    TOOLS="$(echo "$OUT" | sed -n 's/^tools=//p')"
    COUNT="$(echo "$TOOLS" | tr ',' '\n' | grep -c . || true)"

    pass "$name: initialize OK → $SRV"
    pass "$name: tools/list OK → $COUNT tool"

    case " $READONLY_SERVERS " in
        *" $name "*)
            BAD="$(echo "$TOOLS" | tr ',' '\n' | grep -Ei "$MUTATING_RE" | tr '\n' ' ' || true)"
            if [ -z "$BAD" ]; then
                pass "$name: KHÔNG lộ tool mutate nào (giữ 'mutations only via executor')"
            else
                fail "$name: LỘ tool mutate → $BAD"
                case "$name" in
                    kubernetes) fail "$name: kiểm ALLOW_ONLY_READONLY_TOOLS trong .mcp.json" ;;
                    postgres)   fail "$name: kiểm .mcp/postgres-readonly-tools.json có đọc được không (server FAIL-OPEN)" ;;
                esac
            fi
            ;;
    esac
done

group "D. Kết quả"
printf '  %d PASS / %d FAIL\n\n' "$PASS" "$FAIL"

[ "$FAIL" -eq 0 ] || exit 1

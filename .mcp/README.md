# MCP — công cụ cho Claude Code khi phát triển Omni

> **Phạm vi: đây là đồ nghề của lập trình viên, KHÔNG phải một phần sản phẩm.**
>
> MCP ở đây chỉ để Claude Code đọc cluster/DB lab và tra tài liệu thư viện **trong
> lúc code**. Nó không được import, không được deploy, không chạy trong runtime.
>
> **Omni vẫn dùng Ollama local** (`qwen2.5-coder:7b` + `nomic-embed-text`, xem
> `OMNI_OLLAMA_BASE_URL`). Không có Claude/Anthropic trong đường chạy của sản
> phẩm — `grep -rE 'anthropic|claude' src/` phải luôn **rỗng**. Nếu một ngày nó
> không rỗng, đó là bug ranh giới, không phải tính năng.
>
> Hệ quả: xoá `.mcp.json` thì Omni vẫn chạy y nguyên; chỉ Claude Code mất tiện
> nghi. Toàn bộ thay đổi của thiết lập này nằm ngoài `src/`, `ui/`, `k8s/`.

**Cấu hình thật nằm ở `.mcp.json` tại gốc repo**, không phải trong thư mục này.
Đó là nơi duy nhất Claude Code đọc server MCP theo phạm vi project. Thư mục
`.mcp/` chỉ chứa công cụ xác minh và tài liệu.

```bash
bash .mcp/verify.sh     # bắt tay THẬT với từng server → 18 PASS / 0 FAIL
```

Điều tra đầy đủ 16 MCP (chọn cái nào, loại cái nào, vì sao):
[`docs/MCP_DISCOVERY_REPORT.md`](docs/MCP_DISCOVERY_REPORT.md).

## Server đang bật

| Server | Gói (ghim phiên bản) | Tool | Bí mật lưu trong repo? |
|---|---|---|---|
| `kubernetes` | `mcp-server-kubernetes@4.1.2` | 8, read-only | không cần |
| `postgres` | `@henkey/postgres-mcp-server@1.0.7` | 4, chỉ đọc | **không** — xem dưới |
| `context7` | `@upstash/context7-mcp@3.2.5` | 2 | không cần |

Cả ba chạy **stdio** — Claude Code tự spawn tiến trình con. Không container,
không cổng, không daemon, không có gì phải "start" trước.

### `kubernetes`

⚠️ **`ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS` là tên gây hiểu nhầm — đừng dùng.**
Đã đo trực tiếp trên `v4.1.2`:

| Cấu hình | Số tool | Còn tool mutate? |
|---|---|---|
| mặc định | 23 | có — `delete`, `cleanup`, `node_management`, `kubectl_generic`, … |
| `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS=true` | 18 | **vẫn còn** `apply`, `create`, `patch`, `scale`, `rollout`, `exec_in_pod`, `install_helm_chart` |
| `ALLOW_ONLY_READONLY_TOOLS=true` | 8 | không |

Chỉ `ALLOW_ONLY_READONLY_TOOLS` mới thật sự read-only. `MASK_SECRETS=true` bật
kèm vì `kubectl_get` vẫn đọc được Secret.

### `postgres` — đọc `omni_admin`, và hai cái bẫy

Cho phép soi schema/index/bảng của `omni_admin` (source-of-truth cho autonomy
config + tenant registry) mà không phải gõ `psql` từng lệnh.

**Bẫy 1 — không có mật khẩu nào nằm trong repo.** `.mcp.json` không chứa DSN.
Nó gọi `sh -c` để lấy DSN **sống** từ Secret ngay lúc server khởi động:

```
kubectl get secret omni-pg-secret -n multi-agent \
  -o jsonpath='{.data.OMNI_ADMIN_PG_DSN}' | base64 -d
```

Ưu điểm: không có credential trong git, luôn khớp cluster, và nếu cluster chết
thì server báo lỗi to chứ không chạy với DSN cũ.

**Bẫy 2 — server này FAIL-OPEN. Đây là lý do phải có hai lớp chặn.**
`v1.0.7` phơi **18 tool**, gồm `pg_execute_sql`, `pg_execute_mutation`,
`pg_manage_users`, `pg_copy_between_databases`. Ta thu về 4 bằng
`--tools-config .mcp/postgres-readonly-tools.json`. Nhưng đọc mã trong
`build/index.js` thì thấy: nếu file config **không đọc được hoặc sai định dạng**,
nó chỉ `console.error` một cảnh báo rồi **bật lại toàn bộ 18 tool**. Một lỗi
đánh máy trong đường dẫn là đủ để mở toàn quyền ghi lên DB source-of-truth.

Vì vậy lớp cưỡng chế thật là **`permissions.deny` trong `.claude/settings.json`**
— do Claude Code thi hành, không phụ thuộc server. Deny-list liệt kê đủ 14 tool
ghi; nó đầy đủ **chính vì phiên bản đã ghim**, nên tập tool không đổi ngầm.
`verify.sh` canh cổng này mỗi lần chạy.

Lưu ý: `--security-mode readonly` mà README trên GitHub nhắc tới **không tồn tại
trong v1.0.7** — repo đi trước bản phát hành. Đừng chép lệnh từ README GitHub.

`pg_execute_query` chỉ nhận `operation ∈ {select, count, exists}` — read-only
theo thiết kế, đã kiểm bằng `tools/list` schema thật.

### `context7`

Repo đã có agent `docs-lookup` khai đúng hai tool
`mcp__context7__resolve-library-id` và `mcp__context7__query-docs`, nhưng
**chưa từng có server nào cung cấp chúng** — agent đó hỏng từ đầu. Đây là
server duy nhất trong ba cái cho năng lực mà Bash không có sẵn: tài liệu thư
viện **bản hiện tại**, thay vì trí nhớ mô hình.

## Cố ý KHÔNG bật

| Server | Lý do (đã kiểm, không phải phỏng đoán) |
|---|---|
| `redis` | `redis/mcp-redis` (chính hãng Redis) **vỡ**: `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` trên **cả** Python 3.12 và 3.14. Chạy được nếu ghim `mcp==1.9.4`, nhưng khi đó phơi **45 tool**, trong đó ~25 tool **ghi** (`delete`, `hdel`, `json_del`, `expire`, `rename`, `zrem`, …) lên chính nơi giữ **CRAT audit chain**, RAG và trace state. Ghim SDK đã lỗi thời để đổi lấy thứ `redis-cli` làm được là đánh đổi sai. |
| `postgres` (bản khác) | `crystaldba/postgres-mcp` ★3143 repo còn active nhưng PyPI đứng im ở **v0.3.0 (2025-05-16)** và vỡ đúng cùng lỗi. |
| `kafka` | Gói npm/PyPI tồn tại nhưng **không khai `repository`** ⇒ không truy nguyên được về repo nào. Không cài thứ không xác định được nguồn. |
| `mysql`, `prometheus` | Chưa từng có bản official; không cần để phát triển repo này. |
| `grafana` | `grafana/mcp-grafana` v1.0.0 chính hãng, chất lượng tốt — nhưng phục vụ vận hành hơn là viết code. Để dành. |
| `filesystem`, `git`, `github` | Claude Code đã có Read/Write/Glob/Grep, Bash+git, và `gh` đã đăng nhập. Thêm vào chỉ làm yếu đi. |
| `memory`, `sequential-thinking`, `time`, `fetch` | Trùng với bộ nhớ file sẵn có, extended thinking, và WebFetch. |

**Nguyên nhân gốc chung của các ca vỡ:** MCP Python SDK đã lên **2.0.0** và gỡ
`mcp.server.fastmcp` của nhánh 1.x. Mọi gói còn ghim API cũ mà chưa phát hành
lại đều chết — bất kể repo trông sống động thế nào.

## Khả năng kết nối từ host (đã đo lại, xem cảnh báo)

| Đích | Kết quả |
|---|---|
| `localhost:5432/6379/9092/9090/3000` | đóng hết |
| Pod IP (vd `192.168.194.105:5432`) | mở, nhưng **đổi mỗi lần pod restart** |
| `omni-postgres.multi-agent.svc.cluster.local:5432` | **mở, 3/3** |
| `redis.multi-agent.svc.cluster.local:6379` | **mở, 3/3** |
| `kafka.multi-agent.svc.cluster.local:9092` | **mở, 3/3** |

⚠️ **Đính chính.** Một bản trước của file này ghi Redis/Kafka "đóng". Sai. Nguyên
nhân: script đo dùng `set -- $var` để tách chuỗi, nhưng shell ở đây là **zsh** —
zsh không tách từ khi mở rộng biến không nháy (`SH_WORD_SPLIT` tắt mặc định), nên
`nc` nhận nguyên chuỗi `"host port label"` làm hostname và trượt. Kết luận
"không tới được" là **âm tính giả do bug script của chính mình**.

Cùng lớp lỗi đã xảy ra với `docker manifest inspect` chạy vòng lặp (rate-limit
Docker Hub ẩn danh) — xem §0 báo cáo điều tra.
**Bài học: một phép đo âm tính phải tự chứng minh nó đo đúng thứ cần đo.**

`host.docker.internal` mà cấu hình cũ dùng khắp nơi thì vô nghĩa ở môi trường này.

## Nâng phiên bản

Cả ba gói **ghim phiên bản**. `latest` thả nổi chính là cách `postgres-mcp` và
`redis-mcp-server` tự vỡ.

```bash
npm view mcp-server-kubernetes version
bash .mcp/verify.sh     # phải còn xanh, ĐẶC BIỆT cổng "không lộ tool mutate"
```

Deny-list trong `.claude/settings.json` là **liệt kê**, nên khi nâng phiên bản
phải kiểm lại: gói mới có thể thêm tool ghi chưa nằm trong danh sách.

## Vì sao cấu hình cũ bị xoá

Bản `.mcp/` trước (chưa từng commit) không chạy được, không phải chạy sai:

- `servers/docker-compose.yml` khai 16 image `modelcontextprotocol/*` — **không
  image nào tồn tại**. (Image MCP có thật, nhưng ở namespace **`mcp/`**.)
- `scripts/health-check.sh` **không parse nổi**: `bash -n` lỗi syntax dòng 98.
- Sai kiến trúc gốc: Claude Code nói MCP qua **stdio**, không phải HTTP cổng
  8080–8095; và không có `.mcp.json` nên chẳng gì kết nối được.
- `.env` chứa credential giả, hai bản mâu thuẫn ở hai chỗ.

**Bài học đưa vào `verify.sh`:** mỗi PASS đều đến từ một lần spawn server thật
và đọc phản hồi JSON-RPC của nó. Không dòng nào viết dựa trên trí nhớ về gói nào
tồn tại.

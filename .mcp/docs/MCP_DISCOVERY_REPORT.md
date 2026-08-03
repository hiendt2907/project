# MCP Discovery & Validation Report

**Ngày:** 2026-07-31 · **Phạm vi:** 16 MCP được chỉ định · **Trạng thái:** điều tra xong, **chưa cài gì**

Báo cáo này chỉ để đọc và duyệt. Không có script cài đặt, không có thay đổi cấu
hình nào đi kèm.

---

## 0. Phương pháp — và giới hạn của nó

Mỗi kết luận dưới đây đến từ một trong các nguồn sau, không từ trí nhớ:

| Câu hỏi | Nguồn được dùng |
|---|---|
| Server nào là chính thức | `gh api repos/modelcontextprotocol/servers/contents/src` |
| Server nào bị bỏ | `gh api repos/modelcontextprotocol/servers-archived/contents/src` |
| Tên gói phát hành | đọc `package.json` / `pyproject.toml` **trong chính repo** |
| Phiên bản, deprecated | `npm view <pkg> --json` · `pypi.org/pypi/<pkg>/json` |
| Repo còn sống không | `gh api repos/<owner>/<repo>` → `archived`, `pushed_at` |
| Bản phát hành mới nhất | `gh api repos/<r>/releases/latest` |
| Image Docker có thật không | `hub.docker.com/v2/repositories/mcp/<n>/tags` |
| Transport Claude Code hỗ trợ | `code.claude.com/docs/en/mcp` |

### Ba cảnh báo về chính dữ liệu này

1. **`docker manifest inspect` cho kết quả SAI khi chạy vòng lặp.** Lần quét 16
   image đầu tiên báo "không tồn tại" cho **cả 16**, trong khi kiểm lại từng cái
   thì `mcp/grafana`, `mcp/redis`, `mcp/kubernetes` đều có thật. Nguyên nhân:
   rate-limit Docker Hub ẩn danh. Số liệu trong báo cáo này lấy từ **Docker Hub
   registry API**, không phải `docker manifest`.
   → *Kết quả âm tính từ một API bị rate-limit không phải bằng chứng vắng mặt.*

2. **Cột Transport một phần suy ra từ grep README.** Chữ "http" có thể xuất hiện
   trong README vì lý do khác. Những ô đánh dấu ⚠️ là chưa xác minh bằng cách
   chạy thật. Chỉ 2 server đã được bắt tay JSON-RPC thật (xem §3).

3. **Tên gói tồn tại ≠ đúng gói.** Với Kafka, npm `kafka-mcp-server` và
   `mcp-kafka` đều tồn tại nhưng **không khai `repository`**, nên không thể nối
   về repo GitHub nào. Đó là lý do Kafka bị chấm Confidence thấp.

4. **Đính chính sau khi triển khai (2026-07-31 17:2x).** Một phép đo trong lượt
   trước kết luận Redis/Kafka "không tới được từ host". **Sai.** Script đo dùng
   `set -- $var`, nhưng shell là **zsh** — zsh không tách từ khi mở rộng biến
   không nháy, nên `nc` nhận nguyên chuỗi `"host port label"` làm hostname và
   trượt. Đo lại đúng cách: Postgres, Redis, Kafka đều **3/3 qua
   `*.svc.cluster.local`**. Kết luận loại Redis/Kafka trong báo cáo này **vẫn
   giữ nguyên**, nhưng lý do là *gói vỡ / không truy nguyên được nguồn*,
   **không phải** vì mạng.
   → *Cùng lớp lỗi với ca `docker manifest` ở mục 1: một phép đo âm tính phải tự
   chứng minh nó đo đúng thứ cần đo.*

---

## 1. Bảng tổng hợp

Ký hiệu Maintenance: **Official** = do MCP org hoặc chính hãng phát hành ·
**Vendor** = chính hãng dịch vụ · **Community** = bên thứ ba · **Archived** = đã bỏ.

| MCP | Repository chính thức | Lệnh cài (theo tài liệu gốc) | Runtime | Transport | Maintenance | Claude Code | Thay thế cần thiết | Confidence |
|---|---|---|---|---|---|---|---|---|
| **Filesystem** | `modelcontextprotocol/servers` `/src/filesystem` | `npx -y @modelcontextprotocol/server-filesystem@2026.7.10 <path>` | Node | stdio | **Official**, npm v2026.7.10 (2026-07-10) | ✅ | — | **Cao** |
| **Git** | `modelcontextprotocol/servers` `/src/git` | `uvx mcp-server-git@2026.7.10` | Python | stdio | **Official**, PyPI v2026.7.10 | ✅ | — | **Cao** |
| **GitHub** | `github/github-mcp-server` | binary / `ghcr.io/github/github-mcp-server` / remote `https://api.githubcopilot.com/mcp/` | Go | stdio, http ⚠️ | **Official (GitHub)** v1.8.0 (2026-07-30) | ✅ | **Có** — bản reference đã bị lưu trữ | **Cao** |
| **Docker** | `docker/mcp-gateway` | `docker mcp` (plugin CLI của Docker Desktop) | Go | stdio (gateway) | **Vendor**, ★1513 | ⚠️ khác hình dạng | Không có server quản-lý-container đáng tin | **Trung bình** |
| **Kubernetes** | `containers/kubernetes-mcp-server` **hoặc** `Flux159/mcp-server-kubernetes` | `npx -y kubernetes-mcp-server@0.0.65` · `npx -y mcp-server-kubernetes@4.1.2` | Go / TS | stdio, sse, http | **Community**, cả hai active | ✅ | — | **Cao** |
| **PostgreSQL** | ~~`servers-archived/src/postgres`~~ → `crystaldba/postgres-mcp` | `uvx postgres-mcp` | Python | stdio, sse ⚠️ | **Archived** (gốc) · thay thế **v0.3.0 từ 2025-05-16** | ❌ **hỏng** | **Bắt buộc — nhưng chưa có bản chạy được** | **Cao** |
| **MySQL** | `designcomputer/mysql_mcp_server` | `uvx mysql-mcp-server` (PyPI v0.4.4) | Python | stdio, http ⚠️ | **Community**, ★1346, 2026-07-30 | ⚠️ chưa thử | chưa từng có bản official | **Trung bình** |
| **Redis** | ~~`servers-archived/src/redis`~~ → `redis/mcp-redis` | `uvx redis-mcp-server` (PyPI v0.5.0) | Python | stdio, sse, http ⚠️ | **Vendor (Redis)** 2026-03-16 | ⚠️ chưa thử | **Có — và đã tồn tại bản chính hãng** | **Cao** |
| **Kafka** | `tuannvm/kafka-mcp-server` (★53) | *không xác minh được* | Go | stdio ⚠️ | **Community**, ít dùng | ⚠️ chưa thử | — | **Thấp** |
| **Prometheus** | `pab1it0/prometheus-mcp-server` | `uvx prometheus-mcp-server` (PyPI v1.6.1) | Python | stdio, sse, http ⚠️ | **Community**, ★510, 2026-07-23 | ⚠️ chưa thử | chưa từng có bản official | **Trung bình** |
| **Grafana** | `grafana/mcp-grafana` | binary / `mcp/grafana` | Go | stdio, sse, streamable-http | **Vendor (Grafana Labs)** v1.0.0 (2026-07-28) | ✅ | — | **Cao** |
| **Context7** | `upstash/context7` | `npx -y @upstash/context7-mcp@3.2.5` · remote `https://mcp.context7.com/mcp` | Node | stdio, http | **Vendor (Upstash)** ★60058 | ✅ **đã bắt tay thật** | — | **Cao** |
| **Sequential Thinking** | `modelcontextprotocol/servers` `/src/sequentialthinking` | `npx -y @modelcontextprotocol/server-sequential-thinking@2026.7.4` | Node | stdio | **Official** | ✅ | — | **Cao** |
| **Time** | `modelcontextprotocol/servers` `/src/time` | `uvx mcp-server-time@2026.7.10` | Python | stdio | **Official** | ✅ | — | **Cao** |
| **Fetch** | `modelcontextprotocol/servers` `/src/fetch` | `uvx mcp-server-fetch@2026.7.10` | Python | stdio | **Official** | ✅ | — | **Cao** |
| **Memory** | `modelcontextprotocol/servers` `/src/memory` | `npx -y @modelcontextprotocol/server-memory@2026.7.4` | Node | stdio | **Official** | ✅ | — | **Cao** |

---

## 2. Phân loại theo yêu cầu

### ✅ Official — MCP org còn duy trì (7)

Đây là **toàn bộ** danh sách reference server còn sống, lấy trực tiếp từ
`modelcontextprotocol/servers/src`:

```
everything  fetch  filesystem  git  memory  sequentialthinking  time
```

Không có server nào khác được MCP org duy trì. Mọi thứ ngoài danh sách này là
bên thứ ba hoặc chính hãng dịch vụ.

### ⚰️ Deprecated / Archived

Repo `modelcontextprotocol/servers-archived` (đã archive, đứng im từ
2025-05-28) chứa: `github`, `postgres`, `redis`, `sqlite`, `gdrive`, `gitlab`,
`slack`, `sentry`, `puppeteer`, `brave-search`, `google-maps`, `everart`,
`aws-kb-retrieval-server`.

Ngoài ra, npm đã gắn cờ **deprecated** cho:
- `@modelcontextprotocol/server-postgres` (v0.6.2)
- `@modelcontextprotocol/server-redis` (v2025.4.25)

### 🔁 Replacement required

| Gốc | Thay bằng | Ghi chú |
|---|---|---|
| GitHub | `github/github-mcp-server` | chính GitHub phát hành, thay thế sạch |
| Redis | `redis/mcp-redis` | chính Redis phát hành, thay thế sạch |
| PostgreSQL | `crystaldba/postgres-mcp` | **chưa dùng được — xem §4** |

### 🚫 Unavailable / không có bản đáng tin

- **Docker (quản lý container):** tìm kiếm chỉ ra các repo ★0. Thứ Docker thật
  sự phát hành là **gateway** (`docker/mcp-gateway`), không phải server quản lý
  container. Đây là khác biệt về bản chất, không phải khác biệt về tên gói.
- **Kafka:** không có bản official; gói trên registry không truy nguyên được repo.
- **MySQL, Prometheus:** chưa từng có bản official; chỉ có community.

---

## 3. Mức độ xác minh — cái gì đã CHẠY thật, cái gì mới chỉ đọc

Đây là cột quan trọng nhất và là thứ bản `.mcp/` cũ đã bỏ qua.

| Server | Đã bắt tay JSON-RPC thật? | Kết quả |
|---|---|---|
| `mcp-server-kubernetes@4.1.2` | **Có** | `initialize` OK → `kubernetes v4.1.2`; `tools/list` → 8 tool (chế độ `ALLOW_ONLY_READONLY_TOOLS`) |
| `@upstash/context7-mcp@3.2.5` | **Có** | `initialize` OK → `Context7 v3.2.5`; tools = `resolve-library-id`, `query-docs` |
| `postgres-mcp` (PyPI 0.3.0) | **Có** | ❌ **THẤT BẠI** — `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` trên cả Python 3.14 và 3.12 |
| 13 server còn lại | **Chưa** | mới chỉ xác minh tồn tại + metadata registry |

**Hệ quả:** mọi ô ✅ ở cột "Claude Code" cho 13 server chưa chạy đều là **suy
luận từ tài liệu**, không phải quan sát. Nếu bạn duyệt cài, bước đầu tiên phải
là bắt tay thật từng cái.

---

## 4. PostgreSQL — trường hợp cần nói rõ

Đây là mục dễ bị kết luận sai nhất, vì mọi tín hiệu bề mặt đều tốt:

| Tín hiệu | Giá trị | Đọc thế nào |
|---|---|---|
| Sao GitHub | ★3143 | trông rất đáng tin |
| Repo archived? | không | trông còn sống |
| `pushed_at` | 2026-07 | trông đang phát triển |
| **Release mới nhất** | **v0.3.0 — 2025-05-16** | **đứng im 14 tháng** |
| **PyPI mới nhất** | **v0.3.0 — 2025-05-16** | khớp release, tức bản vá chưa phát hành |
| **Chạy thử** | **crash khi import** | ❌ |

Repo hoạt động nhưng **không phát hành**. `uvx postgres-mcp` kéo về bản
2025-05 vốn viết cho API `mcp.server.fastmcp` đã bị gỡ khỏi SDK. Đây đúng là lớp
lỗi mà sao/commit-gần-đây không phát hiện được — chỉ chạy thử mới thấy.

**Khuyến nghị:** để PostgreSQL ở trạng thái *replacement required, chưa có bản
dùng được*. Truy cập DB hiện đã có đường khác, không bị chặn việc gì.

---

## 5. Ràng buộc của chính máy này

Đo trực tiếp, ảnh hưởng tới việc phương án nào khả thi:

| Hạng mục | Trạng thái |
|---|---|
| Node / npx | v25.8.2 / 11.11.1 → chạy được mọi server npm |
| uv / uvx | 0.12.0 → chạy được mọi server PyPI |
| Docker Engine | 29.4.0 |
| **`docker mcp` CLI plugin** | **KHÔNG có** → đường Docker MCP Toolkit hiện không dùng được nếu không cài thêm |
| kubectl / context | v1.33.1 / `orbstack` |
| Go toolchain | chưa kiểm — cần nếu muốn build server Go từ nguồn thay vì dùng image |

**Image Docker trong catalog `mcp/`** (đã xác minh qua Hub API):

```
CÓ    : filesystem git github docker kubernetes redis grafana
        context7 sequentialthinking time fetch memory
KHÔNG : postgres mysql kafka prometheus
```

⚠️ Lưu ý cho lần sau: namespace đúng là **`mcp/`**. Namespace
`modelcontextprotocol/*` mà cấu hình cũ dùng **không tồn tại** trên Docker Hub —
đó là gốc của toàn bộ sự cố trước.

---

## 6. Transport mà Claude Code hỗ trợ

Theo `code.claude.com/docs/en/mcp`:

| Transport | Cách khai |
|---|---|
| `stdio` | `claude mcp add --transport stdio <tên> -- <lệnh> <args>` |
| `http` | `claude mcp add --transport http <tên> <url>` (alias `streamable-http` trong JSON) |
| `sse` | `claude mcp add --transport sse <tên> <url>` |
| `ws` | chỉ khai bằng JSON; `--transport` không nhận `ws` |

Phạm vi cấu hình: `local` (mặc định, trong `~/.claude.json`) · `project`
(`.mcp.json` ở gốc repo, chia sẻ qua git) · `user`.

Server khai trong `.mcp.json` **luôn phải được duyệt thủ công** — hiện lên là
`⏸ Pending approval` cho tới khi chạy `claude` và chấp nhận. Điều này áp dụng cho
bất kỳ thứ gì được duyệt cài sau này.

---

## 7. Những gì tôi CHƯA xác minh

Ghi ra để không bị hiểu nhầm là đã kiểm:

- Chưa chạy thử 13/16 server (xem §3).
- Chưa kiểm hành vi thật của bất kỳ tool nào — chỉ đọc danh sách tên tool.
- Chưa đánh giá bề mặt bảo mật của các server community (`mysql`, `prometheus`,
  `kafka`): chưa đọc mã, chưa kiểm có chặn SQL injection / lệnh phá hoại không.
- Chưa kiểm license compatibility cho mục đích thương mại.
- Chưa đo tiêu thụ token của từng server khi nạp tool định nghĩa vào context.
- Chưa xác minh server Go chạy trên darwin/arm64 (chỉ thấy có release, chưa chạy).
- "Officially recommended for Claude Code": **Anthropic không công bố danh sách
  server khuyến nghị**. Tài liệu chỉ nêu ví dụ (Notion, Asana, Stripe, Figma,
  Sentry). Vì vậy cột "Claude Code" trong bảng nghĩa là *tương thích về mặt kỹ
  thuật*, **không phải** *được Anthropic khuyến nghị*. Không mục nào trong 16 mục
  được Anthropic khuyến nghị chính thức.

---

## 8. Trạng thái hiện tại của repo

Để bạn quyết định trên nền đúng sự thật:

- `.mcp.json` ở gốc repo hiện khai **2 server** (`kubernetes`, `context7`), đã
  được duyệt và **đang connected** trong phiên này.
- Cả hai đã bắt tay thật, `bash .mcp/verify.sh` → 15 PASS / 0 FAIL.
- **Chưa commit.** Nếu bạn muốn quay về trạng thái không có MCP nào, xoá
  `.mcp.json` là đủ.
- Báo cáo này **không thay đổi** cấu hình đó.

---

## 9. Kết quả triển khai (user duyệt 2026-07-31)

Đã triển khai theo mặc định: **stdio qua npx** (do `docker mcp` plugin không có
trên máy), và **bắt tay thật trước khi ghi vào `.mcp.json`**.

`bash .mcp/verify.sh` → **18 PASS / 0 FAIL**

| Server | Gói | Tool | Ghi chú |
|---|---|---|---|
| `kubernetes` | `mcp-server-kubernetes@4.1.2` | 8 | `ALLOW_ONLY_READONLY_TOOLS=true` |
| `postgres` | `@henkey/postgres-mcp-server@1.0.7` | 4 | DSN lấy sống từ Secret; server FAIL-OPEN nên lớp chặn thật là `permissions.deny` |
| `context7` | `@upstash/context7-mcp@3.2.5` | 2 | không cần khoá |

**Phát hiện trong lúc triển khai:**

- `@henkey/postgres-mcp-server@1.0.7` mặc định phơi **18 tool** gồm
  `pg_execute_sql`, `pg_manage_users`, `pg_copy_between_databases`.
  `--tools-config` thu về 4, nhưng đọc `build/index.js` thấy nó **fail-open**:
  config lỗi ⇒ cảnh báo rồi bật lại toàn bộ. Lớp cưỡng chế thật là deny-list
  phía Claude Code.
- `--security-mode readonly` mà README GitHub mô tả **không có trong v1.0.7** —
  repo đi trước bản phát hành, đúng lại bẫy của `crystaldba/postgres-mcp`.
- `redis/mcp-redis` chạy được nếu ghim `mcp==1.9.4`, nhưng phơi **45 tool**
  (~25 tool ghi) lên nơi giữ CRAT audit chain ⇒ **loại**.
- MCP Python SDK đã lên **2.0.0**, gỡ `mcp.server.fastmcp` của 1.x. Đó là
  nguyên nhân gốc chung khiến cả `postgres-mcp` lẫn `redis-mcp-server` vỡ.

**Chưa commit.** Xoá `.mcp.json` là quay về trạng thái không có MCP nào.

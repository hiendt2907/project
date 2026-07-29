# Lộ trình đưa Omni lên production

> Viết 2026-07-29. Mọi con số trong tài liệu này đến từ kiểm chứng runtime tại thời
> điểm viết, không chép lại từ audit cũ. Xem mục "Cách dùng tài liệu này" ở cuối
> trước khi tin bất kỳ trạng thái nào — chúng hết hạn nhanh.

## 0. Kết luận trước, lý lẽ sau

Omni **chưa sẵn sàng production**, nhưng khoảng cách không nằm ở chỗ hầu hết người ta
nghĩ. Phần khó nhất — reasoning nhiều lane, sổ audit ký mã hoá, cách ly tenant,
governance fail-closed — đã chạy thật và có bằng chứng. Cái thiếu là **những thứ nhàm
chán**: không có bản sao dự phòng cho bất kỳ thành phần nào, dữ liệu nằm trên đĩa một
máy tính xách tay, và chưa một khách hàng thật nào từng dùng hệ thống.

Ba câu hỏi quyết định lộ trình, và **không câu nào là kỹ thuật**:

1. Có khách hàng thật chưa? Nếu chưa, mọi việc hạ tầng bên dưới đều là đầu tư mù.
2. Có pháp nhân chưa? Không có thì không ký được hợp đồng, không mua được cloud
   doanh nghiệp, không nhận được credit chương trình startup.
3. Chấp nhận mất bao nhiêu tiền mỗi tháng trước khi có doanh thu?

Nếu câu trả lời là "chưa / chưa / rất ít" thì **Giai đoạn 1 dưới đây là toàn bộ việc
cần làm trong 1–3 tháng tới**, và tuyệt đối không nên đụng tới Giai đoạn 3.

---

## 1. Hiện trạng đã kiểm chứng (2026-07-29)

### Đang chạy thật

| | Bằng chứng |
|---|---|
| 4 lane chẩn đoán | `omni-diagnostic-evidence` có traffic, LAG=0 |
| CRAT ký Ed25519 | `signature_hex` 128 hex thật, hash-chain liền mạch, CronJob integrity-check |
| RAG vector | `idx:itops_sop_ledger` **1093 docs** — đã đóng gap "0 docs" của audit 2026-07-22 |
| Cách ly tenant | `resolve_scope` phủ `/autonomy`, `/trace`, `/reports`, CRAT, và **HITL** (vá 2026-07-29) |
| Kill-switch | `OMNI_AUTO_EXECUTE_ENABLED=false`, tier hiệu lực `shadow` |
| Test | **6750 passed**, coverage gate 90% enforce qua Makefile |
| Public surface | `www.omnisre.xyz` + `app.omnisre.xyz` sau Cloudflare Access |
| RBAC | Không còn ClusterRoleBinding `cluster-admin` cho SA `omni-*` — đã đóng gap CRITICAL của audit |

### Chưa có — và đây mới là khoảng cách thật

| Vấn đề | Bằng chứng |
|---|---|
| **Mọi thứ 1 replica** | 12/12 Deployment đều `replicas: 1`. Bất kỳ pod nào chết = mất tính năng đó |
| **Dữ liệu trên một đĩa** | PVC `local-path` — PostgreSQL 2Gi, Redis 10Gi. Hỏng đĩa = **mất toàn bộ sổ audit** |
| **Kafka một broker** | RF=1. Mất broker = mất message chưa consume |
| **MacBook là SPOF** | Máy ngủ/reboot/mất mạng = toàn hệ thống ngừng |
| **Chưa có backup** | Không tìm thấy job backup nào cho PostgreSQL hay Redis AOF |
| **HITL dispatcher tắt** | `omni-hitl-dispatcher` replicas=0 — luồng phê duyệt qua Telegram chưa chứng minh chạy sống |
| **Semantic cache trống** | `idx:semcache` 0 docs — mỗi chẩn đoán đều gọi LLM, không tái dùng |
| **`k8s_expert` trống** | 0 docs — một nguồn tri thức đã dựng nhưng chưa nạp |
| **Chưa có khách hàng thật** | Toàn bộ dữ liệu vận hành đến từ 3 VM lab tự dựng |
| **Chưa có pháp nhân** | Chặn hợp đồng, cloud doanh nghiệp, và chương trình credit |

### Điểm cần trung thực với chính mình

`shadow` tier nghĩa là **hệ thống chưa từng tự sửa gì trong đời thật**. Toàn bộ giá
trị đang được chứng minh là "chẩn đoán đúng", chưa phải "khắc phục đúng". Bước từ
chẩn đoán sang hành động là bước rủi ro nhất còn lại, và không có lượng test nào thay
thế được việc chạy thật với hậu quả thật.

---

## 2. Bốn giai đoạn, mỗi giai đoạn có cổng ra rõ ràng

Nguyên tắc: **không sang giai đoạn sau khi cổng ra chưa đạt.** Cổng viết dưới dạng
kiểm chứng được, không phải cảm giác.

---

### Giai đoạn 1 — Chứng minh giá trị với một khách hàng thật *(1–3 tháng)*

**Mục tiêu duy nhất: có một người không phải bạn dựa vào Omni để làm việc.**

Không nâng cấp hạ tầng ở giai đoạn này. Kiến trúc hiện tại thừa sức phục vụ 1–3 khách
hàng thử nghiệm ở chế độ `shadow`.

Việc cần làm:

1. **Tìm 1–2 design partner.** Tiêu chí: vận hành Kubernetes hoặc đội máy Linux, có
   người trực thật, và chấp nhận `shadow` (Omni chỉ quan sát và khuyến nghị).
2. **Onboarding thật, đo bằng đồng hồ.** Ghi lại mất bao lâu từ lúc cài agent tới lúc
   có advisory đầu tiên. Đây là chỉ số bán hàng quan trọng nhất, và hiện chưa ai biết.
3. **Bật lại HITL dispatcher.** Không có nó, khách hàng không nhận được gì qua
   Telegram — mà đó là kênh họ thực sự dùng.
   ```bash
   kubectl -n multi-agent scale deploy/omni-hitl-dispatcher --replicas=1
   ```
4. **Đo chất lượng advisory bằng dữ liệu khách hàng.** `make benchmark-advisory`
   (thang 100, pass=70) hiện chưa có kết quả gần đây được xác nhận.
5. **Sổ tay khách hàng.** Không phải tài liệu kiến trúc — là "hệ thống báo thế này thì
   tôi làm gì".

**Cổng ra:**
- ≥ 1 khách hàng ngoài chạy ≥ 30 ngày liên tục
- ≥ 50 advisory sinh từ sự cố thật, có người chấm điểm
- Tỉ lệ chấp nhận ≥ 60% (đo được, không ước lượng)
- Khách hàng trả lời được: "nếu tắt Omni ngày mai, bạn có thấy thiếu không?"

**Nếu cổng này không qua trong 3 tháng: dừng đầu tư hạ tầng, xem lại sản phẩm.**

---

### Giai đoạn 2 — Không mất dữ liệu, không mất mặt *(song song Giai đoạn 1)*

Việc phải làm ngay cả khi chưa có khách hàng, vì đây là thứ khiến bạn mất *uy tín*
chứ không chỉ mất thời gian.

1. **Backup PostgreSQL + Redis, có khôi phục thử.** Sổ CRAT là bằng chứng tuân thủ —
   mất nó là mất toàn bộ giá trị pháp lý của hệ thống. Backup chưa từng khôi phục thử
   thì chưa phải backup.
2. **Đưa CRAT ra khỏi máy.** Ít nhất đẩy hash-chain định kỳ sang một nơi thứ hai
   (object storage, hoặc repo riêng). Rẻ, và cắt được rủi ro tệ nhất.
3. **Rate limit `/auth` và `_require_api_key`.** Hiện **không có ở tầng ứng dụng**.
   Cloudflare Access đang che, nhưng đây là điều kiện bắt buộc trước khi public
   `api.omnisre.xyz`.
4. **Cảnh báo khi hệ thống chết.** Hiện nếu MacBook ngủ, không ai biết. Cần một
   heartbeat ngoài (uptime check của bên thứ ba) bắn về Telegram.
5. **Xoay secret có lịch.** Quy trình đã viết trong runbook nhưng chưa ai chạy lần nào.

**Cổng ra:**
- Khôi phục PostgreSQL từ backup trên máy trắng, CRAT verify hash-chain thành công
- Hệ thống chết → có cảnh báo trong vòng 5 phút
- `/auth` chịu được 1000 request/phút mà không sập, và chặn brute force

---

### Giai đoạn 3 — Hạ tầng chịu được production *(chỉ khi Giai đoạn 1 đã qua cổng)*

**Không làm giai đoạn này trước khi có khách hàng trả tiền.** Chi phí cloud là dòng
tiền ra hàng tháng; đốt nó để phục vụ 3 VM lab là sai lầm dễ mắc nhất.

Khi đã đủ điều kiện, thứ tự ưu tiên:

| Thành phần | Từ | Sang | Vì sao trước |
|---|---|---|---|
| PostgreSQL | StatefulSet 1 pod, local-path | Cloud SQL / RDS có backup tự động | Chứa CRAT + tenant registry — mất là không cứu được |
| Redis | 1 pod, AOF | Managed Redis có replica | Chứa session, RAG, hot path |
| Kafka | 1 broker RF=1 | Managed Kafka / Pub/Sub RF≥3 | Mất message = mất bằng chứng sự cố |
| Control plane | 1 pod mỗi loại | ≥2 replica, PDB, anti-affinity | Sau cùng — vô nghĩa nếu dữ liệu bên dưới vẫn mong manh |

Đồng thời:
- **TLS đầu-cuối.** Ngoại lệ HTTP giữa cloudflared và Traefik (xem ADR 0001) chỉ chấp
  nhận được vì cả hai nằm trong một máy. Rời MacBook là phải bỏ ngoại lệ đó.
- **Suy luận.** Quyết định: giữ LLM tại chỗ (bán được cho ngành chịu quản lý, nhưng
  phải có GPU) hay cho phép mô hình đặt ngoài (rẻ, nhanh, mất lợi thế bán hàng cốt
  lõi). **Đây là quyết định sản phẩm, không phải quyết định kỹ thuật.**
- **CI/CD.** Đã gỡ toàn bộ GitHub Actions vì hết quota (commit `2038de1`). Trước
  production phải có lại cổng tự động quét secret + chạy test; repo public hoặc trả
  phí Actions, hoặc chuyển sang runner khác.

**Cổng ra:**
- Giết bất kỳ pod nào → hệ thống vẫn phục vụ, có bằng chứng chaos drill
- Mất một AZ → không mất dữ liệu
- RTO/RPO có số cụ thể và đã diễn tập

---

### Giai đoạn 4 — Từ khuyến nghị sang hành động *(giai đoạn rủi ro nhất)*

Đây là lúc Omni thực sự trở thành cái nó tự nhận. Cũng là lúc một lỗi có thể làm hỏng
hệ thống của khách hàng.

Nguyên tắc: **nâng tier theo từng khách hàng, dựa trên dữ liệu của chính họ**, không
nâng đồng loạt.

1. `shadow` → `minimal` cho khách hàng đầu tiên, sau khi họ có ≥ 90 ngày và tỉ lệ chấp
   nhận ≥ 80%.
2. Mọi mutation đầu tiên của mỗi loại tool đều đi qua HITL, bất kể tier.
3. Rollback phải diễn tập thật trước khi bật, không phải sau.
4. Escalation tier gate cho nhánh SIEM **chưa implement** (`_is_siem_batch` không gọi
   `tier_gate`) — phải đóng trước khi cho SIEM tự hành động.

**Cổng ra:**
- 100 mutation thật, 0 sự cố do Omni gây ra
- Rollback đã dùng thật ít nhất 1 lần và thành công
- Khách hàng đồng ý bằng văn bản cho từng nấc tier

---

## 3. Việc không nên làm

Ghi lại để khỏi bị cám dỗ:

| | Vì sao |
|---|---|
| Migrate sang Cloudflare Workers/D1/KV | Viết lại toàn bộ core, mất khả năng chạy tại chỗ — chính là lợi thế bán hàng |
| Multi-region trước khi có khách hàng | Chi phí và độ phức tạp không đổi lấy gì |
| Bật `autonomous` tier sớm | Một mutation sai xoá sạch niềm tin xây trong nhiều tháng |
| Thêm lane chẩn đoán thứ 5 | 4 lane hiện có chưa khai thác hết; APP_HTTP còn chưa có dữ liệu vận hành |
| Viết lại portal | 50% năng lực operator-visible là đủ để bán thử; đẹp hơn không tạo doanh thu ở giai đoạn này |

---

## 4. Rủi ro lớn nhất, xếp theo mức độ

| # | Rủi ro | Xác suất | Hậu quả | Giảm thiểu |
|---|---|---|---|---|
| 1 | **Không ai cần sản phẩm này** | Trung bình | Mất toàn bộ công sức | Giai đoạn 1 trả lời trong 3 tháng, rẻ |
| 2 | Hỏng đĩa MacBook | Thấp/năm | Mất CRAT, không khôi phục được | Giai đoạn 2, làm ngay |
| 3 | Mutation sai làm hỏng hệ thống khách | Thấp (đang `shadow`) | Mất khách + uy tín | Giữ `shadow`; Giai đoạn 4 có cổng chặt |
| 4 | LLM 7B chạm trần chất lượng | **Cao** | Advisory không đủ tốt để tin | Đã có carry-over model-ceiling trong ledger; cần đo bằng dữ liệu thật trước khi đổi model |
| 5 | Một người duy nhất hiểu hệ thống | Cao | Bus factor = 1 | Tài liệu đã tốt bất thường; giữ kỷ luật đó |
| 6 | Chi phí cloud vượt doanh thu | Cao nếu làm Giai đoạn 3 sớm | Hết vốn | Không sang Giai đoạn 3 trước cổng Giai đoạn 1 |

---

## 5. Việc nhỏ nên làm sớm vì rẻ

Không thuộc giai đoạn nào, nhưng làm sớm thì đỡ phiền:

- Bật `omni-hitl-dispatcher` (1 lệnh) — đang tắt, khách hàng sẽ cần
- Nạp `idx:k8s_expert` và bật semantic cache — giảm tải LLM, đã dựng sẵn hạ tầng
- Dọn `idx:itops_sop_ledger_v2` (0 docs) nếu là index chết
- Redirect apex `omnisre.xyz` → `www` (xem runbook)
- Xoá Deployment `nginx-test` nếu không còn dùng
- Quyết định số phận 4 file untracked ở gốc repo mang tên khách hàng thật

---

## 6. Cách dùng tài liệu này

**Trạng thái ở §1 hết hạn nhanh.** Trước khi tin, chạy lại:

```bash
bash cloudflare/tunnel/verify.sh                       # mặt public
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
kubectl -n multi-agent get deploy                      # replica thật
kubectl -n multi-agent exec redis-0 -- redis-cli FT._LIST
```

Tài liệu này **cố ý không có ngày tháng cho từng giai đoạn**. Giai đoạn 1 quyết định
mọi thứ phía sau, và thời gian của nó phụ thuộc vào việc tìm được khách hàng — thứ
không lập kế hoạch được. Đặt deadline giả cho nó chỉ tạo áp lực sai chỗ.

Bài học đã trả giá trong dự án này, áp dụng cho mọi giai đoạn: **"test pass + push"
không chứng minh đã deploy**, và **"deploy thành công" không chứng minh code mới đang
chạy**. Mọi cổng ra ở trên phải kiểm chứng trên hệ thống đang chạy, không phải trên
CI hay trên tài liệu.

## Tham chiếu

`docs/architecture/ASSESSMENT_autonomous_sre_v2.md` (audit 18 domain 2026-07-22 — lưu ý
2 finding CRITICAL trong đó đã đóng, xem §1) · `docs/adr/0001-cloudflare-pages-tunnel-local-core.md` ·
`docs/runbooks/cloudflare-public-access.md` · `docs/product/PRODUCT_PROOF.md` · `CLAUDE.md`

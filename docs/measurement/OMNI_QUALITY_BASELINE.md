# Omni — Sổ đo chất lượng & Đánh giá tính khả thi

> **File này là bản ghi DUY NHẤT để đối chiếu qua các session.** Mọi phép đo chất lượng,
> mọi kết luận về tính khả thi, mọi con số baseline đều ghi ở đây — không rải ra handoff.
> Handoff (`docs/handoffs/CURRENT_SESSION.md`) ghi *diễn biến phiên*; file này ghi *sự thật
> đo được, có thể so sánh theo thời gian*.
>
> **Quy tắc bắt buộc khi thêm vào đây:** chỉ ghi số **tự chạy ra được**, kèm lệnh tái hiện
> và ngày. Không ghi ước lượng, không ghi "khoảng", không ghi lại claim từ tài liệu khác mà
> chưa tự kiểm. Nếu một biến thay đổi giữa hai lần đo, **ghi rõ là không quy kết được**.

Cập nhật lần cuối: **2026-08-17** (phiên Đ74)

---

## 1. Tình trạng dự án — đánh giá thẳng

Quy mô (đo `2026-08-17`): `src` **96.808 dòng** Python, `tests` **110.248 dòng**, **84** manifest
K8s, **581** commit, commit đầu **2026-04-02** ⇒ **4 tháng rưỡi**.

### Đã chứng minh sống, có log thật
- Vòng kín đầy đủ: agent → Kafka → baseline 3σ tự học (z=3.739) → phán critical → ReAct 8 lượt
  → CRAT ký → Telegram.
- 5/9 domain có bằng chứng lỗi thật: `os_host`, `service`, `network`, `database`, `kubernetes`.
  `security` chạy tới correlator + CRAT qua drill. `hardware` = 0 collector (giới hạn kiến trúc).
- Hạ tầng platform thật: Gitea→Jenkins→Harbor→ArgoCD selfHeal→Argo Rollouts, Vault+ExternalSecret,
  Istio, Dex OIDC, portal provider/tenant, domain thật `omnisre.xyz`.
- CRAT fail-closed: hash-chain SHA-256 + Ed25519, bắt buộc ghi xong mới được emit/dispatch.

### Chưa từng được chứng minh
- **Chưa có một sự cố nào không phải do chính mình dàn dựng.** Mọi drill đều tự tạo.
- `case_ledger` 305 dòng nhưng **domain=unknown 305/305**.
- `playbook` 0 dòng.
- **0 khách hàng thật, 0 số đo precision/recall ngoài đời.**

### Ba trục khả thi
| Trục | Đánh giá 2026-08-17 |
|---|---|
| Kỹ thuật | ✅ Khả thi, phần lớn đã xong, không còn rủi ro lớn chưa biết |
| Vận hành | ⚠️ Chưa. Chưa chạy quá 4 ngày mà không tự mù. Single-node mọi tầng |
| Thương mại | ❌ Chưa kiểm chứng. Đây là rủi ro lớn hơn tất cả phần còn lại cộng lại |

### Hai thứ thật sự khác biệt (đã xây xong)
1. **CRAT** — sổ cái ký số, hash-chain, fail-closed, ghi mọi quyết định AI theo khung SOX §404 /
   PCI-DSS v4.0. Đối thủ AIOps đưa gợi ý rồi quên.
2. **`INV_DATA_RESIDENCY`** — hash-on-arrival, chỉ metadata ≤2000 ký tự rời hạ tầng khách.

Định vị khả thi nhất: **"AIOps có bằng chứng ký số, dữ liệu không rời datacenter của bạn"** cho tổ
chức bị quản chế — không cạnh tranh Datadog, mà cạnh tranh "một anh SRE trực đêm + file Excel".

---

## 2. Bảng đo chất lượng advisory — nguồn đối chiếu chính

Harness: `tests/benchmarks/run_advisory_benchmark.py`, dataset `tests/benchmarks/advisory_golden/`
(**23 case**), kết quả thô lưu `tests/benchmarks/results/benchmark_<ts>.json`.

Thang điểm 100/case: verdict 30 · root_cause_keywords 20 · no_hallucination 20 · remediation 15 ·
verification_steps 15. `pass` = score ≥ 70.

| Ngày | Model | num_predict / ctx | Pass rate | avg_score | Parse fail | File |
|---|---|---|---|---|---|---|
| 2026-07-16 | `qwen2.5-coder:7b` (Ollama) | 512 / 4096 | 43.5% (10/23) | 69.7 | 0 | `benchmark_20260716_155810.json` |
| 2026-08-17 | `llama-3.1-8b` (NIM) | 512 / 4096 | **13.0% (3/23)** | 14.1 | **19/23** | `benchmark_20260817_085814.json` |
| **2026-08-17 09:02** | **`llama-3.1-8b` (NIM)** | **1024 / 8192 = production** | **65.2% (15/23)** | **73.6** | **0** | `benchmark_20260817_090230.json` |
| **2026-08-17 09:30** | **`llama-3.1-8b` (NIM)** | **1024 / 8192 = production** | **56.5% (13/23)** | **73.8** | **0** | `benchmark_20260817_093018.json` |

**Con số production hiện hành: avg_score ~73.7, pass-rate dao động 56–65%.**

### ⭐ Bằng chứng quyết định: pass-rate nhiễu, avg_score ổn định
Hai dòng cuối là **cùng model, cùng tham số, chạy cách nhau 28 phút** — tức mọi biến đều giữ
nguyên, chỉ còn ngẫu nhiên của LLM:

| Chỉ số | 09:02 | 09:30 | Biến động |
|---|---|---|---|
| pass_rate | 65.2% | 56.5% | **−8.7 điểm** |
| avg_score | 73.6 | 73.8 | **+0.2 điểm** |

**pass-rate lệch gấp ~43 lần avg_score trên cùng dữ liệu.** Nếu gate đặt trên pass-rate thì lần
chạy 09:30 đã ĐỎ OAN, trong khi hệ thống thực tế nhỉnh hơn một chút. Đây là lý do thực nghiệm
(không phải lý thuyết) để `baseline.json` gate trên `avg_score`, và để mọi báo cáo về sau trích
avg_score trước, pass-rate sau.

### Lệnh tái hiện
```bash
export KUBECONFIG=~/.kube/config
export OMNI_NIM_API_KEY=$(kubectl get secret omni-nim-secret -n multi-agent \
    -o go-template='{{index .data "api_key"|base64decode}}')
export OMNI_LLM_PROVIDER=nim OMNI_NIM_RATE_LIMIT_RPM=40 PYTHONPATH=src
export BENCHMARK_NUM_PREDICT=1024 BENCHMARK_NUM_CTX=8192   # = giá trị production
.venv/bin/python tests/benchmarks/run_advisory_benchmark.py \
  --model meta/llama-3.1-8b-instruct \
  --llm-url https://integrate.api.nvidia.com/v1
```

### Trung bình từng tiêu chí
| Tiêu chí | qwen2.5-coder @512 | llama-3.1-8b @1024 | Δ |
|---|---|---|---|
| verdict | 13.0/30 | 14.3/30 | +1.3 |
| root_cause_keywords | 11.3/20 | 12.6/20 | +1.3 |
| no_hallucination | 19.4/20 | 19.7/20 | +0.3 |
| remediation | 12.2/15 | 13.3/15 | +1.1 |
| verification_steps | 13.8/15 | 13.7/15 | −0.1 |
| **tổng** | **69.7** | **73.6** | **+3.9** |

---

## 3. Kết luận rút ra từ bảng trên

### 3.1 Truncation JSON một mình ăn ~52 điểm pass-rate
So sánh **sạch biến duy nhất**: cùng model, đổi đúng `num_predict` 512→1024 ⇒ 13.0% → 65.2%.
Output thô xác nhận JSON đứt giữa chừng: `"impact_chain": [ { "cause": "...", "me` ← đứt.
Nội dung vốn ĐÚNG, chỉ vượt trần token. `qwen2.5-coder` (model code) viết súc tích nên lọt;
`llama-3.1-8b` viết dài nên vỡ. **Đổi model kích hoạt một giới hạn có sẵn.**

### 3.2 KHÔNG quy kết được model nào giỏi hơn
`qwen@512` vs `llama@1024` đổi **hai biến cùng lúc**. Muốn so model phải có `qwen@1024`, nhưng
qwen sống trên Ollama/MacBook đang tắt. **Giới hạn thật của phép đo, không lấp liếm.**

### 3.3 Pass-rate ở ngưỡng 70 là chỉ số tồi — có bằng chứng số học
Trung bình nhích **+3.9** điểm nhưng pass-rate nhảy **+21.7** điểm, vì quá nhiều case nằm sát vạch
70. ⇒ **Dùng `avg_score` + thang 0–4 theo case, bỏ đạt/trượt nhị phân.**

Thang 0–4 đề xuất: 0 sai/gây hại · 1 chung chung không hành động được · 2 đúng khu vực sai chi tiết
· 3 đúng root cause · 4 đúng root cause + fix chạy được.

### 3.4 Lỗi severity nằm ở PROMPT, không phải model
`verdict` là tiêu chí yếu nhất (14.3/30 = 48%) và sai **một chiều tuyệt đối**: 9 case đánh giá
**nhẹ hơn** thực tế, **0 case đánh giá nặng hơn**. Cùng thiên lệch ở **hai model kiến trúc khác
hẳn nhau** ⇒ nguyên nhân ở định nghĩa severity trong prompt. Lệch về phía nguy hiểm nhất (bỏ sót).

### 3.5 Điểm mạnh có thật, xác nhận qua 2 model
`no_hallucination` 19.7/20; cổng chống bịa đặt chặn thật ở case_009
(`advisory_grounding_gate_fired ungrounded=['omni-llm']`). **Hệ thống không bịa — nó đánh giá nhẹ tay.**

### 3.6 n=23 là quá nhỏ
Biến động run-to-run lớn (case_023 −30, case_016 −25, case_002 −16.7 dù xu hướng chung cải thiện).
Cần **60–100 case**, hoặc chạy lặp lấy trung bình, mới so hai lần chạy tin cậy được.

---

## 4. Bệnh nền: hệ thống suy giảm có tín hiệu nhưng không ai nhận

Đây là mẫu hình lặp lại, quan trọng hơn bất kỳ con số đơn lẻ nào:

| Sự cố | Thời gian im lặng | Có tín hiệu không? |
|---|---|---|
| 0 agent kết nối | **4 ngày** | Có (Redis rỗng, log gateway rỗng) — không ai đọc |
| RAG `omni:rag:sop` rỗng | **~7 ngày** | Có (HLEN=0) — không ai đọc |
| Benchmark trượt 43.5% | **> 1 tháng** | Có (file JSON) — `\|\| true` nuốt mất |
| `case_ledger.domain=unknown` 305/305 | Không rõ | Có — không ai truy vấn |
| LLM rời MacBook sang NIM | Không rõ | Không có entry handoff nào ghi |

**Chỉ số cần xây: TTDOB (Time To Detect Own Blindness)** — tiêm lỗi vào chính Omni (giết agent,
xoá index RAG, dừng consumer), đo tới lúc nó **tự khai** là đang mù. Giá trị đo được hiện tại:
**không bao giờ**. Mục tiêu: **< 15 phút**.

---

## 5. Kiến trúc đo — 5 tầng

| Tầng | Đo gì | Cần agent thật? | Trạng thái |
|---|---|---|---|
| 0 | Cài đặt: time-to-first-evidence (mục tiêu **<90s**), install success theo distro, capability coverage | Có | ⬜ chưa xây |
| 1 | Phát hiện: precision + recall | Có | ⬜ chưa xây |
| 2 | Chẩn đoán: golden set | **Không** | ✅ có, đang dùng (mục 2) |
| 3 | **Giá trị gia tăng của AI** | **Không** | ⬜ chưa xây — **ưu tiên cao nhất** |
| 4 | TTDOB — điểm mù của chính nó | Một phần | ⬜ chưa xây |
| 5 | Kinh tế: token/ca, chi phí/ca | Không | ⬜ chưa xây |

### Tầng 1 — nguồn ground truth
| Nguồn | Cho ra | Hạn chế nghiêm trọng |
|---|---|---|
| HITL Telegram Đúng/Sai (đã sống từ Đ61) | Precision | Chỉ gán nhãn thứ Omni đã cảnh báo ⇒ **không đo được recall** |
| Chaos injection (13 script ở `scripts/chaos/`) | **Recall** | Chỉ đo lỗi mình nghĩ ra |
| Nhật ký sự cố tự ghi | Recall ngoài đời | Sẽ quên ghi |
| Đối chiếu Prometheus (namespace `monitor`) | Ý kiến thứ hai | Không phải chân lý |

⚠️ **Bẫy lớn nhất: laptop có base rate sự cố ≈ 0.** 14 ngày dogfooding rất có thể cho **0 sự cố
thật** ⇒ recall không xác định, và dễ tưởng "im lặng = tốt". **Bắt buộc ghép dogfooding với chaos.**

### Tầng 3 — phép đo quyết định tính khả thi (chưa chạy)
Cùng bộ case, ba cấu hình:
| | Gồm gì | Trả lời |
|---|---|---|
| A | Ngưỡng tĩnh, không LLM | Bao nhiêu % chỉ cần `if disk > 90`? |
| B | LLM một phát, không RAG/ReAct | LLM thô thêm được bao nhiêu so với A? |
| C | Omni đầy đủ (ReAct 8 lượt + RAG) | **ReAct và RAG có xứng chi phí không?** |

- C ≈ B ⇒ 8 lượt ReAct + RAG đốt token vô ích, cắt xuống 1–2 lượt.
- B ≈ A ⇒ luận điểm "cần LLM" sụp, phải định vị lại sản phẩm.
- C > B > A rõ rệt ⇒ **con số bán hàng đầu tiên của dự án.**

So sánh theo cặp trên cùng case ⇒ chỉ cần ~30 case đã có tín hiệu. **Không cần agent, chạy được ngay.**

### Tầng 5 — trần kinh tế đã biết
`OMNI_NIM_RATE_LIMIT_RPM=40`. Một ca ReAct 8 lượt ≈ 8 request ⇒ **trần cứng ~5 ca/phút**.
Benchmark 60 case × 3 cấu hình = 1440 request ≈ 36 phút chỉ riêng rate limit.

---

## 6. Năm luật chống tự lừa

1. **Không `|| true` cho bất kỳ chỉ số chất lượng nào.**
2. Chỉ số tính từ kho dữ liệu bền mà hệ thống **không sửa được** (`case_ledger`, chuỗi hash CRAT) —
   không phải script rời hay bảng tính tay.
3. **Mọi kết quả là chuỗi thời gian, không phải lần chạy.** (`results/` đã có cơ chế nhưng ngừng
   ghi từ 2026-07-16 → đó chính là lỗi.)
4. **Người chấm không được là người bị chấm.** Dùng LLM-as-judge thì phải khác model + soi tay 10%.
5. Mọi kết quả **ghi kèm model + config**. Đã làm đúng (trường `model` trong JSON) — giữ.

---

## 7. Kế hoạch giả lập khách hàng thật (Manjaro)

**Bối cảnh mạng đã xác minh 2026-08-17:** phiên Claude chạy **trên chính GCP VM** `omni-k3s-vm`
(Ubuntu 24.04), KHÔNG phải trên máy user. Máy Manjaro của user = `hiendt66` = Tailscale
`100.114.41.59`, **online**; MacBook `macbook-pro-ca-hiendang` **offline**. Manjaro **không chạy
sshd** (`connection refused`) ⇒ Claude không push cài đặt được, **user phải tự chạy lệnh** — đúng
bằng cách khách hàng thật cài.

### Vì sao Manjaro tốt hơn lab OrbStack
| | OrbStack lab | Manjaro |
|---|---|---|
| Distro | Ubuntu/Debian, cùng họ môi trường dev | **Arch-based, chưa từng test** |
| Tải | Tự dàn dựng | Thật, hỗn loạn |
| Người phản hồi | Không ai | **User — biết ngay nếu cảnh báo sai** |

### One-command install: hạ tầng đã có sẵn 90%
| Có sẵn | Vai trò |
|---|---|
| `POST /webhook/agent/enroll` (`src/gateway/routes/agent_enroll.py:63`) | **Không cần API key** — token là credential. One-time, single-use trong 1 transaction PG, rate-limit theo IP. Trả `tenant_id` + `api_key` |
| `POST /autonomy/tenants/{tid}/enroll-tokens` | Phát token |
| `GET /webhook/agent/release/bundle` (`agent_commands.py:370`) | Gateway stream sẵn tarball |
| `GET /webhook/agent/versions` + `scripts/publish_agent_release.py` | Manifest + sha256 để verify |

Đích đến: `curl -sSL https://gateway.omnisre.xyz/install.sh | sudo sh -s -- --token ENR_xxx`
Thiếu đúng 2 mảnh: **route `GET /install.sh`** và **tầng dò môi trường**.

### 4 hardcode chặn Manjaro (đã tìm, CHƯA sửa)
| File:dòng | Hardcode | Hậu quả trên Arch |
|---|---|---|
| `src/remote_agent/settings.py:43` | default `/var/log/syslog` | Chỉ có journald ⇒ **domain `application` mù, im lặng** |
| `src/remote_agent/discovery.py:158` | `dpkg -l` | Inventory gói rỗng |
| `src/remote_agent/pkg_origin.py:18,49-58` | chỉ dpkg/rpm | **Mọi unit → `ORIGIN_UNKNOWN`**, mất phân biệt "app khách" vs "gói OS" |
| `scripts/aoip-agent.service` | `SupplementaryGroups=adm utmp` | Arch thường không có `adm` ⇒ service refuse-to-start |

### Chống hardcode: Host Capability Profile
Một tầng dò năng lực duy nhất, chạy lúc cài + tự kiểm lúc runtime. Không có default kiểu Debian —
`/var/log/syslog` là *một khả năng dò ra*, không phải mặc định. Dò không ra ⇒ `unavailable` **kèm
lý do, không im lặng**. Profile **gửi lên Omni như evidence** ⇒ Omni biết điểm mù của nó trên từng
host. Đây chính là thuốc cho bệnh ở mục 4.

### Test hiệu quả — 4 tầng, rẻ dần từ dưới lên
| Tầng | Chạy ở đâu | Bắt được gì | Chi phí |
|---|---|---|---|
| 1 | pytest + fixture | Logic dò: `/etc/os-release` giả + binary giả → assert profile, ~10 distro | ~0 |
| 2 | `docker run` trên GCP VM | Installer **thật** `--dry-run` trên `archlinux`/`debian:12`/`rockylinux:9`/`alpine:3` | vài phút |
| 3 | podman `--systemd=always` | Cài thật, enable service thật, đo time-to-first-evidence | ~10 phút |
| 4 | Máy Manjaro | Acceptance thật, người dùng thật | 1 lần |

`archlinux:latest` = base của Manjaro ⇒ **tầng 2 dự đoán được máy user trước khi động vào.**
Thêm 2 test gần như không ai viết: **idempotency** (cài → cài lại = no-op) và **uninstall sạch**.

### An toàn khi cài lên máy cá nhân
- Dùng **tenant mới** (vd `dogfood`) ⇒ tự động nằm ngoài
  `OMNI_LAB_AUTO_EXECUTE_AGENTS=loyalty-uat_*` — an toàn theo cấu trúc, không nhờ trí nhớ.
- `AOIP_AGENT_MODE=observe_only` (unit đã mặc định) + tier `shadow`.
- ⚠️ **Riêng tư:** `src/remote_agent/collectors/logs.py:115` gửi `"sample": errors[-3:]` ⇒ **log
  thô rời khỏi máy**. `INV_DATA_RESIDENCY` chỉ hash *tài liệu*, không áp cho log.
  `ProtectHome=true` chặn `/home`, nhưng vẫn nên thu hẹp `OMNI_AGENT_LOG_PATHS`.
- ⚠️ Laptop ≠ server: suspend/resume tạo lỗ hổng metric giống bất thường; build project làm CPU
  vọt 3σ. **Đó là phép đo false-positive rate, không phải bug cần giấu.**

---

## 8. Việc đã làm / còn lại

### ✅ Đã làm (2026-08-17, Đ74)
- Đo lại benchmark trên NIM, ghi 2 mốc time-series.
- **Vá `finish_reason`**: `src/llm/vllm_client.py` thêm `TRUNCATED_FINISH_REASONS` +
  `_note_finish_reason()`, gắn vào **cả 3 đường trả về** (OpenAI-compat non-stream, stream,
  Ollama native `done_reason`). `src/workers/advisory_analyst_handler.py` phân biệt
  `advisory_analyst_truncated` với `advisory_analyst_parse_failed`.
  Test: `tests/test_llm_finish_reason.py` (10 test). Verify thật ở 512 token:
  ```
  event=llm_response_truncated ... finish_reason=length max_tokens=512 hint=tăng num_predict
  event=advisory_analyst_truncated trace=diag-case_001 num_predict=512 hint=tăng OMNI_ADVISORY_NUM_PREDICT
  ```

**(c) Dựng gate chống thụt lùi — mục 2, XONG.** Hoá ra không phải MỘT mà **BA khiếm khuyết chồng
nhau** khiến `make benchmark-advisory` không đo được gì suốt hơn một tháng:
1. `|| true` ở `Makefile:258` nuốt sạch kết quả.
2. **Nhãn cũ ghi "live LLM benchmark … requires OMNI_OLLAMA_BASE_URL" là SAI** —
   `test_benchmark_pass_rate` dùng `_FakeLLMClient`, **không hề gọi model thật**. Nó là self-test
   của bộ chấm điểm, không phải phép đo chất lượng. Tức **chưa từng có gate chất lượng thật nào**.
3. Mã thoát `run_advisory_benchmark.py` là `0 if passed == total else 1` — **đòi 23/23 case hoàn
   hảo**, nên lần chạy tốt nhất từng đo (65.2%) vẫn thoát 1. Không dùng làm gate được.

Đã sửa cả ba:
- `Makefile`: bỏ `|| true`, cả 2 bước không-cần-LLM nay **chặn** (165 test, pass); sửa nhãn cho
  đúng sự thật; thêm target **`benchmark-advisory-live`** ghim sẵn tham số production
  (`BENCHMARK_NUM_PREDICT=1024 BENCHMARK_NUM_CTX=8192`) — thiếu key thì **báo SKIP rõ ràng và
  thoát khác 0**, không giả vờ PASS.
- `run_advisory_benchmark.py`: thêm `no_advisory_count` + `num_predict`/`num_ctx` vào report;
  chạy trần nay thoát 0; cờ `--gate` mới so với baseline.
- `tests/benchmarks/baseline.json` (MỚI): baseline đo thật 2026-08-17.
- `tests/test_advisory_benchmark_gate.py` (MỚI): 11 test.

**Thiết kế gate — CỐ Ý là gate CHỐNG THỤT LÙI, không phải gate chất lượng tuyệt đối.** Lý do:
mục 3.3 đã chứng minh pass-rate ở ngưỡng cố định nhiễu cực mạnh; một gate tuyệt đối đặt trên nó sẽ
đỏ/xanh ngẫu nhiên rồi bị vô hiệu hoá — đúng số phận của `|| true` cũ. Chất lượng tuyệt đối là mục
tiêu sản phẩm; CI chỉ nên chặn thụt lùi. Hai điều kiện:
| Điều kiện | Ngưỡng | Vì sao |
|---|---|---|
| `no_advisory_count` | **> 0 là đỏ** | Tín hiệu hạ tầng/cấu hình, gần như nhị phân, ít nhiễu nhất. Chính chỉ số này nhảy 0→19/23 khi đổi sang NIM |
| `avg_score` | < `73.6 − 5.0` là đỏ | Ổn định hơn pass-rate nhiều; biên dung sai vì biến động run-to-run có thật |

Test `test_the_actual_nim_regression_would_have_been_caught` tái hiện đúng số của lần chạy hỏng
(avg 14.1, 19 case không advisory) và khẳng định gate đỏ vì **cả hai** lý do.

⚠️ **Không hạ baseline để CI xanh.** Chỉ cập nhật `baseline.json` khi có lần đo MỚI TỐT HƠN, và
phải ghi kèm vào chính file này.

### ⬜ Còn lại, theo thứ tự đã thống nhất
3. Sửa thiên lệch severity trong prompt (nguyên nhân đã quy kết: prompt, không phải model).
4. Tầng dò năng lực host + fixture ~10 distro.
5. Container matrix dry-run (`archlinux` = base Manjaro).
6. Route `GET /install.sh` trên gateway.
7. Cài lên Manjaro, đo time-to-first-evidence.

### Chưa có khuyến nghị rõ / cần quyết định riêng
- Tầng 3 (so A/B/C) — **giá trị cao nhất, không cần agent, nên làm sớm.**
- TTDOB / dead-man's switch.
- Mở rộng golden set 23 → 60–100 case, nuôi bằng ca thật từ `case_ledger`.
- Golden case còn trường `"lane": "SYS_HARD_FAIL"` — trục lane đã gỡ từ Đ39, **dataset lệch schema**.

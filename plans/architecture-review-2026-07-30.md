# Soi lại kiến trúc — 3 mục, khảo sát runtime + code

> Chưa sửa gì. Đây là báo cáo khảo sát, chờ quyết định của chủ hệ thống.

## Mục 1 — 4 lane có còn hợp lý?

**Không.** Lý do cụ thể, không phải cảm tính:

`classify_event(ev: AnomalyEvent, matrix: DiagnosticMatrixFile)` — lane được gán bằng
cách phân loại một **AnomalyEvent**. Nghĩa là lane là thuộc tính của MỘT CÁI ALERT,
không phải của hệ thống khách. Đúng như nhận định: mindset alert-driven.

Và nay có **hai trục xung đột**:

| trục | số | bản chất |
|---|---|---|
| lane | 4 | SYS_RESOURCE · SYS_HARD_FAIL · APP_HTTP · SIEM_SECURITY |
| domain (vừa hợp nhất) | 9 | kubernetes · os_host · network · storage · database · service · application · security · hardware |

**4 lane không diễn đạt được 5/9 domain**: không có lane nào cho `network`, `storage`,
`database`, `hardware`, `service`. Ta vừa khai 99 lệnh chẩn đoán phủ 9 domain, nhưng
pipeline chẩn đoán chỉ có 4 cửa vào và không cửa nào là mạng/đĩa/DB/phần cứng.

Nên câu hỏi không phải "4 lane có hợp lý" mà là "lane còn nên là một taxonomy?".

## Mục 2 — ReAct loop: có, nhưng gắn sai chỗ

ReAct tồn tại ở **ba** nơi, và cả ba đều KHÔNG nằm trên đường bằng chứng:

| hàm | dùng để | chạy khi |
|---|---|---|
| `run_agentic_mutate_plan` | lập kế hoạch **mutation** | có mutation |
| `run_post_verify_react_loop` | nghiệm thu **sau** mutation | sau mutation |
| `agentic_slow_path_with_llm_and_tools` | điều tra read-only nhiều bước | `handle_inbound_payload` — **đường CHAT**, khi người hỏi |

Hệ quả: ở `shadow` (không mutation), vòng ReAct **chỉ chạy khi có người nhắn tin**.
Không có vòng điều tra tự động nào chạy khi bằng chứng tới.

### Đường dữ liệu thô: tính z-score rồi bỏ

`knowledge_pipeline._handle_metric_sample()`:
```
update_remote_host_baseline(...) → zscores
if zscores:
    logger.debug("metric_sample host=%s zscores=%s", ...)   # ← hết
```
Cập nhật baseline 3σ, tính z-score, **ghi log DEBUG, không làm gì nữa**. Không dựng
bất thường, không vào pipeline.

Trong khi `remote_agent_pipeline.py:192` thì CÓ hành động:
```
is_anomalous = result == "FAILED" or any(abs(v) > 3.0 for v in zscores.values())
```
Nhưng đường đó chỉ chạy cho envelope **agent đã dán nhãn `ANOMALY`**.

### Ai quyết định "bất thường"? — Agent, bằng ngưỡng tĩnh

`agent.py:152` — *"Anomaly thresholds pushed by Omni (omni_admin runtime flags)"*.
Omni đẩy **ngưỡng số** xuống, agent so sánh rồi tự dán `PASSED`/`FAILED`. Đây là
ngưỡng cấu hình, KHÔNG phải kiến thức/kinh nghiệm/hiểu biết của Omni về hệ thống đó.

Và `INV_KNOWLEDGE_NOT_ALERT` ghi rõ: non-ANOMALY signal **không RAG, không LLM**.

**Kết luận mục 2:** luồng hiện tại **ngược** với luồng mong muốn. Agent phán bất
thường bằng ngưỡng tĩnh; phần Omni tự phân tích trên dòng dữ liệu thường xuyên thì
tính xong rồi bỏ. "Omni theo dõi, phân tích bất thường dựa trên kiến thức và hiểu biết
về hệ thống khách" — chưa xảy ra.

## Mục 3 — Omni và agent làm việc trong khuôn khổ khách: hai hệ thống rời nhau

| hệ thống quyền | phạm vi | ai đặt |
|---|---|---|
| `scope_grant` (sổ ca, mới) | theo `pattern_key`, **per-tenant** | admin tenant duyệt đơn Omni xin |
| `MUTATE_TOOL_ALLOWLIST` | theo tên tool K8s | hardcode trong code |
| catalogue lệnh chẩn đoán | theo tên lệnh, **toàn tiến trình** | file YAML + env |
| ngưỡng anomaly agent | theo metric, per-tenant | `resolve_agent_thresholds` |

Bốn hệ thống, **không cái nào tham chiếu cái nào**.

Điểm hỏng cụ thể: `OMNI_DIAG_COMMAND_CATALOG` là **biến môi trường của tiến trình**,
nên trong một deployment **không thể** cho khách A tập lệnh hẹp hơn khách B. Khuôn khổ
lệnh chẩn đoán hiện KHÔNG per-tenant, dù mọi thứ khác của sản phẩm đều multi-tenant.

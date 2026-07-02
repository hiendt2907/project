# Quota / Resume Protocol

## Khi nào kích hoạt

Khi Claude Code hiển thị usage ~90%, còn ~10%, hoặc bất kỳ cảnh báo "gần usage limit" nào (chính
xác hay không chính xác) → chuyển ngay `status = QUOTA_DRAINING` trong
`docs/operations/AUTONOMOUS_LOOP_STATE.json`. KHÔNG bắt đầu iteration mới sau thời điểm này.

Nếu CLI chỉ đưa cảnh báo dạng text (không có số % chính xác):

```json
"quota": {
  "usage_percent_reported": null,
  "reset_source": "<nguyên văn cảnh báo CLI hiển thị>",
  "reset_at": null
}
```

KHÔNG bịa số % hoặc thời điểm reset chính xác nếu CLI không cung cấp.

## Dùng quota còn lại để

1. Hoàn tất bước hiện tại nếu an toàn (không bắt đầu migration/build/deploy mới nếu khó hoàn thành
   trong quota còn lại).
2. Nếu acceptance của iteration hiện tại đã pass: final verify → docs → commit.
3. Nếu acceptance CHƯA pass: giữ trạng thái resumable, KHÔNG tuyên bố DONE, ghi rõ `hypothesis` và
   `next_step` chính xác (không mơ hồ) vào state JSON.
4. Verify trước khi checkpoint: git state, HEAD, safety (`auto_execute_enabled`), pod health, image
   digest, Kafka lag, Redis/DB, Agent health.
5. Cập nhật `docs/handoffs/CURRENT_SESSION.md`, state JSON, và append
   `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`.
6. Xác định reset time (xem dưới).
7. Sleep tới reset + buffer.
8. Resume: verify lại reality từ đầu.
9. Tiếp tục iteration.

## Xác định reset time — thứ tự ưu tiên

1. Reset timestamp chính xác mà Claude Code CLI hiển thị.
2. Duration (vd "resets in 2h30m") mà CLI hiển thị → cộng vào thời điểm hiện tại.
3. Supervisor fallback được cấu hình thủ công (biến môi trường `OMNI_LOOP_FALLBACK_RESET_MINUTES`
   hoặc giá trị mặc định bảo thủ trong `scripts/calculate_sleep.py`).

KHÔNG bịa reset time nếu không có nguồn nào ở trên. Normalize về UTC ISO8601. Buffer mặc định 120
giây (`quota.buffer_seconds` trong state JSON).

## Sleep — hai lớp

### Primary — same-process sleep

Nếu môi trường tool cho phép block trong tool call:

```bash
python3 .claude/skills/omni-autonomous-productizer/scripts/calculate_sleep.py --sleep
```

Script tự tính `remaining = reset_at - now + buffer_seconds`, clamp về 0 nếu âm, sleep đúng số
giây đó. Sau khi wake: đổi `status = RESUMING`, đọc lại toàn bộ source of truth, verify
git/runtime/safety, chạy `resume_checks` trong state JSON, tiếp tục `next_step`.

**Giới hạn thật**: tool-call trong harness này có thể có timeout — nếu thời gian ngủ vượt quá
timeout khả dụng, primary sleep sẽ bị cắt giữa chừng. Bootstrap report PHẢI nêu rõ giới hạn này nếu
gặp phải, và fallback sang supervisor.

### Fallback — external supervisor

`scripts/supervisor.sh` là tiến trình NGOÀI phiên Claude Code hiện tại (chạy qua `nohup`/cron/
launchd), có nhiệm vụ THUẦN orchestration:

```
Claude invocation → state inspection → sleep → resume
```

Supervisor process (bash script) **không được** tự: sửa code, deploy, xóa resource, bật
auto-execute, commit, push — nó chỉ gọi `claude -p` rồi để invocation đó tự làm mọi thứ theo skill.
Mọi thay đổi sản phẩm phải do Claude (qua skill) thực hiện.

⚠️ **Override đã kích hoạt (2026-07-02, user xác nhận tường minh 2 lần)**: các invocation `claude -p`
trong `supervisor.sh` chạy với `--dangerously-skip-permissions` vì `-p` không có TTY để duyệt
permission — không có flag này, mọi Edit/Write/Bash bị auto-deny và loop hot-loop vô ích (đã quan
sát thật: nhiều iteration liên tiếp bị chặn ở `src/gateway/routes/autonomy.py`, 0 tiến triển). Đây
LÀ NGOẠI LỆ so với quy tắc mặc định "không dùng --dangerously-skip-permissions" bên dưới — chỉ áp
dụng cho invocation của riêng supervisor.sh, không áp dụng cho phiên tương tác thường.
`OMNI_AUTO_EXECUTE_ENABLED=false` là kill-switch riêng, không bị ảnh hưởng.

Yêu cầu với supervisor.sh:
- Lock file chống chạy hai instance song song.
- Không cần secret trong script.
- Đọc `docs/operations/AUTONOMOUS_LOOP_STATE.json`.
- Nếu `status == IDLE` → gọi ngay `claude -p "/omni-autonomous-productizer one-iteration"` (đây là
  trạng thái an toàn duy nhất để tự mở iteration mới — `one-iteration` tự checkpoint về IDLE hoặc
  QUOTA_DRAINING/SLEEPING khi xong, không tự mở iteration thứ hai, nên gọi lặp lại từ supervisor là
  cách thật sự "drive" loop 24/7).
- Nếu `status == SLEEPING_UNTIL_QUOTA_RESET` → sleep tới `reset_at + buffer_seconds`.
- Sau khi sleep xong, gọi `claude -p "/omni-autonomous-productizer resume"` (verify flag `-p` tồn
  tại bằng `claude --help` trước khi hard-code — ĐÃ verify trong bootstrap: `-p/--print` tồn tại,
  `-c/--continue` và `-r/--resume [id]` cũng tồn tại; KHÔNG có `--max-turns`).
- Nếu `status == BLOCKED_FOR_HUMAN` → thoát, không tự resume.
- Nếu `status in (STOPPED, COMPLETED)` → thoát.
- Nếu state JSON invalid (`validate_state.py` exit non-zero) → dừng, log lỗi rõ ràng.
- Log vào `.autonomous-loop/logs/`.
- Dùng `caffeinate` trên macOS nếu có sẵn (tránh sleep hệ điều hành trong lúc chờ).
- `--dangerously-skip-permissions` chỉ dùng trong invocation `claude -p` (xem override ở trên) —
  KHÔNG tự thêm flag tương đương ở nơi khác trong script.
- Backoff khi gặp quota/rate-limit lặp lại (không retry nhanh liên tục).
- `trap` cleanup lock file khi thoát (kể cả SIGINT/SIGTERM).
- Tự kiểm tra không có supervisor khác đang chạy trước khi start.

## Checkpoint trước sleep (nội dung bắt buộc trong CURRENT_SESSION + ledger)

branch, HEAD, iteration, phase, bottleneck, completed steps, incomplete steps, tests, deployment,
runtime proof, product proof, last command, last failure, hypothesis, next step, working tree,
unrelated files, safety state, health, reset time, resume checks chính xác.

Ledger entry format:

```
Timestamp:
Iteration:
Quota state:
HEAD:
Acceptance:
Last verified:
Pending:
Reset at:
Resume action:
```

State: `status = SLEEPING_UNTIL_QUOTA_RESET`.

Output marker cuối câu trả lời (để harness/human dễ nhận biết): `AUTONOMOUS_LOOP_SLEEPING_UNTIL_QUOTA_RESET`

## Sau khi wake/resume — luôn làm lại từ đầu

```bash
date -u
git status
git branch --show-current
git rev-parse HEAD
kubectl get deploy,pod,svc -A -o wide
kubectl get events -A --sort-by=.lastTimestamp
orb status
orb list
```

Verify: auto-execute vẫn false, workload images/digests, pod restart count, Redis, Kafka lag,
database, Agent liveness, Twin revision hiện tại, response API sản phẩm gần nhất.

Chạy toàn bộ `resume_checks` đã ghi trong state JSON.

- Nếu reality khớp checkpoint → `status = RUNNING` (hoặc phase tương ứng), tiếp tục đúng `next_step`.
- Nếu reality drift → ghi drift vào ledger, dựng lại Reality Map, đánh giá lại bottleneck đầu tiên,
  KHÔNG tiếp tục hypothesis cũ một cách mù quáng.

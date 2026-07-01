# Claude Session Automation

Tự động lưu & phục hồi ngữ cảnh công việc giữa các Claude Code session. Sau `/clear`,
compact, đóng terminal hay mở session mới, Claude tự đọc đúng trạng thái repository và
tiếp tục từ Next step — không cần người dùng nhắc lại lịch sử.

**Nguyên tắc cốt lõi:** repository artifacts là source of truth. Không phụ thuộc
conversation history hay memory ngoài repository.

## Kết quả khảo sát

- **Claude Code:** 2.1.197.
- **Schema hook đã xác minh (local):** `settings.json → hooks.<Event>[].hooks[] = {type:"command", command, timeout}`; `matcher` optional cho session events.
- **Events dùng:** `SessionStart` (source: startup|resume|clear|compact), `SessionEnd` (reason), `PreCompact` (trigger: manual|auto), `Stop` (stop_hook_active để chống recursion).
- **Payload:** JSON qua stdin (`hook_event_name`, `cwd`, `source`/`reason`/`trigger`, `stop_hook_active`, ...). `CLAUDE_PROJECT_DIR` có sẵn.
- **Inject context:** SessionStart đọc `hookSpecificOutput.additionalContext`.
- **Block turn:** Stop đọc `{"decision":"block","reason":...}`.
- **User-level hooks hiện có** (`~/.claude/settings.json`: block-no-verify, tmux, git-push, commit-quality...) — **không bị đụng tới**; chỉ merge vào project `.claude/settings.json`.
- `jq` có sẵn nhưng scripts dùng Python 3 (portable hơn) để build/parse JSON.

## Kiến trúc

```
Claude làm việc → repo thay đổi
   └─(Stop) ensure-handoff.sh ──> block nếu handoff cũ hơn file đã đổi
compact / session end
   └─(PreCompact, SessionEnd) save-session-state.sh ──> .claude/state/last-session.json
session mới / sau /clear
   └─(SessionStart) load-session-context.sh ──> inject handoff + Git state + rules
```

## Hook lifecycle

| Event | Script | Vai trò |
|---|---|---|
| `Stop` | `ensure-handoff.sh` | Nếu turn có thay đổi repo mà handoff cũ → block, yêu cầu cập nhật. Chống loop bằng `stop_hook_active`. Không block turn read-only/không đổi. |
| `PreCompact` | `save-session-state.sh` | Snapshot metadata trước khi compact. |
| `SessionEnd` | `save-session-state.sh` | Snapshot metadata khi phiên kết thúc. |
| `SessionStart` | `load-session-context.sh` | Nạp handoff + Git state + last-session + continuation rules vào context. |

**Freshness của handoff** = so `mtime(handoff)` với `mtime` lớn nhất của các file đã thay đổi
(loại trừ `docs/handoffs/` và `.claude/state/`). Handoff mới hơn ⇒ fresh ⇒ không block.

## File ownership

| Path | Chủ | Commit? |
|---|---|---|
| `.claude/hooks/*.sh` | tooling | ✅ |
| `.claude/settings.json` (hooks) | tooling | ✅ |
| `.claude/state/last-session.json` | runtime | ❌ (gitignored) |
| `.claude/state/.gitkeep` | tooling | ✅ |
| `docs/handoffs/TEMPLATE.md` | tooling | ✅ |
| `docs/handoffs/CURRENT_SESSION.md` | handoff sống | ✅ |

## Security model

- Snapshot **chỉ metadata an toàn**: timestamp, event/reason, branch, HEAD, git status short, 5 commit gần nhất, handoff path + checksum, deliverable line.
- **Không lưu/không log:** transcript, prompt đầy đủ, secret, environment variables, access token, customer raw evidence.
- Payload hook chỉ đọc field cần, **không echo lại**.
- Ghi file **atomic**: mktemp → validate JSON → `mv`. JSON hỏng ⇒ giữ state cũ.
- Ngoài git repo ⇒ fail gracefully (exit 0), không phá session.
- Handoff bị giới hạn 8000 bytes khi inject; quá dài ⇒ cảnh báo + cắt.

## Cách test

```bash
bash tests/claude_hooks/test_session_hooks.sh   # 21 assertions
```

Bao phủ: save-state (branch/commit/JSON/no-secret/atomic/non-git), load-context
(handoff/branch/source/no-transcript/missing/oversized), ensure-handoff
(no-change/missing/fresh/recursion), git edge cases (detached HEAD, filename có space).

## Dùng `/prepare-clear`

Trước khi `/clear`: chạy `/prepare-clear`. Command yêu cầu Claude dừng implement, kiểm tra
Git, cập nhật `CURRENT_SESSION.md` + artifacts liên quan, chạy verification tối thiểu,
báo cáo checkpoint ≤20 dòng, rồi mới an toàn để người dùng gõ `/clear`.

## Hành vi sau `/clear`

Session mới ⇒ `SessionStart` (source=`clear`) ⇒ `load-session-context.sh` inject handoff +
Git state + rules. Claude tiếp tục từ Next step, không cần dán lại lịch sử.

## Giới hạn: không auto-trigger `/clear`

> Save và restore hoàn toàn tự động. Trigger `/clear` vẫn là thao tác thủ công an toàn.

Claude Code 2.1.197 không cung cấp API/hook chính thức để tự động trigger `/clear`.
Tuyệt đối không kill process, spawn Claude lồng nhau, gửi phím vào terminal, hay dùng
AppleScript/expect/tmux để giả lập `/clear`.

## Cách disable

Xoá 4 mục trong `hooks` của `.claude/settings.json` (hoặc block cần bỏ). Scripts và handoff
có thể giữ lại vô hại. Để tắt tạm 1 hook: xoá đúng entry của event đó.

## Cách debug

Chạy tay với payload giả:

```bash
printf '{"hook_event_name":"SessionStart","source":"clear"}' \
  | CLAUDE_PROJECT_DIR="$PWD" bash .claude/hooks/load-session-context.sh
```

Kiểm tra Claude Code có nhận hook: `/hooks` trong phiên, hoặc `claude --debug` xem log thực thi.

## Recovery khi handoff lỗi

- Handoff hỏng/thiếu ⇒ `load-session-context.sh` cảnh báo, session vẫn chạy; tạo lại từ `TEMPLATE.md`.
- Git state mâu thuẫn handoff ⇒ continuation rule buộc Claude **dừng và báo mâu thuẫn** trước khi code.
- `last-session.json` hỏng ⇒ save-state giữ file cũ; có thể xoá an toàn (sẽ tạo lại).

## Cập nhật schema khi nâng Claude Code

Sau khi nâng version: kiểm tra lại tên field (`hookSpecificOutput`, `stop_hook_active`,
`source`/`reason`/`trigger`) và các matcher session events. Nếu schema đổi, cập nhật 3 script
+ mục "Kết quả khảo sát" ở trên, rồi chạy lại `tests/claude_hooks/test_session_hooks.sh`.

## Status-line / context warning

Ngưỡng workflow khuyến nghị (nếu status-line hỗ trợ hiển thị context usage):

```
< 80k       normal
80k–140k    prepare checkpoint
140k–200k   update handoff and clear soon
> 200k      do not start a new feature
```

Claude Code không expose token/context usage qua API chính thức cho hook ⇒ không parser
brittle từ terminal output; đây chỉ là hướng dẫn thủ công.

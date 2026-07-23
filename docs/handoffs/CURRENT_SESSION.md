# Current Session Handoff

## Deliverable hiện tại

Tổng hợp/refactor toàn bộ tài liệu-báo cáo-audit của dự án Omni thành 1 bộ duy nhất, chính xác
trạng thái hiện tại, không rác dự án.

## Definition of Done

- `docs/` chỉ còn file phản ánh đúng kiến trúc/trạng thái hiện tại (đối chiếu `CLAUDE.md`) —
  KHÔNG còn báo cáo/audit/plan mô tả kiến trúc split-role (`omni-prober`/`analyst`/`core`/
  `executor`, RETIRED 2026-07-02), `omni-ui` (RETIRED 2026-07-06), hay `brain-go` (RETIRED
  2026-07-22) mà không có ghi chú rõ đó là lịch sử.
- Không còn pointer chết (dangling link) tới file đã xoá trong `docs/`, `CLAUDE.md`, `AGENTS.md`,
  `MEMORY.md`.
- 3 file index (`docs/README.md`, `docs/DOCUMENTATION_INDEX.md`, `docs/reports/README.md`) phản
  ánh đúng cấu trúc còn lại.
- Không phá vỡ test suite (không file code nào phụ thuộc doc đã xoá).

**Trạng thái: DONE**, verified, CHƯA commit/push (chờ user).

## Trạng thái hiện tại

Hoàn tất. Không còn việc dở dang cho deliverable này.

## Đã hoàn thành

- `docs/`: 248 file / 32MB → 73 file / <1MB. Xoá `docs/vendor/*.html` (31MB cache bên thứ 3),
  `docs/reports/` phase-1..7 + chaos-rag snapshot cũ (giữ 6/~50 file), `docs/post-mortems/` 19/20
  file scratch (giữ `drift-correction-2026-07-02.md`), toàn bộ `docs/audit/`, `docs/analysis/`,
  `docs/acceptance/`, `docs/benchmarks/`, `docs/CODEMAPS/`, 11/17 `docs/runbooks/`,
  `docs/vendor/OMNI_PROJECT_CANONICAL.md` + `knownbase.md` (nội dung ~90%+ mô tả kiến trúc/RAG
  backend đã bị thay hoàn toàn — active-misleading, không chỉ cũ).
- Root repo: xoá `PLAN.md`, `RESTRUCTURE_PLAN.md`, `CONCEPT_MAP.md` (kế hoạch cũ, không ai tham
  chiếu).
- `AGENTS.md` (bản fork lỗi thời của `CLAUDE.md`, lệch ~1 tháng, còn liệt kê role đã RETIRED) →
  viết lại thành pointer mỏng trỏ về `CLAUDE.md`.
- Sửa nội dung (không chỉ xoá) 4 file còn giữ nhưng có pointer chết:
  `docs/omni_playbook_index.md`, `docs/proactive_slo.md`, `docs/mcp_integration.md`,
  `docs/reports/project-memory.md` (thêm banner staleness cho entry cũ).
- Cập nhật skill ngoài repo `~/.claude/skills/omni-lane-operator-loop/SKILL.md` — bỏ tham chiếu 2
  file vendor đã xoá.
- Viết lại hoàn toàn `docs/README.md`, `docs/DOCUMENTATION_INDEX.md`, `docs/reports/README.md`.
- **KHÔNG xoá** cụm 18 file "AOIP Constitution" (`docs/architecture/FRAMEWORK_LAWS.md`,
  `META_MODEL.md`, v.v.) + root `MASTER_PLAN.md` — trông giống sprawl nhưng xác minh
  `src/aoip/__init__.py` (code chạy thật) tham chiếu làm ontology nền; đã ghi chú rõ trong
  `DOCUMENTATION_INDEX.md` Tầng 2 (frozen, không mở rộng).

## Branch và commit

`main`. HEAD `6863601` (docs: audit Autonomous SRE Team 2026-07-22 + handoff port SIEM engine) —
chưa có commit mới trong phiên này.

## Working tree

178 file bị xoá (`D`), 21 file bị sửa (`M`) — toàn bộ trong `docs/` + `AGENTS.md`. Không đụng
`src/`, `tests/`, `k8s/`. **Cộng dồn** với working tree từ phiên trước (SIEM correlation port +
command_executor security follow-up round, 9 file `src/`/`tests/`) — cả hai đều CHƯA commit.

## Files chính đã thay đổi

`docs/README.md`, `docs/DOCUMENTATION_INDEX.md`, `docs/reports/README.md` (rewrite),
`AGENTS.md` (rewrite thành pointer), `docs/omni_playbook_index.md`, `docs/proactive_slo.md`,
`docs/mcp_integration.md`, `docs/reports/project-memory.md` (edit pointer),
`~/.claude/skills/omni-lane-operator-loop/SKILL.md` (edit pointer, ngoài repo).
178 file xoá — xem `git status --short` cho danh sách đầy đủ.

## Quyết định đã chốt

- Xoá hẳn (không archive) — đã hỏi và được user duyệt qua AskUserQuestion. Không thiết kế lại
  quyết định này ở session sau; nếu cần khôi phục 1 file cụ thể, dùng `git log --diff-filter=D`
  + `git show <commit>:<path>` (git vẫn giữ lịch sử, working tree chỉ mới xoá, chưa commit nên
  thậm chí `git checkout -- <path>` khôi phục được ngay nếu cần trước khi commit).
- Cụm "AOIP Constitution" (`FRAMEWORK_LAWS.md` và 17 file liên quan) giữ nguyên vĩnh viễn trừ khi
  có quyết định kiến trúc mới rõ ràng — đây là ontology nền cho `src/aoip/`, không phải sprawl.
- `docs/vendor/*.html` cache: nếu cần lại, chạy `scripts/sync_vendor_docs.py`, KHÔNG khôi phục từ
  git history (bản cache cũ có thể đã lỗi thời so với vendor thật).

## Verification đã chạy

```
grep dangling-reference sweep (docs/ + CLAUDE.md + AGENTS.md + MEMORY.md) → clean
  (2 hit còn lại có chủ đích: ADR-001 dòng 85 mô tả hành động lịch sử đã xảy ra, chính xác dù
  target đã xoá — không sửa)
.venv/bin/python -m pytest tests/ -q --collect-only → 6550/6555 collected, 0 lỗi import
```

## Deployment hiện tại

N/A — thay đổi chỉ ở tài liệu, không đụng runtime/deploy.

## Blockers

None.

## Next step chính xác

User chạy `git status` + `git diff --stat` review 178 xoá / 21 sửa, rồi quyết định commit (gộp
chung 1 commit "docs: consolidate and prune stale documentation", hoặc tách riêng khỏi phần code
SIEM/security đang có sẵn trong working tree từ phiên trước).

## Lệnh cần chạy lại

```
git status --short                        # xem đầy đủ danh sách 199 file thay đổi
git diff --stat -- docs/                   # review chi tiết phần docs
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration   # xác nhận không regression
```

## Không được làm lại

- Đừng audit lại toàn bộ `docs/` từ đầu — danh sách giữ/xoá đã có lý do ghi trong
  `docs/DOCUMENTATION_INDEX.md` và log phiên này.
- Đừng xoá cụm AOIP Constitution — đã xác minh load-bearing.
- Đừng viết lại nội dung đầy đủ vào `AGENTS.md` — giữ là pointer mỏng.
- Đừng khôi phục `docs/vendor/*.html` từ git history.

## Tài liệu liên quan

- `docs/DOCUMENTATION_INDEX.md` — chỉ mục mới, điểm vào duy nhất cho `docs/`.
- `docs/README.md` — tóm tắt cleanup.
- `CLAUDE.md` — nguồn sự thật, không đổi trong phiên này.

# Current Session Handoff

Updated: 2026-07-22 — Port SIEM correlation engine brain-go (Go) → Python DONE (chưa commit).
Audit "Autonomous SRE Team" cùng ngày vẫn còn hiệu lực: `docs/architecture/AUDIT_autonomous_sre_team_2026_07_22.md`.

## Deliverable hiện tại
**Port `omni-brain-go` (Go SIEM correlation engine) sang Python, chạy như async loop trong
`omni-fullstack` — DONE, deployed, cutover hoàn tất, CHƯA commit/push (chờ user).**
Deployment `omni-brain-go` đã xoá khỏi cluster + git (`git rm k8s/deployments/omni-brain-go.yaml`).
Python engine consume `omni-siem-raw` → produce `omni-siem-incidents`/`omni-siem-chains`,
consumer group `omni-siem-correlation`, Redis prefix riêng khi chạy parity (`pycorr-*`).

## Đã hoàn thành
- Engine mới: `src/services/siem_correlation/` (models, entities, decode, confidence, chain,
  graph, config) + `src/workers/siem_correlation_loop.py`, đăng ký trong `omni_worker.py`
  cho role `full`/`analyst`, gate bằng `OMNI_SIEM_CORRELATION_ENABLED` (default False,
  manifest omni-fullstack bật true + comment cảnh báo không chạy song song 2 engine).
- TDD: 68 test mới viết trước (RED→GREEN), 6 file `tests/test_siem_correlation_*.py`.
  Full suite: **6471 passed, 0 failed**.
- Parity Go↔Python: `scripts/siem_correlation_parity.py` chạy trong pod — PASS 2/2
  (27/27 incident envelope khớp từng field; 2/2 chain khớp mọi field trừ chain_id/timestamp;
  tenant `parity-*`, không đụng state Go).
- Cutover: xoá brain-go SAU parity pass → deploy image mới flag=true → group joined →
  e2e post-cutover: 53 event (50 noise + 3 attack) → đúng 1 chain (conf 0.525) → CRAT
  `CHAIN_CORRELATED` signed=True → ChainConsumer `chain_advisory_emitted`.
  `make e2e-incident-matrix` 5/5 PASS.
- Review (sub agent): APPROVE-WITH-FIXES, 0 CRITICAL/HIGH; 1 MEDIUM (2 engine song song cùng
  prefix `corr:*`) đã xử lý bằng thứ tự cutover + comment manifest. Monitor (sub agent):
  verdict CLEAN — 0 chain_rejected/poison/Telegram spam, Redis Go vs Python khớp 144 key 1:1,
  lag 0.
- Cập nhật `CLAUDE.md` (COMPONENT ROLES + DEPLOYMENT STATE) và `~/.claude/skills/omni-siem/SKILL.md`.

## Branch và commit
`main`, HEAD `5b587b8` (chưa push từ trước). Toàn bộ port CHƯA commit.

## Working tree
Modified: `CLAUDE.md`, `k8s/deployments/omni-fullstack.yaml`, `src/workers/omni_worker.py`,
`src/workers/settings.py`, 3 test cũ (thêm field fake settings:
`test_cov_omni_worker_gaps.py`, `test_track1b_worker_kafka.py`,
`test_worker_role_discovery_consumer.py`), `docs/handoffs/CURRENT_SESSION.md`,
`reports/incident-matrix/latest.json` (auto-gen).
Deleted (staged): `k8s/deployments/omni-brain-go.yaml`.
Untracked: `src/services/siem_correlation/`, `src/workers/siem_correlation_loop.py`,
`scripts/siem_correlation_parity.py`, 6 file `tests/test_siem_correlation_*.py`,
`docs/architecture/AUDIT_autonomous_sre_team_2026_07_22.md` (audit session trước, cũng chưa commit).

## Quyết định đã chốt
- Không port Redis-stream transport / legacy single-key correlator (chưa từng deploy).
- Không sửa ChainConsumer trong scope này (finding #3 `_cohesion` fail-open giữ nguyên).
- 2 LOW deferred: tenant_id không escape trong Redis key (parity-faithful với Go, trust
  boundary ở Kafka ACL/gateway); `max_severity` dead-code Go không port.
- Tie-order nuance (không phải bug): event cùng 1 giây → sequence-score phụ thuộc sort tie
  (Go unstable = ngẫu nhiên, Python stable = newest-first bảo thủ); parity script giãn 1.5s
  để deterministic.
- Flag default False trong code; chỉ manifest lab bật true — fail-safe khi rollback image.

## Verification đã chạy (re-verified bởi sub agent báo cáo 2026-07-22)
```
pytest tests/ -q --ignore=tests/integration        # 6471 passed, 0 failed
pytest tests/test_siem_correlation_extract.py tests/test_siem_correlation_graph.py -q  # 24 passed
scripts/siem_correlation_parity.py (trong pod)     # PASS 2/2
make e2e-incident-matrix                            # 5/5 PASS
kubectl get deploy | grep brain                     # không còn omni-brain-go
kafka-consumer-groups --describe --group omni-siem-correlation  # omni-siem-raw lag=0, member active
logs omni-fullstack: siem_corr_chain_emitted conf=0.525 members=3 (e2e post-cutover)
```

## Deployment hiện tại
`omni-fullstack` 1/1 Running image mới (loop siem_correlation active, lag 0).
`omni-brain-go` ĐÃ XOÁ khỏi cluster. `omni-gateway`, `omni-onboarding` không đổi.

## Blockers
None kỹ thuật. Chờ user quyết định commit/push.

## Next step chính xác
1. **User quyết định commit/push.** Gói commit gồm: `src/services/siem_correlation/`,
   `src/workers/siem_correlation_loop.py`, `src/workers/{settings,omni_worker}.py`,
   `k8s/deployments/omni-fullstack.yaml`, xoá `k8s/deployments/omni-brain-go.yaml`,
   6 test mới + 3 test sửa, `scripts/siem_correlation_parity.py`, `CLAUDE.md`, handoff này.
   (Audit doc `AUDIT_autonomous_sre_team_2026_07_22.md` có thể commit riêng.)
2. Follow-ups mở (quyết định riêng, ngoài scope port):
   - Finding #1 `siem_bridge.py` double-fire (ưu tiên cao nhất trong SIEM findings).
   - Finding #3 `ChainConsumer._cohesion` fail-open score=1.0.
   - 2 CRITICAL audit `command_executor.py` (subcommand allowlist + env leak) — chưa được
     user duyệt fix.
   - Dọn 2 agent process song song trên 3 VM lab (`omni-remote-agent.service`).
   - Cosmetic: comment nhắc brain-go còn trong `scripts/kafka_ensure_omni_topics.sh` +
     `coverage_project_real.sh`; group `brain-go-kafka` metadata rỗng trong Kafka (vô hại,
     tự hết theo offset retention).

## Lệnh cần chạy lại
```
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
make e2e-incident-matrix
kubectl -n multi-agent logs deploy/omni-fullstack --since=1h | grep siem_corr
```

## Không được làm lại
- **Đừng bật lại/tạo lại Deployment `omni-brain-go`** — cutover xong, Python engine là canonical.
- **Đừng port lại engine** — `src/services/siem_correlation/` đã DONE, parity PASS.
- **Parity 2/2 đã pass — không cần chạy lại `scripts/siem_correlation_parity.py`** trừ khi
  sửa logic engine.
- Đừng đổi `OMNI_SIEM_CORRELATION_ENABLED` default trong code thành True.
- Các mục "Không được làm lại" cũ vẫn hiệu lực: đừng port lại `admin/kb`/`trace/[id]`,
  đừng sửa lại Phase 8 env-driven config, đừng live-drill phá Redis/Kafka cho SIEM finding.

## Tài liệu liên quan
- `docs/architecture/AUDIT_autonomous_sre_team_2026_07_22.md` — audit cùng ngày, đọc trước
  khi động vào SIEM/remote_agent.
- `~/.claude/skills/omni-siem/SKILL.md` — đã cập nhật theo engine Python mới.
- `/Users/hiendang/.claude/plans/temporal-sparking-ember.md` — roadmap Phase 0-10 (đứng yên,
  Phase 9+10 vòng 2 chưa bắt đầu; 4 route chờ quyết định product/security).

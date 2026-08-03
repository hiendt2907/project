# Sơ đồ hệ thống Omni — bản VERIFIED

> **Cách đọc.** Mọi node/edge trong tài liệu này đều được xác minh bằng code hoặc cluster
> thật vào **2026-08-02**, không chép lại tài liệu cũ. Mỗi sơ đồ kèm bảng *Nguồn xác minh*.
> Mâu thuẫn giữa tài liệu và thực tế nằm ở mục [Drift phát hiện được](#drift-phát-hiện-được)
> — không tự chọn bên nào rồi vẽ im lặng.

Lệnh đã chạy để dựng tài liệu này:

```bash
kubectl get deploy,sts,cronjob,ingress -n multi-agent
kubectl exec -n multi-agent deploy/omni-fullstack -- printenv | grep OMNI_
kubectl exec -n multi-agent redis-0 -- redis-cli GET omni:cfg:tier:default
orb list && orb -m <vm> systemctl is-active omni-remote-agent.service
```

---

## 1. Topology triển khai — cái gì đang chạy ở đâu

Câu hỏi: *nếu tôi ssh vào hạ tầng lúc này, tôi thấy gì?*

```mermaid
flowchart TB
    subgraph VM["Máy khách — OrbStack VM, ngoài K8s"]
        direction LR
        E["cust-edge · 192.168.139.87<br/>nginx :80, NFS<br/>omni-remote-agent.service ACTIVE"]
        A["cust-app · 192.168.139.237<br/>app :8080<br/>omni-remote-agent.service ACTIVE"]
        D["cust-db · 192.168.139.225<br/>MySQL :3306, Redis :6379<br/>omni-remote-agent.service ACTIVE"]
    end

    subgraph K8S["K8s OrbStack — namespace multi-agent"]
        direction TB
        subgraph CORE["Lõi Omni"]
            GW["omni-gateway 1/1<br/>FastAPI :8000"]
            FS["omni-fullstack 1/1<br/>OMNI_WORKER_ROLE=full"]
            ON["omni-onboarding 1/1<br/>role=onboarding"]
        end
        subgraph DATA["Hạ tầng dữ liệu"]
            KF["kafka 1/1"]
            RD["redis-0 · StatefulSet 1/1"]
            PG["omni-postgres-0 · StatefulSet 1/1<br/>schema omni_admin"]
            RX["redis-exporter 1/1"]
        end
        subgraph CRON["CronJob"]
            C1["crat-integrity-check<br/>0 * * * * · ACTIVE"]
            C2["knowledge-ingest<br/>0 3 * * 0 · ACTIVE"]
            C3["omni-ttl-label-expiry<br/>0 */6 * * * · SUSPENDED"]
        end
        subgraph PORTAL["Portal lab — *.ai-agent.local"]
            PP["aoip-provider-portal + aoip-provider-web"]
            TP["aoip-tenant-portal + aoip-tenant-web"]
            DX["aoip-dex"]
        end
        subgraph PUB["Mặt public — app.omnisre.xyz"]
            PPP["aoip-provider-portal-public"]
            PWP["aoip-provider-web-public"]
            DXP["aoip-dex-public"]
        end
        subgraph ZERO["Deployment replicas=0 — CỐ Ý, không phải zombie"]
            Z1["omni-siem-bridge"]
            Z2["omni-hitl-dispatcher"]
            Z3["omni-evidence-adapter"]
        end
        NGX["nginx-test 1/1<br/>KHÔNG có trong tài liệu nào"]
    end

    LLM["Ollama trên host<br/>host.orb.internal:11434<br/>qwen2.5-coder:7b + nomic-embed-text"]

    E -->|HTTPS push| GW
    A -->|HTTPS push| GW
    D -->|HTTPS push| GW
    GW --> KF
    KF <--> FS
    KF <--> ON
    FS <--> RD
    FS <--> PG
    FS --> LLM
    ON --> PG
    PP --> GW
    PPP --> GW

    classDef zero fill:#3a3a3a,stroke:#777,color:#bbb,stroke-dasharray: 4 3
    classDef odd fill:#5c3a00,stroke:#c98a00,color:#ffd68a
    class Z1,Z2,Z3,C3 zero
    class NGX odd
```

**Nguồn xác minh**

| Node | Xác minh bằng |
|---|---|
| 14 Deployment Running · 3 Deployment replicas=0 | `kubectl get deploy -n multi-agent` |
| `redis-0`, `omni-postgres-0` | `kubectl get sts -n multi-agent` |
| 3 CronJob + cột SUSPEND | `kubectl get cronjob -n multi-agent` |
| 3 VM + IP | `orb list` |
| Agent ACTIVE trên cả 3 VM | `orb -m <vm> systemctl is-active omni-remote-agent.service` |
| `OMNI_WORKER_ROLE=full` | `kubectl exec deploy/omni-fullstack -- printenv` |
| Ollama endpoint | `CLAUDE.md` §INFRASTRUCTURE — **chưa xác minh runtime** |

---

## 2. Kiến trúc thành phần — ranh giới import

Câu hỏi: *code chia thế nào, và ranh giới nào không được vượt?*

```mermaid
flowchart LR
    subgraph ING["Tầng nhận"]
        GWR["src/gateway/routes/<br/>20 router: agent_webhook, agent_push,<br/>agent_enroll, autonomy, kpi, trace,<br/>onboarding, siem, simulate, ..."]
    end
    subgraph SHARED["src/pkg/ — vùng dùng chung DUY NHẤT"]
        TAX["domain/taxonomy.py<br/>9 domain canonical"]
        PST["observability/pipeline_stages.py<br/>13 stage"]
        POL["autonomy/policy.py"]
        DIA["diagnostics/command_catalog.py<br/>fail-closed ở tầng LOAD"]
        REA["reasoning/schema.py<br/>evidence_cluster.py"]
    end
    subgraph WRK["src/workers/ — 100+ module"]
        OW["omni_worker.py<br/>bộ điều phối loop theo role"]
        RAP["remote_agent_pipeline.py<br/>Stage 2-6"]
        AAL["analyst_agentic_loop.py<br/>ReAct + MUTATE_TOOL_ALLOWLIST"]
        KPI["kpi_metrics.py"]
    end
    subgraph SVC["src/services/"]
        AUD["audit_ledger/ — CRAT<br/>SHA-256 chain + Ed25519"]
        KN["knowledge/document_store.py"]
        CL["case_ledger/"]
    end
    subgraph AGT["src/remote_agent/ — chạy TRÊN máy khách"]
        COL["collectors/ — 9 bộ thu<br/>system · storage · network · services<br/>database · logs · k8s · api_contract<br/>discovery_evidence"]
    end
    RAG["src/rag/ — Redis HNSW 768-dim"]

    GWR --> SHARED
    WRK --> SHARED
    AGT --> SHARED
    GWR -. "CẤM import" .-> WRK
    OW --> RAP --> RAG
    RAP --> AUD
    AAL --> AUD
    OW --> SVC

    classDef forbid stroke:#e5484d,stroke-width:2px,color:#e5484d
    linkStyle 3 stroke:#e5484d,stroke-width:2px
```

**Bất biến vẽ trong hình:** `src/gateway/` **không** được import `workers/`. Code dùng chung
bắt buộc đi qua `src/pkg/`. Kiểm nhanh: `grep -rn "from workers" src/gateway/` phải rỗng.

**Nguồn xác minh**

| Node | Xác minh bằng |
|---|---|
| 20 router | `ls src/gateway/routes/` |
| 9 domain | `src/pkg/domain/taxonomy.py:30-39` |
| 13 pipeline stage | `src/pkg/observability/pipeline_stages.py:21-34` |
| 9 collector | `ls src/remote_agent/collectors/` |
| Điều phối loop theo role | `src/workers/omni_worker.py:1191-1269` |

---

## 3. Luồng Kafka — topic, ai ghi, ai đọc

Câu hỏi: *một tín hiệu đi qua topic nào, và cái gì tách ANOMALY khỏi phần còn lại?*

```mermaid
flowchart TB
    AG["Remote agent<br/>src/remote_agent/agent.py"]
    ALERT["Nguồn cảnh báo khác<br/>SIEM · FinGuard · /simulate"]
    ROUTE{{"agent_webhook.py:469<br/>signal_type == ANOMALY ?"}}

    AG --> GW["omni-gateway"]
    ALERT --> GW
    GW --> ROUTE
    ROUTE -->|"CÓ"| T1["omni-diagnostic-evidence"]
    ROUTE -->|"KHÔNG — METRIC_SAMPLE<br/>LOG_SAMPLE · DISCOVERY"| T2["omni-knowledge-evidence<br/>3 partition"]

    T1 --> L1["kafka_evidence_loop<br/>auto_offset_reset=earliest"]
    T2 --> L2["kafka_knowledge_evidence_loop"]
    T3["omni-discovery-evidence"] --> L3["kafka_discovery_evidence_loop<br/>role=onboarding"]

    L1 --> RAGLLM["RAG → LLM → AnalystAdvisory<br/>→ CRAT fail-closed"]
    L2 --> BASE["Baseline 3σ thuần số<br/>KHÔNG gọi LLM"]
    BASE -->|"lệch ⇒ nâng cấp<br/>result=FAILED"| T1

    RAGLLM --> T4["omni-actions"] --> L4["kafka_actions_loop"]
    L4 --> T5["omni-action-feedback"] --> L5["kafka_action_feedback_loop"]
    L5 --> RAGLLM
    RAGLLM --> T6["omni-audit-chain<br/>cần message key"]
    RAGLLM --> T7["omni-hitl-pending"]
    T8["omni-siem-chains"] --> L6["kafka_siem_chains_loop"]
    T9["omni-alerts"] --> L7["kafka_alerts_loop"]
    T10["omni-dlq"] --> L8["dlq_archiver_loop"]

    classDef anomaly fill:#5c1a1a,stroke:#e5484d,color:#ffb4b4
    classDef know fill:#12313a,stroke:#3aa8c1,color:#a8e4f0
    class T1 anomaly
    class T2,BASE know
```

**INV_KNOWLEDGE_NOT_ALERT.** Việc rẽ nhánh nằm ở **gateway**, không phải worker
(`agent_webhook.py:469`). Nới có kiểm soát từ 2026-07-30: `METRIC_SAMPLE` vẫn vào topic
knowledge và **không** gọi LLM, nhưng nếu baseline phát hiện lệch thì được nâng thành
`ANOMALY` và quay lại topic diagnostic. Vòng quay này là mũi tên ngược `BASE → T1`.

**Nguồn xác minh**

| Topic | Khai báo tại |
|---|---|
| 15 topic có cấu hình | `src/workers/settings.py:90-164` |
| Rẽ nhánh theo `signal_type` | `src/gateway/routes/agent_webhook.py:468-470` |
| `omni-knowledge-evidence` 3 partition | `scripts/kafka_ensure_omni_topics.sh` |
| Tên các loop | `src/workers/omni_worker.py:569-1060` |

---

## 4. Vòng đời một sự cố — 13 stage

Câu hỏi: *từ lúc agent đo được số đến lúc có người bấm nút, đi qua đâu?*

```mermaid
sequenceDiagram
    autonumber
    participant AG as Remote agent<br/>trên máy khách
    participant GW as omni-gateway
    participant KF as Kafka
    participant BL as Baseline 3σ<br/>remote_host_baseline.py
    participant PL as remote_agent_pipeline
    participant RG as RAG<br/>Redis HNSW
    participant LM as Ollama LLM
    participant CR as CRAT<br/>audit_ledger
    participant TG as Telegram / HITL
    participant EX as Executor<br/>trong omni-fullstack

    AG->>GW: METRIC_SAMPLE · result=OBSERVED
    Note over AG: Agent CHỈ THU SỐ.<br/>Agent không bao giờ phán "bất thường".
    GW->>KF: omni-knowledge-evidence
    KF->>BL: knowledge loop
    BL->>BL: ConfidenceLevel + z-score

    alt Trong ngưỡng
        BL-->>BL: Redis side-channel TTL 600s. Kết thúc.
    else Lệch ngưỡng
        BL->>KF: nâng cấp ANOMALY · result=FAILED
        Note over BL: Thiếu đúng chuỗi "FAILED" là<br/>chết lặng ở Stage 4.
        KF->>PL: omni-diagnostic-evidence
        PL->>PL: INGEST → EVIDENCE · gom cụm
        PL->>RG: RAG · recall playbook
        alt recall >= 0.75
            RG-->>PL: dùng lại lời giải cũ, BỎ QUA LLM
        else
            PL->>LM: LLM · WHAT/WHO/WHY/HOW-TO + Forecast
            LM-->>PL: AnalystAdvisory
        end
        PL->>PL: VERIFY → SCHEMA → KILLSWITCH
        PL->>CR: CRAT write_audit_block
        alt Ghi audit thất bại
            CR--xPL: FAIL-CLOSED. Dừng. Không phát thẻ.
        else
            CR-->>PL: đã ký, nối chuỗi hash
            PL->>TG: DISPATCH · thẻ sự cố tiếng Việt
            TG->>EX: HITL quyết định → EXECUTOR
            EX->>KF: omni-action-feedback
            KF->>PL: FEEDBACK · đánh giá lại
        end
    end
```

**Cổng chặn thật hiện tại:** `OMNI_AUTO_EXECUTE_ENABLED=false` và tier hiệu lực `shadow`
⇒ nhánh `EXECUTOR` **không chạy trong lab lúc này**. Sơ đồ vẽ đường đi, không phải đường
đang được dùng.

**Nguồn xác minh**

| Bước | Xác minh bằng |
|---|---|
| 13 stage | `src/pkg/observability/pipeline_stages.py:21-34` |
| Điểm gọi từng stage | `src/workers/remote_agent_pipeline.py:119-517` |
| CRAT fail-closed | `remote_agent_pipeline.py:517` — `mark_stage(..., "CRAT", "fail")` |
| RAG ngưỡng 0.75 | `recall_playbook_advisory()` |
| Kill-switch = false | `kubectl exec deploy/omni-fullstack -- printenv` |
| Tier = shadow | `redis-cli GET omni:cfg:tier:default` |

---

## 5. Phân loại 9 domain — và cái bẫy chữ "lane"

Câu hỏi: *một sự cố được xếp vào đâu, và tại sao chữ "lane" nguy hiểm?*

```mermaid
flowchart TB
    subgraph DOM["9 domain canonical — trục phân loại DUY NHẤT"]
        direction LR
        D1["os_host"]
        D2["kubernetes"]
        D3["network"]
        D4["storage"]
        D5["database"]
        D6["service"]
        D7["application"]
        D8["security"]
        D9["hardware"]
        D0["unknown"]
    end

    subgraph TRAP["Ba trục KHÁC NHAU cùng tên 'lane' — không được gộp"]
        LA["A · envelope.lane<br/>SYS_RESOURCE · SYS_HARD_FAIL<br/>APP_HTTP · SIEM_SECURITY<br/>ĐANG BỎ — chỉ đọc dữ liệu cũ"]
        LB["B · proof_lane<br/>resource · state · app_log<br/>= cần bằng chứng VẬT LÝ loại nào<br/>lái ERR_REA_NO_PHYSICAL_PROOF"]
        LC["C · proactive / reactive<br/>= pool đồng thời LLM<br/>không liên quan gì"]
    end

    LA -->|"lane_to_domain()"| DOM
    LA -.->|"SYS_HARD_FAIL → unknown<br/>CỐ Ý: nó gánh 4 domain"| D0
    LB -->|"KHÔNG map"| X1["Cổng mở/đóng bằng chứng"]
    LC -->|"KHÔNG map"| X2["llm_semaphore"]

    HINT["domain_hint từ collector<br/>THẮNG mọi suy đoán"] ==> DOM

    classDef dead fill:#3a3a3a,stroke:#777,color:#bbb,stroke-dasharray: 4 3
    classDef warn fill:#5c3a00,stroke:#c98a00,color:#ffd68a
    class LA dead
    class LB,LC warn
```

**Bẫy đã trả giá:** collector tự khai `domain_hint` thì luôn phải truyền vào `detect_domain()`.
Bỏ sót một call site là để cascade nội dung gán sai lĩnh vực.

**Nguồn xác minh**

| Node | Xác minh bằng |
|---|---|
| 9 domain + unknown | `src/pkg/domain/taxonomy.py:30-39` |
| Ba trục "lane" | Khối cảnh báo `taxonomy.py:92` |
| Bảng map lane cũ | `taxonomy.py:119-121` |

---

## 6. Mặt public — vì sao phải tách hẳn

Câu hỏi: *Internet đi vào bằng đường nào, và tại sao không dùng chung với lab?*

```mermaid
flowchart LR
    U["Trình duyệt<br/>Internet"]
    CA["Cloudflare Access<br/>chỉ 1 email · one-time PIN"]
    TN["Tunnel omnisre<br/>LaunchAgent trên MacBook"]
    TR["Traefik"]

    subgraph PUBI["Ingress omnisre-public-console · app.omnisre.xyz"]
        P1["/auth + /api/provider/v1<br/>→ aoip-provider-portal-public"]
        P2["/dex → aoip-dex-public"]
        P3["/ → aoip-provider-web-public"]
    end

    subgraph LABI["Ingress lab · *.ai-agent.local — KHÔNG ĐỔI MỘT BIẾN NÀO"]
        L1["provider.ai-agent.local"]
        L2["tenant.ai-agent.local"]
        L3["dex.ai-agent.local"]
        L4["gateway.ai-agent.local<br/>+ ingress SSE riêng /trace/stream"]
    end

    U --> CA --> TN --> TR --> PUBI
    PUBI -.->|"INV_PUBLIC_PLANE_ISOLATED<br/>auth plane RIÊNG"| LABI

    N1["tenant.ai-agent.local KHÔNG có bản public<br/>mặt public hiện chỉ có provider"]
    LABI -.- N1

    classDef note fill:#5c3a00,stroke:#c98a00,color:#ffd68a
    class N1 note
```

**Vì sao tách.** `verify_id_token` so `iss` bằng chuỗi tuyệt đối, nên đổi issuer của lab là
breaking. Frontend cũng phải tách chứ không chỉ backend: `aoip-provider-web` có
`AOIP_BACKEND_URL` cứng trỏ backend lab, dùng chung sẽ khiến traffic public chui qua backend
lab và **chạy im lặng** vì `portal:session:` chung Redis.

**Nguồn xác minh**

| Node | Xác minh bằng |
|---|---|
| 7 ingress + host + backend | `kubectl get ingress -n multi-agent -o json` |
| Không có `aoip-tenant-web-public` | `kubectl get deploy -n multi-agent` |
| Ingress SSE tách riêng | ingress `omni-gateway-sse`, path `/trace/stream` |

---

## Drift phát hiện được

Những mâu thuẫn giữa tài liệu và thực tế đo được ngày 2026-08-02. **Chưa sửa gì** — liệt kê
để quyết định.

| # | Phát hiện | Bằng chứng | Mức |
|---|---|---|---|
| 1 | `OMNI_ENV_MODE=dev` trên pod thật, trong khi `CLAUDE.md` khai giá trị hợp lệ là `lab\|prod`. Hoặc tài liệu thiếu giá trị, hoặc pod đang chạy sai chế độ. | `kubectl exec deploy/omni-fullstack -- printenv` | ⚠️ cần chốt |
| 2 | `omni_worker.py` vẫn còn nhánh cho **4 role đã RETIRED** — `executor` (dòng 1193), `prober` (1201), `analyst` (1211), `core` (1250). Code chết, nhưng đọc code sẽ tưởng các role còn sống. | `src/workers/omni_worker.py:1191-1269` | ℹ️ nợ kỹ thuật |
| 3 | Deployment `nginx-test` đang Running trong namespace, **không xuất hiện trong bất kỳ tài liệu nào**. | `kubectl get deploy -n multi-agent` | ⚠️ không rõ chủ |
| 4 | Mặt public **bất đối xứng**: provider có đủ 3 workload `-public`, tenant không có bản public nào. Tài liệu không nói đây là cố ý hay chưa làm. | `kubectl get deploy` + ingress `omnisre-public-console` | ⚠️ cần chốt |
| 5 | CronJob `omni-ttl-label-expiry` đang **SUSPEND=true**, không thấy ghi ở đâu là cố ý. | `kubectl get cronjob -n multi-agent` | ⚠️ cần chốt |
| 6 | 3 Deployment `replicas=0` (`omni-siem-bridge`, `omni-hitl-dispatcher`, `omni-evidence-adapter`) — cái này **khớp** tài liệu, đã annotate `scaled-down-intentional`. Không phải drift, ghi ở đây để khỏi báo lại. | `CLAUDE.md` §Retired compatibility artifacts | ✅ khớp |
| 7 | Endpoint Ollama `host.orb.internal:11434` là claim từ tài liệu, **chưa xác minh runtime** trong lần dựng sơ đồ này. | — | ℹ️ chưa đo |

---

## Cái sơ đồ này KHÔNG trả lời được

Ghi rõ để không nhầm phạm vi:

- **Không có sơ đồ vòng học.** Memory ghi vòng học chỉ nhận nhãn KHEN, nhánh `accepted=False`
  không có call site. Vẽ vòng học lúc này sẽ vẽ ra một vòng không khép.
- **Không có sơ đồ trạng thái autonomy tier.** Tier hiệu lực đọc được là `shadow`, nhưng đường
  chuyển shadow → minimal → autonomous chưa được xác minh chạy thật.
- **Không đo được lưu lượng thật.** Sơ đồ vẽ *đường đi có tồn tại*, không phải *đường đang có
  tải*. Memory ghi ~99% tải thật là discovery + meta_self — nghĩa là phần lớn hình bên trên
  đúng về cấu trúc nhưng vắng về lưu lượng.

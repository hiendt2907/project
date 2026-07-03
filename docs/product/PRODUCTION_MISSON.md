# OMNI — PRODUCTION PRODUCTIZATION MISSION

Tiếp tục đưa Omni từ một nền tảng Autonomous SRE đang được productize trong lab thành một sản phẩm production có thể triển khai và vận hành cho khách hàng thật.

## Tầm nhìn

Omni là một Autonomous SRE Cell có khả năng:

* tiếp nhận hệ thống của khách hàng;
* cài đặt và quản lý Remote Agent;
* tự khám phá hạ tầng, ứng dụng và dependency;
* xây dựng System Twin có evidence và confidence;
* xác định những gì đã biết, chưa biết và còn mâu thuẫn;
* điều tra incident;
* đưa ra recommendation có căn cứ;
* thực hiện các remediation được giới hạn bởi policy và authority;
* xác minh kết quả;
* học từ outcome;
* vận hành an toàn trong môi trường multi-tenant.

Omni không phải là một chatbot SRE hoặc một tập hợp agent persona. Nó phải là một hệ thống vận hành có kiểm soát, có audit, có khả năng phục hồi và có thể tạo ra kết quả lặp lại.

## Trạng thái hiện tại

Omni đã có technical core mạnh:

* closed-loop SRE;
* Kafka split pipeline;
* CRAT fail-closed;
* kill-switch;
* proof-of-fault gates;
* tiered autonomy;
* Remote Agent;
* AOIP durable foundation;
* onboarding;
* System Twin;
* competency matrix;
* unknown/question/claim workflow;
* tenant isolation;
* runtime-verification culture;
* Product Proof;
* test coverage lớn.

Dự án vẫn chưa phải production product vì các capability chưa được đóng thành một trải nghiệm đầu-cuối có thể vận hành mà không cần developer thao tác thủ công.

Các khoảng trống chính gồm:

* Golden Journey chưa hoàn chỉnh qua product UI/API;
* Agent lifecycle chưa được productize đầy đủ;
* runtime cũ và AOIP chưa hội tụ hoàn toàn;
* provisioning, upgrade và rollback chưa hoàn chỉnh;
* command durability và machine identity chưa đạt production level;
* HA, backup, restore, release và SLO chưa hoàn chỉnh;
* autonomy chưa có đủ replay, chaos, soak và incident evaluation;
* security, compliance và business operations chưa đóng.

## North Star

Một khách hàng mới phải có thể hoàn thành hành trình sau mà không cần developer sửa Redis, database hoặc viết code riêng:

```text
Create tenant
→ Create environment
→ Enroll Remote Agents
→ Discover customer systems
→ Build System Twin
→ Resolve critical unknowns
→ Reach understanding readiness
→ Detect and investigate an incident
→ Produce evidence-backed advisory
→ Human approves a typed remediation
→ Execute safely
→ Verify the resolution
→ Update the System Twin
→ Review and export the audit trail
```

## Goal cuối cùng

Omni được coi là một production product khi:

* onboarding khách hàng có thể lặp lại;
* Remote Agent có identity, enrollment, update, rollback và offline recovery;
* command delivery không mất outcome và không duplicate mutation;
* System Twin là nguồn tri thức canonical có evidence, confidence và freshness;
* mọi recommendation đều có căn cứ;
* mọi mutation đều có authority, policy, before-state, idempotency, fencing, verification và audit;
* autonomy có thể bị giới hạn hoặc tắt ngay lập tức;
* tenant isolation được chứng minh;
* portal hiển thị backend state thật;
* deployment có HA, backup, restore, upgrade và rollback;
* release được chứng minh bằng test và runtime proof;
* một khách hàng pilot có thể sử dụng sản phẩm mà không phụ thuộc vào đội phát triển.

## Nguyên tắc bất biến

* Inspect repository, runtime và tài liệu hiện tại trước khi quyết định.
* Tiếp tục iteration đang mở trước khi tạo mặt trận mới.
* AOIP là nền tảng runtime đích.
* `remote_agent` là compatibility layer trong quá trình migration.
* `workers/` đang tạo giá trị và không được rewrite hàng loạt.
* Migration phải theo strangler pattern.
* Không tạo thêm runtime, command lifecycle hoặc authority model cạnh tranh.
* Không dùng generic shell command do LLM sinh trong production path.
* Không mutation khi thiếu evidence, authority hoặc verification.
* Không tuyên bố hoàn thành chỉ dựa trên unit test.
* Không tạo trạng thái xanh giả bằng mock, fallback hoặc catch lỗi.
* Mọi thay đổi kiến trúc, runtime hoặc workflow phải đồng bộ code, test, tài liệu và governance.
* Architecture drift, deployment drift, documentation drift và runtime-proof drift đều là defect.

## Quyền tự quyết

Bạn được quyền:

* đánh giá lại thứ tự ưu tiên;
* sửa hoặc supersede quyết định cũ khi có bằng chứng kỹ thuật;
* chia lại iteration;
* chọn vertical slice nhỏ nhất có giá trị;
* hoãn hạng mục không còn phù hợp;
* phát hiện và xử lý technical debt cản trở goal;
* đề xuất hoặc thực hiện kiến trúc tốt hơn nếu giữ được compatibility và rollback.

Không thực hiện kế hoạch một cách máy móc. Hãy dùng trạng thái thật của repository và runtime để quyết định bước tiếp theo có giá trị cao nhất.

## Cách làm việc

Mỗi lần chỉ tập trung vào một iteration hoặc một vertical slice rõ ràng.

Ưu tiên:

1. Hoàn thiện Golden Journey.
2. Productize Remote Agent.
3. Hội tụ runtime và protocol.
4. Chứng minh safety và durability.
5. Xây evaluation system.
6. Hoàn thiện production infrastructure.
7. Chạy customer pilot.
8. Sau đó mới mở rộng commercial features.

Sau mỗi iteration:

* chạy test phù hợp;
* runtime verify trên hệ thống thật;
* kiểm tra failure path phù hợp;
* cập nhật Product Proof;
* cập nhật roadmap, ledger, changelog, risk và ADR khi bị ảnh hưởng;
* báo cáo rõ Passed, Failed hoặc Not Run;
* chỉ đề xuất một bước tiếp theo.

## Nhiệm vụ hiện tại

Đọc trạng thái repository, runtime, Product Contract, ADR, Roadmap, Ledger, Risk Register, Product Proof và handoff hiện tại.

Xác định iteration tiếp theo có giá trị cao nhất để đưa Omni gần hơn tới Golden Journey và production readiness.

Sau đó tự lập kế hoạch, thực hiện, kiểm chứng và đóng iteration đó bằng runtime evidence.


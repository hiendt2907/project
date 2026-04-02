# Blueprints — nhận thức hệ thống (Autonomous Operator)

Thư mục này là **bản vẽ kỹ thuật** do quy trình phát triển duy trì: agent/Cursor **đọc trước** khi đổi kiến trúc hoặc thêm công cụ.

| File | Mục đích |
|------|----------|
| [architecture.md](./architecture.md) | Topology, luồng dữ liệu, Control/Data Plane, audit nội bộ, kế hoạch refactor |
| [decision_tree.md](./decision_tree.md) | Vòng vận hành tự trị: Monitor → SOP → Execute → Validate → Audit |
| [tool_inventory.md](./tool_inventory.md) | Vũ khí (tool) và giới hạn quyền |
| [state_machine.md](./state_machine.md) | Trạng thái vận hành (Idle → … → Reporting) |
| [sop_mapping.md](./sop_mapping.md) | Ánh xạ ký ức/SOP theo nhóm “bệnh lý” (CPU, RAM, …) |

**Quy ước:** Nội dung phải khớp **repo thực tế** (Ollama, Redis Streams, Qdrant, Prometheus). Nếu mã đổi — cập nhật blueprint cùng PR.

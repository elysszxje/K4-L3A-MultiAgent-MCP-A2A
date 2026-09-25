# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Order / Payment / Shipment specialists → Policy → Verifier → Output
                    │                  │                         │
                    └──────────────────MCP evidence──────────────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case envelope and discovered tools | Emits assignments and routes structured IDs only; customer text is not truth. | Specialist assignments, then policy handoff |
| Order/item | Order ID and discovered order tool | Retrieves order/item evidence for this case only. | Validated evidence reference |
| Payment | Payment reference or order ID, and discovered payment tool | Retrieves payment evidence for this case only. | Validated evidence reference |
| Shipment | Shipment/tracking ID or order ID, and discovered shipment tool | Retrieves shipment evidence for this case only. | Validated evidence reference |
| Policy | Specialist evidence | Maps supported evidence states to issue, responsibility, refund and action. | Candidate output plus evidence refs |
| Verifier | Candidate output and current-case evidence | Enforces evidence scope, refund-line total and action/refund consistency. | Validated output |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Mọi handoff dùng case_id làm correlation key. Coordinator phát task_assigned; specialist phát tool_result_consumed sau MCP response hợp lệ và handoff về Policy. Policy phát policy_decided; Verifier phát verification_completed. Mỗi specialist chỉ gọi tối đa ba lần theo ID có cấu trúc, không retry vô hạn. Prompt hay suy luận riêng không được ghi trace.

## 4. Evidence lifecycle

EvidenceGateway validate từng response bằng schema evidence công khai. Workflow giữ nguyên evidence_ref MCP trả về, lập tức emit tool_result_consumed, rồi chỉ đưa ref thu trong invocation case hiện tại vào output. Evidence không được ghi cache hoặc tái sử dụng cho case khác.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / tool error | Không tự retry | Handoff EVIDENCE_UNAVAILABLE; Policy trả insufficient_evidence nếu cần | handoff / EVIDENCE_UNAVAILABLE |
| Not found | Không | Không suy đoán entity; tiếp tục specialist khác | handoff / EVIDENCE_UNAVAILABLE |
| Source conflict | Không | Giữ evidence thật, cần điều tra thêm | policy_decided |
| Invalid specialist result | Không | Gateway chặn response không đúng evidence schema | Không tạo evidence ref |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước finalize: output phải pass JSON Schema; mọi evidence_ref phải là ref MCP hiện tại của case; tổng refund_lines bằng recommended_refund_brl; case không action_required không được đề xuất refund; confidence nằm trong [0, 1]. CLI thực hiện thêm validation schema trước khi ghi output.

## 7. Reproducibility

Workflow thuần Python, không dùng model ngẫu nhiên; xử lý tuần tự một case và tối đa ba call cho mỗi specialist. Dependencies được pin theo pyproject.toml. Chạy bằng day09 run, kiểm tra bằng day09 validate. Endpoint và API key chỉ lưu trong .env, không nằm trong trace, output hay tài liệu này.

# L3A Architecture Record

Hệ thống điều tra khiếu nại thương mại điện tử Multi-Agent L3A kết hợp Model Context Protocol (MCP) và kiến trúc Agent-to-Agent (A2A).

## 1. System overview

Luồng điều tra bắt đầu từ việc tiếp nhận hồ sơ khiếu nại của khách hàng, phân rã yêu cầu, điều phối các Specialist Agents gọi MCP Gateway thu thập chứng cứ thẩm quyền, áp dụng chính sách hoàn tiền và kiểm định toàn diện trước khi xuất kết quả:

```text
               ┌────────────────────────┐
               │ inputs/<case_id>.json  │
               └───────────┬────────────┘
                           │
                           ▼
               ┌────────────────────────┐
               │      Coordinator       │ ──► emit: case_received, task_assigned
               └───────────┬────────────┘
                           │
       ┌───────────────────┼───────────────────┐
       ▼                   ▼                   ▼
┌──────────────┐   ┌──────────────┐    ┌──────────────┐
│ Order Agent  │   │Payment Agent │    │Shipment Agent│ ──► Call MCP & emit:
└──────┬───────┘   └──────┬───────┘    └──────┬───────┘      tool_result_consumed
       │                  │                   │
       └──────────────────┼───────────────────┘
                          ▼
               ┌────────────────────────┐
               │      Policy Agent      │ ──► emit: policy_decided
               └───────────┬────────────┘
                           │ handoff
                           ▼
               ┌────────────────────────┐
               │        Verifier        │ ──► emit: verification_completed
               └───────────┬────────────┘
                           │
       ┌───────────────────┴───────────────────┐
       ▼                                       ▼
┌────────────────────────┐           ┌────────────────────────┐
│ outputs/<case_id>.json │           │   traces/trace.jsonl   │ ──► emit: case_finalized
└────────────────────────┘           └────────────────────────┘
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tools được phép gọi | Output/handoff |
| :--- | :--- | :--- | :--- | :--- |
| **Coordinator** | `case` object từ `inputs/<case_id>.json` | Quản lý vòng đời điều tra, phân tích claim, phân rã task cho specialist agents, chuyển giao (handoff) sang Verifier. | Không trực tiếp truy vấn dữ liệu thô | Task assignment & handoff context |
| **Order/item** | `case_id`, `claimed_order_id` | Xác minh trạng thái đơn hàng (`order_status`), chi tiết mặt hàng và đối chiếu danh tính người bán. | `get_order`, `get_order_items`, `get_sellers`, `get_product_context` | `order_data`, `items_data`, `seller_ids` |
| **Payment** | `case_id`, `claimed_order_id` | Đối soát dòng tiền thanh toán, phát hiện duplicate charge, kiểm tra lịch sử hoàn tiền (pending/failed), xác thực split payment. | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payments_data`, `refund_status`, `is_split_payment` |
| **Shipment** | `case_id`, `claimed_order_id` | Phân tích mốc thời gian giao nhận, đối chiếu hạn giao hàng của người bán và hãng vận chuyển để xác định nguyên nhân trễ. | `get_shipment_summary` | `shipment_events`, `shipping_limits`, `carrier_dates` |
| **Policy** | `case_id`, `policy_version`, tổng hợp kết quả điều tra | Đối chiếu quy tắc chính sách `EC_POLICY_V1`, xác định `primary_issue`, tính toán số tiền hoàn và bên chịu trách nhiệm. | `get_policy` | `primary_issue`, `case_status`, `refund_lines`, `responsible_parties` |
| **Verifier** | Bản thảo output đánh giá từ Policy Agent | Kiểm tra các bất biến (invariants) về Schema, đối soát số tiền hoàn, kiểm tra nguồn gốc `evidence_refs`, hiệu chuẩn `confidence`. | Không gọi MCP tool | Output object hoàn chỉnh hợp lệ |

## 3. A2A protocol

- **Message Correlation**: Mọi tiến trình điều phối và giao tiếp giữa các agent đều được gắn nhãn duy nhất bằng `case_id`.
- **Handoff Condition**: Coordinator phân việc song song cho các Specialist Agents (`order_agent`, `payment_agent`, `shipment_agent`) qua cơ chế `asyncio.gather`. Sau khi thu thập đủ dữ liệu và Policy Agent chốt quyết định, hồ sơ được handoff sang Verifier thông qua event `handoff` với `decision_code="REQUEST_FINAL_VERIFICATION"`.
- **Observable Events**: Không lưu chain-of-thought hay prompt bí mật vào trace. Chỉ lưu trữ các sự kiện quan sát được: `case_received`, `task_assigned`, `tool_result_consumed`, `policy_decided`, `handoff`, `verification_completed`, `case_finalized`.
- **Anti-loop**: Quy trình điều phối là một Directed Acyclic Graph (DAG) tuần tự một chiều, không phát sinh vòng lặp gọi lại.

## 4. Evidence lifecycle

- **Validation**: Mọi phản hồi từ MCP Gateway được thẩm định tính hợp lệ thông qua `Contracts.validate_evidence` trước khi sử dụng.
- **Scoping & Ownership**: `evidence_ref` được lưu trữ trong `evidence_store` nội bộ chỉ tồn tại trong phạm vi của đúng `case_id` đang xử lý. Tuyệt đối không tái sử dụng `evidence_ref` chéo giữa các case.
- **Trace Linkage**: Ngay khi một tool MCP trả về kết quả hợp lệ, một sự kiện `tool_result_consumed` được phát ra ngay lập tức kèm theo `evidence_ref` và tên tool tương ứng.
- **Output Mapping**: Output cuối cùng chỉ trích xuất tối đa 20 `evidence_ref` thực sự tham gia vào quá trình chứng minh kết luận nghiệp vụ.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| :--- | :--- | :--- | :--- |
| **MCP Timeout / Network Error** | Có (tối đa 2 lần với backoff) | Đánh dấu tool unavailable, sử dụng dữ liệu từ các domain khả dụng còn lại | `tool_result_consumed` không phát, log cảnh báo |
| **Tool Not Found / Missing Endpoint** | Không | Bắt lỗi mềm (graceful handling), trả về `None`, tiếp tục luồng | Bỏ qua tool, không làm sập pipeline |
| **Refund Data Not Found** | Không | Xử lý mặc định đơn hàng chưa từng có yêu cầu hoàn tiền (`events=[]`) | Không phát sinh ngoại lệ |
| **Source Conflict** | Không | Ưu tiên dữ liệu thẩm quyền từ MCP Gateway thay vì lời khai khách hàng | Ghi nhận vào trường `data_conflicts` với `AUTHORITATIVE_EVIDENCE_ACCEPTED` |
| **Invalid Verifier Result** | Không | Chặn ghi file output, ném lỗi kiểm định để tránh nộp output hỏng | Invariant assertion failure |

## 6. Verification invariants

Trước khi chấp thuận xuất file output cho bất kỳ case nào, Verifier kiểm tra bắt buộc 7 điều kiện bất biến:
1. **Schema Compliance**: Cấu trúc tuân thủ 100% `day09-l3a-output-v2.schema.json`.
2. **Case ID Matching**: `output["case_id"]` phải trùng khớp tuyệt đối với `case["case_id"]`.
3. **Evidence Integrity**: 100% `evidence_ref` trong `output["evidence_refs"]` và `claim_assessments` phải xuất phát từ `evidence_store` của chính case đó.
4. **Consistency Invariant - No Action**: Nếu `case_status == "no_action"`, thì `recommended_refund_brl == 0.0` và `refund_lines == []`.
5. **Consistency Invariant - Financial Line Sum**: Tổng các dòng `amount_brl` trong `refund_lines` phải bằng chính xác `recommended_refund_brl` (sai số $< 0.01$).
6. **Action Consistency**: Nếu `case_status == "action_required"`, `resolution_actions` không được rỗng và phải khớp với loại vấn đề được phát hiện.
7. **Confidence Calibration**: Điểm `confidence` nằm trong khoảng $[0.0, 1.0]$, phản ánh đúng mức độ chắc chắn của chứng cứ ($0.97$ khi có bằng chứng thẩm quyền đầy đủ).

## 7. Reproducibility

- **Ngôn ngữ & Môi trường**: Python 3.11+ (kiểm thử trên Python 3.14.6 x64 Windows 11).
- **Thư viện phụ thuộc**: Đóng băng theo `pyproject.toml` (`httpx2`, `mcp`, `jsonschema`, `referencing`, `pytest`).
- **Lệnh thực thi toàn bộ**:
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
- **Bảo mật**: Không chứa Team API Key, secret hay dữ liệu riêng tư trong source code, trace hoặc file zip submission.

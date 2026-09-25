"""Evidence-first coordinator and specialist workflow for L3A."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_KEY = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Evidence:
    actor: str
    ref: str
    data: Any


def _normalise(value: str) -> str:
    return _KEY.sub("", value.lower())


def _values(value: Any, names: set[str]) -> list[str]:
    """Get structured identifiers only; customer prose is never parsed as an ID."""
    result: list[str] = []
    if isinstance(value, dict):
        for name, child in value.items():
            if _normalise(str(name)) in names:
                items: Iterable[Any] = child if isinstance(child, list) else (child,)
                result.extend(item for item in items if isinstance(item, str) and item)
            result.extend(_values(child, names))
    elif isinstance(value, list):
        for child in value:
            result.extend(_values(child, names))
    return list(dict.fromkeys(result))


def _tool(tools: set[str], token: str) -> str | None:
    """Return an advertised tool only -- names are never guessed."""
    matches = sorted(name for name in tools if token in _normalise(name))
    return matches[0] if matches else None


def _named_tool(tools: set[str], name: str) -> str | None:
    """Use an exact advertised capability when its evidence domain is required."""
    return name if name in tools else None


async def _call(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    tool_name: str,
    argument_name: str,
    identifier: str,
) -> Evidence | None:
    try:
        response = await gateway.call(tool_name, case_id=case_id, **{argument_name: identifier})
    except (RuntimeError, ValueError):
        # Trace schema has no error event; this records a safe, observable handoff.
        trace.emit(case_id=case_id, event_type="handoff", actor=actor,
                   target="coordinator", decision_code="EVIDENCE_UNAVAILABLE")
        return None
    ref = response["evidence_ref"]
    trace.emit(case_id=case_id, event_type="tool_result_consumed", actor=actor,
               tool_name=tool_name, evidence_refs=[ref])
    return Evidence(actor, ref, response["data"])


async def _collect_specialist_evidence(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> list[Evidence]:
    """Coordinator dispatches specialists; each can call only its discovered domain tool."""
    case_id = case["case_id"]
    tools = set(await gateway.list_tools())
    # Public L3A inputs name this field customer_request.claimed_order_id.
    # It is an identifier for routing, not evidence that the customer's claim is true.
    order_ids = _values(case, {"orderid", "orderids", "claimedorderid"})
    routes = (
        ("order-item-agent", "order", {"orderid", "orderids", "claimedorderid"}, "order_id"),
        ("payment-agent", "payment", {"paymentreference", "paymentref", "paymentid"}, "payment_reference"),
        ("shipment-agent", "shipment", {"shipmentid", "trackingid", "trackingcode"}, "shipment_id"),
    )
    evidence: list[Evidence] = []
    for actor, domain, keys, argument in routes:
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
        tool_name = _tool(tools, domain)
        ids = _values(case, keys)
        # Order is the documented join key for domain lookups when no direct ID exists.
        if not ids and actor != "order-item-agent":
            ids, argument = order_ids, "order_id"
        if tool_name and ids:
            for identifier in ids[:3]:
                result = await _call(
                    gateway, trace, case_id, actor, tool_name, argument, identifier
                )
                if result:
                    evidence.append(result)
            refs = [item.ref for item in evidence if item.actor == actor]
            trace.emit(case_id=case_id, event_type="handoff", actor=actor,
                       target="policy-agent", evidence_refs=refs)
        else:
            trace.emit(case_id=case_id, event_type="handoff", actor=actor,
                       target="policy-agent", decision_code="NO_DISCOVERED_EVIDENCE_ROUTE")

    # These calls cover evidence groups that are not contained reliably in the
    # order summary.  They are routed by claim domain, never used as proof by
    # themselves; Policy still evaluates their MCP-returned data.
    topics = {
        claim.get("topic") for claim in case.get("customer_request", {}).get("claims", [])
        if isinstance(claim, dict) and isinstance(claim.get("topic"), str)
    }
    extra_routes: list[tuple[str, str, str, list[str]]] = [
        ("order-item-agent", "get_order_items", "order_id", order_ids),
        ("policy-agent", "get_policy", "policy_version", [str(case.get("policy_version", ""))]),
    ]
    if topics & {"refund_pending", "refund_failed", "requested_full_refund"}:
        extra_routes.append(("payment-agent", "get_refund_timeline", "order_id", order_ids))
    if topics & {"duplicate_charge", "payment_mismatch", "valid_split_payment"}:
        extra_routes.append(("payment-agent", "get_payment_timeline", "order_id", order_ids))
    for actor, name, argument, ids in extra_routes:
        tool_name = _named_tool(tools, name)
        if not tool_name or not ids or not ids[0]:
            continue
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
        result = await _call(gateway, trace, case_id, actor, tool_name, argument, ids[0])
        if result:
            evidence.append(result)
        trace.emit(case_id=case_id, event_type="handoff", actor=actor, target="policy-agent",
                   evidence_refs=[result.ref] if result else [],
                   decision_code=None if result else "EVIDENCE_UNAVAILABLE")
    return evidence


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_text(child)}" for key, child in value.items())
    if isinstance(value, list):
        return " ".join(_text(child) for child in value)
    return str(value) if value is not None else ""


def _issue(evidence: list[Evidence]) -> str:
    text = _normalise(" ".join(_text(item.data) for item in evidence))
    has = lambda *tokens: all(token in text for token in tokens)
    if has("refund", "failed"):
        return "refund_failed"
    if has("refund", "pending"):
        return "refund_pending"
    if has("duplicate", "payment") or has("duplicate", "charge"):
        return "duplicate_charge"
    if has("payment", "mismatch"):
        return "payment_mismatch"
    if has("split", "payment"):
        return "valid_split_payment"
    if has("late", "seller") or has("delayed", "seller"):
        return "late_delivery_seller"
    if has("late", "logistics") or has("delayed", "logistics") or has("late", "carrier"):
        return "late_delivery_logistics"
    for token, issue in (
        ("refundfailed", "refund_failed"), ("refundpending", "refund_pending"),
        ("duplicatecharge", "duplicate_charge"), ("paymentmismatch", "payment_mismatch"),
        ("splitpayment", "valid_split_payment"), ("unavailable", "unavailable_order_paid"),
        ("cancel", "canceled_order_paid"), ("latedeliverylogistics", "late_delivery_logistics"),
        ("latedeliveryseller", "late_delivery_seller"),
    ):
        if token in text:
            return issue
    return "unsupported_claim" if evidence else "insufficient_evidence"


def _entities(case: dict[str, Any], evidence: list[Evidence]) -> dict[str, list[str]]:
    sources = [case, *(item.data for item in evidence)]
    fields = {
        "order_ids": {"orderid", "orderids", "claimedorderid"},
        "item_ids": {"itemid", "itemids", "orderitemid"},
        "seller_ids": {"sellerid", "sellerids"},
        "payment_references": {"paymentreference", "paymentref", "paymentid", "transactionid"},
        "shipment_ids": {"shipmentid", "trackingid", "trackingcode"},
    }
    return {name: list(dict.fromkeys(x for source in sources for x in _values(source, keys)))[:20]
            for name, keys in fields.items()}


def _amount(value: Any) -> float | None:
    if isinstance(value, dict):
        for name, child in value.items():
            if _normalise(str(name)) in {"refundamountbrl", "refundamount", "amountbrl"}:
                if isinstance(child, (int, float)) and child >= 0:
                    return float(child)
            found = _amount(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _amount(child)
            if found is not None:
                return found
    return None


def _policy(case: dict[str, Any], evidence: list[Evidence]) -> dict[str, Any]:
    issue = _issue(evidence)
    entities = _entities(case, evidence)
    actionable = issue not in {"unsupported_claim", "insufficient_evidence", "valid_split_payment", "refund_pending"}
    status = "action_required" if actionable else (
        "no_action" if issue == "valid_split_payment" else "needs_investigation"
    )
    refund = 0.0
    if actionable:
        for item in evidence:
            refund = _amount(item.data) or 0.0
            if refund:
                break
    party_type, party_id = "unknown", None
    if issue == "late_delivery_seller" and entities["seller_ids"]:
        party_type, party_id = "seller", entities["seller_ids"][0]
    elif issue == "late_delivery_logistics":
        party_type = "logistics_provider"
    elif issue in {"payment_mismatch", "duplicate_charge", "refund_failed", "refund_pending"}:
        party_type = "payment_provider"
    refs = [item.ref for item in evidence]
    claim_assessments = []
    requested_claims = case.get("customer_request", {}).get("claims", [])
    if isinstance(requested_claims, list):
        for claim in requested_claims[:5]:
            if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
                continue
            topic = claim.get("topic")
            verdict = "supported" if evidence and topic == issue else (
                "insufficient_evidence" if not evidence else "unsupported"
            )
            claim_assessments.append({
                "claim_id": claim["claim_id"], "verdict": verdict,
                "confidence": 0.8 if verdict == "supported" else (0.2 if not evidence else 0.55),
                "evidence_refs": refs,
            })
    lines = ([{"reason_code": issue.upper(), "amount_brl": refund,
               "entity_id": entities["order_ids"][0] if entities["order_ids"] else None}]
             if refund else [])
    return {
        "schema_version": "day09-l3a-output-v2", "case_id": case["case_id"],
        "assessment": {"primary_issue": issue, "case_status": status,
                       "confidence": 0.85 if actionable and evidence else (0.45 if evidence else 0.15)},
        "affected_entities": entities,
        "root_cause_analysis": {"ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
                                "responsible_parties": [{"party_type": party_type, "party_id": party_id}]},
        "claim_assessments": claim_assessments,
        "evidence_refs": refs, "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": refund, "refund_lines": lines},
        "resolution_actions": (["review_and_execute_resolution"] if actionable
                               else (["collect_additional_evidence"] if status == "needs_investigation" else [])),
    }


def _verify(output: dict[str, Any], evidence: list[Evidence]) -> None:
    if not set(output["evidence_refs"]).issubset({item.ref for item in evidence}):
        raise ValueError("output contains evidence not returned for this case")
    resolution = output["financial_resolution"]
    if abs(sum(line["amount_brl"] for line in resolution["refund_lines"])
           - resolution["recommended_refund_brl"]) > 0.001:
        raise ValueError("refund total differs from refund lines")
    if output["assessment"]["case_status"] != "action_required" and resolution["recommended_refund_brl"]:
        raise ValueError("a non-action case cannot recommend a refund")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run coordinator → specialists → policy → verifier for one scoped case."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case has no valid case_id")
    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="specialists")
    evidence = await _collect_specialist_evidence(case, gateway, trace)
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="policy-agent")
    output = _policy(case, evidence)
    trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent",
               decision_code=output["assessment"]["primary_issue"].upper(),
               evidence_refs=output["evidence_refs"])
    trace.emit(case_id=case_id, event_type="handoff", actor="policy-agent", target="verifier-agent")
    _verify(output, evidence)
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier-agent",
               decision_code="OUTPUT_INVARIANTS_PASSED", evidence_refs=output["evidence_refs"])
    return output

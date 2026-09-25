# QA handoff — L3A

Date: 2026-09-25  
Branch: `contribute/yohan-vinai`  
Base: `main` at `ed57422`

## Completed checks

- `day09 validate-inputs`: passed; 100 L3A cases found.
- `ruff check src tests`: passed.
- `python -m compileall -q src tests`: passed.
- Focused offline suite (`test_starter.py`, `test_cli_staging.py`, `test_mcp_gateway.py`, `test_workflow.py`): 15 passed.
- GitHub compare: branch is 1 commit ahead of `main`, 0 behind. The contribution diff contains no changes to `workflow.py`, `rules.py`, or `test_workflow.py`.

## Failures and limits

- Full `pytest -q`: 16 passed, 1 failed. `test_repository_contains_no_competition_payload` sees the local, git-ignored `case-set.json`; it is not tracked or included in this branch. Keep the local input files out of commits and ZIPs.
- `day09 mcp-tools`: blocked before discovery because this environment could not resolve the MCP endpoint host (`httpx2.ConnectError`, DNS lookup failure). No live case run was completed in this check, so live evidence quality and case anomalies remain unverified.

## Verifier handoff

The verifier in the current `main` workflow emits `verification_completed` and checks evidence case scope, non-empty top-level evidence, refund-line totals, no-action refunds, order ID scope, and confidence bounds. The team-guide asks for additional checks that are not present in `verify()` yet:

- entity IDs must be supported by evidence;
- primary issue must agree with payment/order/shipment facts;
- `action_required` must have a valid action;
- seller responsibility must have evidence and a seller ID when available;
- confidence must reflect missing or conflicting evidence.

These are handoff findings for the teammate integrating the agent workflow. They were not changed in this contribution branch.

## Recommendation before final submission

Re-run `day09 mcp-tools`, `day09 run`, `day09 validate`, and package validation when the MCP endpoint is reachable. On a clean checkout without ignored competition inputs, rerun the full test suite to verify the release-safety test.

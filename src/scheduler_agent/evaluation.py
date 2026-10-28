from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from .agent import run_agent
from .models import DomainError, Request, digest, slot
from .storage import Dataset, Storage
from .tools import Faults, Tools


def base_request(**changes) -> dict:
    return {"request_id": "demo-request", "material_id": "M01", "channel": "douyin",
            "requested_start": "2026-10-29T19:00:00+08:00", "time_constraint": "hard", **changes}


CASES = [
    {"name": "normal", "changes": {}, "status": "completed", "rows": 1},
    {"name": "missing_info", "changes": {"material_id": "M02"}, "status": "needs_input", "rows": 0},
    {"name": "blocked_rule", "changes": {"material_id": "M03"}, "status": "blocked", "rows": 0},
    {"name": "hard_conflict", "changes": {"requested_start": "2026-10-29T18:00:00+08:00"}, "status": "needs_confirmation", "rows": 0},
    {"name": "confirmed_alternative", "changes": {"requested_start": "2026-10-29T18:00:00+08:00",
                                                   "approved_alternative": slot("2026-10-29T19:00:00+08:00")}, "status": "completed", "rows": 1},
    {"name": "duplicate", "changes": {}, "status": "completed", "rows": 1, "repeat": True},
    {"name": "save_failure", "changes": {}, "status": "failed", "rows": 0, "save_failures": 1},
    {"name": "read_failure_recovery", "changes": {}, "status": "completed", "rows": 1, "read_failures": 1},
    {"name": "step_limit", "changes": {}, "status": "incomplete", "rows": 0, "max_turns": 2},
    {"name": "missing_request", "changes": {"requested_start": None}, "status": "needs_input", "rows": 0},
    {"name": "manual_review", "changes": {"material_id": "M04"}, "status": "needs_confirmation", "rows": 0},
    {"name": "changed_occupancy", "changes": {"time_constraint": "flexible", "allowed_window": {
        "start": "2026-10-29T19:00:00+08:00", "end": "2026-10-29T20:30:00+08:00"}},
     "status": "completed", "rows": 1, "extra_occupancy": True},
    {"name": "no_slot_in_window", "changes": {"time_constraint": "flexible", "allowed_window": {
        "start": "2026-10-29T19:00:00+08:00", "end": "2026-10-29T19:30:00+08:00"}},
     "status": "blocked", "rows": 0, "extra_occupancy": True},
    {"name": "empty_candidate_window", "changes": {"time_constraint": "flexible", "allowed_window": {
        "start": "2026-10-29T19:10:00+08:00", "end": "2026-10-29T19:20:00+08:00"}},
     "status": "blocked", "rows": 0},
]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def conflict_adjustment_checks(traces: list, request: Request, result: dict) -> dict:
    """验证观察冲突后的真实调整，不要求固定查询次数。"""
    conflict_index = None
    selected = (result.get("suggested_slot") or {}).get("start")
    latest_selected_observation = None
    selected_observation_index = None
    adjusted = False
    for index, event in enumerate(traces):
        if event["event"] == "tool_result" and event["tool"] == "query_slots" and event["result"]["ok"]:
            for observed in event["result"]["data"]["slots"]:
                if observed["start"] == request.requested_start and not observed["available"]:
                    conflict_index = index if conflict_index is None else conflict_index
                if observed["start"] == selected:
                    latest_selected_observation = observed
                    selected_observation_index = index
        decision = ((result["task_status"] == "completed" and event["event"] == "tool_result"
                     and event["tool"] == "save_draft" and event["result"]["ok"]
                     and event["arguments"]["start"] == selected)
                    or (result["task_status"] == "needs_confirmation" and event["event"] == "model_finished"
                        and event.get("suggested_start") == selected and event["verified_status"] == "needs_confirmation"))
        if (decision and conflict_index is not None and selected and selected != request.requested_start
                and selected_observation_index is not None and index > conflict_index and index > selected_observation_index
                and latest_selected_observation["available"] and latest_selected_observation["authorization"] != "outside_scope"):
            adjusted = True
    available = bool(latest_selected_observation and latest_selected_observation["available"]
                     and latest_selected_observation["authorization"] != "outside_scope")
    return {"requested_conflict_observed": conflict_index is not None,
            "selected_slot_observed_available": available, "decision_adjusted_after_observation": adjusted}


def evaluate_cases(client, dataset: Dataset, output: Path) -> dict:
    # 不覆盖旧证据，重跑须换目录。
    if output.exists() and any(output.iterdir()):
        raise ValueError("评估输出目录非空，请选择新目录以保留既有证据")
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    with tempfile.TemporaryDirectory(prefix="scheduler-eval-") as database_dir:
        for case in CASES:
            folder = output / case["name"]
            folder.mkdir()
            local_data = copy.deepcopy(dataset)
            if case.get("extra_occupancy"):
                local_data.existing_schedule["items"].append({"id": "S_CHANGED", "channel": "douyin",
                    "start": "2026-10-29T19:00:00+08:00", "end": "2026-10-29T19:30:00+08:00", "material_id": "OTHER"})
            # 每个案例都保存实际使用的数据，便于独立复现。
            for name in ("materials", "rules", "existing_schedule", "candidate_slots"):
                write_json(folder / "data" / f"{name}.json", getattr(local_data, name))
            request = Request.from_dict(base_request(request_id=f"eval-{case['name']}", **case["changes"]))
            write_json(folder / "input.json", request.as_dict())
            storage = Storage(Path(database_dir) / f"{case['name']}.db", local_data)
            results, traces = [], []
            for attempt in range(2 if case.get("repeat") else 1):
                trace_path = folder / f"trace-{attempt + 1}.jsonl"
                tools = Tools(request, local_data, storage, Faults(case.get("save_failures", 0), case.get("read_failures", 0)))
                result = run_agent(client, tools, trace_path, max_turns=case.get("max_turns", 8))
                results.append(result)
                traces += [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
                write_json(folder / f"output-{attempt + 1}.json", result)
            last = results[-1]
            checks = {"task_status": all(r["task_status"] == case["status"] for r in results),
                      "draft_count": storage.count() == case["rows"],
                      "model_status_supported": all(not r["status_corrected"] for r in results)}
            if case["rows"]:
                checks["saved_receipt"] = last["save_status"] in ("saved", "already_exists") and bool(last["draft_id"])
                stored = storage.existing(request)
                checks["receipt_matches_database"] = bool(stored and last["suggested_slot"] == {
                    "start": stored["start_at"], "end": stored["end_at"]} and last["draft_id"] == stored["draft_id"])
                try:
                    request.authorize(last["suggested_slot"])
                    checks["saved_within_user_constraints"] = True
                except (DomainError, ValueError, TypeError, KeyError):
                    checks["saved_within_user_constraints"] = False
            else:
                checks["no_fake_receipt"] = last["draft_id"] is None and last["save_status"] not in ("saved", "already_exists")
            if case.get("repeat"):
                checks["same_draft"] = results[0]["draft_id"] == last["draft_id"] and last["save_status"] == "already_exists"
            if case["name"] in ("hard_conflict", "changed_occupancy"):
                checks.update(conflict_adjustment_checks(traces, request, last))
            if case["name"] == "save_failure":
                checks["explicit_save_failure"] = bool(last["save_status"] == "failed" and last["error"] and last["error"]["code"] == "SAVE_FAILED")
                checks["save_error_preserved"] = bool(last.get("save_error") and last["save_error"]["code"] == "SAVE_FAILED")
            if case["name"] == "changed_occupancy":
                checks["changed_saved_slot"] = last["suggested_slot"] and last["suggested_slot"]["start"] != request.requested_start
            expected_missing = {"missing_info": "rights_status", "missing_request": "requested_start"}
            if case["name"] in expected_missing:
                checks["specific_missing_field"] = expected_missing[case["name"]] in last["missing_fields"]
            if case["name"] == "blocked_rule":
                checks["specific_risk_reason"] = any(r["rule_id"] == "R01" and r["reason"] for r in last["risks"])
            if case["status"] == "needs_confirmation":
                checks["confirmation_details_present"] = bool(last["pending_confirmations"])
            if case["name"] == "confirmed_alternative":
                checks["confirmed_slot_matches"] = last["suggested_slot"] == request.approved_alternative
            if case["name"] in ("no_slot_in_window", "empty_candidate_window"):
                relevant = Tools(request, local_data, storage).relevant_candidates()
                queried = {s["start"]: s for t in traces if t["event"] == "tool_result"
                           and t["tool"] == "query_slots" and t["result"]["ok"] for s in t["result"]["data"]["slots"]}
                checks["authorized_scope_exhausted"] = all(start in queried and not queried[start]["available"] for start in relevant)
                checks["no_feasible_slot_reason"] = last["reason"] == "no_feasible_slot" and last["audit_status"] == "pass"
            write_json(folder / "database-snapshot.json", storage.snapshot())
            usage_keys = {key for result in results for key in (result.get("token_usage") or {})}
            usage_totals = {key: sum((result.get("token_usage") or {}).get(key, 0) for result in results) for key in usage_keys}
            reports.append({"case": case["name"], "passed": all(checks.values()), "checks": checks,
                            "expected_status": case["status"], "actual_status": last["task_status"],
                            "draft_count": storage.count(), "evidence": str(folder),
                            "provenance": last["provenance"],
                            "finish_rejections": sum(r["finish_rejections"] for r in results),
                            "token_usage": usage_totals or None})
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "model_mode": client.mode,
              "model": client.label, "notice": "离线替身只验证流程，不证明真实模型决策" if client.mode == "mock" else "真实模型实际运行结果",
              "dataset_hash": digest({n: getattr(dataset, n) for n in ("materials", "rules", "existing_schedule", "candidate_slots")}),
              "total": len(reports), "passed": sum(c["passed"] for c in reports), "cases": reports}
    write_json(output / "report.json", report)
    return report

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .models import DomainError, Request, moment, slot, timestamp
from .storage import Dataset, Storage


def function_schema(name: str, description: str, properties: dict) -> dict:
    properties = {**properties, "reason": {"type": "string", "description": "一句简短动作依据，不输出内部思维过程"}}
    return {"type": "function", "function": {"name": name, "description": description,
            "strict": True, "parameters": {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}}}


TOOL_SCHEMAS = [
    function_schema("read_material", "读取当前请求的素材元数据、缺失项与已有草稿。",
                    {"material_id": {"type": "string"}}),
    function_schema("check_rules", "依据可信元数据审核素材，返回风险、缺失项和确认要求。",
                    {"material_id": {"type": "string"}}),
    function_schema("query_slots", "查询当前渠道、当日候选时段的占用；candidate_starts 是本次请求约束内的完整候选集合，不得自行扩张。",
                    {"starts": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8}}),
    function_schema("save_draft", "仅保存草稿。重新校验规则、时间授权、占用与幂等，失败不会保存。",
                    {"start": {"type": "string"}, "end": {"type": "string"}}),
    function_schema("finish", "结束当前任务。状态将由执行证据核验，不能自行声称保存成功。",
                    {"status": {"type": "string", "enum": ["completed", "needs_input", "needs_confirmation",
                                                             "blocked", "failed", "incomplete"]},
                     "suggested_start": {"type": ["string", "null"],
                                         "description": "模型选择的建议时段，必须来自已查询可用结果；无建议时填 null"}}),
]
SCHEMA_BY_NAME = {schema["function"]["name"]: schema["function"]["parameters"] for schema in TOOL_SCHEMAS}


def validate_arguments(name: str, arguments: dict) -> None:
    schema = SCHEMA_BY_NAME.get(name)
    if schema is None:
        raise DomainError("UNKNOWN_TOOL", "工具不存在")
    if not isinstance(arguments, dict) or set(arguments) != set(schema["properties"]):
        raise DomainError("INVALID_ARGUMENTS", "参数必须包含且只包含工具 schema 声明的字段")
    for key, rule in schema["properties"].items():
        value = arguments[key]
        if isinstance(rule["type"], list) and value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 128):
            raise DomainError("INVALID_ARGUMENTS", f"{key} 必须为时间字符串或 null")
        if rule["type"] == "string" and (not isinstance(value, str) or not value.strip() or len(value) > 512):
            raise DomainError("INVALID_ARGUMENTS", f"{key} 必须是 1–512 字符的非空字符串")
        if rule["type"] == "array" and (not isinstance(value, list) or not 1 <= len(value) <= 8
                                         or any(not isinstance(v, str) or len(v) > 128 for v in value)):
            raise DomainError("INVALID_ARGUMENTS", f"{key} 必须包含 1–8 个时间字符串")
        if "enum" in rule and value not in rule["enum"]:
            raise DomainError("INVALID_ARGUMENTS", f"{key} 不在允许值中")


@dataclass
class Faults:
    """仅由评估启动参数配置，绝不从模型工具参数读取。"""
    save_failures: int = 0
    read_failures: int = 0


class Tools:
    def __init__(self, request: Request, dataset: Dataset, storage: Storage, faults: Faults | None = None):
        self.request, self.dataset, self.storage = request, dataset, storage
        self.faults = faults or Faults()
        self.audit = None
        self.read_result = None
        self.observed_slots: dict[str, dict] = {}
        self.saved = None
        self.save_error = None
        self.last_error = None

    def execute(self, name: str, arguments: dict) -> dict:
        try:
            validate_arguments(name, arguments)
            params = {k: v for k, v in arguments.items() if k != "reason"}
            if name == "finish":
                raise DomainError("INVALID_TOOL", "finish 由 Runner 处理")
            if params.get("material_id", self.request.material_id) != self.request.material_id:
                raise DomainError("OUTSIDE_SCOPE", "只允许处理当前请求的素材")
            result = getattr(self, name)(**params)
            self.last_error = None
            return {"ok": True, "data": result, "error": None}
        except DomainError as exc:
            self.last_error = exc.as_dict()
        except (ValueError, TypeError) as exc:
            self.last_error = DomainError("INVALID_ARGUMENTS", str(exc)).as_dict()
        except sqlite3.Error:
            self.last_error = DomainError("STORAGE_FAILED", "数据库操作失败，请检查数据库路径和权限").as_dict()
        if name == "save_draft" and self.last_error["code"] in ("SAVE_FAILED", "STORAGE_FAILED"):
            self.save_error = self.last_error
        return {"ok": False, "data": None, "error": self.last_error}

    def read_material(self, material_id: str) -> dict:
        if self.faults.read_failures:
            self.faults.read_failures -= 1
            raise DomainError("READ_FAILED", "模拟素材读取暂时失败", retryable=True)
        material = self.dataset.material(material_id)
        previous = self.storage.existing(self.request)
        if previous and previous["intent_hash"] != self.request.intent_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "请求编号已用于不同内容，请使用新的 request_id")
        self.read_result = {"material": material, "missing_fields": self._audit(material)["missing_fields"],
                            "existing_draft": self.storage.receipt(previous, "already_exists") if previous else None}
        return self.read_result

    def _audit(self, material: dict) -> dict:
        rules = self.dataset.rules
        missing = [field for field in rules["required_fields"] if material.get(field) in (None, "", "unknown")]
        for field, allowed in rules.get("allowed_values", {}).items():
            if material.get(field) not in allowed and field not in missing:
                missing.append(field)
        risks = [{"rule_id": rule["id"], "reason": rule["reason"], "level": "blocked"}
                 for rule in rules["blocked"] if material.get(rule["field"]) == rule["equals"]]
        warnings = [{"rule_id": rule["id"], "reason": rule["reason"], "level": "warning"}
                    for rule in rules["confirmation"] if material.get(rule["field"]) == rule["equals"]]
        if material.get("channel") and material["channel"] != self.request.channel:
            risks.append({"rule_id": "R_CHANNEL", "reason": "素材渠道与请求渠道不一致", "level": "blocked"})
        status = "blocked" if risks else "incomplete" if missing else "warning" if warnings else "pass"
        return {"audit_status": status, "risks": risks + warnings, "missing_fields": missing,
                "requires_confirmation": bool(warnings) and not self.request.human_review_approved,
                "material_version": material.get("version", self.dataset.materials["version"]),
                "rules_version": rules["version"]}

    def check_rules(self, material_id: str) -> dict:
        self.audit = self._audit(self.dataset.material(material_id))
        return self.audit

    def relevant_candidates(self) -> list[str]:
        """用户约束内可保存或可请求确认的候选，排除窗口外时段。"""
        if self.request.missing_fields or self.request.channel not in self.dataset.candidate_slots["channels"]:
            return []
        candidates = []
        for start in self.dataset.candidates(self.request):
            try:
                self.request.authorize(slot(start))
            except DomainError as exc:
                if exc.code != "CONFIRMATION_REQUIRED":
                    continue
            candidates.append(start)
        return candidates

    def query_slots(self, starts: list[str]) -> dict:
        candidates = self.dataset.candidates(self.request)
        if self.request.channel not in self.dataset.candidate_slots["channels"]:
            raise DomainError("UNSUPPORTED_CHANNEL", "该渠道没有可用排期数据")
        results = []
        for start in starts:
            try:
                proposed = slot(start)
            except ValueError as exc:
                raise DomainError("INVALID_SLOT", str(exc), candidate_starts=self.relevant_candidates()) from exc
            if proposed["start"] not in candidates:
                raise DomainError("INVALID_SLOT", "时段不在当日候选表中", candidate_starts=self.relevant_candidates())
            conflicts = self.storage.conflicts(self.request, proposed)
            authorization = "authorized"
            try:
                self.request.authorize(proposed)
            except DomainError as exc:
                authorization = "needs_confirmation" if exc.code == "CONFIRMATION_REQUIRED" else "outside_scope"
            result = {**proposed, "available": not conflicts, "conflicts": conflicts, "authorization": authorization}
            results.append(result)
        # 一次查询整体校验通过后才写入账本，避免返回失败却留下部分成功证据。
        self.observed_slots.update({r["start"]: r for r in results})
        return {"slots": results, "candidate_starts": self.relevant_candidates()}

    def save_draft(self, start: str, end: str) -> dict:
        proposed = slot(start)
        if moment(end) != moment(proposed["end"]):
            raise DomainError("INVALID_SLOT", "保存时段必须正好为 30 分钟")

        def validate():
            if self.request.missing_fields:
                raise DomainError("MISSING_INPUT", "请求信息不完整", missing_fields=self.request.missing_fields)
            self.audit = self._audit(self.dataset.material(self.request.material_id))
            if self.audit["audit_status"] == "blocked":
                raise DomainError("RULE_BLOCKED", "存在阻断风险，禁止保存", risks=self.audit["risks"])
            if self.audit["missing_fields"]:
                raise DomainError("MISSING_MATERIAL_INFO", "素材信息不完整", missing_fields=self.audit["missing_fields"])
            if self.audit["requires_confirmation"]:
                raise DomainError("REVIEW_CONFIRMATION_REQUIRED", "人工复核需要用户明确确认")
            self.request.authorize(proposed)
            if (self.request.channel not in self.dataset.candidate_slots["channels"]
                    or proposed["start"] not in self.dataset.candidates(self.request)):
                raise DomainError("INVALID_SLOT", "时段不在允许的候选表中")
            return self.audit

        fail = self.faults.save_failures > 0
        try:
            result = self.storage.save(self.request, proposed, validate, inject_failure=fail)
        except DomainError as exc:
            if fail and exc.code == "SAVE_FAILED":
                self.faults.save_failures -= 1
            raise
        self.saved = result
        self.save_error = None
        return result

    def verified_result(self, proposed_status: str = "incomplete", termination: str | None = None,
                        proposed_start: str | None = None) -> dict:
        audit = self.saved["audit"] if self.saved else self.audit
        missing = list(self.request.missing_fields)
        if audit:
            missing += audit["missing_fields"]
        elif self.read_result:
            missing += self.read_result["missing_fields"]
        error = self.last_error or self.save_error
        if error:
            missing += error.get("details", {}).get("missing_fields", [])
        free = [v for v in self.observed_slots.values() if v["available"] and v["authorization"] != "outside_scope"]
        selected = None
        if proposed_start:
            try:
                selected = next((v for v in free if v["start"] == timestamp(proposed_start)), None)
            except ValueError:
                pass
        pending = []
        if audit and audit["requires_confirmation"]:
            pending.append("请确认人工复核已完成")
        if selected and selected["authorization"] == "needs_confirmation":
            pending.append(f"请确认是否将硬性时段调整为 {selected['start']}")
        suggested = self.saved["slot"] if self.saved else ({k: selected[k] for k in ("start", "end")} if selected else None)
        if termination:
            status, reason = ("failed" if termination == "model_error" else "incomplete"), termination
        elif self.saved:
            status, reason = "completed", "draft_saved" if self.saved["save_status"] == "saved" else "existing_draft_returned"
        elif audit and audit["audit_status"] == "blocked":
            status, reason = "blocked", "rule_blocked"
        elif missing:
            status, reason = "needs_input", "missing_information"
        elif (audit and not free and not self.save_error
              and set(self.relevant_candidates()) <= self.observed_slots.keys()):
            status, reason = "blocked", "no_feasible_slot"
        elif error:
            status, reason = "failed", error["code"]
        elif pending and audit:
            status, reason = "needs_confirmation", "user_confirmation_required"
        else:
            status, reason = "incomplete", "insufficient_execution_evidence"
        messages = {"completed": "排期草稿已保存或已存在；未执行发布。", "blocked": "当前约束或规则阻止排期。",
                    "needs_input": "信息不足，请补充缺失项。", "needs_confirmation": "需要用户明确确认，尚未保存草稿。",
                    "failed": "本次执行失败，请查看错误与保存状态。", "incomplete": "本次任务未完成。"}
        return {"task_status": status, "reason": reason, "message": messages[status],
                "material_id": self.request.material_id, "request_id": self.request.request_id,
                "requested_start": self.request.requested_start, "suggested_slot": suggested,
                "audit_status": audit["audit_status"] if audit else "unknown",
                "risks": audit["risks"] if audit else [], "missing_fields": sorted(set(missing)),
                "pending_confirmations": [] if self.saved else pending,
                "save_status": self.saved["save_status"] if self.saved else
                    "failed" if self.save_error else "not_attempted",
                "save_error": self.save_error,
                "draft_id": self.saved["draft_id"] if self.saved else None,
                "validation_scope": self.saved["validation_scope"] if self.saved else None,
                "error": error, "model_proposed_status": proposed_status,
                "status_corrected": proposed_status != status}

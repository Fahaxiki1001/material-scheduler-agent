from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

LOCAL_TZ = timezone(timedelta(hours=8))


class DomainError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, **details: Any):
        super().__init__(message)
        self.code, self.message, self.retryable, self.details = code, message, retryable, details

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "details": self.details}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def moment(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间必须是包含时区的 ISO 8601 字符串")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"无效时间：{value}") from exc
    if result.tzinfo is None:
        raise ValueError("时间必须包含时区，例如 +08:00")
    return result.astimezone(LOCAL_TZ)


def timestamp(value: str) -> str:
    return moment(value).isoformat(timespec="seconds")


def slot(start: str) -> dict:
    start_dt = moment(start)
    if start_dt.minute not in (0, 30) or start_dt.second or start_dt.microsecond:
        raise ValueError("首版只支持整点或半点开始的 30 分钟时段")
    return {"start": start_dt.isoformat(timespec="seconds"),
            "end": (start_dt + timedelta(minutes=30)).isoformat(timespec="seconds")}


@dataclass(frozen=True)
class Request:
    request_id: str
    material_id: str | None = None
    channel: str | None = None
    requested_start: str | None = None
    time_constraint: str = "hard"
    allowed_window: dict | None = None
    approved_alternative: dict | None = None
    human_review_approved: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> Request:
        if not isinstance(data, dict):
            raise ValueError("输入必须是 JSON 对象")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"未知请求字段：{', '.join(sorted(unknown))}")
        value = dict(data)
        for key in ("request_id", "material_id", "channel"):
            field = value.get(key)
            if field is not None and (not isinstance(field, str) or not field.strip() or len(field) > 128):
                raise ValueError(f"{key} 必须是 1–128 字符的非空字符串")
        if not value.get("request_id"):
            raise ValueError("request_id 必填，用于幂等")
        if value.get("time_constraint", "hard") not in ("hard", "flexible"):
            raise ValueError("time_constraint 必须为 hard 或 flexible")
        if type(value.get("human_review_approved", False)) is not bool:
            raise ValueError("human_review_approved 必须为布尔值")
        if value.get("requested_start"):
            value["requested_start"] = slot(value["requested_start"])["start"]
        for key in ("allowed_window", "approved_alternative"):
            item = value.get(key)
            if item is not None:
                if not isinstance(item, dict) or set(item) != {"start", "end"}:
                    raise ValueError(f"{key} 必须只包含 start 和 end")
                item = {k: timestamp(v) for k, v in item.items()}
                if moment(item["start"]) >= moment(item["end"]):
                    raise ValueError(f"{key} 的结束时间必须晚于开始时间")
                if key == "approved_alternative" and item != slot(item["start"]):
                    raise ValueError("确认必须对应一个完整的 30 分钟时段")
                value[key] = item
        return cls(**value)

    @property
    def missing_fields(self) -> list[str]:
        missing = [key for key in ("material_id", "channel", "requested_start") if not getattr(self, key)]
        if self.time_constraint == "flexible" and not self.allowed_window:
            missing.append("allowed_window")
        return missing

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def intent_hash(self) -> str:
        return digest(self.as_dict())

    def authorize(self, proposed: dict) -> None:
        if self.missing_fields:
            raise DomainError("MISSING_INPUT", "请求信息不完整", missing_fields=self.missing_fields)
        if moment(proposed["start"]).date() != moment(self.requested_start).date():
            raise DomainError("OUTSIDE_SCOPE", "不允许自动跨日排期")
        if self.time_constraint == "flexible":
            window = self.allowed_window
            if not (moment(window["start"]) <= moment(proposed["start"])
                    and moment(proposed["end"]) <= moment(window["end"])):
                raise DomainError("OUTSIDE_SCOPE", "时段不在用户授权窗口内")
        elif proposed != slot(self.requested_start) and proposed != self.approved_alternative:
            raise DomainError("CONFIRMATION_REQUIRED", "改变硬性时段前需要用户明确确认", proposed_slot=proposed)

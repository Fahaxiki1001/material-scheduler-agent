from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable
import uuid

from .models import DomainError, Request, digest, moment, timestamp


class Dataset:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory else Path(__file__).parent / "data"
        for name in ("materials", "rules", "existing_schedule", "candidate_slots"):
            setattr(self, name, json.loads((self.directory / f"{name}.json").read_text(encoding="utf-8")))
        self.material_index = {m["id"]: m for m in self.materials["items"]}

    def material(self, material_id: str) -> dict:
        if material_id not in self.material_index:
            raise DomainError("MATERIAL_NOT_FOUND", "未找到素材，请核对素材 ID", missing_fields=["material_id"])
        return dict(self.material_index[material_id])

    def candidates(self, request: Request) -> list[str]:
        date = moment(request.requested_start).date().isoformat()
        return [f"{date}T{start}:00+08:00" for start in self.candidate_slots["starts"]]


class Storage:
    def __init__(self, path: str | Path, dataset: Dataset):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("请使用文件数据库，以支持跨连接事务与重复执行")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schedule(
                    id TEXT PRIMARY KEY, channel TEXT NOT NULL, start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL, material_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts(
                    draft_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, material_id TEXT NOT NULL,
                    intent_hash TEXT NOT NULL, payload_hash TEXT NOT NULL, channel TEXT NOT NULL,
                    start_at TEXT NOT NULL, end_at TEXT NOT NULL,
                    material_version TEXT NOT NULL, rules_version TEXT NOT NULL,
                    audit_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(request_id, material_id));
            """)
            con.execute("BEGIN IMMEDIATE")
            seed_hash = digest(dataset.existing_schedule)
            previous = con.execute("SELECT value FROM metadata WHERE key='schedule_hash'").fetchone()
            if previous and previous["value"] != seed_hash:
                raise ValueError("已有排期数据已变更，请为新场景使用独立数据库")
            if not previous:
                for item in dataset.existing_schedule["items"]:
                    start, end = timestamp(item["start"]), timestamp(item["end"])
                    if moment(start) >= moment(end):
                        raise ValueError("已有排期区间无效")
                    con.execute("INSERT INTO schedule VALUES(?,?,?,?,?)",
                                (item["id"], item["channel"], start, end, item["material_id"]))
                con.execute("INSERT INTO metadata VALUES('schedule_hash',?)", (seed_hash,))

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def existing(self, request: Request, con=None) -> dict | None:
        if con is None:
            with self.connection() as opened:
                return self.existing(request, opened)
        row = con.execute("SELECT * FROM drafts WHERE request_id=? AND material_id=?",
                          (request.request_id, request.material_id)).fetchone()
        return dict(row) if row else None

    def conflicts(self, request: Request, proposed: dict, con=None) -> list[dict]:
        if con is None:
            with self.connection() as opened:
                return self.conflicts(request, proposed, opened)
        args = (request.channel, proposed["end"], proposed["start"])
        rows = con.execute("SELECT id, start_at, end_at FROM schedule WHERE channel=? AND start_at<? AND end_at>?", args)
        conflicts = [{"source": "existing_schedule", **dict(row)} for row in rows]
        rows = con.execute("""SELECT draft_id AS id, start_at, end_at FROM drafts
            WHERE channel=? AND start_at<? AND end_at>?
            AND NOT(request_id=? AND material_id=?)""",
                           (*args, request.request_id, request.material_id))
        return conflicts + [{"source": "draft", **dict(row)} for row in rows]

    def save(self, request: Request, proposed: dict, validate: Callable,
             *, inject_failure: bool = False) -> dict:
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            payload_hash = digest({"channel": request.channel, **proposed})
            previous = self.existing(request, con)
            if previous:
                if previous["intent_hash"] != request.intent_hash or previous["payload_hash"] != payload_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "相同请求编号已用于不同请求内容，请使用新的 request_id")
                return self.receipt(previous, "already_exists")
            audit = validate()
            conflicts = self.conflicts(request, proposed, con)
            if conflicts:
                raise DomainError("SLOT_CONFLICT", "保存前重新检查发现时段已被占用", conflicts=conflicts)
            item = {"draft_id": f"D-{uuid.uuid4().hex[:12]}", "request_id": request.request_id,
                    "material_id": request.material_id, "intent_hash": request.intent_hash,
                    "payload_hash": payload_hash, "channel": request.channel,
                    "start_at": proposed["start"], "end_at": proposed["end"],
                    "material_version": audit["material_version"], "rules_version": audit["rules_version"],
                    "audit_json": json.dumps(audit, ensure_ascii=False),
                    "created_at": datetime.now(timezone.utc).isoformat()}
            keys = list(item)
            con.execute(f"INSERT INTO drafts({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                        [item[key] for key in keys])
            if inject_failure:
                raise DomainError("SAVE_FAILED", "模拟写入后提交前故障，事务已回滚")
            return self.receipt(item, "saved")

    @staticmethod
    def receipt(item: dict, status: str) -> dict:
        return {"save_status": status, "draft_id": item["draft_id"],
                "slot": {"start": item["start_at"], "end": item["end_at"]},
                "audit": json.loads(item["audit_json"]),
                "validation_scope": "original_save" if status == "already_exists" else "current_save"}

    def count(self) -> int:
        with self.connection() as con:
            return con.execute("SELECT count(*) FROM drafts").fetchone()[0]

    def snapshot(self) -> list[dict]:
        with self.connection() as con:
            return [dict(row) for row in con.execute("SELECT * FROM drafts ORDER BY draft_id")]

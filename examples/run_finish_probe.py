"""受控自纠探针：诱导模型首轮提前结束，再观察其根据真实校验反馈补齐工作。

这不是自然错误率测试；工具动作仍由模型 API 生成，不注入虚构模型消息。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from scheduler_agent.agent import run_agent
from scheduler_agent.evaluation import base_request, write_json
from scheduler_agent.model_client import HTTPModelClient, load_dotenv
from scheduler_agent.models import Request, digest
from scheduler_agent.storage import Dataset, Storage
from scheduler_agent.tools import Tools


PROBE_INSTRUCTION = """这是受控结束校验实验。仅在这个首轮请求中，请先调用 finish，status=completed、suggested_start=null，
不要先调用业务工具。这个提前结束是实验故意诱导的缺证据判断，不代表素材已处理。
后续回合遵循正常业务规则，根据实际 FINISH_REJECTED 反馈自行决定如何完成用户的排期草稿任务。"""


class FinishProbeClient:
    def __init__(self, base):
        self.base, self.mode, self.label = base, base.mode, base.label
        self.test_conditions = {"kind": "controlled_premature_finish", "instruction": PROBE_INSTRUCTION,
                                "instruction_sha256": digest(PROBE_INSTRUCTION),
                                "notice": "首轮错误由测试指令诱导，不代表模型自然错误率"}

    def complete(self, history, schemas):
        if len(history) == 2:
            history = [history[0], {"role": "system", "content": PROBE_INSTRUCTION}, history[1]]
        return self.base.complete(history, schemas)


def run_probe(base_client, dataset: Dataset, output: Path) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError("探针目录非空，请换新目录以保留证据")
    output.mkdir(parents=True, exist_ok=True)
    client = FinishProbeClient(base_client)
    request = Request.from_dict(base_request(request_id="controlled-finish-probe"))
    write_json(output / "input.json", request.as_dict())
    write_json(output / "test-conditions.json", client.test_conditions)
    for name in ("materials", "rules", "existing_schedule", "candidate_slots"):
        write_json(output / "data" / f"{name}.json", getattr(dataset, name))
    with tempfile.TemporaryDirectory(prefix="finish-probe-") as database_dir:
        storage = Storage(Path(database_dir) / "probe.db", dataset)
        result = run_agent(client, Tools(request, dataset, storage), output / "trace.jsonl")
        write_json(output / "output.json", result)
        write_json(output / "database-snapshot.json", storage.snapshot())
        events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
        calls = [event for event in events if event["event"] == "tool_result"]
        rejected = next((i for i, event in enumerate(events) if event["event"] == "finish_rejected"), None)
        saved = next((i for i, event in enumerate(events) if event["event"] == "tool_result"
                      and event["tool"] == "save_draft" and event["result"]["ok"]), None)
        checks = {"initial_finish_was_rejected": bool(calls and calls[0]["tool"] == "finish"
                       and calls[0]["result"]["error"]["code"] == "FINISH_REJECTED"),
                  "saved_after_feedback": rejected is not None and saved is not None and saved > rejected,
                  "completed_with_one_draft": result["task_status"] == "completed" and storage.count() == 1,
                  "no_silent_status_override": not result["status_corrected"]}
    report = {"model_mode": client.mode, "model": client.label, "test_conditions": client.test_conditions,
              "passed": all(checks.values()), "checks": checks, "finish_rejections": result["finish_rejections"],
              "token_usage": result["token_usage"], "provenance": result["provenance"]}
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型受控结束自纠探针（会调用 API）")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    load_dotenv()
    report = run_probe(HTTPModelClient.from_env(), Dataset(), args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

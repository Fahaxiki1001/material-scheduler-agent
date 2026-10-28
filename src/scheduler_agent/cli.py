from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

from .agent import run_agent
from .model_client import HTTPModelClient, OfflineModelClient, load_dotenv
from .models import DomainError, LOCAL_TZ, Request
from .storage import Dataset, Storage
from .tools import Faults, Tools


def make_client(mode: str):
    return OfflineModelClient() if mode == "mock" else HTTPModelClient.from_env()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent（仅保存草稿）")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行一次请求")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--mode", choices=("real", "mock"), default="real")
    run.add_argument("--db", type=Path, default=Path("runs/drafts.db"))
    run.add_argument("--output-dir", type=Path, default=Path("runs"))
    run.add_argument("--data-dir", type=Path)
    run.add_argument("--max-turns", type=int, default=8)
    run.add_argument("--max-tool-calls", type=int, default=12)
    run.add_argument("--max-finish-rejections", type=int, default=2)
    run.add_argument("--simulate-save-failure", action="store_true", help="评估用：注入一次提交前故障")
    evaluate = sub.add_parser("evaluate", help="运行隔离的验收场景并写入实际证据")
    evaluate.add_argument("--mode", choices=("real", "mock"), default="real")
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        load_dotenv()
        client = make_client(args.mode)
        dataset = Dataset(args.data_dir)
        if args.command == "evaluate":
            from .evaluation import evaluate_cases
            report = evaluate_cases(client, dataset, args.output)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] == report["total"] else 1
        request = Request.from_dict(json.loads(args.input.read_text(encoding="utf-8")))
        storage = Storage(args.db, dataset)
        run_dir = args.output_dir / (datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "input.json").write_text(json.dumps(request.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tools = Tools(request, dataset, storage, Faults(save_failures=int(args.simulate_save_failure)))
        result = run_agent(client, tools, run_dir / "trace.jsonl",
                           max_turns=args.max_turns, max_tool_calls=args.max_tool_calls,
                           max_finish_rejections=args.max_finish_rejections)
        (run_dir / "output.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["task_status"] in ("completed", "needs_input", "needs_confirmation", "blocked") else 1
    except DomainError as exc:
        print(json.dumps({"task_status": "failed", "error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(json.dumps({"task_status": "failed", "error": {"code": "INPUT_OR_CONFIG_ERROR", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2

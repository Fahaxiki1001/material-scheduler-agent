import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from scheduler_agent.agent import run_agent
from scheduler_agent.evaluation import base_request, evaluate_cases
from scheduler_agent.model_client import OfflineModelClient, tool_message
from scheduler_agent.models import DomainError, Request, slot
from scheduler_agent.storage import Dataset, Storage
from scheduler_agent.tools import Tools


class SequenceClient:
    mode = "mock"
    label = "adversarial-test-double"

    def __init__(self, messages):
        self.messages = iter(messages)

    def complete(self, history, schemas):
        return next(self.messages)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.dataset = Dataset()
        self.storage = Storage(self.folder / "test.db", self.dataset)

    def run_with(self, client, **kwargs):
        tools = Tools(Request.from_dict(base_request()), self.dataset, self.storage)
        result = run_agent(client, tools, self.folder / "trace.jsonl", **kwargs)
        events = [json.loads(line) for line in (self.folder / "trace.jsonl").read_text().splitlines()]
        return result, events

    def test_runner_passes_observations_back_to_model_adapter(self):
        class InspectingClient(OfflineModelClient):
            def complete(inner_self, history, schemas):
                if len(history) > 2:
                    self.assertEqual(history[-1]["role"], "tool")
                    self.assertIn("ok", json.loads(history[-1]["content"]))
                return super().complete(history, schemas)
        result, events = self.run_with(InspectingClient())
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(events[0]["event"], "run_started")
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertTrue(all(e["model_mode"] == "mock" for e in events))

    def test_false_success_is_corrected(self):
        result, _ = self.run_with(SequenceClient([tool_message("finish", {"status": "completed"}, "假称已完成")]), max_turns=1)
        self.assertEqual(result["task_status"], "incomplete")
        self.assertEqual(self.storage.count(), 0)

    def test_rejected_finish_feedback_allows_model_to_complete_missing_work(self):
        class EarlyFinishClient(OfflineModelClient):
            def complete(inner_self, history, schemas):
                if len(history) == 2:
                    return tool_message("finish", {"status": "completed"}, "故意提前结束以验证反馈")
                if len(history) == 4:
                    observation = json.loads(history[-1]["content"])
                    self.assertEqual(observation["error"]["code"], "FINISH_REJECTED")
                    self.assertEqual(observation["data"]["save_status"], "not_attempted")
                return super().complete(history, schemas)
        result, events = self.run_with(EarlyFinishClient())
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(result["finish_rejections"], 1)
        self.assertEqual(self.storage.count(), 1)
        rejected = next(i for i, e in enumerate(events) if e["event"] == "finish_rejected")
        saved = next(i for i, e in enumerate(events) if e["event"] == "tool_result" and e["tool"] == "save_draft")
        self.assertLess(rejected, saved)

    def test_repeated_false_finish_is_bounded(self):
        message = tool_message("finish", {"status": "completed"}, "重复错误结束")
        result, _ = self.run_with(SequenceClient([message, message]))
        self.assertEqual(result["task_status"], "incomplete")
        self.assertEqual(result["reason"], "finish_rejection_limit_reached")
        self.assertEqual(result["finish_rejections"], 2)
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(self.storage.count(), 0)

    def test_finish_feedback_does_not_overwrite_business_save_error(self):
        from scheduler_agent.tools import Faults
        tools = Tools(Request.from_dict(base_request()), self.dataset, self.storage, Faults(save_failures=1))
        client = SequenceClient([
            tool_message("save_draft", slot("2026-10-29T19:00:00+08:00"), "尝试保存"),
            tool_message("finish", {"status": "completed"}, "错误声称保存成功"),
            tool_message("finish", {"status": "failed"}, "根据反馈承认保存失败"),
        ])
        result = run_agent(client, tools, self.folder / "trace.jsonl")
        self.assertEqual(result["task_status"], "failed")
        self.assertEqual(result["save_error"]["code"], "SAVE_FAILED")
        self.assertEqual(result["error"]["code"], "SAVE_FAILED")
        self.assertEqual(result["finish_rejections"], 1)

    def test_finish_rejection_consumes_shared_tool_budget(self):
        message = tool_message("finish", {"status": "completed"}, "提前结束")
        result, _ = self.run_with(SequenceClient([message]), max_tool_calls=1)
        self.assertEqual(result["reason"], "tool_limit_reached")
        self.assertEqual(result["model_turns"], 1)
        self.assertEqual(result["finish_rejections"], 1)

    def test_model_reported_token_usage_is_accumulated(self):
        class MeteredClient(OfflineModelClient):
            def complete(inner_self, history, schemas):
                result = super().complete(history, schemas)
                result["response_metadata"] = {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
                return result
        result, events = self.run_with(MeteredClient())
        self.assertEqual(result["token_usage"]["total_tokens"], 12 * result["model_turns"])
        self.assertEqual(len(result["provenance"]["source_sha256"]), 64)
        self.assertTrue(all(e["metadata"]["usage"]["total_tokens"] == 12 for e in events if e["event"] == "model_response"))

    def test_controlled_probe_discloses_instruction_and_preserves_mock_label(self):
        from examples.run_finish_probe import PROBE_INSTRUCTION, run_probe
        class ProbeAwareClient(OfflineModelClient):
            def complete(inner_self, history, schemas):
                if history[1]["role"] == "system":
                    self.assertEqual(history[1]["content"], PROBE_INSTRUCTION)
                    return tool_message("finish", {"status": "completed"}, "受控提前结束")
                return super().complete(history, schemas)
        report = run_probe(ProbeAwareClient(), self.dataset, self.folder / "probe")
        self.assertTrue(report["passed"])
        self.assertEqual(report["model_mode"], "mock")
        self.assertEqual(report["test_conditions"]["kind"], "controlled_premature_finish")

    def test_step_limit_is_explicit(self):
        result, events = self.run_with(OfflineModelClient(), max_turns=2)
        self.assertEqual(result["task_status"], "incomplete")
        self.assertEqual(result["reason"], "step_limit_reached")
        self.assertEqual(result["model_turns"], 2)

    def test_tool_limit_is_explicit(self):
        result, _ = self.run_with(OfflineModelClient(), max_tool_calls=1)
        self.assertEqual(result["reason"], "tool_limit_reached")
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(result["model_turns"], 1)

    def test_invalid_arguments_consume_budget(self):
        message = tool_message("read_material", {"material_id": "M01"}, "测试非法参数")
        message["tool_calls"][0]["function"]["arguments"] = "{bad json"
        result, events = self.run_with(SequenceClient([message, message]), max_turns=2)
        self.assertEqual(result["task_status"], "incomplete")
        self.assertEqual(result["tool_calls"], 2)
        self.assertTrue(all(e["result"]["error"]["code"] == "INVALID_ARGUMENTS"
                            for e in events if e["event"] == "tool_result"))

    def test_multiple_calls_cannot_execute_unvalidated_batch(self):
        message = tool_message("save_draft", slot("2026-10-29T19:00:00+08:00"), "尝试批量保存")
        message["tool_calls"].append(copy.deepcopy(message["tool_calls"][0]))
        result, _ = self.run_with(SequenceClient([message]))
        self.assertEqual(result["task_status"], "failed")
        self.assertEqual(self.storage.count(), 0)

    def test_saved_receipt_is_retained_if_limit_prevents_final_turn(self):
        result, _ = self.run_with(OfflineModelClient(), max_turns=4)
        self.assertEqual(result["task_status"], "incomplete")
        self.assertEqual(result["save_status"], "saved")
        self.assertEqual(self.storage.count(), 1)
        self.assertTrue(result["draft_id"])

    def test_model_error_is_not_exposed_or_silently_mocked(self):
        class BrokenClient:
            mode, label = "real", "test-model"
            def complete(self, history, schemas):
                raise DomainError("TEST", "secret-api-key-must-not-leak")
        result, events = self.run_with(BrokenClient())
        self.assertEqual(result["task_status"], "failed")
        self.assertEqual(result["model_mode"], "real")
        self.assertNotIn("secret-api-key", json.dumps(events))

    def test_trace_cannot_be_overwritten(self):
        self.run_with(OfflineModelClient())
        with self.assertRaises(FileExistsError):
            self.run_with(OfflineModelClient())

    def test_offline_acceptance_suite_covers_all_cases(self):
        report = evaluate_cases(OfflineModelClient(), self.dataset, self.folder / "evidence")
        self.assertEqual(report["passed"], report["total"])
        self.assertEqual(report["model_mode"], "mock")
        for case in report["cases"]:
            folder = Path(case["evidence"])
            self.assertTrue((folder / "input.json").is_file())
            self.assertTrue((folder / "database-snapshot.json").is_file())
            self.assertTrue((folder / "trace-1.jsonl").is_file())
        conflict = next(c for c in report["cases"] if c["case"] == "hard_conflict")
        self.assertTrue(conflict["checks"]["decision_adjusted_after_observation"])

    def test_evaluation_does_not_overwrite_evidence(self):
        folder = self.folder / "evidence"
        folder.mkdir()
        (folder / "keep.txt").write_text("existing")
        with self.assertRaises(ValueError):
            evaluate_cases(OfflineModelClient(), self.dataset, folder)


if __name__ == "__main__":
    unittest.main()

import copy
import unittest

from scheduler_agent.evaluation import base_request, conflict_adjustment_checks
from scheduler_agent.models import Request, slot


class ConflictEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.request = Request.from_dict(base_request())
        self.result = {"task_status": "completed", "suggested_slot": slot("2026-10-29T19:30:00+08:00")}
        self.query = {"event": "tool_result", "tool": "query_slots", "result": {"ok": True, "data": {"slots": [
            {"start": self.request.requested_start, "available": False, "authorization": "authorized"},
            {"start": self.result["suggested_slot"]["start"], "available": True, "authorization": "authorized"}]}}}
        self.save = {"event": "tool_result", "tool": "save_draft", "result": {"ok": True},
                     "arguments": self.result["suggested_slot"]}

    def test_one_query_with_multiple_candidates_can_prove_adjustment(self):
        self.assertTrue(all(conflict_adjustment_checks([self.query, self.save], self.request, self.result).values()))

    def test_missing_requested_conflict_cannot_pass(self):
        query = copy.deepcopy(self.query)
        query["result"]["data"]["slots"].pop(0)
        checks = conflict_adjustment_checks([query, self.save], self.request, self.result)
        self.assertFalse(checks["decision_adjusted_after_observation"])

    def test_save_before_observation_cannot_pass(self):
        checks = conflict_adjustment_checks([self.save, self.query], self.request, self.result)
        self.assertFalse(checks["decision_adjusted_after_observation"])

    def test_verification_query_after_save_does_not_hide_prior_valid_decision(self):
        checks = conflict_adjustment_checks([self.query, self.save, self.query], self.request, self.result)
        self.assertTrue(all(checks.values()))

    def test_latest_selected_slot_occupied_cannot_pass(self):
        later = copy.deepcopy(self.query)
        later["result"]["data"]["slots"][1]["available"] = False
        checks = conflict_adjustment_checks([self.query, later, self.save], self.request, self.result)
        self.assertFalse(checks["selected_slot_observed_available"])

    def test_confirmation_decision_after_same_query_is_supported(self):
        result = {**self.result, "task_status": "needs_confirmation"}
        finish = {"event": "model_finished", "suggested_start": self.result["suggested_slot"]["start"],
                  "verified_status": "needs_confirmation"}
        self.assertTrue(all(conflict_adjustment_checks([self.query, finish], self.request, result).values()))

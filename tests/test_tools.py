import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scheduler_agent.evaluation import base_request
from scheduler_agent.models import Request, slot
from scheduler_agent.storage import Dataset, Storage
from scheduler_agent.tools import Faults, Tools


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dataset = Dataset()
        self.storage = Storage(Path(self.temp.name) / "drafts.db", self.dataset)

    def tools(self, faults=None, **changes):
        return Tools(Request.from_dict(base_request(**changes)), self.dataset, self.storage, faults)

    def call(self, tools, name, **arguments):
        return tools.execute(name, {**arguments, "reason": "测试可验证的业务边界"})

    def save(self, tools, start="2026-10-29T19:00:00+08:00"):
        return self.call(tools, "save_draft", **slot(start))

    def test_normal_save_matches_committed_record(self):
        result = self.save(self.tools())
        self.assertTrue(result["ok"])
        self.assertEqual(self.storage.count(), 1)
        record = self.storage.snapshot()[0]
        self.assertEqual(record["draft_id"], result["data"]["draft_id"])
        self.assertEqual(record["start_at"], result["data"]["slot"]["start"])

    def test_missing_metadata_never_saved(self):
        tools = self.tools(material_id="M02")
        audit = self.call(tools, "check_rules", material_id="M02")["data"]
        self.assertIn("rights_status", audit["missing_fields"])
        result = self.save(tools)
        self.assertEqual(result["error"]["code"], "MISSING_MATERIAL_INFO")
        self.assertEqual(self.storage.count(), 0)

    def test_blocked_material_cannot_bypass_audit_by_saving_directly(self):
        result = self.save(self.tools(material_id="M03"))
        self.assertEqual(result["error"]["code"], "RULE_BLOCKED")
        self.assertEqual(self.storage.count(), 0)

    def test_unknown_material_requests_input(self):
        tools = self.tools(material_id="UNKNOWN")
        self.call(tools, "read_material", material_id="UNKNOWN")
        result = tools.verified_result("completed")
        self.assertEqual(result["task_status"], "needs_input")
        self.assertIsNone(result["draft_id"])

    def test_hard_slot_cannot_change_without_user_confirmation(self):
        tools = self.tools(requested_start="2026-10-29T18:00:00+08:00")
        result = self.save(tools)
        self.assertEqual(result["error"]["code"], "CONFIRMATION_REQUIRED")
        self.assertEqual(self.storage.count(), 0)

    def test_model_cannot_add_confirmation_to_tool_arguments(self):
        result = self.call(self.tools(), "save_draft", **slot("2026-10-29T19:00:00+08:00"), confirmed=True)
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(self.storage.count(), 0)

    def test_user_confirmation_is_bound_to_exact_slot(self):
        tools = self.tools(requested_start="2026-10-29T18:00:00+08:00",
                           approved_alternative=slot("2026-10-29T19:00:00+08:00"))
        refused = self.save(tools, "2026-10-29T19:30:00+08:00")
        self.assertEqual(refused["error"]["code"], "CONFIRMATION_REQUIRED")
        self.assertTrue(self.save(tools)["ok"])

    def test_confirmation_does_not_bypass_risk(self):
        tools = self.tools(material_id="M03", approved_alternative=slot("2026-10-29T19:00:00+08:00"))
        self.assertEqual(self.save(tools)["error"]["code"], "RULE_BLOCKED")

    def test_manual_review_requires_explicit_user_input(self):
        self.assertEqual(self.save(self.tools(material_id="M04"))["error"]["code"], "REVIEW_CONFIRMATION_REQUIRED")
        self.assertTrue(self.save(self.tools(material_id="M04", human_review_approved=True))["ok"])

    def test_flexible_window_authorizes_only_contained_slots(self):
        tools = self.tools(time_constraint="flexible", allowed_window=slot("2026-10-29T19:00:00+08:00"))
        self.assertEqual(self.save(tools, "2026-10-29T19:30:00+08:00")["error"]["code"], "OUTSIDE_SCOPE")
        self.assertTrue(self.save(tools)["ok"])

    def test_even_confirmed_slot_cannot_cross_day(self):
        tools = self.tools(approved_alternative=slot("2026-10-30T19:00:00+08:00"))
        self.assertEqual(self.save(tools, "2026-10-30T19:00:00+08:00")["error"]["code"], "OUTSIDE_SCOPE")

    def test_existing_schedule_blocks_original_slot(self):
        tools = self.tools(requested_start="2026-10-29T18:00:00+08:00")
        self.assertEqual(self.save(tools, "2026-10-29T18:00:00+08:00")["error"]["code"], "SLOT_CONFLICT")

    def test_adjacent_slots_do_not_overlap(self):
        self.assertTrue(self.save(self.tools())["ok"])
        self.assertTrue(self.save(self.tools(request_id="second", requested_start="2026-10-29T19:30:00+08:00"),
                                  "2026-10-29T19:30:00+08:00")["ok"])
        self.assertEqual(self.storage.count(), 2)

    def test_partial_overlap_with_existing_schedule_is_detected(self):
        data = copy.deepcopy(self.dataset)
        data.existing_schedule["items"].append({"id": "partial", "channel": "douyin", "material_id": "other",
            "start": "2026-10-29T19:15:00+08:00", "end": "2026-10-29T19:45:00+08:00"})
        storage = Storage(Path(self.temp.name) / "partial.db", data)
        request = Request.from_dict(base_request())
        tools = Tools(request, data, storage)
        self.assertEqual(self.save(tools)["error"]["code"], "SLOT_CONFLICT")

    def test_duplicate_returns_same_id_without_reinsertion(self):
        first = self.save(self.tools())["data"]
        second = self.save(self.tools())["data"]
        self.assertEqual(second["draft_id"], first["draft_id"])
        self.assertEqual(second["save_status"], "already_exists")
        self.assertEqual(self.storage.count(), 1)

    def test_same_request_id_changed_intent_is_rejected(self):
        self.save(self.tools())
        tools = self.tools(requested_start="2026-10-29T19:30:00+08:00")
        self.assertEqual(self.save(tools, "2026-10-29T19:30:00+08:00")["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.storage.count(), 1)

    def test_replay_is_labelled_as_original_validation(self):
        self.save(self.tools())
        self.dataset.material_index["M01"]["rights_status"] = "expired"
        tools = self.tools()
        self.call(tools, "check_rules", material_id="M01")
        self.save(tools)
        result = tools.verified_result("completed")
        self.assertEqual(result["validation_scope"], "original_save")
        self.assertEqual(result["save_status"], "already_exists")
        self.assertEqual(result["audit_status"], "pass")

    def test_failure_after_insert_rolls_back_and_is_consumed_once(self):
        tools = self.tools(faults=Faults(save_failures=1))
        self.assertEqual(self.save(tools)["error"]["code"], "SAVE_FAILED")
        self.assertEqual(self.storage.count(), 0)
        self.assertTrue(self.save(tools)["ok"])
        self.assertEqual(self.storage.count(), 1)

    def test_idempotent_replay_survives_injected_new_write_failure(self):
        first = self.save(self.tools())["data"]
        second = self.save(self.tools(faults=Faults(save_failures=1)))["data"]
        self.assertEqual(first["draft_id"], second["draft_id"])

    def test_conflict_added_after_query_is_rechecked_at_save(self):
        first = self.tools(request_id="first")
        self.call(first, "query_slots", starts=["2026-10-29T19:00:00+08:00"])
        self.assertTrue(self.save(self.tools(request_id="other"))["ok"])
        self.assertEqual(self.save(first)["error"]["code"], "SLOT_CONFLICT")

    def test_two_competing_requests_only_one_commits(self):
        barrier = threading.Barrier(2)
        def worker(request_id):
            tools = self.tools(request_id=request_id)
            self.call(tools, "query_slots", starts=["2026-10-29T19:00:00+08:00"])
            barrier.wait(timeout=5)
            return self.save(tools)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ("A", "B")))
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual([r["error"]["code"] for r in results if not r["ok"]], ["SLOT_CONFLICT"])
        self.assertEqual(self.storage.count(), 1)

    def test_concurrent_duplicate_has_one_row_and_same_id(self):
        barrier = threading.Barrier(2)
        def worker(_):
            tools = self.tools()
            barrier.wait(timeout=5)
            return self.save(tools)["data"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.assertEqual({r["save_status"] for r in results}, {"saved", "already_exists"})
        self.assertEqual(len({r["draft_id"] for r in results}), 1)
        self.assertEqual(self.storage.count(), 1)

    def test_model_cannot_claim_success_without_receipt(self):
        result = self.tools().verified_result("completed")
        self.assertEqual(result["task_status"], "incomplete")
        self.assertTrue(result["status_corrected"])
        self.assertIsNone(result["draft_id"])

    def test_suggestion_comes_from_model_selected_observed_slot(self):
        tools = self.tools(requested_start="2026-10-29T18:00:00+08:00")
        self.call(tools, "check_rules", material_id="M01")
        self.call(tools, "query_slots", starts=["2026-10-29T18:30:00+08:00", "2026-10-29T19:00:00+08:00"])
        result = tools.verified_result("needs_confirmation", proposed_start="2026-10-29T19:00:00+08:00")
        self.assertEqual(result["task_status"], "needs_confirmation")
        self.assertEqual(result["suggested_slot"]["start"], "2026-10-29T19:00:00+08:00")

    def test_unobserved_suggestion_is_not_treated_as_verified(self):
        tools = self.tools(requested_start="2026-10-29T18:00:00+08:00")
        self.call(tools, "check_rules", material_id="M01")
        self.call(tools, "query_slots", starts=["2026-10-29T18:30:00+08:00"])
        result = tools.verified_result("needs_confirmation", proposed_start="2026-10-29T19:00:00+08:00")
        self.assertEqual(result["task_status"], "incomplete")
        self.assertIsNone(result["suggested_slot"])

    def test_failed_save_remains_visible_after_another_read(self):
        tools = self.tools(faults=Faults(save_failures=1))
        self.save(tools)
        self.call(tools, "read_material", material_id="M01")
        result = tools.verified_result("completed")
        self.assertEqual(result["save_status"], "failed")
        self.assertEqual(result["task_status"], "failed")

    def test_failed_save_survives_a_later_different_tool_error(self):
        tools = self.tools(faults=Faults(save_failures=1))
        self.save(tools)
        self.call(tools, "query_slots", starts=[])
        result = tools.verified_result("failed")
        self.assertEqual(result["save_status"], "failed")
        self.assertEqual(result["save_error"]["code"], "SAVE_FAILED")
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(self.storage.count(), 0)

    def test_successful_save_clears_previous_save_failure(self):
        tools = self.tools(faults=Faults(save_failures=1))
        self.save(tools)
        self.save(tools)
        result = tools.verified_result("completed")
        self.assertEqual(result["save_status"], "saved")
        self.assertIsNone(result["save_error"])

    def test_only_authorized_candidates_are_returned_for_followup_queries(self):
        tools = self.tools(time_constraint="flexible", allowed_window=slot("2026-10-29T19:00:00+08:00"))
        result = self.call(tools, "query_slots", starts=["2026-10-29T19:00:00+08:00"])
        self.assertEqual(result["data"]["candidate_starts"], ["2026-10-29T19:00:00+08:00"])

    def test_invalid_query_error_also_returns_only_relevant_candidates(self):
        tools = self.tools(time_constraint="flexible", allowed_window=slot("2026-10-29T19:00:00+08:00"))
        result = self.call(tools, "query_slots", starts=["2026-10-29T23:00:00+08:00"])
        self.assertEqual(result["error"]["details"]["candidate_starts"], ["2026-10-29T19:00:00+08:00"])

    def test_exhausted_window_does_not_require_outside_queries(self):
        self.save(self.tools(request_id="occupant"))
        tools = self.tools(request_id="window", time_constraint="flexible",
                           allowed_window=slot("2026-10-29T19:00:00+08:00"))
        self.call(tools, "check_rules", material_id="M01")
        self.call(tools, "query_slots", starts=["2026-10-29T19:00:00+08:00"])
        result = tools.verified_result("blocked")
        self.assertEqual(result["task_status"], "blocked")
        self.assertEqual(result["reason"], "no_feasible_slot")

    def test_unqueried_authorized_candidate_prevents_no_slot_claim(self):
        tools = self.tools(time_constraint="flexible", allowed_window={
            "start": "2026-10-29T18:00:00+08:00", "end": "2026-10-29T19:00:00+08:00"})
        self.call(tools, "check_rules", material_id="M01")
        self.call(tools, "query_slots", starts=["2026-10-29T18:00:00+08:00"])
        result = tools.verified_result("blocked")
        self.assertEqual(result["task_status"], "incomplete")

    def test_window_without_a_complete_slot_is_blocked(self):
        tools = self.tools(time_constraint="flexible", allowed_window={
            "start": "2026-10-29T19:10:00+08:00", "end": "2026-10-29T19:20:00+08:00"})
        self.call(tools, "check_rules", material_id="M01")
        self.assertEqual(tools.relevant_candidates(), [])
        self.assertEqual(tools.verified_result("blocked")["task_status"], "blocked")

    def test_known_empty_scope_stays_blocked_after_invalid_time_query(self):
        tools = self.tools(time_constraint="flexible", allowed_window={
            "start": "2026-10-29T19:10:00+08:00", "end": "2026-10-29T19:20:00+08:00"})
        self.call(tools, "check_rules", material_id="M01")
        query = self.call(tools, "query_slots", starts=["2026-10-29T19:10:00+08:00"])
        self.assertEqual(query["error"]["details"]["candidate_starts"], [])
        result = tools.verified_result("blocked")
        self.assertEqual(result["task_status"], "blocked")
        self.assertEqual(result["reason"], "no_feasible_slot")

    def test_channel_mismatch_cannot_save(self):
        result = self.save(self.tools(channel="other"))
        self.assertEqual(result["error"]["code"], "RULE_BLOCKED")

    def test_tool_cannot_read_a_different_material(self):
        result = self.call(self.tools(), "read_material", material_id="M03")
        self.assertEqual(result["error"]["code"], "OUTSIDE_SCOPE")

    def test_invalid_enum_in_metadata_is_not_treated_as_pass(self):
        self.dataset.material_index["M01"]["rights_status"] = "made-up"
        result = self.save(self.tools())
        self.assertEqual(result["error"]["code"], "MISSING_MATERIAL_INFO")

    def test_unknown_tool_and_extra_arguments_are_rejected(self):
        self.assertEqual(self.call(self.tools(), "publish")["error"]["code"], "UNKNOWN_TOOL")
        self.assertEqual(self.call(self.tools(), "read_material", material_id="M01", request_id="fake")["error"]["code"], "INVALID_ARGUMENTS")

    def test_save_requires_exact_duration(self):
        result = self.call(self.tools(), "save_draft", start="2026-10-29T19:00:00+08:00", end="2026-10-29T20:00:00+08:00")
        self.assertEqual(result["error"]["code"], "INVALID_SLOT")

    def test_invalid_query_does_not_leave_partial_observations(self):
        tools = self.tools()
        result = self.call(tools, "query_slots", starts=["2026-10-29T19:00:00+08:00", "2026-10-29T23:00:00+08:00"])
        self.assertFalse(result["ok"])
        self.assertEqual(tools.observed_slots, {})


class RequestTests(unittest.TestCase):
    def test_timezone_is_required(self):
        with self.assertRaises(ValueError):
            Request.from_dict(base_request(requested_start="2026-10-29T19:00:00"))

    def test_equivalent_timezone_is_normalized(self):
        request = Request.from_dict(base_request(requested_start="2026-10-29T11:00:00Z"))
        self.assertEqual(request.requested_start, "2026-10-29T19:00:00+08:00")

    def test_non_boolean_confirmation_is_rejected(self):
        with self.assertRaises(ValueError):
            Request.from_dict(base_request(human_review_approved="true"))

    def test_flexible_input_needs_window(self):
        self.assertIn("allowed_window", Request.from_dict(base_request(time_constraint="flexible")).missing_fields)

    def test_confirmation_must_be_complete_slot(self):
        with self.assertRaises(ValueError):
            Request.from_dict(base_request(approved_alternative={"start": "2026-10-29T19:00:00+08:00", "end": "2026-10-29T19:10:00+08:00"}))

    def test_unknown_input_field_is_rejected(self):
        with self.assertRaises(ValueError):
            Request.from_dict(base_request(confirmed=True))

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.validation.api import router
from src.validation.decision import Route, decide_validation_route


class ValidationDecisionTests(unittest.TestCase):
    def test_approval_requires_explicit_complete_validation(self):
        incomplete = {
            "valid": True,
            "checks": {"inventory": {"status": "passed"}, "price": {"status": "passed"}},
        }
        decision = decide_validation_route(incomplete)
        self.assertEqual(decision.route, Route.CLARIFICATION_REQUIRED)
        self.assertFalse(decision.approved)

        complete = {**incomplete, "complete_validation": True}
        self.assertEqual(decide_validation_route(complete).route, Route.APPROVED)

    def test_inconsistent_complete_result_with_failed_check_is_not_approved(self):
        result = {
            "valid": True,
            "complete_validation": True,
            "checks": {"inventory": {"status": "failed", "result": {"errors": ["Short stock"]}}},
        }
        decision = decide_validation_route(result)
        self.assertNotEqual(decision.route, Route.APPROVED)
        self.assertEqual(decision.route, Route.REVISION_REQUIRED)

    def test_technical_failure_does_not_request_design_revision(self):
        result = {
            "valid": False,
            "status": "error",
            "technical_errors": [{"check": "inventory", "message": "Backend unavailable"}],
            "checks": {"inventory": {"status": "error", "result": None}},
        }
        decision = decide_validation_route(result, configuration={"gem": "ruby"})
        self.assertEqual(decision.route, Route.TECHNICAL_ERROR)
        self.assertFalse(decision.revision_constraints)
        self.assertEqual(decision.state.revision_attempts, 0)

    def test_unresolved_mapping_requires_confirmation(self):
        result = {
            "valid": False,
            "complete_validation": False,
            "clarification_required": True,
            "unresolved_mappings": [{"field": "materialID"}],
            "clarifications": [{"code": "ambiguous", "message": "Choose a material", "fields": ["metal"]}],
            "checks": {"catalog": {"status": "clarification_required", "result": {"status": "ambiguous"}}},
        }
        decision = decide_validation_route(result)
        self.assertEqual(decision.route, Route.CLARIFICATION_REQUIRED)
        self.assertTrue(any(item.customer_confirmation_required for item in decision.feedback))

    def test_unsupported_explicit_property_requires_customer_clarification(self):
        result = {
            "valid": False,
            "complete_validation": False,
            "checks": {
                "compatibility": {
                    "status": "failed",
                    "result": {
                        "unsupported_fields": ["engraving"],
                        "errors": [{"code": "unsupported_field", "field": "engraving", "message": "Generator cannot represent engraving."}],
                        "revision_suggestions": ["Confirm whether to remove engraving."],
                    },
                }
            },
        }
        decision = decide_validation_route(result, configuration={"engraving": "initials"})
        self.assertEqual(decision.route, Route.CLARIFICATION_REQUIRED)
        self.assertTrue(decision.feedback[0].customer_confirmation_required)

    def test_design_failure_returns_actionable_feedback_and_revision_constraints(self):
        result = {
            "valid": False,
            "status": "failed",
            "complete_validation": False,
            "checks": {
                "price": {
                    "status": "failed",
                    "result": {"errors": [{"message": "Design exceeds budget", "field": "budget"}]},
                }
            },
        }
        decision = decide_validation_route(result, configuration={"budget": 100, "gem": "sapphire"})
        self.assertEqual(decision.route, Route.REVISION_REQUIRED)
        self.assertEqual(decision.state.revision_attempts, 1)
        self.assertEqual(decision.feedback[0].recipient, "price_validator")
        self.assertIn("budget", decision.feedback[0].affected_fields)
        self.assertTrue(any("stated budget" in item for item in decision.revision_constraints))
        self.assertTrue(any("budget unchanged" in item for item in decision.feedback[0].suggested_actions))

    def test_repeated_unchanged_invalid_configuration_stops_routing(self):
        result = {
            "valid": False,
            "complete_validation": False,
            "checks": {"compatibility": {"status": "failed", "result": {"errors": ["Invalid size"]}}},
        }
        config = {"size": 3.25, "gem": "sapphire"}
        first = decide_validation_route(result, configuration=config)
        second = decide_validation_route(result, configuration=config, state=first.state)
        self.assertEqual(first.route, Route.REVISION_REQUIRED)
        self.assertEqual(second.route, Route.RETRY_LIMIT_REACHED)
        self.assertTrue(second.repeated_unchanged_configuration)

    def test_configurable_retry_limit_and_changed_revisions(self):
        result = {
            "valid": False,
            "complete_validation": False,
            "checks": {"inventory": {"status": "failed", "result": {"errors": ["Unavailable"]}}},
        }
        state = {"revision_attempts": 0, "max_revision_attempts": 2}
        first = decide_validation_route(result, configuration={"stone": "S1"}, state=state)
        second = decide_validation_route(result, configuration={"stone": "S2"}, state=first.state)
        third = decide_validation_route(result, configuration={"stone": "S3"}, state=second.state)
        self.assertEqual(first.route, Route.REVISION_REQUIRED)
        self.assertEqual(second.route, Route.REVISION_REQUIRED)
        self.assertEqual(third.route, Route.RETRY_LIMIT_REACHED)

    def test_clarification_does_not_consume_a_revision_attempt(self):
        result = {"valid": False, "complete_validation": False, "clarifications": [{"message": "Choose a size"}]}
        decision = decide_validation_route(result, state={"revision_attempts": 1})
        self.assertEqual(decision.route, Route.CLARIFICATION_REQUIRED)
        self.assertEqual(decision.state.revision_attempts, 1)

    def test_legacy_unified_result_is_not_approved(self):
        legacy = {
            "valid": True,
            "status": "passed",
            "checks": {"inventory": {"status": "passed"}, "price": {"status": "passed"}},
        }
        decision = decide_validation_route(legacy)
        self.assertEqual(decision.route, Route.CLARIFICATION_REQUIRED)
        self.assertFalse(decision.approved)

    def test_api_endpoint_consumes_unified_result_and_returns_pydantic_shape(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response = client.post("/validation/decision", json={
            "unified_validation_result": {"valid": True, "complete_validation": True},
            "configuration": {"kind": "ring"},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route"], "approved")
        self.assertTrue(response.json()["approved"])


if __name__ == "__main__":
    unittest.main()

"""API tests for the unified validation orchestrator endpoint."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.validation import api as validation_api


class ValidationRunAPITests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(validation_api.router)
        self.client = TestClient(app)
        self.request_data = {
            "inventory_request": {
                "material_id": "MT01",
                "stones": [{"stoneID": "S001", "quantity": 1}],
                "charms": [],
            },
            "price_request": {
                "design": {"type": "ring", "materialID": "MT01", "stones": []},
                "budget": 100,
            },
        }

    def test_both_checks_passing_returns_complete_unified_result(self):
        expected = {
            "valid": True,
            "status": "passed",
            "checks": {
                "inventory": {"status": "passed", "result": {"valid": True}},
                "price": {"status": "passed", "result": {"valid": True}},
            },
            "errors": [],
            "warnings": [],
            "technical_errors": [],
        }
        with patch("src.validation.api.orchestrate_validation", return_value=expected) as run:
            response = self.client.post("/validation/run", json=self.request_data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        run.assert_called_once_with(
            inventory_request=self.request_data["inventory_request"],
            price_request=self.request_data["price_request"],
        )

    def test_inventory_failure_returns_unified_result(self):
        expected = {
            "valid": False,
            "status": "failed",
            "checks": {
                "inventory": {
                    "status": "failed",
                    "result": {"valid": False, "errors": ["Insufficient stock"]},
                },
                "price": {"status": "passed", "result": {"valid": True}},
            },
            "errors": [{"check": "inventory", "message": "Insufficient stock"}],
            "warnings": [],
            "technical_errors": [],
        }
        with patch("src.validation.api.orchestrate_validation", return_value=expected):
            response = self.client.post("/validation/run", json=self.request_data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_over_budget_price_returns_unified_failed_result(self):
        expected = {
            "valid": False,
            "status": "failed",
            "checks": {
                "inventory": {"status": "passed", "result": {"valid": True}},
                "price": {
                    "status": "failed",
                    "result": {
                        "valid": False,
                        "computed_price": 125,
                        "budget": 100,
                        "difference": -25,
                        "errors": ["Design exceeds the budget by 25."],
                        "backend_error": None,
                    },
                },
            },
            "errors": [{"check": "price", "message": "Design exceeds the budget by 25."}],
            "warnings": [],
            "technical_errors": [],
        }
        with patch("src.validation.api.orchestrate_validation", return_value=expected):
            response = self.client.post("/validation/run", json=self.request_data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_technical_connection_error_returns_503_and_complete_result(self):
        expected = {
            "valid": False,
            "status": "error",
            "checks": {
                "inventory": {
                    "status": "error",
                    "result": {
                        "valid": False,
                        "errors": ["Inventory could not be checked: backend offline"],
                        "backend_error": "connection",
                    },
                },
                "price": {"status": "passed", "result": {"valid": True}},
            },
            "errors": [
                {"check": "inventory", "message": "Inventory could not be checked: backend offline"}
            ],
            "warnings": [],
            "technical_errors": [
                {"check": "inventory", "message": "Inventory could not be checked: backend offline"}
            ],
        }
        with patch("src.validation.api.orchestrate_validation", return_value=expected):
            response = self.client.post("/validation/run", json=self.request_data)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), expected)

    def test_invalid_request_shape_returns_422(self):
        response = self.client.post(
            "/validation/run",
            json={"inventory_request": self.request_data["inventory_request"]},
        )
        self.assertEqual(response.status_code, 422)

    def test_compatibility_endpoint_returns_valid_configuration(self):
        response = self.client.post("/validation/compatibility", json={
            "kind": "bracelet", "style": "plain", "metal": "silver",
            "gem": "none", "diameter": 60,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])

    def test_compatibility_endpoint_returns_actionable_invalid_configuration(self):
        response = self.client.post("/validation/compatibility", json={
            "kind": "ring", "style": "tennis", "metal": "gold",
            "gem": "none", "count": 7, "size": 7,
        })
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertFalse(body["valid"])
        self.assertTrue(body["errors"])
        self.assertTrue(body["revision_suggestions"])

    def test_run_endpoint_forwards_optional_3d_configuration(self):
        body = {
            **self.request_data,
            "three_d_configuration": {
                "kind": "ring", "style": "solitaire", "metal": "gold",
                "gem": "sapphire", "size": 7,
            },
            "three_d_field_provenance": {"kind": "explicit"},
            "three_d_confirmed_components": {"materialID": "MT01"},
            "three_d_approved_mappings": {"metal": "gold"},
        }
        with patch("src.validation.api.orchestrate_validation", return_value={"valid": False, "status": "configuration_mapping_required", "checks": {}}) as run:
            response = self.client.post("/validation/run", json=body)
        self.assertEqual(response.status_code, 200)
        run.assert_called_once_with(
            inventory_request=body["inventory_request"],
            price_request=body["price_request"],
            three_d_configuration=body["three_d_configuration"],
            three_d_field_provenance=body["three_d_field_provenance"],
            three_d_confirmed_components=body["three_d_confirmed_components"],
            three_d_approved_mappings=body["three_d_approved_mappings"],
        )

    def test_run_rejects_orphaned_3d_metadata(self):
        body = {**self.request_data, "three_d_field_provenance": {"kind": "explicit"}}
        response = self.client.post("/validation/run", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"][0]["code"], "3d_metadata_without_configuration")


if __name__ == "__main__":
    unittest.main()

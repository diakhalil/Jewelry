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


if __name__ == "__main__":
    unittest.main()

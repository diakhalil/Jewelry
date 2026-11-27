"""FastAPI tests for the price validation endpoint."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.validation import api as validation_api
from src.validation.backend_client import BackendConnectionError, BackendHTTPError


class MockPriceClient:
    def preview_accessory_price(self, design):
        self.design = design
        return {"computedPrice": 35.5, "breakdown": {"basePrice": 35.5}}


class ConnectionFailureClient:
    def preview_accessory_price(self, _design):
        raise BackendConnectionError("backend unavailable")


class NodeBadRequestClient:
    def preview_accessory_price(self, _design):
        raise BackendHTTPError(
            "backend returned 400", status_code=400, detail="Stone ID not found"
        )


class NodeServerFailureClient:
    def preview_accessory_price(self, _design):
        raise BackendHTTPError("backend returned 500", status_code=500)


class PriceValidationAPITests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(validation_api.router)
        self.client = TestClient(app)
        self.request_data = {
            "type": "bracelet",
            "materialID": "MT01",
            "product": {"style": "name bracelet"},
            "charms": [{"charmID": "A001", "quantity": 2}],
            "stones": [],
            "budget": 40,
        }

    def test_returns_structured_result_and_excludes_budget_from_node_request(self):
        with patch("src.validation.pricing.BackendInventoryClient", MockPriceClient):
            response = self.client.post("/validation/price", json=self.request_data)

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(
            set(result),
            {"valid", "computed_price", "budget", "difference", "errors", "warnings", "breakdown"},
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["computed_price"], 35.5)
        self.assertEqual(result["difference"], 4.5)

    def test_returns_503_for_backend_connection_failure(self):
        with patch(
            "src.validation.pricing.BackendInventoryClient", ConnectionFailureClient
        ):
            response = self.client.post("/validation/price", json=self.request_data)

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["valid"])

    def test_node_400_becomes_422_design_validation_result(self):
        with patch(
            "src.validation.pricing.BackendInventoryClient", NodeBadRequestClient
        ):
            response = self.client.post("/validation/price", json=self.request_data)

        self.assertEqual(response.status_code, 422)
        self.assertIn("Stone ID not found", response.json()["errors"])

    def test_other_backend_http_failure_returns_502(self):
        with patch(
            "src.validation.pricing.BackendInventoryClient", NodeServerFailureClient
        ):
            response = self.client.post("/validation/price", json=self.request_data)

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["valid"])

    def test_invalid_budget_or_request_returns_422(self):
        invalid_budget = {**self.request_data, "budget": -1}
        self.assertEqual(
            self.client.post("/validation/price", json=invalid_budget).status_code,
            422,
        )
        missing_design_material = {**self.request_data, "materialID": None}
        self.assertEqual(
            self.client.post("/validation/price", json=missing_design_material).status_code,
            422,
        )


if __name__ == "__main__":
    unittest.main()

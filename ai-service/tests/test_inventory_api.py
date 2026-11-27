"""API tests for inventory validation with a mocked backend inventory client."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.validation import api as inventory_api
from src.validation.backend_client import BackendConnectionError


class MockInventoryClient:
    def get_materials(self):
        return [{"materialID": "MT01", "metal": "silver", "price": 2}]

    def get_stones(self):
        return [{"stoneID": "S001", "qty": 3}]

    def get_charms(self):
        return [{"charmID": "A001", "qty": 2}]


class BrokenInventoryClient:
    def get_materials(self):
        raise BackendConnectionError("mock backend unavailable")


class InventoryValidationAPITests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(inventory_api.router)
        self.client = TestClient(app)

    def test_returns_structured_inventory_result(self):
        with patch("src.validation.inventory.BackendInventoryClient", MockInventoryClient):
            response = self.client.post(
                "/validation/inventory",
                json={
                    "material_id": "MT01",
                    "stones": [{"stoneID": "S001", "quantity": 2}],
                    "charms": [{"charmID": "A001", "quantity": 1}],
                },
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["valid"])
        self.assertEqual(set(result), {"valid", "errors", "warnings", "components"})
        self.assertEqual(result["components"]["stones"][0]["available_quantity"], 3)
        self.assertEqual(result["components"]["charms"][0]["requested_quantity"], 1)
        self.assertFalse(result["components"]["material"]["quantity_checked"])

    def test_invalid_component_quantities_return_validation_result(self):
        with patch("src.validation.inventory.BackendInventoryClient", MockInventoryClient):
            response = self.client.post(
                "/validation/inventory",
                json={
                    "material_id": "MT01",
                    "stones": [{"stoneID": "S001", "quantity": 4}],
                    "charms": [],
                },
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertFalse(result["valid"])
        self.assertTrue(any("insufficient stock" in error for error in result["errors"]))

    def test_backend_connection_failure_returns_503_and_structured_result(self):
        with patch("src.validation.inventory.BackendInventoryClient", BrokenInventoryClient):
            response = self.client.post(
                "/validation/inventory",
                json={"material_id": "MT01", "stones": [], "charms": []},
            )

        self.assertEqual(response.status_code, 503)
        result = response.json()
        self.assertFalse(result["valid"])
        self.assertEqual(result["backend_error"], "connection")
        self.assertIn("components", result)

    def test_invalid_request_shape_returns_422(self):
        response = self.client.post(
            "/validation/inventory",
            json={"material_id": "MT01", "stones": "not-a-list", "charms": []},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

"""FastAPI tests for catalog matching and optional run integration."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.validation import api as validation_api


class CatalogAPITests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(validation_api.router)
        self.client = TestClient(app)
        self.catalog_request = {
            "catalog_preferences": {
                "material": "14K Gold",
                "stones": [{"gem": "Sapphire", "color": "Pink", "quantity": 1}],
                "charms": [{"design": "shapes", "color": "Silver"}],
            }
        }

    def test_catalog_endpoint_returns_full_matching_results(self):
        expected = {
            "status": "matched",
            "requested_preferences": self.catalog_request["catalog_preferences"],
            "matches": {
                "material": {"status": "matched", "candidates": [{"id": "MT01", "metadata": {}}], "alternatives": [], "warnings": [], "errors": []},
                "stones": [{"status": "matched", "candidates": [{"id": "S019", "metadata": {}, "available_quantity": 2}], "alternatives": [], "warnings": [], "errors": [], "requested_quantity": 1}],
                "charms": [{"status": "matched", "candidates": [{"id": "A001", "metadata": {}, "available_quantity": 1}], "alternatives": [], "warnings": [], "errors": []}],
            },
            "resolved": {"materialID": "MT01", "stones": [{"stoneID": "S019", "quantity": 1}], "charms": [{"charmID": "A001"}]},
            "errors": [],
            "warnings": [],
        }
        with patch("src.validation.api.resolve_catalog_preferences", return_value=expected) as resolve:
            response = self.client.post("/validation/catalog", json=self.catalog_request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        resolve.assert_called_once_with(self.catalog_request["catalog_preferences"])

    def test_ambiguous_catalog_match_returns_complete_result_without_http_error(self):
        expected = {
            "status": "ambiguous",
            "matches": {"stones": [{"status": "ambiguous", "candidates": [{"id": "S001"}, {"id": "S002"}], "alternatives": [], "errors": [], "warnings": []}]},
            "resolved": None,
            "errors": [],
            "warnings": [],
        }
        with patch("src.validation.api.resolve_catalog_preferences", return_value=expected):
            response = self.client.post("/validation/catalog", json=self.catalog_request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_catalog_backend_errors_follow_existing_status_conventions(self):
        for kind, status in (("connection", 503), ("http", 502), ("response", 502)):
            with self.subTest(kind=kind):
                result = {
                    "status": "error",
                    "matches": {},
                    "resolved": None,
                    "errors": ["Catalog could not be checked."],
                    "warnings": [],
                    "backend_error": kind,
                }
                with patch("src.validation.api.resolve_catalog_preferences", return_value=result):
                    response = self.client.post("/validation/catalog", json=self.catalog_request)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), result)

    def test_invalid_catalog_preferences_return_422(self):
        result = {
            "status": "error",
            "matches": {},
            "resolved": None,
            "errors": ["Unsupported catalog preference groups."],
            "warnings": [],
            "request_error": True,
        }
        with patch("src.validation.api.resolve_catalog_preferences", return_value=result):
            response = self.client.post("/validation/catalog", json=self.catalog_request)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), result)

    def test_run_endpoint_forwards_optional_catalog_preferences(self):
        expected = {
            "valid": True,
            "status": "passed",
            "checks": {
                "catalog": {"status": "passed", "result": {"status": "matched", "resolved": {}}},
                "inventory": {"status": "passed", "result": {"valid": True}},
                "price": {"status": "passed", "result": {"valid": True}},
            },
            "errors": [],
            "warnings": [],
            "technical_errors": [],
        }
        body = {
            "inventory_request": {"material_id": "MT01", "stones": [], "charms": []},
            "price_request": {"design": {"type": "ring", "materialID": "MT01"}, "budget": 100},
            **self.catalog_request,
        }
        with patch("src.validation.api.orchestrate_validation", return_value=expected) as run:
            response = self.client.post("/validation/run", json=body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        run.assert_called_once_with(
            inventory_request=body["inventory_request"],
            price_request=body["price_request"],
            catalog_preferences=body["catalog_preferences"],
        )


if __name__ == "__main__":
    unittest.main()

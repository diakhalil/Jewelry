"""Unit tests for read-only inventory validation with mocked HTTP responses."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.validation.backend_client import (
    BackendConnectionError,
    BackendHTTPError,
    BackendInventoryClient,
    BackendResponseError,
)
from src.validation.inventory import validate_inventory


class FakeResponse:
    def __init__(self, payload=None, status_code=200, json_error=None):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StaticInventoryClient:
    def __init__(self, materials=None, stones=None, charms=None):
        self.materials = materials or []
        self.stones = stones or []
        self.charms = charms or []

    def get_materials(self):
        return self.materials

    def get_stones(self):
        return self.stones

    def get_charms(self):
        return self.charms


class BackendInventoryClientTests(unittest.TestCase):
    def test_uses_configured_url_and_existing_endpoints(self):
        session = FakeSession([
            FakeResponse([{"materialID": "MT01", "metal": "silver", "price": 2}]),
            FakeResponse([{"stoneID": "S001", "qty": 4}]),
            FakeResponse([{"charmID": "A001", "qty": 3}]),
        ])
        client = BackendInventoryClient("http://backend.test/", session=session)

        self.assertEqual(client.get_materials()[0]["materialID"], "MT01")
        self.assertEqual(client.get_stones()[0]["qty"], 4)
        self.assertEqual(client.get_charms()[0]["qty"], 3)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "http://backend.test/api/materials",
                "http://backend.test/api/stones",
                "http://backend.test/api/charms",
            ],
        )

    def test_reads_backend_url_from_environment(self):
        session = FakeSession([FakeResponse([])])
        with patch.dict(os.environ, {"SHECRAFT_BACKEND_URL": "http://env-backend:5000"}):
            client = BackendInventoryClient(session=session)
        client.get_materials()
        self.assertEqual(session.calls[0][0], "http://env-backend:5000/api/materials")

    def test_connection_http_and_malformed_response_errors(self):
        connection_client = BackendInventoryClient(
            session=FakeSession([__import__("requests").ConnectionError("offline")])
        )
        with self.assertRaises(BackendConnectionError):
            connection_client.get_materials()

        http_client = BackendInventoryClient(
            session=FakeSession([FakeResponse(status_code=503)])
        )
        with self.assertRaises(BackendHTTPError):
            http_client.get_materials()

        malformed_client = BackendInventoryClient(
            session=FakeSession([FakeResponse({"unexpected": "object"})])
        )
        with self.assertRaises(BackendResponseError):
            malformed_client.get_materials()

        invalid_json_client = BackendInventoryClient(
            session=FakeSession([FakeResponse(json_error=ValueError("not JSON"))])
        )
        with self.assertRaises(BackendResponseError):
            invalid_json_client.get_materials()

    def test_rejects_malformed_inventory_records(self):
        client = BackendInventoryClient(
            session=FakeSession([FakeResponse([{"stoneID": "S001"}])])
        )
        with self.assertRaises(BackendResponseError):
            client.get_stones()


class InventoryValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = StaticInventoryClient(
            materials=[{"materialID": "MT01", "metal": "silver", "price": 2}],
            stones=[{"stoneID": "S001", "qty": 4}],
            charms=[{"charmID": "A001", "qty": 3}],
        )

    def test_valid_design_checks_material_existence_only(self):
        result = validate_inventory(
            "MT01",
            stones=[{"stoneID": "S001", "quantity": 2}],
            charms=[{"charmID": "A001"}],
            client=self.client,
        )
        self.assertTrue(result["valid"])
        self.assertTrue(result["components"]["material"]["exists"])
        self.assertIsNone(result["components"]["material"]["available_quantity"])
        self.assertFalse(result["components"]["material"]["quantity_checked"])
        self.assertEqual(result["components"]["stones"][0]["available_quantity"], 4)
        self.assertEqual(result["components"]["charms"][0]["requested_quantity"], 1)
        self.assertTrue(any("stock quantity is not tracked" in warning for warning in result["warnings"]))

    def test_reports_missing_components_and_aggregates_duplicate_quantities(self):
        result = validate_inventory(
            "UNKNOWN",
            stones=[
                {"stoneID": "S001", "quantity": 3},
                {"stoneID": "S001", "quantity": 2},
                {"stoneID": "ABSENT", "quantity": 1},
            ],
            charms=[{"charmID": "A001", "quantity": 4}],
            client=self.client,
        )
        self.assertFalse(result["valid"])
        stone_by_id = {item["id"]: item for item in result["components"]["stones"]}
        self.assertEqual(stone_by_id["S001"]["requested_quantity"], 5)
        self.assertFalse(stone_by_id["S001"]["quantity_sufficient"])
        self.assertFalse(stone_by_id["ABSENT"]["exists"])
        self.assertFalse(result["components"]["material"]["exists"])
        self.assertTrue(any("insufficient stock" in error for error in result["errors"]))
        self.assertTrue(any("does not exist" in error for error in result["errors"]))

    def test_reports_malformed_requested_quantities(self):
        result = validate_inventory(
            "MT01",
            stones=[{"stoneID": "S001", "quantity": "not-a-number"}],
            charms=[],
            client=self.client,
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any("invalid quantity" in error for error in result["errors"]))

    def test_backend_failure_returns_unresolved_component_results(self):
        class BrokenClient:
            def get_materials(self):
                raise BackendConnectionError("backend offline")

        result = validate_inventory(
            "MT01", stones=[{"stoneID": "S001", "quantity": 2}], client=BrokenClient()
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any("could not be checked" in error for error in result["errors"]))
        self.assertIsNone(result["components"]["stones"][0]["exists"])


if __name__ == "__main__":
    unittest.main()

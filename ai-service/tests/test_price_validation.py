"""Price validation tests with mocked Node.js preview responses."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from src.validation.backend_client import (
    BackendConnectionError,
    BackendHTTPError,
    BackendInventoryClient,
    BackendResponseError,
)
from src.validation.pricing import validate_price


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
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class BackendPricePreviewClientTests(unittest.TestCase):
    def test_posts_design_only_to_existing_preview_endpoint(self):
        session = FakeSession(FakeResponse({"computedPrice": 25, "breakdown": {}}))
        client = BackendInventoryClient(
            "http://backend.test/", timeout=3, session=session
        )

        client.preview_accessory_price({"type": "ring", "materialID": "MT01"})

        self.assertEqual(
            session.calls,
            [(
                "http://backend.test/api/accessory-instance/preview",
                {"type": "ring", "materialID": "MT01"},
                3,
            )],
        )

    def test_handles_connection_http_and_malformed_responses(self):
        with self.assertRaises(BackendConnectionError):
            BackendInventoryClient(
                session=FakeSession(requests.ConnectionError("offline"))
            ).preview_accessory_price({"type": "ring"})

        with self.assertRaises(BackendHTTPError) as raised:
            BackendInventoryClient(
                session=FakeSession(
                    FakeResponse({"message": "Material not found"}, status_code=400)
                )
            ).preview_accessory_price({"type": "ring"})
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Material not found")

        with self.assertRaises(BackendResponseError):
            BackendInventoryClient(
                session=FakeSession(FakeResponse({"computedPrice": "bad", "breakdown": {}}))
            ).preview_accessory_price({"type": "ring"})


class PriceValidationTests(unittest.TestCase):
    def test_within_budget_uses_backend_price_and_does_not_send_budget(self):
        design = {
            "type": "ring",
            "materialID": "MT01",
            "product": {"style": "tennis"},
            "charms": [],
            "stones": [{"stoneID": "S01", "quantity": 2}],
            "budget": 100,
        }

        class MockClient:
            def preview_accessory_price(self, sent_design):
                self.sent_design = sent_design
                return {"computedPrice": 70.25, "breakdown": {"basePrice": 70}}

        client = MockClient()
        result = validate_price(design, 100, client=client)

        self.assertTrue(result["valid"])
        self.assertEqual(result["computed_price"], 70.25)
        self.assertEqual(result["difference"], 29.75)
        self.assertEqual(result["breakdown"], {"basePrice": 70})
        self.assertNotIn("budget", client.sent_design)

    def test_over_budget_reports_excess(self):
        class MockClient:
            def preview_accessory_price(self, _design):
                return {"computedPrice": 42.5, "breakdown": {}}

        result = validate_price(
            {"type": "ring", "materialID": "MT01"}, 40, client=MockClient()
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["difference"], -2.5)
        self.assertIn("exceeds the budget by 2.5", result["errors"][0])

    def test_invalid_budget_and_design_do_not_call_backend(self):
        class FailingClient:
            def preview_accessory_price(self, _design):
                raise AssertionError("backend must not be called")

        invalid_budget = validate_price(
            {"type": "ring", "materialID": "MT01"}, -1, client=FailingClient()
        )
        invalid_design = validate_price({"type": "ring"}, 40, client=FailingClient())
        self.assertFalse(invalid_budget["valid"])
        self.assertIn("Budget must", invalid_budget["errors"][0])
        self.assertFalse(invalid_design["valid"])
        self.assertIn("materialID or metal", invalid_design["errors"][0])

    def test_node_400_becomes_actionable_design_error(self):
        class Client:
            def preview_accessory_price(self, _design):
                raise BackendHTTPError(
                    "bad request", status_code=400, detail="Material not found"
                )

        result = validate_price(
            {"type": "ring", "materialID": "MT01"}, 50, client=Client()
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["backend_error"], "design")
        self.assertEqual(result["errors"], ["Material not found"])


if __name__ == "__main__":
    unittest.main()

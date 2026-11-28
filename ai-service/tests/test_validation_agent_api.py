from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.validation.agent import ValidationAgentState
from src.validation.api import router


class ValidationAgentAPITests(unittest.TestCase):
    def test_agent_endpoint_returns_structured_session_state(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response_state = ValidationAgentState(
            session_id="demo-session",
            validation_results={"valid": True, "complete_validation": True},
            routing_decision={"route": "approved", "approved": True},
            final_response="Validation is complete.",
        )
        with patch("src.validation.api.run_validation_agent", return_value=response_state) as run:
            response = client.post("/validation/agent", json={
                "session_id": "demo-session",
                "inventory_request": {"material_id": "M-1", "stones": [], "charms": []},
                "price_request": {"design": {"type": "ring", "materialID": "M-1"}, "budget": 100},
                "customer_preferences": {"metal": "gold"},
            })

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], "demo-session")
        self.assertEqual(body["routing_decision"]["route"], "approved")
        self.assertEqual(body["final_response"], "Validation is complete.")
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()

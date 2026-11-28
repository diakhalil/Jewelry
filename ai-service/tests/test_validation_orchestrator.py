"""Tests for composing the inventory and price validators."""

from __future__ import annotations

import unittest

from src.validation.orchestrator import orchestrate_validation


class ValidationOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.inventory_request = {
            "material_id": "MT01",
            "stones": [{"stoneID": "S001", "quantity": 1}],
            "charms": [],
        }
        self.price_request = {
            "design": {"type": "ring", "materialID": "MT01", "stones": []},
            "budget": 100,
        }

    def test_success_requires_both_checks_and_preserves_details(self):
        inventory_result = {
            "valid": True,
            "errors": [],
            "warnings": ["Material stock is not tracked."],
            "components": {"stones": [{"id": "S001", "available_quantity": 4}]},
        }
        price_result = {
            "valid": True,
            "computed_price": 75,
            "budget": 100,
            "difference": 25,
            "errors": [],
            "warnings": [],
            "breakdown": {"basePrice": 70},
        }
        calls = []

        def inventory_validator(**kwargs):
            calls.append(("inventory", kwargs))
            return inventory_result

        def price_validator(**kwargs):
            calls.append(("price", kwargs))
            return price_result

        result = orchestrate_validation(
            inventory_request=self.inventory_request,
            price_request=self.price_request,
            inventory_validator=inventory_validator,
            price_validator=price_validator,
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["checks"]["inventory"]["result"], inventory_result)
        self.assertEqual(result["checks"]["price"]["result"], price_result)
        self.assertEqual(result["warnings"][0]["check"], "inventory")
        self.assertEqual(calls[0][1]["material_id"], "MT01")
        self.assertEqual(calls[1][1]["design"], self.price_request["design"])

    def test_incompatible_3d_configuration_skips_catalog_inventory_and_price(self):
        calls = []
        result = orchestrate_validation(
            inventory_request=self.inventory_request,
            price_request=self.price_request,
            catalog_preferences={"material": "gold"},
            three_d_configuration={
                "kind": "ring", "style": "plain", "metal": "gold",
                "gem": "sapphire", "size": 7,
            },
            inventory_validator=lambda **_: calls.append("inventory"),
            price_validator=lambda **_: calls.append("price"),
            catalog_resolver=lambda *_: calls.append("catalog"),
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["checks"]["compatibility"]["status"], "failed")
        self.assertEqual(result["checks"]["catalog"]["status"], "skipped")
        self.assertEqual(result["checks"]["inventory"]["status"], "skipped")
        self.assertEqual(result["checks"]["price"]["status"], "skipped")
        self.assertEqual(calls, [])
        self.assertEqual(result["unresolved_mappings"], [])

    def test_compatible_3d_config_does_not_auto_approve_database_mappings(self):
        result = orchestrate_validation(
            inventory_request=self.inventory_request,
            price_request=self.price_request,
            catalog_preferences={"material": "gold"},
            three_d_configuration={
                "kind": "ring", "style": "tennis", "metal": "gold",
                "gem": "sapphire", "count": 8, "size": 7,
            },
            three_d_field_provenance={"kind": "explicit", "style": "explicit", "metal": "explicit", "gem": "explicit", "count": "explicit", "size": "explicit"},
            three_d_confirmed_components={"materialID": "MT01", "stones": [{"stoneID": "S001", "quantity": 1}]},
            inventory_validator=lambda **_: {"valid": True, "errors": [], "warnings": [], "components": {}},
            price_validator=lambda **_: {
                "valid": True, "computed_price": 20, "budget": 100,
                "difference": 80, "errors": [], "warnings": [], "breakdown": {},
            },
            catalog_resolver=lambda _: {
                "status": "matched", "resolved": {"materialID": "MT01", "stones": [{"stoneID": "S001", "quantity": 1}]},
                "matches": {
                    "material": {"status": "matched", "candidates": [{"id": "MT01"}]},
                    "stones": [{"status": "matched", "candidates": [{"id": "S001"}]}],
                }, "errors": [], "warnings": [],
            },
        )

        self.assertTrue(result["checks_valid"])
        self.assertFalse(result["valid"])
        self.assertFalse(result["complete_validation"])
        self.assertEqual(result["status"], "clarification_required")
        self.assertTrue(any(item["configuration_field"] == "size" for item in result["unresolved_mappings"]))
        self.assertEqual(result["checks"]["catalog"]["status"], "passed")

    def test_3d_orchestration_sends_identical_canonical_components_to_validators(self):
        config = {
            "kind": "ring", "style": "tennis", "metal": "gold",
            "gem": "diamond", "color": "blue", "count": 20, "size": 7,
        }
        seen = {}

        def resolver(preferences):
            return {
                "status": "matched", "resolved": {}, "errors": [], "warnings": [],
                "matches": {
                    "material": {"status": "matched", "candidates": [{"id": "MT01"}]},
                    "stones": [{"status": "matched", "candidates": [{"id": "S001"}]}],
                },
            }

        def inventory(**kwargs):
            seen["inventory"] = kwargs
            return {"valid": True, "errors": [], "warnings": [], "components": {}}

        def price(**kwargs):
            seen["price"] = kwargs
            return {"valid": True, "computed_price": 50, "budget": 100, "difference": 50, "errors": [], "warnings": [], "breakdown": {}}

        result = orchestrate_validation(
            inventory_request={},
            price_request={"design": {}, "budget": 100},
            three_d_configuration=config,
            three_d_field_provenance={field: "explicit" for field in config},
            three_d_confirmed_components={"materialID": "MT01", "stones": [{"stoneID": "S001", "quantity": 2}]},
            three_d_approved_mappings={"diamond_color": {"catalog_field": "color", "catalog_value": "Blue"}},
            catalog_resolver=resolver,
            inventory_validator=inventory,
            price_validator=price,
        )

        self.assertEqual(seen["inventory"]["stones"], seen["price"]["design"]["stones"])
        self.assertEqual(seen["inventory"]["material_id"], seen["price"]["design"]["materialID"])
        self.assertTrue(result["checks_valid"])
        self.assertFalse(result["complete_validation"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "clarification_required")

    def test_failed_inventory_makes_overall_validation_fail(self):
        inventory_result = {
            "valid": False,
            "errors": ["Stone S001 has insufficient stock."],
            "warnings": [],
            "components": {"stones": [{"id": "S001", "quantity_sufficient": False}]},
        }
        price_result = {
            "valid": True,
            "computed_price": 75,
            "budget": 100,
            "difference": 25,
            "errors": [],
            "warnings": [],
            "breakdown": {},
        }
        result = orchestrate_validation(
            inventory_request=self.inventory_request,
            price_request=self.price_request,
            inventory_validator=lambda **_: inventory_result,
            price_validator=lambda **_: price_result,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"]["inventory"]["status"], "failed")
        self.assertEqual(result["checks"]["price"]["status"], "passed")
        self.assertEqual(result["errors"][0]["message"], inventory_result["errors"][0])

    def test_over_budget_price_fails_overall_validation(self):
        price_result = {
            "valid": False,
            "computed_price": 120,
            "budget": 100,
            "difference": -20,
            "errors": ["Design exceeds the budget by 20."],
            "warnings": [],
            "breakdown": {"basePrice": 120},
        }
        result = orchestrate_validation(
            inventory_request=self.inventory_request,
            price_request=self.price_request,
            inventory_validator=lambda **_: {
                "valid": True,
                "errors": [],
                "warnings": [],
                "components": {},
            },
            price_validator=lambda **_: price_result,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"]["price"]["result"]["computed_price"], 120)
        self.assertEqual(result["errors"][0]["check"], "price")

    def test_backend_failure_is_reported_as_technical_and_other_check_runs(self):
        inventory_result = {
            "valid": False,
            "errors": ["Inventory could not be checked: offline"],
            "warnings": [],
            "components": {},
            "backend_error": "connection",
        }
        price_called = []

        def price_validator(**_):
            price_called.append(True)
            return {
                "valid": True,
                "computed_price": 75,
                "budget": 100,
                "difference": 25,
                "errors": [],
                "warnings": [],
                "breakdown": {},
            }

        result = orchestrate_validation(
            inventory_request=self.inventory_request,
            price_request=self.price_request,
            inventory_validator=lambda **_: inventory_result,
            price_validator=price_validator,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["technical_errors"][0]["check"], "inventory")
        self.assertTrue(price_called)

    def test_unexpected_validator_exception_is_technical_error(self):
        result = orchestrate_validation(
            inventory_request=self.inventory_request,
            price_request=self.price_request,
            inventory_validator=lambda **_: {"valid": True, "errors": [], "warnings": []},
            price_validator=lambda **_: (_ for _ in ()).throw(TimeoutError("timed out")),
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["checks"]["price"]["result"])
        self.assertIn("timed out", result["technical_errors"][0]["message"])

    def test_catalog_resolution_feeds_same_ids_and_quantities_to_both_validators(self):
        resolved = {
            "status": "matched",
            "requested_preferences": {},
            "matches": {},
            "resolved": {
                "materialID": "MT01",
                "stones": [{"stoneID": "S019", "quantity": 2.5}],
                "charms": [{"charmID": "A001", "quantity": 3}],
            },
            "errors": [],
            "warnings": [],
        }
        calls = {}

        def inventory_validator(**kwargs):
            calls["inventory"] = kwargs
            return {"valid": True, "errors": [], "warnings": [], "components": {}}

        def price_validator(**kwargs):
            calls["price"] = kwargs
            return {
                "valid": True,
                "computed_price": 50,
                "budget": 60,
                "difference": 10,
                "errors": [],
                "warnings": [],
                "breakdown": {},
            }

        result = orchestrate_validation(
            inventory_request={"stones": [], "charms": []},
            price_request={"design": {"type": "ring", "product": {}}, "budget": 60},
            catalog_preferences={
                "material": "gold",
                "stones": [{"gem": "Sapphire", "color": "Pink", "quantity": 2.5}],
                "charms": [{"design": "shapes", "color": "Silver", "quantity": 3}],
            },
            inventory_validator=inventory_validator,
            price_validator=price_validator,
            catalog_resolver=lambda _: resolved,
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["checks"]["catalog"]["result"], resolved)
        self.assertEqual(calls["inventory"]["material_id"], "MT01")
        self.assertEqual(calls["price"]["design"]["materialID"], "MT01")
        self.assertEqual(calls["inventory"]["stones"], calls["price"]["design"]["stones"])
        self.assertEqual(calls["inventory"]["charms"], calls["price"]["design"]["charms"])
        self.assertEqual(calls["inventory"]["stones"][0]["quantity"], 2.5)
        self.assertEqual(calls["inventory"]["charms"][0]["quantity"], 3)

    def test_ambiguous_or_not_found_catalog_match_short_circuits_validators(self):
        for match_status in ("ambiguous", "not_found"):
            with self.subTest(match_status=match_status):
                catalog_result = {
                    "status": match_status,
                    "requested_preferences": {"stones": [{"gem": "Diamond", "color": "Pink"}]},
                    "matches": {
                        "stones": [{
                            "status": match_status,
                            "candidates": [],
                            "alternatives": [{"id": "S019", "label": "alternative; requires explicit confirmation"}],
                            "errors": ["No exact catalog match was found."],
                            "warnings": ["Alternative needs confirmation."],
                        }]
                    },
                    "resolved": None,
                    "errors": [{"component": "stones", "index": 0, "message": "No exact catalog match was found."}],
                    "warnings": [],
                }
                calls = []

                def should_not_run(**_kwargs):
                    calls.append("ran")
                    return {"valid": True, "errors": [], "warnings": []}

                result = orchestrate_validation(
                    inventory_request={},
                    price_request={},
                    catalog_preferences={"stones": [{"gem": "Diamond", "color": "Pink"}]},
                    inventory_validator=should_not_run,
                    price_validator=should_not_run,
                    catalog_resolver=lambda _: catalog_result,
                )

                self.assertFalse(result["valid"])
                self.assertEqual(result["status"], "clarification_required")
                self.assertTrue(result["clarification_required"])
                self.assertTrue(result["revision_required"])
                self.assertEqual(result["checks"]["inventory"]["status"], "skipped")
                self.assertEqual(result["checks"]["price"]["status"], "skipped")
                self.assertEqual(result["checks"]["catalog"]["result"], catalog_result)
                self.assertEqual(calls, [])

    def test_catalog_match_with_insufficient_stock_fails_after_resolution(self):
        resolved = {"status": "matched", "matches": {}, "resolved": {"stones": [{"stoneID": "S001", "quantity": 4}]}, "errors": [], "warnings": []}
        result = orchestrate_validation(
            inventory_request={},
            price_request={"design": {}, "budget": 10},
            catalog_preferences={"stones": [{"gem": "Diamond", "quantity": 4}]},
            inventory_validator=lambda **_: {
                "valid": False,
                "errors": ["Stone S001 has insufficient stock: requested 4, available 2."],
                "warnings": [],
                "components": {"stones": [{"id": "S001", "available_quantity": 2}]},
            },
            price_validator=lambda **_: {
                "valid": True,
                "computed_price": 8,
                "budget": 10,
                "difference": 2,
                "errors": [],
                "warnings": [],
                "breakdown": {},
            },
            catalog_resolver=lambda _: resolved,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["checks"]["catalog"]["status"], "passed")
        self.assertEqual(result["checks"]["inventory"]["status"], "failed")
        self.assertEqual(result["checks"]["price"]["status"], "passed")

    def test_catalog_match_with_over_budget_price_fails_after_resolution(self):
        resolved = {"status": "matched", "matches": {}, "resolved": {"charms": [{"charmID": "A001", "quantity": 1}]}, "errors": [], "warnings": []}
        result = orchestrate_validation(
            inventory_request={},
            price_request={"design": {}, "budget": 10},
            catalog_preferences={"charms": [{"design": "shapes", "quantity": 1}]},
            inventory_validator=lambda **_: {"valid": True, "errors": [], "warnings": [], "components": {}},
            price_validator=lambda **_: {
                "valid": False,
                "computed_price": 15,
                "budget": 10,
                "difference": -5,
                "errors": ["Design exceeds the budget by 5."],
                "warnings": [],
                "breakdown": {},
            },
            catalog_resolver=lambda _: resolved,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["checks"]["inventory"]["status"], "passed")
        self.assertEqual(result["checks"]["price"]["status"], "failed")

    def test_catalog_backend_failure_skips_inventory_and_price(self):
        catalog_result = {
            "status": "error",
            "matches": {},
            "resolved": None,
            "errors": [{"component": "stones", "index": 0, "message": "Catalog offline"}],
            "warnings": [],
            "backend_error": "connection",
        }
        calls = []
        result = orchestrate_validation(
            inventory_request={},
            price_request={},
            catalog_preferences={"stones": [{"gem": "Diamond"}]},
            inventory_validator=lambda **_: calls.append("inventory"),
            price_validator=lambda **_: calls.append("price"),
            catalog_resolver=lambda _: catalog_result,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["technical_errors"][0]["check"], "catalog")
        self.assertEqual(calls, [])

    def test_catalog_resolution_feeds_same_ids_and_quantities_to_both_validators(self):
        resolved = {
            "status": "matched",
            "requested_preferences": {},
            "matches": {},
            "resolved": {
                "materialID": "MT01",
                "stones": [{"stoneID": "S019", "quantity": 2.5}],
                "charms": [{"charmID": "A001", "quantity": 3}],
            },
            "errors": [],
            "warnings": [],
        }
        calls = {}

        def inventory_validator(**kwargs):
            calls["inventory"] = kwargs
            return {"valid": True, "errors": [], "warnings": [], "components": {}}

        def price_validator(**kwargs):
            calls["price"] = kwargs
            return {
                "valid": True,
                "computed_price": 50,
                "budget": 60,
                "difference": 10,
                "errors": [],
                "warnings": [],
                "breakdown": {},
            }

        result = orchestrate_validation(
            inventory_request={"stones": [], "charms": []},
            price_request={"design": {"type": "ring", "product": {}}, "budget": 60},
            catalog_preferences={
                "material": "gold",
                "stones": [{"gem": "Sapphire", "color": "Pink", "quantity": 2.5}],
                "charms": [{"design": "shapes", "color": "Silver", "quantity": 3}],
            },
            inventory_validator=inventory_validator,
            price_validator=price_validator,
            catalog_resolver=lambda _: resolved,
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["checks"]["catalog"]["result"], resolved)
        self.assertEqual(calls["inventory"]["material_id"], "MT01")
        self.assertEqual(calls["price"]["design"]["materialID"], "MT01")
        self.assertEqual(calls["inventory"]["stones"], calls["price"]["design"]["stones"])
        self.assertEqual(calls["inventory"]["charms"], calls["price"]["design"]["charms"])
        self.assertEqual(calls["inventory"]["stones"][0]["quantity"], 2.5)
        self.assertEqual(calls["inventory"]["charms"][0]["quantity"], 3)

    def test_ambiguous_or_not_found_catalog_match_short_circuits_validators(self):
        for match_status in ("ambiguous", "not_found"):
            with self.subTest(match_status=match_status):
                catalog_result = {
                    "status": match_status,
                    "requested_preferences": {"stones": [{"gem": "Diamond", "color": "Pink"}]},
                    "matches": {
                        "stones": [{
                            "status": match_status,
                            "candidates": [],
                            "alternatives": [{"id": "S019", "label": "alternative; requires explicit confirmation"}],
                            "errors": ["No exact catalog match was found."],
                            "warnings": ["Alternative needs confirmation."],
                        }]
                    },
                    "resolved": None,
                    "errors": [{"component": "stones", "index": 0, "message": "No exact catalog match was found."}],
                    "warnings": [],
                }
                calls = []

                def should_not_run(**_kwargs):
                    calls.append("ran")
                    return {"valid": True, "errors": [], "warnings": []}

                result = orchestrate_validation(
                    inventory_request={},
                    price_request={},
                    catalog_preferences={"stones": [{"gem": "Diamond", "color": "Pink"}]},
                    inventory_validator=should_not_run,
                    price_validator=should_not_run,
                    catalog_resolver=lambda _: catalog_result,
                )

                self.assertFalse(result["valid"])
                self.assertEqual(result["status"], "clarification_required")
                self.assertTrue(result["clarification_required"])
                self.assertTrue(result["revision_required"])
                self.assertEqual(result["checks"]["inventory"]["status"], "skipped")
                self.assertEqual(result["checks"]["price"]["status"], "skipped")
                self.assertEqual(result["checks"]["catalog"]["result"], catalog_result)
                self.assertEqual(calls, [])

    def test_catalog_match_with_insufficient_stock_fails_after_resolution(self):
        resolved = {"status": "matched", "matches": {}, "resolved": {"stones": [{"stoneID": "S001", "quantity": 4}]}, "errors": [], "warnings": []}
        result = orchestrate_validation(
            inventory_request={},
            price_request={"design": {}, "budget": 10},
            catalog_preferences={"stones": [{"gem": "Diamond", "quantity": 4}]},
            inventory_validator=lambda **_: {
                "valid": False,
                "errors": ["Stone S001 has insufficient stock: requested 4, available 2."],
                "warnings": [],
                "components": {"stones": [{"id": "S001", "available_quantity": 2}]},
            },
            price_validator=lambda **_: {
                "valid": True,
                "computed_price": 8,
                "budget": 10,
                "difference": 2,
                "errors": [],
                "warnings": [],
                "breakdown": {},
            },
            catalog_resolver=lambda _: resolved,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["checks"]["catalog"]["status"], "passed")
        self.assertEqual(result["checks"]["inventory"]["status"], "failed")
        self.assertEqual(result["checks"]["price"]["status"], "passed")

    def test_catalog_match_with_over_budget_price_fails_after_resolution(self):
        resolved = {"status": "matched", "matches": {}, "resolved": {"charms": [{"charmID": "A001", "quantity": 1}]}, "errors": [], "warnings": []}
        result = orchestrate_validation(
            inventory_request={},
            price_request={"design": {}, "budget": 10},
            catalog_preferences={"charms": [{"design": "shapes", "quantity": 1}]},
            inventory_validator=lambda **_: {"valid": True, "errors": [], "warnings": [], "components": {}},
            price_validator=lambda **_: {
                "valid": False,
                "computed_price": 15,
                "budget": 10,
                "difference": -5,
                "errors": ["Design exceeds the budget by 5."],
                "warnings": [],
                "breakdown": {},
            },
            catalog_resolver=lambda _: resolved,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["checks"]["inventory"]["status"], "passed")
        self.assertEqual(result["checks"]["price"]["status"], "failed")

    def test_catalog_backend_failure_skips_inventory_and_price(self):
        catalog_result = {
            "status": "error",
            "matches": {},
            "resolved": None,
            "errors": [{"component": "stones", "index": 0, "message": "Catalog offline"}],
            "warnings": [],
            "backend_error": "connection",
        }
        calls = []
        result = orchestrate_validation(
            inventory_request={},
            price_request={},
            catalog_preferences={"stones": [{"gem": "Diamond"}]},
            inventory_validator=lambda **_: calls.append("inventory"),
            price_validator=lambda **_: calls.append("price"),
            catalog_resolver=lambda _: catalog_result,
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["technical_errors"][0]["check"], "catalog")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

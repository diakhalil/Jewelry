"""Catalog matching tests with mocked Node catalog collections."""

from __future__ import annotations

import unittest

from src.validation.backend_client import (
    BackendConnectionError,
    BackendHTTPError,
    BackendResponseError,
)
from src.validation.catalog import (
    match_charm,
    match_material,
    match_stone,
    resolve_catalog_preferences,
)


def stone(
    stone_id,
    *,
    gem="Diamond",
    color="clear",
    cut="round",
    purity="real gem",
    qty=5,
    weight=1.2,
):
    return {
        "stoneID": stone_id,
        "certificationID": f"CERT-{stone_id}",
        "cut": cut,
        "gem": gem,
        "purity": purity,
        "rarity": "rare",
        "price": 450.0,
        "color": color,
        "weight": weight,
        "qty": qty,
        "photoURL": None,
        "colorHex": None,
    }


def charm(charm_id, *, design="shapes", color="Silver", qty=3):
    return {
        "charmID": charm_id,
        "design": design,
        "qty": qty,
        "price": 12.5,
        "color": color,
        "photoURL": None,
    }


class StaticCatalogClient:
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


class CatalogValidationTests(unittest.TestCase):
    def test_exact_material_match_returns_candidate_and_metadata(self):
        client = StaticCatalogClient(
            materials=[
                {"materialID": "MT01", "metal": "14K Gold", "price": 12.0},
                {"materialID": "MT02", "metal": "silver", "price": 2.0},
            ]
        )

        result = match_material("14K Gold", client=client)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["id"], "MT01")
        self.assertEqual(result["candidates"][0]["metadata"]["price"], 12.0)
        self.assertNotIn("available_quantity", result["candidates"][0])

    def test_exact_gem_and_color_match_includes_quantity_as_metadata(self):
        client = StaticCatalogClient(
            stones=[
                stone("S001", gem="Diamond", color="clear"),
                stone("S019", gem="Sapphire", color="Pink", cut="princess", purity="lab grown"),
            ]
        )

        result = match_stone({"gem": "Sapphire", "color": "Pink"}, client=client)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["id"], "S019")
        self.assertEqual(result["candidates"][0]["available_quantity"], 5)
        self.assertEqual(result["candidates"][0]["metadata"]["purity"], "lab grown")

    def test_matching_is_case_insensitive_and_normalizes_whitespace(self):
        client = StaticCatalogClient(
            materials=[{"materialID": "MT05", "metal": "rose   gold", "price": 13.0}],
            stones=[stone("S020", gem="Sapphire", color="Blue Green")],
            charms=[charm("A001", design="letter bubble", color="RoseGold")],
        )

        self.assertEqual(match_material(" ROSE GOLD ", client=client)["status"], "matched")
        self.assertEqual(
            match_stone({"gem": "sapphire", "color": " blue   green "}, client=client)["status"],
            "matched",
        )
        self.assertEqual(
            match_charm({"design": " LETTER   BUBBLE ", "color": "rosegold"}, client=client)["status"],
            "matched",
        )

    def test_multiple_exact_records_are_ambiguous_without_selection(self):
        client = StaticCatalogClient(
            materials=[
                {"materialID": "MT01", "metal": "gold", "price": 15.0},
                {"materialID": "MT06", "metal": "Gold", "price": 20.0},
            ]
        )

        result = match_material(" GOLD ", client=client)

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual({candidate["id"] for candidate in result["candidates"]}, {"MT01", "MT06"})
        self.assertNotIn("selected_id", result)
        self.assertTrue(any("select a candidate explicitly" in warning for warning in result["warnings"]))

    def test_pink_diamond_is_not_substituted_and_alternatives_explain_differences(self):
        client = StaticCatalogClient(
            stones=[
                stone("S001", gem="Diamond", color="clear"),
                stone("S019", gem="Sapphire", color="Pink", cut="princess", purity="lab grown"),
                stone("S020", gem="Sapphire", color="Pink", cut="pear", purity="lab grown"),
            ]
        )

        result = match_stone({"gem": "Diamond", "color": "Pink"}, client=client)

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["candidates"], [])
        alternatives = {candidate["id"]: candidate for candidate in result["alternatives"]}
        self.assertIn("S001", alternatives)  # diamond, but not pink
        self.assertIn("S019", alternatives)  # pink, but Sapphire
        self.assertEqual(alternatives["S019"]["differences"]["gem"]["available"], "Sapphire")
        self.assertIn("explicit confirmation", alternatives["S019"]["label"])

    def test_charm_design_and_color_are_matched_exactly(self):
        client = StaticCatalogClient(
            charms=[
                charm("A088", design="letter bubble", color="Silver"),
                charm("A071", design="shapes", color="Silver"),
            ]
        )

        result = match_charm(
            {"design": "letter bubble", "color": "silver"}, client=client
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["id"], "A088")

    def test_zero_stock_is_still_a_catalog_match(self):
        client = StaticCatalogClient(stones=[stone("S023", qty=0)])

        result = match_stone({"stoneID": "S023"}, client=client)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["available_quantity"], 0)
        self.assertTrue(any("Inventory Validator" in warning for warning in result["warnings"]))

    def test_backend_connection_and_malformed_responses_are_errors(self):
        class OfflineClient:
            def get_materials(self):
                raise BackendConnectionError("backend offline")

        connection_result = match_material("gold", client=OfflineClient())
        self.assertEqual(connection_result["status"], "error")
        self.assertEqual(connection_result["backend_error"], "connection")

        class HTTPFailureClient:
            def get_charms(self):
                raise BackendHTTPError("upstream error", status_code=503)

        http_result = match_charm({"design": "shapes"}, client=HTTPFailureClient())
        self.assertEqual(http_result["status"], "error")
        self.assertEqual(http_result["backend_error"], "http")

        class MalformedClient:
            def get_stones(self):
                raise BackendResponseError("invalid backend payload")

        backend_response_error = match_stone(
            {"gem": "Diamond"}, client=MalformedClient()
        )
        self.assertEqual(backend_response_error["status"], "error")
        self.assertEqual(backend_response_error["backend_error"], "response")

        malformed_result = match_stone(
            {"gem": "Diamond"}, client=StaticCatalogClient(stones={"not": "a list"})
        )
        self.assertEqual(malformed_result["status"], "error")
        self.assertEqual(malformed_result["backend_error"], "response")

    def test_preference_resolution_returns_consistent_ids_and_preserves_quantities(self):
        client = StaticCatalogClient(
            materials=[{"materialID": "MT01", "metal": "Gold", "price": 15.0}],
            stones=[stone("S019", gem="Sapphire", color="Pink", purity="lab grown")],
            charms=[charm("A001", design="shapes", color="Silver")],
        )

        result = resolve_catalog_preferences(
            {
                "material": "gold",
                "stones": [{"gem": "Sapphire", "color": "pink", "quantity": 2.5}],
                "charms": [{"design": "shapes", "color": "silver"}],
            },
            client=client,
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(
            result["resolved"],
            {
                "materialID": "MT01",
                "stones": [{"stoneID": "S019", "quantity": 2.5}],
                "charms": [{"charmID": "A001"}],
            },
        )
        self.assertEqual(result["matches"]["stones"][0]["requested_quantity"], 2.5)

    def test_preference_resolution_keeps_alternatives_unresolved(self):
        client = StaticCatalogClient(
            stones=[stone("S001", gem="Diamond", color="clear")]
        )

        result = resolve_catalog_preferences(
            {"stones": [{"gem": "Diamond", "color": "Pink", "quantity": 1}]},
            client=client,
        )

        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["resolved"])
        self.assertTrue(result["matches"]["stones"][0]["alternatives"])
        self.assertTrue(any("explicit confirmation" in warning["message"] for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()

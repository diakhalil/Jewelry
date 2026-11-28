"""Regression tests for canonical 3D-to-validation input adaptation."""

from __future__ import annotations

import unittest

from src.validation.adapter import adapt_three_d_configuration


def ring_config(style="tennis", gem="diamond"):
    result = {"kind": "ring", "style": style, "metal": "gold", "gem": gem, "size": 7}
    if gem == "diamond":
        result["color"] = "blue"
    if style == "tennis":
        result["count"] = 20
    return result


def provenance(config):
    active = {"kind", "style", "metal", "gem"}
    if config.get("gem") == "diamond":
        active.add("color")
    if config.get("style") == "tennis":
        active.add("count")
    if config.get("kind") == "ring":
        active.add("size")
    if config.get("kind") == "bracelet":
        active.add("diameter")
    return {field: "explicit" for field in active}


def matched_catalog(preferences):
    matches = {
        "material": {
            "status": "matched", "candidates": [{"id": "MT77", "metadata": {"materialID": "MT77", "metal": "gold"}}],
            "errors": [], "warnings": [],
        }
    }
    if preferences.get("stones"):
        matches["stones"] = []
        for index, item in enumerate(preferences["stones"]):
            stone_id = item.get("stoneID", f"ST{index + 1}")
            matches["stones"].append({
                "status": "matched", "candidates": [{"id": stone_id, "metadata": {**item, "stoneID": stone_id}}],
                "errors": [], "warnings": [],
            })
    return {"status": "matched", "matches": matches, "resolved": {}, "errors": [], "warnings": []}


class ThreeDAdapterTests(unittest.TestCase):
    def test_explicit_mappings_produce_one_canonical_component_set(self):
        requests = []

        def resolver(preferences):
            requests.append(preferences)
            return matched_catalog(preferences)

        config = ring_config()
        result = adapt_three_d_configuration(
            config,
            field_provenance=provenance(config),
            confirmed_components={"materialID": "MT77", "stones": [{"stoneID": "ST1", "quantity": 3}]},
            approved_mappings={"diamond_color": {"catalog_field": "color", "catalog_value": "Blue"}},
            inventory_request={"stones": [], "charms": []},
            price_request={"design": {}, "budget": 200},
            catalog_preferences={"material": "gold", "stones": []},
            catalog_resolver=resolver,
        )

        self.assertTrue(result["catalog_resolved"])
        self.assertTrue(result["ready_for_inventory"])
        self.assertTrue(result["ready_for_price"])
        canonical = result["canonical_design"]
        expected = [{"stoneID": "ST1", "quantity": 3}]
        self.assertEqual(canonical["inventory_request"]["stones"], expected)
        self.assertEqual(canonical["price_request"]["design"]["stones"], expected)
        self.assertEqual(canonical["inventory_request"]["material_id"], "MT77")
        self.assertEqual(canonical["price_request"]["design"]["materialID"], "MT77")
        self.assertEqual(requests[0]["stones"][0]["color"], "Blue")
        self.assertEqual(result["canonical_design"]["three_d_configuration"]["count"], 20)
        self.assertTrue(any(item["configuration_field"] == "size" for item in result["unresolved_mappings"]))

    def test_ambiguous_material_requires_explicit_candidate(self):
        config = ring_config("plain", "none")
        def resolver(_):
            return {
                "status": "ambiguous", "resolved": None,
                "matches": {"material": {
                    "status": "ambiguous", "candidates": [{"id": "M1"}, {"id": "M2"}],
                }}, "errors": [], "warnings": [],
            }
        result = adapt_three_d_configuration(
            config, field_provenance=provenance(config), catalog_resolver=resolver,
        )
        self.assertFalse(result["catalog_resolved"])
        self.assertTrue(any(item["code"] == "ambiguous_catalog_match" for item in result["clarifications"]))

    def test_unavailable_approved_gem_color_requires_revision(self):
        config = ring_config()
        def resolver(preferences):
            result = matched_catalog(preferences)
            result["status"] = "not_found"
            result["matches"]["stones"][0] = {"status": "not_found", "candidates": [], "errors": ["not found"]}
            return result
        result = adapt_three_d_configuration(
            config,
            field_provenance=provenance(config),
            confirmed_components={"stones": [{"quantity": 2}]},
            approved_mappings={"diamond_color": {"catalog_field": "color", "catalog_value": "Blue"}},
            catalog_resolver=resolver,
        )
        self.assertFalse(result["catalog_resolved"])
        self.assertTrue(any(item["code"] == "catalog_match_unavailable" for item in result["clarifications"]))

    def test_color_equivalence_is_never_inferred(self):
        config = ring_config()
        requested = []
        result = adapt_three_d_configuration(
            config,
            field_provenance=provenance(config),
            catalog_resolver=lambda preferences: (requested.append(preferences) or matched_catalog(preferences)),
        )
        self.assertTrue(any(item["code"] == "diamond_color_mapping_required" for item in result["clarifications"]))
        self.assertEqual(len(requested), 1)
        self.assertNotIn("stones", requested[0])

    def test_conflicting_material_ids_stop_before_catalog(self):
        config = ring_config("plain", "none")
        calls = []
        result = adapt_three_d_configuration(
            config,
            field_provenance=provenance(config),
            confirmed_components={"materialID": "M1"},
            inventory_request={"material_id": "M1"},
            price_request={"design": {"materialID": "M2"}},
            catalog_resolver=lambda _: calls.append(True),
        )
        self.assertTrue(any(item["code"] == "conflicting_material_ids" for item in result["conflicts"]))
        self.assertEqual(calls, [])

    def test_different_inventory_and_price_quantities_are_rejected(self):
        config = ring_config()
        result = adapt_three_d_configuration(
            config,
            field_provenance=provenance(config),
            approved_mappings={"diamond_color": {"catalog_field": "color", "catalog_value": "Blue"}},
            inventory_request={"stones": [{"stoneID": "ST1", "quantity": 2}]},
            price_request={"design": {"stones": [{"stoneID": "ST1", "quantity": 3}]}},
        )
        self.assertTrue(any(item["code"] == "inconsistent_stone_quantities" for item in result["conflicts"]))

    def test_unverified_backend_style_skips_only_price_and_quantity_is_explicit(self):
        config = ring_config("solitaire", "sapphire")
        result = adapt_three_d_configuration(
            config,
            field_provenance=provenance(config),
            confirmed_components={"stones": [{"stoneID": "S1", "quantity": 1}]},
            catalog_resolver=matched_catalog,
        )
        self.assertTrue(result["ready_for_inventory"])
        self.assertFalse(result["ready_for_price"])
        self.assertTrue(any(item["code"] == "pricing_style_unverified" for item in result["clarifications"]))

    def test_missing_provenance_and_missing_inventory_quantity_require_clarification(self):
        config = ring_config()
        missing_provenance = adapt_three_d_configuration(config)
        self.assertTrue(any(item["code"] == "field_provenance_required" for item in missing_provenance["clarifications"]))

        result = adapt_three_d_configuration(
            config,
            field_provenance=provenance(config),
            approved_mappings={"diamond_color": {"catalog_field": "color", "catalog_value": "Blue"}},
            catalog_resolver=matched_catalog,
        )
        self.assertTrue(any(item["code"] == "explicit_stone_quantity_required" for item in result["clarifications"]))
        self.assertFalse(result["ready_for_inventory"])

    def test_conversation_default_inactive_field_is_distinguished_from_explicit_request(self):
        config = ring_config("tennis", "sapphire")
        config["color"] = "white"
        source = provenance(config)
        source["color"] = "conversation_default"
        result = adapt_three_d_configuration(
            config,
            field_provenance=source,
            confirmed_components={"stones": [{"stoneID": "S1", "quantity": 1}]},
            catalog_resolver=matched_catalog,
        )
        self.assertNotIn("color", result["canonical_design"]["three_d_configuration"])
        self.assertTrue(any(item["code"] == "inactive_conversation_default_omitted" for item in result["warnings"]))

        explicit = provenance(config)
        explicit["color"] = "explicit"
        rejected = adapt_three_d_configuration(config, field_provenance=explicit, catalog_resolver=matched_catalog)
        self.assertFalse(rejected["compatibility"]["valid"])
        self.assertTrue(any(error.get("code") == "ignored_property" for error in rejected["compatibility"]["errors"]))


if __name__ == "__main__":
    unittest.main()

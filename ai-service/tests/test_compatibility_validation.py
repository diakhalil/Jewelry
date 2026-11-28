"""Tests for the 3D preview configuration compatibility contract."""

from __future__ import annotations

import unittest

from src.validation.compatibility import validate_3d_configuration


class CompatibilityValidationTests(unittest.TestCase):
    def test_valid_ring_and_bracelet_configs(self):
        ring = validate_3d_configuration({
            "kind": "ring", "style": "solitaire", "metal": "gold",
            "gem": "diamond", "color": "white", "size": 6.5,
        })
        bracelet = validate_3d_configuration({
            "kind": "bracelet", "style": "plain", "metal": "rose-gold",
            "gem": "none", "diameter": 65,
        })
        tennis = validate_3d_configuration({
            "kind": "ring", "style": "tennis", "metal": "silver",
            "gem": "sapphire", "count": 9, "size": 7,
        })

        self.assertTrue(ring["valid"], ring["errors"])
        self.assertTrue(bracelet["valid"], bracelet["errors"])
        self.assertTrue(tennis["valid"], tennis["errors"])
        self.assertEqual(tennis["warnings"][0]["code"], "conversation_restriction_only")

    def test_missing_fields_are_not_defaulted(self):
        result = validate_3d_configuration({"kind": "ring", "style": "solitaire", "metal": "gold"})
        self.assertFalse(result["valid"])
        self.assertIn("gem", {error.get("field") for error in result["errors"]})
        self.assertIn("size", {error.get("field") for error in result["errors"]})

    def test_unsupported_kind_and_color(self):
        kind = validate_3d_configuration({
            "kind": "necklace", "style": "solitaire", "metal": "gold",
            "gem": "diamond", "color": "white",
        })
        color = validate_3d_configuration({
            "kind": "ring", "style": "solitaire", "metal": "gold",
            "gem": "diamond", "color": "purple", "size": 7,
        })
        self.assertFalse(kind["valid"])
        self.assertFalse(color["valid"])
        self.assertIn("kind", {error.get("field") for error in kind["errors"]})
        self.assertIn("color", {error.get("field") for error in color["errors"]})

    def test_invalid_ring_sizes_and_diameters(self):
        for size in (2.5, 13.5, 6.2, 10 ** 400):
            with self.subTest(size=size):
                result = validate_3d_configuration({
                    "kind": "ring", "style": "solitaire", "metal": "gold",
                    "gem": "sapphire", "size": size,
                })
                self.assertFalse(result["valid"])
        for diameter in (49, 81):
            with self.subTest(diameter=diameter):
                result = validate_3d_configuration({
                    "kind": "bracelet", "style": "solitaire", "metal": "gold",
                    "gem": "sapphire", "diameter": diameter,
                })
                self.assertFalse(result["valid"])

    def test_tennis_count_range_integer_and_fractional_quantity(self):
        base = {"kind": "ring", "style": "tennis", "metal": "gold", "gem": "diamond", "color": "white", "size": 7}
        for count in (7, 33, 8.5, True):
            with self.subTest(count=count):
                result = validate_3d_configuration({**base, "count": count})
                self.assertFalse(result["valid"])
                self.assertTrue(any(error.get("field") == "count" for error in result["errors"]))

    def test_conflicting_and_ignored_properties_are_reported(self):
        plain_gem = validate_3d_configuration({
            "kind": "ring", "style": "plain", "metal": "gold", "gem": "ruby", "size": 7,
        })
        tennis_none = validate_3d_configuration({
            "kind": "ring", "style": "tennis", "metal": "gold", "gem": "none", "count": 12, "size": 7,
        })
        solitaire_none = validate_3d_configuration({
            "kind": "ring", "style": "solitaire", "metal": "gold", "gem": "none", "size": 7,
        })
        color_ignored = validate_3d_configuration({
            "kind": "ring", "style": "solitaire", "metal": "gold", "gem": "sapphire", "color": "blue", "size": 7,
        })
        self.assertFalse(plain_gem["valid"])
        self.assertFalse(tennis_none["valid"])
        self.assertFalse(solitaire_none["valid"])
        self.assertFalse(color_ignored["valid"])
        self.assertTrue(plain_gem["unsupported_combinations"])
        self.assertIn("color", color_ignored["unsupported_fields"])

    def test_non_applicable_and_unknown_fields_are_not_silently_ignored(self):
        result = validate_3d_configuration({
            "kind": "bracelet", "style": "plain", "metal": "silver", "gem": "none",
            "diameter": 60, "size": 7, "engraving": "hello",
        })
        self.assertFalse(result["valid"])
        self.assertEqual(result["unsupported_fields"], ["engraving", "size"])


if __name__ == "__main__":
    unittest.main()

"""Strict compatibility checks for the Michelle 3D preview generator.

This validates the preview generator's input contract only. It does not resolve
database components, infer a catalog match, or claim that a procedural gem is a
specific inventory item.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


CAPABILITIES = {
    "kind": ("ring", "bracelet"),
    "style": ("solitaire", "tennis", "plain"),
    "metal": ("gold", "silver", "rose-gold"),
    "gem": ("diamond", "sapphire", "ruby", "emerald", "none"),
    "color": ("white", "pink", "blue", "yellow", "green"),
    "tennis_count": {"minimum": 8, "maximum": 32, "integer": True},
    "ring_size": {"minimum": 3, "maximum": 13, "increment": 0.5, "unit": "US size"},
    "bracelet_diameter": {"minimum": 50, "maximum": 80, "unit": "mm"},
}

CONVERSATION_ONLY_RESTRICTIONS = {
    "suggested_tennis_counts": [12, 20, 28],
    "suggested_ring_sizes": [6, 7, 8],
    "suggested_bracelet_diameters_mm": [55, 60, 65],
    "tennis_defaults_to_diamond_in_conversation": True,
    "note": "These are conversation choices/default behavior; the generator accepts the broader capabilities above.",
}

_REQUIRED_ALWAYS = ("kind", "style", "metal", "gem")
_ALLOWED_FIELDS = frozenset((*_REQUIRED_ALWAYS, "color", "count", "size", "diameter"))


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def validate_3d_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one raw 3D configuration without inserting defaults or coercing values."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    unsupported_fields: list[str] = []
    unsupported_combinations: list[dict[str, Any]] = []
    revision_suggestions: list[str] = []

    if not isinstance(configuration, Mapping):
        return {
            "valid": False,
            "errors": [{"code": "invalid_configuration", "message": "3D configuration must be an object."}],
            "warnings": [],
            "unsupported_fields": [],
            "unsupported_combinations": [],
            "revision_suggestions": ["Provide the generator configuration as an object."],
            "generator_capabilities": CAPABILITIES,
            "conversation_only_restrictions": CONVERSATION_ONLY_RESTRICTIONS,
        }

    config = dict(configuration)

    def error(code: str, message: str, *, field: str | None = None) -> None:
        item: dict[str, Any] = {"code": code, "message": message}
        if field:
            item["field"] = field
        errors.append(item)

    for field in sorted(set(config) - _ALLOWED_FIELDS):
        unsupported_fields.append(field)
        error("unsupported_field", f"Field {field!r} is not consumed by the 3D generator.", field=field)
        revision_suggestions.append(f"Remove {field!r} or handle it in a separately validated integration step.")

    for field in _REQUIRED_ALWAYS:
        if field not in config or config[field] is None or config[field] == "":
            error("missing_required_field", f"{field} is required; the validator will not apply a generator default.", field=field)

    enums = {
        "kind": CAPABILITIES["kind"],
        "style": CAPABILITIES["style"],
        "metal": CAPABILITIES["metal"],
        "gem": CAPABILITIES["gem"],
    }
    for field, supported in enums.items():
        value = config.get(field)
        if value is not None and value != "" and (not isinstance(value, str) or value not in supported):
            error("unsupported_value", f"Unsupported {field} value {value!r}; supported values are {', '.join(supported)}.", field=field)
            revision_suggestions.append(f"Choose a supported {field}: {', '.join(supported)}.")

    kind, style, gem = config.get("kind"), config.get("style"), config.get("gem")

    if "color" in config:
        color = config["color"]
        if gem != "diamond":
            unsupported_fields.append("color")
            error("ignored_property", "The generator applies color only to diamonds; this color would be ignored for the selected gem.", field="color")
            unsupported_combinations.append({"fields": ["gem", "color"], "reason": "Color is implemented only for diamond materials."})
            revision_suggestions.append("Choose diamond to use a diamond color, or remove color if the gemstone color is determined by gem type.")
        elif not isinstance(color, str) or color not in CAPABILITIES["color"]:
            error("unsupported_value", f"Unsupported diamond color {color!r}; supported colors are {', '.join(CAPABILITIES['color'])}.", field="color")
            revision_suggestions.append(f"Choose a diamond color: {', '.join(CAPABILITIES['color'])}.")
    elif gem == "diamond":
        error("missing_required_field", "color is required for a diamond configuration; the validator will not assume white.", field="color")
        revision_suggestions.append("Select an explicit supported diamond color.")

    if style == "plain" and gem not in (None, "none"):
        error("conflicting_style_gem", "Plain style suppresses gemstone geometry, so the requested gem would be lost.")
        unsupported_combinations.append({"fields": ["style", "gem"], "reason": "Plain style does not generate a gemstone."})
        revision_suggestions.append("Set gem to 'none' for a plain design, or choose solitaire or tennis to render a gemstone.")
    if style == "tennis" and gem == "none":
        error("conflicting_style_gem", "Tennis style repeats a gemstone setting; with gem='none' the generator produces no stones.")
        unsupported_combinations.append({"fields": ["style", "gem"], "reason": "Tennis geometry is only repeated when a gemstone is present."})
        revision_suggestions.append("Select diamond, sapphire, ruby, or emerald for a tennis design, or choose plain style.")
    if style == "solitaire" and gem == "none":
        error("conflicting_style_gem", "Solitaire style requests a single gemstone, but gem='none' causes the generator to omit it.")
        unsupported_combinations.append({"fields": ["style", "gem"], "reason": "Solitaire setting geometry is generated only when a gemstone is present."})
        revision_suggestions.append("Select diamond, sapphire, or emerald for solitaire, or choose plain style for a band without a gem.")

    if "count" in config:
        count = config["count"]
        if isinstance(count, bool) or not isinstance(count, int):
            error("invalid_count", "count must be an integer; fractional stone counts are unsupported.", field="count")
            revision_suggestions.append("Use an integer stone count.")
        elif style != "tennis":
            unsupported_fields.append("count")
            error("ignored_property", "count is consumed only by tennis style and would otherwise be ignored.", field="count")
            revision_suggestions.append("Remove count for non-tennis styles, or select tennis.")
        elif not 8 <= count <= 32:
            error("out_of_range", "Tennis stone count must be an integer from 8 through 32.", field="count")
            revision_suggestions.append("Set count to an integer from 8 through 32.")
    elif style == "tennis":
        error("missing_required_field", "count is required for tennis style; the validator will not use the generator's default.", field="count")
        revision_suggestions.append("Provide an integer tennis stone count from 8 through 32.")

    if "size" in config:
        size = config["size"]
        if not _finite_number(size):
            error("invalid_size", "size must be a finite number in US sizing.", field="size")
            revision_suggestions.append("Set size to a US ring size from 3 through 13 in 0.5 increments.")
        elif kind != "ring":
            unsupported_fields.append("size")
            error("ignored_property", "size is consumed only for rings and would otherwise be ignored.", field="size")
            revision_suggestions.append("Remove size for a bracelet, or select ring.")
        elif not (3 <= size <= 13 and (float(size) * 2).is_integer()):
            error("out_of_range", "US ring size must be from 3 through 13 in half-size increments.", field="size")
            revision_suggestions.append("Set size to 3, 3.5, …, or 13.")
    elif kind == "ring":
        error("missing_required_field", "size is required for a ring configuration; the validator will not use the generator's implicit base size.", field="size")
        revision_suggestions.append("Provide a US ring size from 3 through 13 in 0.5 increments.")

    if "diameter" in config:
        diameter = config["diameter"]
        if not _finite_number(diameter):
            error("invalid_diameter", "diameter must be a finite number in millimeters.", field="diameter")
            revision_suggestions.append("Set bracelet diameter to a value from 50 through 80 mm.")
        elif kind != "bracelet":
            unsupported_fields.append("diameter")
            error("ignored_property", "diameter is consumed only for bracelets and would otherwise be ignored.", field="diameter")
            revision_suggestions.append("Remove diameter for a ring, or select bracelet.")
        elif not 50 <= diameter <= 80:
            error("out_of_range", "Bracelet diameter must be from 50 through 80 mm.", field="diameter")
            revision_suggestions.append("Set diameter to a value from 50 through 80 mm.")
    elif kind == "bracelet":
        error("missing_required_field", "diameter is required for a bracelet; the validator will not use the generator's default.", field="diameter")
        revision_suggestions.append("Provide a bracelet diameter from 50 through 80 mm.")

    if style == "tennis" and gem not in (None, "none", "diamond"):
        warnings.append({
            "code": "conversation_restriction_only",
            "message": "The generator can repeat this gemstone in tennis style; the conversation flow currently forces tennis requests to diamond.",
            "fields": ["style", "gem"],
        })

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "unsupported_fields": sorted(set(unsupported_fields)),
        "unsupported_combinations": unsupported_combinations,
        "revision_suggestions": list(dict.fromkeys(revision_suggestions)),
        "generator_capabilities": CAPABILITIES,
        "conversation_only_restrictions": CONVERSATION_ONLY_RESTRICTIONS,
    }

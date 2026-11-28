"""Translate a 3D preview request into the existing validation contracts.

The adapter deliberately requires provenance for conversation-populated values,
an approved database color mapping for diamond color, and explicit inventory
quantities. It never infers component IDs or equates visual stone count with
inventory quantity.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from .catalog import resolve_catalog_preferences
from .compatibility import validate_3d_configuration


CatalogResolver = Callable[[Mapping[str, Any]], Mapping[str, Any]]
CompatibilityValidator = Callable[[Any], Mapping[str, Any]]
_DEFAULTS = {"color": "white", "count": 1, "size": 7, "diameter": 60}
_ACTIVE = {"kind", "style", "metal", "gem", "color", "count", "size", "diameter"}
_METAL_LABELS = {"gold": "gold", "silver": "silver", "rose-gold": "rose gold"}
_METAL_FAMILY_LABELS = {
    "gold": {"gold", "14k gold"},
    "silver": {"silver", "14k silver"},
    "rose-gold": {"rose gold"},
}


def _normalize_label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _clarification(code: str, message: str, fields: list[str], *, blocking: bool = True) -> dict[str, Any]:
    return {"code": code, "message": message, "fields": fields, "blocking": blocking}


def _quantity(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0 and numeric.is_integer()


def _component_list(value: Any, id_key: str, source: str, clarifications: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        clarifications.append(_clarification("invalid_component_list", f"{source} must be a list of confirmed components.", [source]))
        return None
    result = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            clarifications.append(_clarification("invalid_component", f"{source}[{index}] must be an object.", [source]))
            continue
        component_id = entry.get(id_key)
        quantity = entry.get("quantity")
        if not isinstance(component_id, str) or not component_id.strip():
            clarifications.append(_clarification("component_id_required", f"Confirm {id_key} for {source}[{index}].", [source, id_key]))
            continue
        if not _quantity(quantity):
            clarifications.append(_clarification(
                "explicit_integer_quantity_required",
                f"Provide an explicit positive integer quantity for {component_id}; visual stone count and validator defaults are not treated as inventory quantity.",
                [source, "quantity"],
            ))
            continue
        result.append({id_key: component_id.strip(), "quantity": int(quantity)})
    return result


def _same_components(left: list[dict[str, Any]], right: list[dict[str, Any]], id_key: str) -> bool:
    def totals(items: list[dict[str, Any]]) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in items:
            result[item[id_key]] = result.get(item[id_key], 0) + item["quantity"]
        return result
    return totals(left) == totals(right)


def _selected_candidate(
    result: Mapping[str, Any] | None,
    explicit_id: str | None,
    *,
    component: str,
    id_key: str,
    clarifications: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(result, Mapping):
        clarifications.append(_clarification("catalog_result_missing", f"Catalog did not return a {component} match result.", [component]))
        return None
    if result.get("backend_error"):
        clarifications.append(_clarification("catalog_unavailable", f"Catalog could not resolve {component}; retry after the catalog service is available.", [component]))
        return None
    candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
    if explicit_id:
        selected = next((candidate for candidate in candidates if candidate.get("id") == explicit_id), None)
        if selected is None:
            conflicts.append({
                "code": "selected_id_conflicts_with_catalog",
                "message": f"Explicitly selected {component} ID {explicit_id} is not an exact catalog candidate for the requested attributes.",
                "fields": [component, id_key],
            })
            return None
        return selected
    if len(candidates) == 1 and result.get("status") == "matched":
        return candidates[0]
    if result.get("status") == "ambiguous" or len(candidates) > 1:
        clarifications.append(_clarification(
            "ambiguous_catalog_match",
            f"Multiple {component} catalog records match. Select one candidate explicitly; none was chosen.",
            [component, id_key],
        ))
    elif result.get("status") == "not_found":
        clarifications.append(_clarification(
            "catalog_match_unavailable",
            f"No exact {component} catalog match exists. Revise the requested attributes or explicitly review an alternative.",
            [component],
        ))
    else:
        clarifications.append(_clarification("catalog_match_unresolved", f"The {component} catalog match is unresolved.", [component]))
    return None


def adapt_three_d_configuration(
    configuration: Mapping[str, Any],
    *,
    field_provenance: Mapping[str, str] | None = None,
    confirmed_components: Mapping[str, Any] | None = None,
    approved_mappings: Mapping[str, Any] | None = None,
    inventory_request: Mapping[str, Any] | None = None,
    price_request: Mapping[str, Any] | None = None,
    catalog_preferences: Mapping[str, Any] | None = None,
    compatibility_result: Mapping[str, Any] | None = None,
    catalog_resolver: CatalogResolver = resolve_catalog_preferences,
    compatibility_validator: CompatibilityValidator = validate_3d_configuration,
) -> dict[str, Any]:
    """Resolve a 3D request to canonical IDs and identical inventory/price inputs.

    ``field_provenance`` maps supplied fields to ``explicit`` or
    ``conversation_default``. ``confirmed_components`` accepts ``materialID``,
    ``stones`` (stoneID + integer quantity + optional catalog attributes), and
    ``charms`` (charmID + integer quantity). A diamond color is mapped only by
    ``approved_mappings.diamond_color = {catalog_field, catalog_value}``.
    """
    requested_configuration = dict(configuration) if isinstance(configuration, Mapping) else configuration
    raw = requested_configuration
    provenance = dict(field_provenance or {})
    selections = dict(confirmed_components or {})
    approvals = dict(approved_mappings or {})
    inventory_request = dict(inventory_request or {})
    price_request = dict(price_request or {})
    price_design = price_request.get("design")
    price_design = dict(price_design) if isinstance(price_design, Mapping) else {}
    existing_preferences = dict(catalog_preferences or {})
    clarifications: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    unresolved_mappings: list[dict[str, Any]] = []

    for field in sorted(set(inventory_request) - {"material_id", "stones", "charms"}):
        clarifications.append(_clarification(
            "unmapped_inventory_input",
            f"Inventory input {field!r} has no mapping in the current inventory validator and is preserved for review.",
            ["inventory_request", field],
        ))
    for field in sorted(set(price_request) - {"design", "budget"}):
        clarifications.append(_clarification(
            "unmapped_price_input",
            f"Price input {field!r} has no mapping in the current price validator and is preserved for review.",
            ["price_request", field],
        ))
    for field in sorted(set(price_design) - {"type", "materialID", "metal", "product", "charms", "stones"}):
        clarifications.append(_clarification(
            "unmapped_price_design_field",
            f"Price design field {field!r} is not consumed by the existing preview price validator and will not be treated as validated.",
            ["price_request.design", field],
        ))

    for field in sorted(set(selections) - {"materialID", "stones", "charms"}):
        clarifications.append(_clarification(
            "unsupported_confirmed_component_field",
            f"Confirmed component field {field!r} has no adapter mapping and will not be ignored.",
            ["confirmed_components", field],
        ))
    for field in sorted(set(approvals) - {"metal", "diamond_color"}):
        clarifications.append(_clarification(
            "unsupported_approval_mapping",
            f"Approved mapping {field!r} has no verified backend semantics and is not applied.",
            ["approved_mappings", field],
        ))
    for field, source in provenance.items():
        if field not in _ACTIVE:
            clarifications.append(_clarification(
                "unknown_field_provenance",
                f"No 3D configuration field named {field!r} exists; its provenance metadata is not applied.",
                ["three_d_field_provenance", field],
            ))
        elif source not in {"explicit", "conversation_default"}:
            clarifications.append(_clarification(
                "invalid_field_provenance",
                f"Provenance for {field} must be 'explicit' or 'conversation_default'.",
                ["three_d_field_provenance", field],
            ))

    if not isinstance(raw, Mapping):
        raw = {}
    effective = dict(raw)

    # Keep conversation-generated inactive defaults distinct from explicit user intent.
    kind, style, gem = effective.get("kind"), effective.get("style"), effective.get("gem")
    active = {"kind", "style", "metal", "gem"}
    if gem == "diamond":
        active.add("color")
    if style == "tennis":
        active.add("count")
    if kind == "ring":
        active.add("size")
    if kind == "bracelet":
        active.add("diameter")

    for field in active:
        if field not in effective:
            continue  # Compatibility validator returns the required-field diagnostic.
        source = provenance.get(field)
        if source == "conversation_default":
            clarifications.append(_clarification(
                "default_requires_confirmation",
                f"{field} currently comes from a conversation/generator default. Confirm the value before validating the design.",
                [field],
            ))
        elif source != "explicit":
            clarifications.append(_clarification(
                "field_provenance_required",
                f"Identify whether {field} was explicitly requested or generated as a default; the adapter will not assume.",
                [field],
            ))

    inactive_defaults = {"color": "white", "count": 1, "size": 7, "diameter": 60}
    for field in set(effective) & (_ACTIVE - active):
        source = provenance.get(field)
        if source == "conversation_default" and effective.get(field) == inactive_defaults.get(field):
            effective.pop(field)
            warnings.append({
                "code": "inactive_conversation_default_omitted",
                "field": field,
                "message": f"The known conversation default for inactive field {field} was omitted; it is not part of the selected generator configuration.",
            })
        elif source == "explicit":
            clarifications.append(_clarification(
                "explicit_property_not_representable",
                f"{field} was explicitly requested but is inactive for this 3D configuration and the generator would ignore it. Revise the request or choose a supported configuration.",
                [field, "kind", "style", "gem"],
            ))
        else:
            clarifications.append(_clarification(
                "inactive_field_provenance_required",
                f"{field} is inactive for this configuration. Confirm it is a conversation default before the adapter omits it.",
                [field],
            ))

    compatibility = dict(compatibility_result or compatibility_validator(effective))
    if compatibility.get("valid") is not True:
        return {
            "status": "incompatible",
            "valid": False,
            "requested_configuration": requested_configuration,
            "compatibility": compatibility,
            "catalog_result": None,
            "canonical_design": None,
            "errors": compatibility.get("errors", []),
            "clarifications": clarifications,
            "conflicts": conflicts,
            "warnings": warnings,
            "unresolved_mappings": unresolved_mappings,
            "ready_for_inventory": False,
            "ready_for_price": False,
        }

    # Provenance ambiguity is a hard stop: values are not promoted to user intent.
    if clarifications:
        return {
            "status": "clarification_required", "valid": False,
            "requested_configuration": requested_configuration,
            "compatibility": compatibility, "catalog_result": None,
            "canonical_design": None, "errors": [], "clarifications": clarifications,
            "conflicts": conflicts, "warnings": warnings,
            "unresolved_mappings": unresolved_mappings,
            "ready_for_inventory": False, "ready_for_price": False,
        }

    kind, style, metal, gem = (effective.get(field) for field in ("kind", "style", "metal", "gem"))
    db_metal = approvals.get("metal") or _METAL_LABELS.get(metal)
    if not isinstance(db_metal, str) or not db_metal.strip():
        clarifications.append(_clarification("material_mapping_required", "Provide an approved exact database metal label for the selected generator metal.", ["metal"]))
    elif _normalize_label(db_metal) not in _METAL_FAMILY_LABELS.get(metal, set()):
        clarifications.append(_clarification(
            "metal_family_mapping_conflict",
            f"The approved database label {db_metal!r} does not represent the selected 3D metal family {metal!r}.",
            ["metal", "approved_mappings.metal"],
        ))
        db_metal = None

    # The backend stores ring diameter / bracelet length. No verified mapping
    # exists from the generator's ring size / bracelet inner diameter to those fields.
    dimension_field = "size" if kind == "ring" else "diameter"
    backend_dimension = "ring.diameter" if kind == "ring" else "bracelet.length"
    unresolved_mappings.append({
        "configuration_field": dimension_field,
        "backend_field": backend_dimension,
        "status": "unresolved",
        "reason": "The generator and database use different dimension semantics; no approved conversion or equivalence is defined.",
    })
    clarifications.append(_clarification(
        "dimension_mapping_required",
        f"Confirm an approved mapping from 3D {dimension_field} to backend {backend_dimension}; no conversion is applied.",
        [dimension_field, backend_dimension],
        blocking=False,
    ))

    pricing_supported = kind == "ring" and style == "tennis"
    if not pricing_supported:
        clarifications.append(_clarification(
            "pricing_style_unverified",
            f"The backend has no verified fixed-price rule for {kind}/{style}. Its generic fallback is not treated as an approved product-style price rule.",
            ["kind", "style", "price_request.design.product.style"],
            blocking=False,
        ))

    color_approval = approvals.get("diamond_color")
    stone_attributes: dict[str, Any] = {"gem": gem} if gem != "none" else {}
    if gem == "diamond":
        if not isinstance(color_approval, Mapping):
            clarifications.append(_clarification(
                "diamond_color_mapping_required",
                "Map the 3D diamond color to an exact catalog color or colorHex value; no color equivalence is assumed.",
                ["color", "catalog_preferences.stones"],
            ))
        else:
            catalog_field = color_approval.get("catalog_field")
            catalog_value = color_approval.get("catalog_value")
            if catalog_field not in {"color", "colorHex"} or not isinstance(catalog_value, str) or not catalog_value.strip():
                clarifications.append(_clarification(
                    "invalid_color_mapping",
                    "Approved diamond color mapping must name catalog_field='color' or 'colorHex' and provide a non-empty catalog_value.",
                    ["approved_mappings.diamond_color"],
                ))
            else:
                stone_attributes[catalog_field] = catalog_value

    confirmed_stones = selections.get("stones")
    if confirmed_stones is None:
        # Existing caller component requests are explicit input sources. Use one
        # as the candidate selection, then compare all sources before accepting it.
        supplied = inventory_request.get("stones") or price_design.get("stones")
        if isinstance(supplied, list) and supplied:
            confirmed_stones = [
                {key: entry.get(key) for key in ("stoneID", "quantity")}
                for entry in supplied if isinstance(entry, Mapping)
            ]
        else:
            confirmed_stones = []
    if not isinstance(confirmed_stones, list):
        clarifications.append(_clarification("invalid_confirmed_stones", "Confirmed stones must be a list.", ["confirmed_components.stones"]))
        confirmed_stones = []
    if gem == "none" and confirmed_stones:
        conflicts.append({"code": "stones_conflict_with_plain_visual", "message": "A 3D design with gem='none' cannot be combined with confirmed stone components without an explicit non-visual product mapping.", "fields": ["gem", "confirmed_components.stones"]})

    if gem != "none" and not confirmed_stones:
        confirmed_stones = [{}]  # Catalog discovery is allowed; quantity remains unresolved.
    if gem != "none":
        warnings.append({
            "code": "visual_count_is_not_inventory_quantity",
            "message": "The generator's visual stone count is kept separate from explicit catalog/inventory component quantities.",
            "visual_count": effective.get("count"),
        })

    prepared_stones: list[dict[str, Any]] = []
    catalog_stones: list[dict[str, Any]] = []
    for index, entry in enumerate(confirmed_stones):
        if not isinstance(entry, Mapping):
            clarifications.append(_clarification("invalid_confirmed_stone", f"Confirmed stone {index} must be an object.", ["confirmed_components.stones"]))
            continue
        attributes = dict(stone_attributes)
        extra_attributes = entry.get("attributes", {})
        if extra_attributes and not isinstance(extra_attributes, Mapping):
            clarifications.append(_clarification("invalid_stone_attributes", f"Stone {index} attributes must be an object.", ["confirmed_components.stones", "attributes"]))
            continue
        for key, value in dict(extra_attributes or {}).items():
            if key in attributes and attributes[key] != value:
                conflicts.append({"code": "stone_attribute_conflict", "message": f"Confirmed stone {index} attribute {key} conflicts with the 3D gemstone/color mapping.", "fields": ["gem", "color", key]})
            attributes[key] = value
        component_id = entry.get("stoneID")
        if component_id is not None:
            if not isinstance(component_id, str) or not component_id.strip():
                clarifications.append(_clarification("invalid_stone_id", f"Confirmed stone {index} stoneID must be a non-empty string.", ["confirmed_components.stones", "stoneID"]))
            else:
                attributes["stoneID"] = component_id.strip()
        quantity = entry.get("quantity")
        prepared = {"stoneID": component_id.strip() if isinstance(component_id, str) else None, "quantity": quantity, "attributes": attributes}
        prepared_stones.append(prepared)
        catalog_entry = dict(attributes)
        if quantity is not None:
            catalog_entry["quantity"] = quantity
        catalog_stones.append(catalog_entry)

    # Combine 3D-derived catalog constraints with caller constraints without
    # choosing among candidates or dropping extra explicit attributes.
    generated_preferences: dict[str, Any] = {"material": db_metal} if db_metal else {}
    if catalog_stones:
        generated_preferences["stones"] = catalog_stones
    for group, value in existing_preferences.items():
        if group == "material":
            if db_metal and not isinstance(value, str):
                conflicts.append({"code": "invalid_material_preference", "message": "Independent catalog material preference must be a string and is not discarded.", "fields": ["catalog_preferences.material"]})
            elif db_metal and isinstance(value, str) and value.casefold().strip() != db_metal.casefold().strip():
                conflicts.append({"code": "material_preference_conflict", "message": "The independent catalog material preference conflicts with the 3D metal mapping.", "fields": ["metal", "catalog_preferences.material"]})
            elif not db_metal:
                generated_preferences[group] = value
        elif group == "stones" and isinstance(value, list) and value:
            if gem == "none":
                conflicts.append({"code": "stone_preference_conflict", "message": "Catalog stone preferences conflict with a 3D configuration containing no gemstone.", "fields": ["gem", "catalog_preferences.stones"]})
            elif len(value) != len(catalog_stones):
                conflicts.append({"code": "stone_preference_count_conflict", "message": "Independent stone preferences do not align one-to-one with the confirmed 3D component selections.", "fields": ["catalog_preferences.stones", "confirmed_components.stones"]})
            else:
                merged = []
                for index, (base, extra) in enumerate(zip(catalog_stones, value)):
                    if not isinstance(extra, Mapping):
                        conflicts.append({"code": "invalid_independent_stone_preference", "message": f"Independent stone preference {index} must be an object.", "fields": ["catalog_preferences.stones"]})
                        continue
                    combined = dict(base)
                    for key, field_value in extra.items():
                        if key in combined and combined[key] != field_value:
                            conflicts.append({"code": "stone_preference_conflict", "message": f"Independent catalog stone field {key} conflicts with the 3D mapping.", "fields": ["gem", "color", key]})
                        else:
                            combined[key] = field_value
                    if index < len(prepared_stones):
                        selected = prepared_stones[index]
                        if "quantity" in combined:
                            if selected.get("quantity") is not None and selected["quantity"] != combined["quantity"]:
                                conflicts.append({"code": "stone_quantity_conflict", "message": f"Catalog and confirmed quantity differ for stone {index + 1}.", "fields": ["catalog_preferences.stones.quantity", "confirmed_components.stones.quantity"]})
                            selected["quantity"] = combined["quantity"]
                        if combined.get("stoneID"):
                            selected["stoneID"] = combined["stoneID"]
                    merged.append(combined)
                generated_preferences[group] = merged
        elif group == "stones" and value in (None, []):
            # An empty list is a common request default, not a selection that
            # overrides the 3D-derived exact gemstone query.
            continue
        elif group == "stones":
            conflicts.append({"code": "invalid_independent_stones", "message": "catalog_preferences.stones must be a list.", "fields": ["catalog_preferences.stones"]})
        else:
            generated_preferences[group] = value

    for index, selection in enumerate(prepared_stones):
        if not _quantity(selection.get("quantity")) and not any(
            item["code"] == "explicit_stone_quantity_required" and item["fields"] == ["confirmed_components.stones", "quantity"]
            for item in clarifications
        ):
            clarifications.append(_clarification(
                "explicit_stone_quantity_required",
                f"Provide a positive integer inventory quantity for stone {index}; visual count {effective.get('count')!r} is not used as inventory quantity.",
                ["confirmed_components.stones", "quantity"],
            ))

    # Respect explicit IDs from confirmations or independently supplied inputs;
    # different IDs are a conflict, never a precedence rule.
    material_ids = [selections.get("materialID"), inventory_request.get("material_id"), price_design.get("materialID")]
    material_ids = [value for value in material_ids if value is not None]
    if any(not isinstance(value, str) or not value.strip() for value in material_ids):
        clarifications.append(_clarification("invalid_material_id", "Material IDs must be non-empty strings.", ["materialID"]))
    normalized_material_ids = {value.strip() for value in material_ids if isinstance(value, str) and value.strip()}
    if len(normalized_material_ids) > 1:
        conflicts.append({"code": "conflicting_material_ids", "message": "The confirmed, inventory, and pricing material IDs disagree.", "fields": ["confirmed_components.materialID", "inventory_request.material_id", "price_request.design.materialID"]})
    selected_material_id = next(iter(normalized_material_ids), None)

    for field, supplied, expected in (
        ("type", price_design.get("type"), kind),
        ("style", (price_design.get("product") or {}).get("style") if isinstance(price_design.get("product"), Mapping) else None, style),
    ):
        if supplied is not None and supplied != expected:
            conflicts.append({"code": "price_design_conflict", "message": f"Independent price design {field} conflicts with the 3D configuration.", "fields": [field, f"price_request.design.{field}"]})
    if price_design.get("product") is not None and not isinstance(price_design.get("product"), Mapping):
        conflicts.append({"code": "invalid_price_product", "message": "Independent price product details must be an object; they are not discarded.", "fields": ["price_request.design.product"]})
    if price_design.get("metal") is not None and db_metal and str(price_design["metal"]).casefold().strip() != db_metal.casefold().strip():
        conflicts.append({"code": "price_metal_conflict", "message": "Independent pricing metal conflicts with the approved 3D metal label.", "fields": ["metal", "price_request.design.metal"]})

    # Components provided independently are constraints, not values to overwrite.
    confirmed_charms = selections.get("charms", [])
    charms = _component_list(confirmed_charms, "charmID", "confirmed_components.charms", clarifications)
    if charms is None:
        charms = []
    external_stone_lists: list[tuple[str, list[dict[str, Any]]]] = []
    for source, value in (
        ("inventory_request.stones", inventory_request.get("stones")),
        ("price_request.design.stones", price_design.get("stones")),
    ):
        if value:
            parsed = _component_list(value, "stoneID", source, clarifications)
            if parsed is not None:
                external_stone_lists.append((source, parsed))
    if confirmed_stones and selections.get("stones") is not None:
        # Selection IDs may be omitted when exact attributes identify one catalog
        # row; do not require an ID before the catalog has had a chance to resolve it.
        for source, items in external_stone_lists:
            parsed_confirmed = [
                {"stoneID": item.get("stoneID"), "quantity": item.get("quantity")}
                for item in selections.get("stones", []) if isinstance(item, Mapping) and item.get("stoneID")
            ]
            if parsed_confirmed and items and not _same_components(parsed_confirmed, items, "stoneID"):
                conflicts.append({"code": "stone_selection_conflict", "message": f"{source} stone IDs or quantities differ from the explicitly confirmed 3D component selection.", "fields": ["confirmed_components.stones", source]})
    for index in range(1, len(external_stone_lists)):
        if not _same_components(external_stone_lists[0][1], external_stone_lists[index][1], "stoneID"):
            conflicts.append({"code": "inconsistent_stone_quantities", "message": "Inventory and price requests specify different stone IDs or quantities.", "fields": [external_stone_lists[0][0], external_stone_lists[index][0]]})

    external_charm_lists = []
    for source, value in (("inventory_request.charms", inventory_request.get("charms")), ("price_request.design.charms", price_design.get("charms"))):
        if value:
            parsed = _component_list(value, "charmID", source, clarifications)
            if parsed is not None:
                external_charm_lists.append((source, parsed))
    if charms and any(items and not _same_components(charms, items, "charmID") for _, items in external_charm_lists):
        conflicts.append({"code": "charm_selection_conflict", "message": "Independently supplied charm IDs or quantities differ from the confirmed components.", "fields": ["confirmed_components.charms", "inventory_request.charms", "price_request.design.charms"]})
    elif not charms and external_charm_lists:
        charms = external_charm_lists[0][1]
        if any(not _same_components(charms, items, "charmID") for _, items in external_charm_lists[1:]):
            conflicts.append({"code": "inconsistent_charm_quantities", "message": "Inventory and price requests specify different charm IDs or quantities.", "fields": ["inventory_request.charms", "price_request.design.charms"]})

    if conflicts:
        return {
            "status": "conflict", "valid": False, "compatibility": compatibility,
            "requested_configuration": requested_configuration,
            "catalog_result": None, "canonical_design": None, "errors": [],
            "clarifications": clarifications, "conflicts": conflicts, "warnings": warnings,
            "unresolved_mappings": unresolved_mappings,
            "ready_for_inventory": False, "ready_for_price": False,
        }

    # A missing diamond color mapping leaves stone matching unresolved; still
    # resolve the material independently to provide useful clarification data.
    if gem == "diamond" and not (isinstance(color_approval, Mapping) and stone_attributes.get(color_approval.get("catalog_field"))):
        generated_preferences.pop("stones", None)

    if not generated_preferences:
        clarifications.append(_clarification("catalog_preferences_required", "No catalog query could be formed from the supplied 3D request.", ["catalog_preferences"]))
        catalog_result: dict[str, Any] | None = None
    else:
        try:
            catalog_result = dict(catalog_resolver(generated_preferences))
        except Exception as exc:
            catalog_result = {"status": "error", "errors": [str(exc) or type(exc).__name__], "warnings": [], "backend_error": "resolver", "matches": {}, "resolved": None}

    matches = catalog_result.get("matches", {}) if isinstance(catalog_result, Mapping) else {}
    if not isinstance(matches, Mapping):
        matches = {}
    material_match = matches.get("material")
    selected_material = _selected_candidate(
        material_match, selected_material_id, component="material", id_key="materialID",
        clarifications=clarifications, conflicts=conflicts,
    ) if db_metal else None
    material_id = selected_material.get("id") if selected_material else None

    selected_stones: list[dict[str, Any]] = []
    stone_results = matches.get("stones", [])
    if not isinstance(stone_results, list):
        stone_results = []
    if gem != "none" and "stones" in generated_preferences:
        for index, selection in enumerate(prepared_stones):
            match_result = stone_results[index] if index < len(stone_results) else None
            candidate = _selected_candidate(
                match_result, selection.get("stoneID"), component=f"stone {index + 1}", id_key="stoneID",
                clarifications=clarifications, conflicts=conflicts,
            )
            if candidate and _quantity(selection.get("quantity")):
                selected_stones.append({"stoneID": candidate["id"], "quantity": int(selection["quantity"])})

    # Resolve catalog charms against IDs/quantities already provided; never let
    # a catalog preference silently replace an independently selected charm.
    if "charms" in generated_preferences:
        charm_matches = matches.get("charms", [])
        pref_items = generated_preferences.get("charms", [])
        if not isinstance(charm_matches, list) or not isinstance(pref_items, list) or len(charm_matches) != len(pref_items):
            clarifications.append(_clarification("charm_resolution_incomplete", "Catalog charm results do not align with the requested charm components.", ["catalog_preferences.charms"]))
        elif charms and len(charms) != len(charm_matches):
            conflicts.append({"code": "charm_selection_count_conflict", "message": "Confirmed charm components do not align with the catalog charm preferences.", "fields": ["confirmed_components.charms", "catalog_preferences.charms"]})
        else:
            resolved_charm_items = []
            for index, (match, pref) in enumerate(zip(charm_matches, pref_items)):
                pref = pref if isinstance(pref, Mapping) else {}
                selection = charms[index] if index < len(charms) else {}
                candidate = _selected_candidate(
                    match, selection.get("charmID"), component=f"charm {index + 1}", id_key="charmID",
                    clarifications=clarifications, conflicts=conflicts,
                )
                quantity = selection.get("quantity", pref.get("quantity"))
                if selection.get("quantity") is not None and pref.get("quantity") is not None and selection["quantity"] != pref["quantity"]:
                    conflicts.append({"code": "charm_quantity_conflict", "message": f"Confirmed and catalog quantities differ for charm {index + 1}.", "fields": ["confirmed_components.charms.quantity", "catalog_preferences.charms.quantity"]})
                if candidate and _quantity(quantity):
                    resolved_charm_items.append({"charmID": candidate["id"], "quantity": int(quantity)})
                elif candidate:
                    clarifications.append(_clarification("explicit_charm_quantity_required", f"Provide a positive integer quantity for charm {index + 1}.", ["catalog_preferences.charms", "quantity"]))
            if len(resolved_charm_items) == len(charm_matches):
                charms = resolved_charm_items
    normalized_charms = _component_list(charms, "charmID", "canonical charms", clarifications)
    if normalized_charms is not None:
        charms = normalized_charms
    for source, items in external_charm_lists:
        if charms and items and not _same_components(charms, items, "charmID"):
            conflicts.append({"code": "resolved_charm_conflict", "message": f"{source} does not match the canonical charm IDs and quantities.", "fields": [source, "catalog_preferences.charms"]})

    for source, items in external_stone_lists:
        if selected_stones and items and not _same_components(selected_stones, items, "stoneID"):
            conflicts.append({"code": "resolved_stone_conflict", "message": f"{source} does not match the exact catalog-resolved stone IDs and quantities.", "fields": [source, "catalog_preferences.stones"]})
    if conflicts:
        return {
            "status": "conflict", "valid": False, "compatibility": compatibility,
            "requested_configuration": requested_configuration,
            "catalog_result": catalog_result, "canonical_design": None, "errors": [],
            "clarifications": clarifications, "conflicts": conflicts, "warnings": warnings,
            "unresolved_mappings": unresolved_mappings,
            "ready_for_inventory": False, "ready_for_price": False,
        }

    if gem != "none" and not selected_stones:
        if not any(item["code"] == "explicit_stone_quantity_required" for item in clarifications):
            clarifications.append(_clarification(
                "stone_quantity_mapping_required",
                "The 3D stone count does not establish inventory quantity. Confirm a stone ID and positive integer quantity.",
                ["count", "confirmed_components.stones"],
            ))
    if material_match and material_id is None:
        # _selected_candidate already added the ambiguity/not-found clarification.
        pass
    if gem != "none" and selected_stones and len(selected_stones) != len(prepared_stones):
        clarifications.append(_clarification("stone_resolution_incomplete", "Resolve every selected stone before inventory or price checks.", ["confirmed_components.stones"]))
    if not material_id:
        clarifications.append(_clarification("material_resolution_required", "An exact database material record must be resolved before inventory or pricing.", ["metal", "confirmed_components.materialID"]))

    # A ring's US size and bracelet's inner diameter have no verified mapping
    # to the backend's product columns; carry the original values only as 3D data.
    product_canonical = dict(price_design.get("product", {})) if isinstance(price_design.get("product"), Mapping) else {}
    product_canonical["style"] = style
    price_design_canonical: dict[str, Any] = {
        "type": kind,
        "materialID": material_id,
        "product": product_canonical,
        "stones": selected_stones,
        "charms": charms,
    }
    canonical = {
        "three_d_configuration": effective,
        "material": {"materialID": material_id, "metal": db_metal},
        "stones": selected_stones,
        "charms": charms,
        "inventory_request": {"material_id": material_id, "stones": selected_stones, "charms": charms},
        "price_request": {"design": price_design_canonical, "budget": price_request.get("budget")},
        "independent_inputs": {"inventory_request": inventory_request, "price_request": price_request},
    }
    ready_for_inventory = bool(material_id) and (gem == "none" or len(selected_stones) == len(prepared_stones)) and not conflicts
    ready_for_price = ready_for_inventory and pricing_supported
    # Any blocking clarification (including quantity or color) prevents checks
    # that would otherwise silently price/validate an incomplete component set.
    blocking = any(item["blocking"] for item in clarifications)
    ready_for_inventory = ready_for_inventory and not blocking
    ready_for_price = ready_for_price and not blocking

    catalog_resolved = bool(material_id) and (gem == "none" or len(selected_stones) == len(prepared_stones))
    if "charms" in generated_preferences and len(charms) != len(generated_preferences.get("charms", [])):
        catalog_resolved = False

    return {
        "status": "resolved" if not clarifications and ready_for_inventory and ready_for_price else "clarification_required",
        "valid": not clarifications and ready_for_inventory and ready_for_price,
        "requested_configuration": requested_configuration,
        "compatibility": compatibility,
        "catalog_preferences": generated_preferences,
        "catalog_result": catalog_result,
        "canonical_design": canonical,
        "errors": [],
        "clarifications": clarifications,
        "conflicts": conflicts,
        "warnings": warnings,
        "unresolved_mappings": unresolved_mappings,
        "ready_for_inventory": ready_for_inventory,
        "ready_for_price": ready_for_price,
        "selected_candidates": {"materialID": material_id, "stones": selected_stones},
        "catalog_resolved": catalog_resolved,
    }

"""Inventory checks based on read-only Node.js backend snapshots."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .backend_client import (
    BackendConnectionError,
    BackendHTTPError,
    BackendInventoryClient,
    BackendInventoryError,
)


def _number_for_result(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _aggregate_requested(
    entries: Sequence[Mapping[str, Any]] | None,
    *,
    id_key: str,
    label: str,
) -> tuple[dict[str, float], list[dict[str, Any]], list[str]]:
    totals: dict[str, float] = {}
    invalid: list[dict[str, Any]] = []
    errors: list[str] = []

    if entries is None:
        return totals, invalid, errors
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
        return totals, [{"id": None, "requested_quantity": None}], [
            f"{label.capitalize()} selection must be a list."
        ]

    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            message = f"{label.capitalize()} entry {index} must be an object."
            errors.append(message)
            invalid.append({"id": None, "requested_quantity": None, "error": message})
            continue

        component_id = entry.get(id_key)
        if not isinstance(component_id, str) or not component_id.strip():
            message = f"{label.capitalize()} entry {index} is missing a valid {id_key}."
            errors.append(message)
            invalid.append({"id": None, "requested_quantity": None, "error": message})
            continue
        component_id = component_id.strip()

        raw_quantity = entry.get("quantity")
        try:
            quantity = 1.0 if raw_quantity is None else float(raw_quantity)
        except (TypeError, ValueError, OverflowError):
            quantity = math.nan

        if isinstance(raw_quantity, bool) or not math.isfinite(quantity) or quantity <= 0:
            message = f"{label.capitalize()} {component_id} has an invalid quantity."
            errors.append(message)
            invalid.append({"id": component_id, "requested_quantity": None, "error": message})
            continue

        totals[component_id] = totals.get(component_id, 0.0) + quantity

    return totals, invalid, errors


def validate_inventory(
    material_id: str | None,
    stones: Sequence[Mapping[str, Any]] | None = None,
    charms: Sequence[Mapping[str, Any]] | None = None,
    *,
    client: BackendInventoryClient | None = None,
) -> dict[str, Any]:
    """Validate requested inventory without creating or reserving an accessory.

    Component entries use the existing accessory creation field names:
    ``stoneID``/``charmID`` and optional ``quantity``. A missing or null
    quantity defaults to one, matching the backend creation path.
    """
    stone_totals, invalid_stones, stone_errors = _aggregate_requested(
        stones, id_key="stoneID", label="stone"
    )
    charm_totals, invalid_charms, charm_errors = _aggregate_requested(
        charms, id_key="charmID", label="charm"
    )
    errors = [*stone_errors, *charm_errors]
    warnings = [
        "Material stock quantity is not tracked by the database; material availability is existence-checked only."
    ]
    components: dict[str, Any] = {
        "material": {
            "id": material_id,
            "exists": None,
            "requested_quantity": None,
            "available_quantity": None,
            "quantity_checked": False,
        },
        "stones": [],
        "charms": [],
    }

    if not isinstance(material_id, str) or not material_id.strip():
        components["material"]["exists"] = False
        errors.append("Material ID is missing or invalid.")
    else:
        material_id = material_id.strip()
        components["material"]["id"] = material_id

    try:
        inventory_client = client or BackendInventoryClient()
        materials = inventory_client.get_materials()
        stone_rows = inventory_client.get_stones()
        charm_rows = inventory_client.get_charms()
    except BackendInventoryError as exc:
        errors.append(f"Inventory could not be checked: {exc}")
        components["stones"] = [
            _unresolved_component(component_id, quantity)
            for component_id, quantity in stone_totals.items()
        ] + [_unresolved_component(item["id"], item["requested_quantity"], item.get("error")) for item in invalid_stones]
        components["charms"] = [
            _unresolved_component(component_id, quantity)
            for component_id, quantity in charm_totals.items()
        ] + [_unresolved_component(item["id"], item["requested_quantity"], item.get("error")) for item in invalid_charms]
        if isinstance(exc, BackendConnectionError):
            backend_error = "connection"
        elif isinstance(exc, BackendHTTPError):
            backend_error = "http"
        else:
            backend_error = "response"
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "components": components,
            "backend_error": backend_error,
        }

    material_ids = {row["materialID"] for row in materials}
    if isinstance(material_id, str) and material_id.strip():
        exists = material_id.strip() in material_ids
        components["material"]["exists"] = exists
        if not exists:
            errors.append(f"Material {material_id.strip()} does not exist.")

    components["stones"] = _validate_component_rows(
        label="Stone",
        id_key="stoneID",
        requested=stone_totals,
        inventory_rows=stone_rows,
        invalid_entries=invalid_stones,
        errors=errors,
    )
    components["charms"] = _validate_component_rows(
        label="Charm",
        id_key="charmID",
        requested=charm_totals,
        inventory_rows=charm_rows,
        invalid_entries=invalid_charms,
        errors=errors,
    )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "components": components,
    }


def _validate_component_rows(
    *,
    label: str,
    id_key: str,
    requested: dict[str, float],
    inventory_rows: list[dict[str, Any]],
    invalid_entries: list[dict[str, Any]],
    errors: list[str],
) -> list[dict[str, Any]]:
    inventory = {row[id_key]: row["qty"] for row in inventory_rows}
    results = []

    for component_id, requested_quantity in requested.items():
        exists = component_id in inventory
        available_quantity = inventory.get(component_id)
        enough = exists and requested_quantity <= available_quantity
        result = {
            "id": component_id,
            "exists": exists,
            "requested_quantity": _number_for_result(requested_quantity),
            "available_quantity": available_quantity,
            "quantity_sufficient": enough if exists else False,
        }
        results.append(result)
        if not exists:
            errors.append(f"{label} {component_id} does not exist.")
        elif not enough:
            errors.append(
                f"{label} {component_id} has insufficient stock: "
                f"requested {result['requested_quantity']}, available {available_quantity}."
            )

    for invalid in invalid_entries:
        component_id = invalid["id"]
        exists = component_id in inventory if component_id else None
        results.append({
            "id": component_id,
            "exists": exists,
            "requested_quantity": None,
            "available_quantity": inventory.get(component_id) if component_id else None,
            "quantity_sufficient": False,
            "error": invalid.get("error"),
        })
    return results


def _unresolved_component(
    component_id: str | None,
    requested_quantity: int | float | None,
    error: str | None = None,
) -> dict[str, Any]:
    result = {
        "id": component_id,
        "exists": None,
        "requested_quantity": requested_quantity,
        "available_quantity": None,
        "quantity_sufficient": None,
    }
    if error:
        result["error"] = error
    return result

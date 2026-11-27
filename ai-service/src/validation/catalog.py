"""Semantic catalog matching against records owned by the Node backend.

The matcher uses database field names for requested attributes. It does not
resolve stock quantities, calculate prices, or infer design/3D capabilities.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .backend_client import (
    BackendConnectionError,
    BackendHTTPError,
    BackendInventoryClient,
    BackendInventoryError,
    BackendResponseError,
)


_STONE_STRING_FIELDS = frozenset({
    "stoneID",
    "certificationID",
    "cut",
    "gem",
    "purity",
    "rarity",
    "color",
    "colorHex",
})
_STONE_NUMERIC_FIELDS = frozenset({"weight"})
_CHARM_FIELDS = frozenset({"design", "color"})
_CATALOG_WARNINGS = [
    "Catalog matches do not guarantee stock availability; use Inventory Validator for quantity checks.",
    "Catalog matches do not establish compatibility or 3D generator support.",
]


def _normalize_text(value: str) -> str:
    """Case-fold text and collapse surrounding/repeated whitespace only."""
    return " ".join(value.split()).casefold()


def _same_value(actual: Any, requested: Any, *, numeric: bool = False) -> bool:
    if numeric:
        if isinstance(actual, bool) or isinstance(requested, bool):
            return False
        try:
            actual_number = Decimal(str(actual))
            requested_number = Decimal(str(requested))
        except (InvalidOperation, TypeError, ValueError):
            return False
        return actual_number.is_finite() and requested_number.is_finite() and actual_number == requested_number

    return (
        isinstance(actual, str)
        and isinstance(requested, str)
        and _normalize_text(actual) == _normalize_text(requested)
    )


def _error_result(
    requested: Any,
    message: str,
    *,
    backend_error: str | None = None,
) -> dict[str, Any]:
    result = {
        "status": "error",
        "requested_attributes": requested,
        "candidates": [],
        "alternatives": [],
        "errors": [message],
        "warnings": [],
    }
    if backend_error:
        result["backend_error"] = backend_error
    return result


def _read_records(
    *,
    client: BackendInventoryClient,
    getter_name: str,
    id_key: str,
    required_keys: set[str],
    quantity_key: str | None = None,
    requested: Any,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    try:
        records = getattr(client, getter_name)()
    except BackendInventoryError as exc:
        if isinstance(exc, BackendConnectionError):
            kind = "connection"
        elif isinstance(exc, BackendHTTPError):
            kind = "http"
        else:
            kind = "response"
        return None, _error_result(
            requested,
            f"Catalog could not be checked: {exc}",
            backend_error=kind,
        )

    if not isinstance(records, list):
        malformed = BackendResponseError("Catalog endpoint response must be a JSON array.")
        return None, _error_result(
            requested,
            f"Catalog could not be checked: {malformed}",
            backend_error="response",
        )

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            malformed = BackendResponseError(
                f"Catalog record {index} must be an object."
            )
            return None, _error_result(
                requested,
                f"Catalog could not be checked: {malformed}",
                backend_error="response",
            )
        if not required_keys.issubset(record):
            missing = sorted(required_keys.difference(record))
            malformed = BackendResponseError(
                f"Catalog record {index} is missing fields: {', '.join(missing)}."
            )
            return None, _error_result(
                requested,
                f"Catalog could not be checked: {malformed}",
                backend_error="response",
            )
        if not isinstance(record.get(id_key), str) or not record[id_key].strip():
            malformed = BackendResponseError(
                f"Catalog record {index} has no valid {id_key}."
            )
            return None, _error_result(
                requested,
                f"Catalog could not be checked: {malformed}",
                backend_error="response",
            )
        if quantity_key:
            quantity = record.get(quantity_key)
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, (int, float))
                or not math.isfinite(quantity)
                or quantity < 0
            ):
                malformed = BackendResponseError(
                    f"Catalog record {index} has an invalid {quantity_key}."
                )
                return None, _error_result(
                    requested,
                    f"Catalog could not be checked: {malformed}",
                    backend_error="response",
                )
    return records, None


def _candidate(record: dict[str, Any], id_key: str, quantity_key: str | None) -> dict[str, Any]:
    candidate = {"id": record[id_key], "metadata": dict(record)}
    if quantity_key:
        candidate["available_quantity"] = record[quantity_key]
    return candidate


def _compare_attributes(
    record: Mapping[str, Any],
    requested: Mapping[str, Any],
    *,
    numeric_fields: frozenset[str] = frozenset(),
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    matched: list[str] = []
    differences: dict[str, dict[str, Any]] = {}
    for field, requested_value in requested.items():
        available_value = record.get(field)
        if _same_value(
            available_value,
            requested_value,
            numeric=field in numeric_fields,
        ):
            matched.append(field)
        else:
            differences[field] = {
                "requested": requested_value,
                "available": available_value,
            }
    return matched, differences


def _resolve_records(
    *,
    records: list[dict[str, Any]],
    requested: Mapping[str, Any],
    id_key: str,
    quantity_key: str | None,
    numeric_fields: frozenset[str] = frozenset(),
    offer_all_alternatives: bool = False,
) -> dict[str, Any]:
    exact = [
        record
        for record in records
        if all(
            _same_value(
                record.get(field),
                value,
                numeric=field in numeric_fields,
            )
            for field, value in requested.items()
        )
    ]

    warnings = list(_CATALOG_WARNINGS)
    if len(exact) == 1:
        return {
            "status": "matched",
            "requested_attributes": dict(requested),
            "candidates": [_candidate(exact[0], id_key, quantity_key)],
            "alternatives": [],
            "errors": [],
            "warnings": warnings,
        }
    if len(exact) > 1:
        warnings.append(
            "Multiple exact catalog records match; select a candidate explicitly. No record was selected."
        )
        return {
            "status": "ambiguous",
            "requested_attributes": dict(requested),
            "candidates": [
                _candidate(record, id_key, quantity_key) for record in exact
            ],
            "alternatives": [],
            "errors": [],
            "warnings": warnings,
        }

    comparisons = [
        (
            record,
            *_compare_attributes(record, requested, numeric_fields=numeric_fields),
        )
        for record in records
    ]
    best_overlap = max((len(matched) for _, matched, _ in comparisons), default=0)
    if offer_all_alternatives and records:
        alternatives = comparisons
    elif best_overlap:
        alternatives = [
            item for item in comparisons if len(item[1]) == best_overlap
        ]
    else:
        alternatives = []

    alternative_rows = [
        {
            **_candidate(record, id_key, quantity_key),
            "matched_attributes": matched,
            "differences": differences,
            "label": "alternative; requires explicit confirmation",
        }
        for record, matched, differences in alternatives
    ]
    if alternative_rows:
        warnings.append(
            "No exact catalog match exists. Alternatives differ from the request and require explicit confirmation."
        )
    else:
        warnings.append("No exact catalog match or close alternative was found.")

    return {
        "status": "not_found",
        "requested_attributes": dict(requested),
        "candidates": [],
        "alternatives": alternative_rows,
        "errors": ["No exact catalog match was found."],
        "warnings": warnings,
    }


def match_material(
    metal: str,
    *,
    client: BackendInventoryClient | None = None,
) -> dict[str, Any]:
    """Match an exact database metal label, without choosing among duplicates."""
    requested = {"metal": metal}
    if not isinstance(metal, str) or not metal.strip():
        return _error_result(requested, "Metal label must be a non-empty string.")

    backend_client = client or BackendInventoryClient()
    records, error = _read_records(
        client=backend_client,
        getter_name="get_materials",
        id_key="materialID",
        required_keys={"materialID", "metal", "price"},
        requested=requested,
    )
    if error:
        return error
    return _resolve_records(
        records=records or [],
        requested=requested,
        id_key="materialID",
        quantity_key=None,
        offer_all_alternatives=True,
    )


def match_stone(
    attributes: Mapping[str, Any],
    *,
    client: BackendInventoryClient | None = None,
) -> dict[str, Any]:
    """Match explicitly requested stone database attributes exactly.

    Supported keys are stoneID, certificationID, cut, gem, purity, rarity,
    color, colorHex, and weight. ``qty`` remains inventory data, not a match
    criterion.
    """
    if not isinstance(attributes, Mapping) or not attributes:
        return _error_result(attributes, "Stone attributes must be a non-empty object.")
    requested = dict(attributes)
    supported_fields = _STONE_STRING_FIELDS | _STONE_NUMERIC_FIELDS
    unknown = sorted(set(requested).difference(supported_fields))
    if unknown:
        return _error_result(
            requested,
            f"Unsupported stone catalog attributes: {', '.join(unknown)}.",
        )
    for field, value in requested.items():
        if field in _STONE_NUMERIC_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return _error_result(requested, f"Stone attribute {field} must be a finite number.")
        elif not isinstance(value, str) or not value.strip():
            return _error_result(requested, f"Stone attribute {field} must be a non-empty string.")

    backend_client = client or BackendInventoryClient()
    stone_fields = _STONE_STRING_FIELDS | _STONE_NUMERIC_FIELDS
    records, error = _read_records(
        client=backend_client,
        getter_name="get_stones",
        id_key="stoneID",
        required_keys={"stoneID", "qty", *stone_fields},
        quantity_key="qty",
        requested=requested,
    )
    if error:
        return error
    return _resolve_records(
        records=records or [],
        requested=requested,
        id_key="stoneID",
        quantity_key="qty",
        numeric_fields=_STONE_NUMERIC_FIELDS,
    )


def match_charm(
    attributes: Mapping[str, Any],
    *,
    client: BackendInventoryClient | None = None,
) -> dict[str, Any]:
    """Match charms using the database's design and color attributes."""
    if not isinstance(attributes, Mapping) or not attributes:
        return _error_result(attributes, "Charm attributes must be a non-empty object.")
    requested = dict(attributes)
    unknown = sorted(set(requested).difference(_CHARM_FIELDS))
    if unknown:
        return _error_result(
            requested,
            f"Unsupported charm catalog attributes: {', '.join(unknown)}.",
        )
    for field, value in requested.items():
        if not isinstance(value, str) or not value.strip():
            return _error_result(requested, f"Charm attribute {field} must be a non-empty string.")

    backend_client = client or BackendInventoryClient()
    records, error = _read_records(
        client=backend_client,
        getter_name="get_charms",
        id_key="charmID",
        required_keys={"charmID", "qty", "price", "design", "color", "photoURL"},
        quantity_key="qty",
        requested=requested,
    )
    if error:
        return error
    return _resolve_records(
        records=records or [],
        requested=requested,
        id_key="charmID",
        quantity_key="qty",
    )


class _CachedCatalogClient:
    """Cache each existing collection once during a multi-item resolution."""

    def __init__(self, client: BackendInventoryClient):
        self._client = client
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def _get(self, name: str) -> list[dict[str, Any]]:
        if name not in self._cache:
            self._cache[name] = getattr(self._client, name)()
        return self._cache[name]

    def get_materials(self) -> list[dict[str, Any]]:
        return self._get("get_materials")

    def get_stones(self) -> list[dict[str, Any]]:
        return self._get("get_stones")

    def get_charms(self) -> list[dict[str, Any]]:
        return self._get("get_charms")


def resolve_catalog_preferences(
    preferences: Mapping[str, Any],
    *,
    client: BackendInventoryClient | None = None,
) -> dict[str, Any]:
    """Resolve the minimal Phase 2 catalog-preference request.

    Accepted keys are ``material`` (a metal label), ``stones`` and ``charms``.
    Each stone/charm entry contains supported database attribute names and an
    optional ``quantity``. Quantity is copied to the resolved item unchanged;
    stock sufficiency remains the Inventory Validator's responsibility.
    """
    if not isinstance(preferences, Mapping) or not preferences:
        return _error_result(
            preferences,
            "catalog_preferences must be a non-empty object.",
        ) | {"request_error": True}

    supported_keys = {"material", "stones", "charms"}
    unknown = sorted(set(preferences).difference(supported_keys))
    if unknown:
        return _error_result(
            dict(preferences),
            f"Unsupported catalog preference groups: {', '.join(unknown)}.",
        ) | {"request_error": True}

    requested = dict(preferences)
    backend = _CachedCatalogClient(client or BackendInventoryClient())
    matches: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    request_error = False
    has_technical_error = False

    def collect(component: str, result: dict[str, Any], index: int | None = None) -> None:
        nonlocal request_error, has_technical_error
        context: dict[str, Any] = {"component": component}
        if index is not None:
            context["index"] = index
        for message in result.get("errors", []):
            errors.append({**context, "message": message})
        for message in result.get("warnings", []):
            warnings.append({**context, "message": message})
        if result.get("backend_error"):
            has_technical_error = True
        elif result.get("status") == "error":
            request_error = True

    if "material" in requested:
        material_result = match_material(requested["material"], client=backend)
        matches["material"] = material_result
        collect("material", material_result)

    for component, matcher in (("stones", match_stone), ("charms", match_charm)):
        if component not in requested:
            continue
        entries = requested[component]
        if not isinstance(entries, list):
            message = f"Catalog {component} preferences must be a list."
            matches[component] = [{
                "status": "error",
                "requested_attributes": entries,
                "requested_quantity": None,
                "candidates": [],
                "alternatives": [],
                "errors": [message],
                "warnings": [],
            }]
            collect(component, matches[component][0], 0)
            request_error = True
            continue

        component_matches = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                message = f"Catalog {component} preference {index} must be an object."
                item_result = {
                    "status": "error",
                    "requested_attributes": entry,
                    "requested_quantity": None,
                    "candidates": [],
                    "alternatives": [],
                    "errors": [message],
                    "warnings": [],
                }
            else:
                attributes = {key: value for key, value in entry.items() if key != "quantity"}
                item_result = matcher(attributes, client=backend)
                item_result["requested_quantity"] = entry.get("quantity")
            component_matches.append(item_result)
            collect(component, item_result, index)
        matches[component] = component_matches

    statuses = [
        item.get("status")
        for name, value in matches.items()
        for item in (value if name in {"stones", "charms"} else [value])
    ]
    if has_technical_error:
        status = "error"
    elif request_error:
        status = "error"
    elif statuses and all(item_status == "matched" for item_status in statuses):
        status = "matched"
    elif "ambiguous" in statuses:
        status = "ambiguous"
    elif "not_found" in statuses:
        status = "not_found"
    else:
        status = "error"
        request_error = True
        errors.append({
            "component": "catalog_preferences",
            "message": "Provide at least one material, stone, or charm preference.",
        })

    resolved = None
    if status == "matched":
        resolved = {}
        if "material" in matches:
            resolved["materialID"] = matches["material"]["candidates"][0]["id"]
        for component, id_key in (("stones", "stoneID"), ("charms", "charmID")):
            if component not in matches:
                continue
            resolved[component] = []
            for index, item in enumerate(matches[component]):
                resolved_item = {id_key: item["candidates"][0]["id"]}
                requested_item = requested[component][index]
                if isinstance(requested_item, Mapping) and "quantity" in requested_item:
                    resolved_item["quantity"] = item.get("requested_quantity")
                resolved[component].append(resolved_item)

    result = {
        "status": status,
        "requested_preferences": requested,
        "matches": matches,
        "resolved": resolved,
        "errors": errors,
        "warnings": warnings,
    }
    if has_technical_error:
        result["backend_error"] = next(
            (
                item.get("backend_error")
                for value in matches.values()
                for item in (value if isinstance(value, list) else [value])
                if isinstance(item, dict) and item.get("backend_error")
            ),
            "response",
        )
    if request_error:
        result["request_error"] = True
    return result

"""Budget validation using prices calculated by the Node.js backend."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .backend_client import (
    BackendConnectionError,
    BackendHTTPError,
    BackendInventoryClient,
    BackendInventoryError,
)


_DESIGN_FIELDS = ("type", "materialID", "metal", "product", "charms", "stones")


def validate_price(
    design: Mapping[str, Any],
    budget: int | float,
    *,
    client: BackendInventoryClient | None = None,
) -> dict[str, Any]:
    """Compare a customer's budget with Node's authoritative preview price."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        numeric_budget = float(budget)
    except (TypeError, ValueError, OverflowError):
        numeric_budget = math.nan

    if isinstance(budget, bool) or not math.isfinite(numeric_budget) or numeric_budget < 0:
        return {
            "valid": False,
            "computed_price": None,
            "budget": budget if isinstance(budget, (int, float)) and not isinstance(budget, bool) else None,
            "difference": None,
            "errors": ["Budget must be a finite, non-negative number."],
            "warnings": warnings,
            "breakdown": None,
        }

    if not isinstance(design, Mapping):
        return {
            "valid": False,
            "computed_price": None,
            "budget": numeric_budget,
            "difference": None,
            "errors": ["Design must be an object."],
            "warnings": warnings,
            "breakdown": None,
        }

    if not isinstance(design.get("type"), str) or not design["type"].strip():
        errors.append("Design type is required.")
    if not design.get("materialID") and not design.get("metal"):
        errors.append("Provide materialID or metal.")
    for field in ("charms", "stones"):
        if design.get(field) is not None and not isinstance(design[field], list):
            errors.append(f"{field} must be a list.")
    if errors:
        return {
            "valid": False,
            "computed_price": None,
            "budget": numeric_budget,
            "difference": None,
            "errors": errors,
            "warnings": warnings,
            "breakdown": None,
        }

    # Deliberately whitelist the existing Node preview fields; budget stays in Python.
    preview_design = {key: design[key] for key in _DESIGN_FIELDS if key in design}
    try:
        preview = (client or BackendInventoryClient()).preview_accessory_price(
            preview_design
        )
    except BackendHTTPError as exc:
        if exc.status_code == 400:
            errors.append(exc.detail or "The backend rejected the design for pricing.")
            backend_error = "design"
        else:
            errors.append(f"Price could not be checked: {exc}")
            backend_error = "http"
        return {
            "valid": False,
            "computed_price": None,
            "budget": numeric_budget,
            "difference": None,
            "errors": errors,
            "warnings": warnings,
            "breakdown": None,
            "backend_error": backend_error,
        }
    except BackendInventoryError as exc:
        errors.append(f"Price could not be checked: {exc}")
        if isinstance(exc, BackendConnectionError):
            backend_error = "connection"
        else:
            backend_error = "response"
        return {
            "valid": False,
            "computed_price": None,
            "budget": numeric_budget,
            "difference": None,
            "errors": errors,
            "warnings": warnings,
            "breakdown": None,
            "backend_error": backend_error,
        }

    computed_price = float(preview["computedPrice"])
    difference = numeric_budget - computed_price
    if difference < 0:
        errors.append(f"Design exceeds the budget by {abs(difference):.15g}.")

    return {
        "valid": difference >= 0,
        "computed_price": computed_price,
        "budget": numeric_budget,
        "difference": difference,
        "errors": errors,
        "warnings": warnings,
        "breakdown": preview["breakdown"],
    }

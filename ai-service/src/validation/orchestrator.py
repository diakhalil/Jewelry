"""Coordinate the existing inventory and price validation components."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .inventory import validate_inventory
from .pricing import validate_price
from .catalog import resolve_catalog_preferences
from .compatibility import validate_3d_configuration
from .adapter import adapt_three_d_configuration


Validator = Callable[..., Mapping[str, Any]]
CatalogResolver = Callable[..., Mapping[str, Any]]
CompatibilityValidator = Callable[[Any], Mapping[str, Any]]


def _run_check(
    name: str,
    validator: Validator,
    kwargs: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]], list[Any], list[Any]]:
    """Run one validator and collect its result and categorized messages."""
    errors: list[Any] = []
    warnings: list[Any] = []
    technical_errors: list[dict[str, str]] = []

    try:
        raw_result = validator(**kwargs)
    except Exception as exc:  # A validator failure must not hide other check results.
        technical_errors.append({"check": name, "message": str(exc) or type(exc).__name__})
        return (
            {"status": "error", "result": None},
            technical_errors,
            errors,
            warnings,
        )

    if not isinstance(raw_result, Mapping):
        technical_errors.append({
            "check": name,
            "message": "Validator returned a malformed result; expected an object.",
        })
        return (
            {"status": "error", "result": raw_result},
            technical_errors,
            errors,
            warnings,
        )

    result = dict(raw_result)
    result_errors = result.get("errors", [])
    result_warnings = result.get("warnings", [])
    if not isinstance(result.get("valid"), bool):
        technical_errors.append({
            "check": name,
            "message": "Validator returned a malformed result without a boolean valid field.",
        })
    if not isinstance(result_errors, list) or not isinstance(result_warnings, list):
        technical_errors.append({
            "check": name,
            "message": "Validator returned malformed errors or warnings; expected lists.",
        })
    else:
        errors.extend({"check": name, "message": item} for item in result_errors)
        warnings.extend({"check": name, "message": item} for item in result_warnings)

    backend_error = result.get("backend_error")
    if backend_error:
        # Node 400s from the price validator are design validation failures;
        # other backend errors mean the check could not be completed reliably.
        if not (name == "price" and backend_error == "design"):
            technical_errors.append({
                "check": name,
                "message": next(
                    (
                        str(item)
                        for item in (result_errors if isinstance(result_errors, list) else [])
                        if isinstance(item, str) and item
                    ),
                    f"Validator reported backend error: {backend_error}",
                ),
            })

    if any(error["check"] == name for error in technical_errors):
        check_status = "error"
    elif result.get("valid"):
        check_status = "passed"
    else:
        check_status = "failed"

    return (
        {"status": check_status, "result": result},
        technical_errors,
        errors,
        warnings,
    )


def orchestrate_validation(
    *,
    inventory_request: Mapping[str, Any],
    price_request: Mapping[str, Any],
    catalog_preferences: Mapping[str, Any] | None = None,
    three_d_configuration: Any | None = None,
    inventory_validator: Validator = validate_inventory,
    price_validator: Validator = validate_price,
    catalog_resolver: CatalogResolver = resolve_catalog_preferences,
    compatibility_validator: CompatibilityValidator = validate_3d_configuration,
    three_d_field_provenance: Mapping[str, str] | None = None,
    three_d_confirmed_components: Mapping[str, Any] | None = None,
    three_d_approved_mappings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run inventory and price checks and return their combined validation.

    ``inventory_request`` uses the current ``validate_inventory`` inputs:
    ``material_id``, ``stones``, and ``charms``. ``price_request`` uses the
    current ``validate_price`` inputs: ``design`` and ``budget``. Keeping
    these established input shapes separate avoids assuming a future agent
    schema or the mapping between a proposed design and inventory fields.
    """
    inventory_input = dict(inventory_request)
    price_input = dict(price_request)
    if three_d_configuration is not None:
        adapted = adapt_three_d_configuration(
            three_d_configuration,
            field_provenance=three_d_field_provenance,
            confirmed_components=three_d_confirmed_components,
            approved_mappings=three_d_approved_mappings,
            inventory_request=inventory_input,
            price_request=price_input,
            catalog_preferences=catalog_preferences,
            catalog_resolver=catalog_resolver,
            compatibility_validator=compatibility_validator,
        )
        compatibility_result = adapted.get("compatibility", {})
        compatibility_status = "passed" if compatibility_result.get("valid") is True else "failed"
        checks: dict[str, Any] = {
            "compatibility": {"status": compatibility_status, "result": compatibility_result},
            "adapter": {
                "status": (
                    "conflict" if adapted.get("conflicts")
                    else "clarification_required" if adapted.get("clarifications") or adapted.get("unresolved_mappings")
                    else "passed"
                ),
                "result": adapted,
            },
        }
        catalog_result = adapted.get("catalog_result")
        if isinstance(catalog_result, Mapping):
            if catalog_result.get("backend_error"):
                catalog_status = "error"
            elif adapted.get("catalog_resolved"):
                catalog_status = "passed"
            elif catalog_result.get("status") in {"ambiguous", "not_found"}:
                catalog_status = "clarification_required"
            else:
                catalog_status = "passed" if catalog_result.get("status") == "matched" else "failed"
            checks["catalog"] = {"status": catalog_status, "result": dict(catalog_result)}
        else:
            checks["catalog"] = {"status": "skipped", "result": None}

        inventory_check: dict[str, Any] = {"status": "skipped", "result": None}
        price_check: dict[str, Any] = {"status": "skipped", "result": None}
        inventory_technical: list[dict[str, str]] = []
        inventory_errors: list[Any] = []
        inventory_warnings: list[Any] = []
        price_technical: list[dict[str, str]] = []
        price_errors: list[Any] = []
        price_warnings: list[Any] = []
        canonical = adapted.get("canonical_design")
        if compatibility_status == "passed" and adapted.get("catalog_resolved") and canonical:
            if adapted.get("ready_for_inventory"):
                inventory_check, inventory_technical, inventory_errors, inventory_warnings = _run_check(
                    "inventory", inventory_validator, canonical["inventory_request"]
                )
            if adapted.get("ready_for_price"):
                price_call = canonical["price_request"]
                price_check, price_technical, price_errors, price_warnings = _run_check(
                    "price", price_validator,
                    {"design": price_call["design"], "budget": price_call.get("budget")},
                )
        elif compatibility_status != "passed":
            checks["adapter"] = {"status": "skipped", "result": adapted}

        checks.update({"inventory": inventory_check, "price": price_check})
        clarifications = adapted.get("clarifications", [])
        conflicts = adapted.get("conflicts", [])
        adapter_errors = adapted.get("errors", [])
        compatibility_errors = compatibility_result.get("errors", [])
        errors = [
            *({"check": "compatibility", **item} for item in compatibility_errors),
            *({"check": "adapter", **item} for item in adapter_errors),
            *({"check": "adapter", **item} for item in conflicts),
            *inventory_errors,
            *price_errors,
        ]
        warnings = [
            *({"check": "adapter", **item} for item in adapted.get("warnings", [])),
            *inventory_warnings,
            *price_warnings,
        ]
        technical_errors = [*inventory_technical, *price_technical]
        if isinstance(catalog_result, Mapping) and catalog_result.get("backend_error"):
            technical_errors.append({
                "check": "catalog",
                "message": next((str(item) for item in catalog_result.get("errors", []) if item), "Catalog resolution failed."),
            })
        checks_valid = all(
            checks[name]["status"] == "passed"
            for name in ("compatibility", "catalog", "inventory", "price")
        )
        complete = (
            checks_valid and not clarifications and not conflicts and not adapted.get("unresolved_mappings")
        )
        if technical_errors:
            status = "error"
        elif conflicts:
            status = "conflict"
        elif any(check["status"] == "failed" for check in checks.values()):
            status = "failed"
        elif clarifications or adapted.get("unresolved_mappings"):
            status = "clarification_required"
        elif not checks_valid:
            status = "failed"
        else:
            status = "passed"
        return {
            "valid": complete,
            "status": status,
            "checks": checks,
            "checks_valid": checks_valid,
            "complete_validation": complete,
            "canonical_design": canonical,
            "clarification_required": bool(clarifications or conflicts or adapted.get("unresolved_mappings")),
            "clarifications": clarifications,
            "conflicts": conflicts,
            "unresolved_mappings": adapted.get("unresolved_mappings", []),
            "errors": errors,
            "warnings": warnings,
            "technical_errors": technical_errors,
        }

    catalog_check: dict[str, Any] | None = None
    catalog_errors: list[Any] = []
    catalog_warnings: list[Any] = []
    catalog_technical: list[dict[str, str]] = []

    if catalog_preferences is not None:
        try:
            raw_catalog_result = catalog_resolver(catalog_preferences)
        except Exception as exc:
            raw_catalog_result = {
                "status": "error",
                "requested_preferences": catalog_preferences,
                "matches": {},
                "resolved": None,
                "errors": [str(exc) or type(exc).__name__],
                "warnings": [],
                "backend_error": "resolver",
            }

        if not isinstance(raw_catalog_result, Mapping):
            catalog_result = {
                "status": "error",
                "requested_preferences": catalog_preferences,
                "matches": {},
                "resolved": None,
                "errors": ["Catalog resolver returned a malformed result."],
                "warnings": [],
                "backend_error": "response",
            }
        else:
            catalog_result = dict(raw_catalog_result)

        for item in catalog_result.get("errors", []):
            catalog_errors.append(
                {"check": "catalog", **item}
                if isinstance(item, Mapping)
                else {"check": "catalog", "message": item}
            )
        for item in catalog_result.get("warnings", []):
            catalog_warnings.append(
                {"check": "catalog", **item}
                if isinstance(item, Mapping)
                else {"check": "catalog", "message": item}
            )

        backend_error = catalog_result.get("backend_error")
        if backend_error:
            catalog_technical.append({
                "check": "catalog",
                "message": next(
                    (
                        str(item.get("message"))
                        for item in catalog_errors
                        if isinstance(item, Mapping) and item.get("message")
                    ),
                    f"Catalog resolution failed: {backend_error}",
                ),
            })

        catalog_status = catalog_result.get("status")
        resolved = catalog_result.get("resolved")
        if catalog_status == "matched" and isinstance(resolved, Mapping) and not backend_error:
            # Only categories actually resolved from preferences are copied.
            # The same ID and original quantity are sent to both validators.
            design_value = price_input.get("design")
            design = dict(design_value) if isinstance(design_value, Mapping) else {}
            if "materialID" in resolved:
                inventory_input["material_id"] = resolved["materialID"]
                design["materialID"] = resolved["materialID"]
                design.pop("metal", None)
            for component, id_key in (("stones", "stoneID"), ("charms", "charmID")):
                if component in resolved:
                    shared_components = [dict(item) for item in resolved[component]]
                    inventory_input[component] = [dict(item) for item in shared_components]
                    design[component] = [dict(item) for item in shared_components]
            if "design" in price_input or "materialID" in resolved or "stones" in resolved or "charms" in resolved:
                price_input["design"] = design
            catalog_check = {"status": "passed", "result": catalog_result}
        else:
            technical = bool(catalog_technical)
            clarification_required = not technical
            check_status = "error" if technical else "clarification_required"
            catalog_check = {"status": check_status, "result": catalog_result}
            checks = {
                "catalog": catalog_check,
                "inventory": {"status": "skipped", "result": None},
                "price": {"status": "skipped", "result": None},
            }
            result = {
                "valid": False,
                "status": "error" if technical else "clarification_required",
                "checks": checks,
                "errors": catalog_errors,
                "warnings": catalog_warnings,
                "technical_errors": catalog_technical,
                "clarification_required": clarification_required,
                "revision_required": clarification_required,
            }
            return result

    inventory_args = {
        "material_id": inventory_input.get("material_id"),
        "stones": inventory_input.get("stones"),
        "charms": inventory_input.get("charms"),
    }
    price_args = {
        "design": price_input.get("design"),
        "budget": price_input.get("budget"),
    }

    inventory_check, inventory_technical, inventory_errors, inventory_warnings = _run_check(
        "inventory", inventory_validator, inventory_args
    )
    price_check, price_technical, price_errors, price_warnings = _run_check(
        "price", price_validator, price_args
    )

    checks = {}
    if catalog_check:
        checks["catalog"] = catalog_check
    checks.update({"inventory": inventory_check, "price": price_check})
    technical_errors = [*catalog_technical, *inventory_technical, *price_technical]
    errors = [*catalog_errors, *inventory_errors, *price_errors]
    warnings = [*catalog_warnings, *inventory_warnings, *price_warnings]
    all_passed = all(check["status"] == "passed" for check in checks.values())
    status = "error" if technical_errors else "passed" if all_passed else "failed"

    result = {
        "valid": all_passed and not technical_errors,
        "status": status,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "technical_errors": technical_errors,
    }
    if catalog_preferences is not None:
        result["clarification_required"] = False
        result["revision_required"] = False
    return result

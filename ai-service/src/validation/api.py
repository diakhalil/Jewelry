"""FastAPI routes for Validation Agent operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .inventory import validate_inventory
from .catalog import resolve_catalog_preferences
from .orchestrator import orchestrate_validation
from .pricing import validate_price


router = APIRouter()


class InventoryValidationRequest(BaseModel):
    """Inventory fields already used by accessory creation."""

    material_id: str | None = None
    stones: list[dict[str, Any]] | None = Field(default_factory=list)
    charms: list[dict[str, Any]] | None = Field(default_factory=list)


class PriceValidationRequest(BaseModel):
    """Pricing fields accepted by the existing Node accessory preview."""

    type: str | None = None
    materialID: str | None = None
    metal: str | None = None
    product: Any = None
    charms: list[Any] | None = Field(default_factory=list)
    stones: list[Any] | None = Field(default_factory=list)
    budget: float


class ValidationRunRequest(BaseModel):
    """The established inputs for inventory and price validation."""

    inventory_request: dict[str, Any]
    price_request: dict[str, Any]
    catalog_preferences: dict[str, Any] | None = None


class CatalogValidationRequest(BaseModel):
    """Phase 2 catalog preferences using current database attribute names."""

    catalog_preferences: dict[str, Any]


@router.post("/validation/inventory")
def inventory_validation(request: InventoryValidationRequest):
    result = validate_inventory(
        request.material_id,
        stones=request.stones,
        charms=request.charms,
    )

    backend_error = result.get("backend_error")
    if backend_error:
        status_code = 503 if backend_error == "connection" else 502
        return JSONResponse(status_code=status_code, content=result)

    return result


@router.post("/validation/price")
def price_validation(request: PriceValidationRequest):
    design = {
        "type": request.type,
        "materialID": request.materialID,
        "metal": request.metal,
        "product": request.product,
        "charms": request.charms,
        "stones": request.stones,
    }
    result = validate_price(design, request.budget)

    backend_error = result.get("backend_error")
    if backend_error == "connection":
        return JSONResponse(status_code=503, content=result)
    if backend_error in {"http", "response"}:
        return JSONResponse(status_code=502, content=result)
    if backend_error == "design" or result["computed_price"] is None:
        return JSONResponse(status_code=422, content=result)
    return result


@router.post("/validation/catalog")
def catalog_validation(request: CatalogValidationRequest):
    result = resolve_catalog_preferences(request.catalog_preferences)

    backend_error = result.get("backend_error")
    if backend_error == "connection":
        return JSONResponse(status_code=503, content=result)
    if backend_error in {"http", "response"}:
        return JSONResponse(status_code=502, content=result)
    if backend_error:
        return JSONResponse(status_code=500, content=result)
    if result.get("request_error"):
        return JSONResponse(status_code=422, content=result)
    return result


@router.post("/validation/run")
def run_validation(request: ValidationRunRequest):
    if request.catalog_preferences is None:
        result = orchestrate_validation(
            inventory_request=request.inventory_request,
            price_request=request.price_request,
        )
    else:
        result = orchestrate_validation(
            inventory_request=request.inventory_request,
            price_request=request.price_request,
            catalog_preferences=request.catalog_preferences,
        )

    # Mirror the existing component routes: client/design errors use 422,
    # backend connection failures use 503, and backend HTTP/response failures
    # use 502. Other orchestration failures remain server errors (500).
    price_result = result.get("checks", {}).get("price", {}).get("result")
    catalog_result = result.get("checks", {}).get("catalog", {}).get("result")
    if isinstance(catalog_result, dict) and catalog_result.get("request_error"):
        return JSONResponse(status_code=422, content=result)
    if isinstance(price_result, dict) and price_result.get("backend_error") == "design":
        return JSONResponse(status_code=422, content=result)

    for check in result.get("checks", {}).values():
        check_result = check.get("result") if isinstance(check, dict) else None
        if not isinstance(check_result, dict):
            continue
        backend_error = check_result.get("backend_error")
        if backend_error == "connection":
            return JSONResponse(status_code=503, content=result)
        if backend_error in {"http", "response"}:
            return JSONResponse(status_code=502, content=result)

    if result.get("status") == "error":
        return JSONResponse(status_code=500, content=result)
    return result

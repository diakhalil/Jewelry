"""Read-only tools the Customer Requirements Agent may request."""

import os
from typing import Literal

import httpx
from langchain_core.tools import tool

from .brief import OPTIONS, Brief, Preference, inconsistencies, missing_fields


@tool
def check_required_fields(preferences: list[Preference]) -> dict:
    """Check a proposed brief for missing design inputs. Include previously saved choices."""
    brief = Brief(preferences=preferences)
    return {"missing": missing_fields(brief), "optional": ["budget", "occasion"]}


@tool
def check_preference_consistency(preferences: list[Preference]) -> dict:
    """Check the current candidate choices for incompatible budgets, units, exclusions and configurator options."""
    return {"issues": inconsistencies(Brief(preferences=preferences))}


@tool
def lookup_supported_options(
    jewelry_type: str = "",
    catalog: Literal["configurator", "materials", "stones", "charms"] = "configurator",
) -> dict:
    """Look up SheCraft's configurator choices or the real material/stone catalog. Catalog presence alone does not confirm compatibility or stock."""
    kind = jewelry_type.strip().lower()
    if kind and kind not in OPTIONS:
        return {"supported_types": list(OPTIONS), "requested_type_supported": False}
    if catalog == "configurator":
        return OPTIONS[kind] if kind else {"supported_types": list(OPTIONS)}
    url = os.getenv("SHECRAFT_BACKEND_URL", "http://localhost:5000").rstrip("/")
    try:
        response = httpx.get(f"{url}/api/{catalog}", timeout=5)
        response.raise_for_status()
        column = {"materials": "metal", "stones": "gem", "charms": "design"}[catalog]
        names = sorted({str(row[column]) for row in response.json() if row.get(column)})
    except (httpx.HTTPError, ValueError, KeyError, AttributeError, TypeError):
        return {"available": False, "message": "The live catalog could not be checked. Leave availability unconfirmed."}
    result = {"catalog": names, "catalog_type": catalog, "configurator": OPTIONS.get(kind), "available": True}
    if catalog == "charms":
        result["note"] = "Rows labeled 'shapes' are identified by photos. Individual motifs cannot be verified from this label."
    return result


REQUIREMENTS_TOOLS = [check_required_fields, check_preference_consistency, lookup_supported_options]

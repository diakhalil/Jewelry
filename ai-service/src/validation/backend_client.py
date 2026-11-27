"""HTTP client for inventory data owned by the Node.js backend."""

from __future__ import annotations

import math
import os
from typing import Any
from urllib.parse import urljoin

import requests


class BackendInventoryError(RuntimeError):
    """Base error for failures retrieving inventory from the Node backend."""


class BackendConnectionError(BackendInventoryError):
    """The backend could not be reached."""


class BackendHTTPError(BackendInventoryError):
    """The backend returned an unsuccessful HTTP status."""

    def __init__(self, message: str, *, status_code: int, detail: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class BackendResponseError(BackendInventoryError):
    """The backend response was not in the expected format."""


class BackendInventoryClient:
    """Call read-only inventory and price-preview APIs on the Node backend."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 5.0,
        session: requests.Session | None = None,
    ) -> None:
        configured_url = base_url or os.getenv(
            "SHECRAFT_BACKEND_URL", "http://localhost:5000"
        )
        self.base_url = configured_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = session or requests.Session()

    def get_materials(self) -> list[dict[str, Any]]:
        return self._get_collection("api/materials", "materialID")

    def get_stones(self) -> list[dict[str, Any]]:
        return self._get_collection("api/stones", "stoneID", quantity_key="qty")

    def get_charms(self) -> list[dict[str, Any]]:
        return self._get_collection("api/charms", "charmID", quantity_key="qty")

    def preview_accessory_price(self, design: dict[str, Any]) -> dict[str, Any]:
        """Request the authoritative accessory price without creating an instance."""
        url = urljoin(self.base_url, "api/accessory-instance/preview")
        try:
            response = self.session.post(url, json=design, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BackendConnectionError(
                f"Could not reach the Node.js backend at {url}: {exc}"
            ) from exc

        if not 200 <= response.status_code < 300:
            detail = None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = payload.get("message") or payload.get("error")
            except ValueError:
                pass
            raise BackendHTTPError(
                f"Node.js backend returned HTTP {response.status_code} for {url}.",
                status_code=response.status_code,
                detail=detail,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendResponseError(
                f"Node.js backend returned invalid JSON for {url}."
            ) from exc

        if not isinstance(payload, dict):
            raise BackendResponseError(
                "Node.js price preview response must be a JSON object."
            )
        price = payload.get("computedPrice")
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
        ):
            raise BackendResponseError(
                "Node.js price preview response has no valid computedPrice."
            )
        if "breakdown" not in payload or not isinstance(payload["breakdown"], dict):
            raise BackendResponseError(
                "Node.js price preview response has no valid breakdown."
            )
        return payload

    def _get_collection(
        self,
        endpoint: str,
        id_key: str,
        *,
        quantity_key: str | None = None,
    ) -> list[dict[str, Any]]:
        url = urljoin(self.base_url, endpoint)
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BackendConnectionError(
                f"Could not reach the Node.js backend at {url}: {exc}"
            ) from exc

        if not 200 <= response.status_code < 300:
            raise BackendHTTPError(
                f"Node.js backend returned HTTP {response.status_code} for {url}.",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendResponseError(
                f"Node.js backend returned invalid JSON for {url}."
            ) from exc

        if not isinstance(payload, list):
            raise BackendResponseError(
                f"Node.js backend response for {url} must be a JSON array."
            )

        seen_ids: set[str] = set()
        for index, record in enumerate(payload):
            if not isinstance(record, dict):
                raise BackendResponseError(
                    f"Record {index} from {url} must be a JSON object."
                )
            component_id = record.get(id_key)
            if not isinstance(component_id, str) or not component_id.strip():
                raise BackendResponseError(
                    f"Record {index} from {url} has no valid {id_key}."
                )
            if component_id in seen_ids:
                raise BackendResponseError(
                    f"Node.js backend returned duplicate {id_key} {component_id!r}."
                )
            seen_ids.add(component_id)

            if quantity_key:
                quantity = record.get(quantity_key)
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, (int, float))
                    or not math.isfinite(quantity)
                    or quantity < 0
                ):
                    raise BackendResponseError(
                        f"Record {index} from {url} has an invalid {quantity_key}."
                    )

        return payload

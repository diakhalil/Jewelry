"""Available month, color and cut combinations from the storefront's stone catalog."""

import os

import httpx


def load_birthstones() -> list[tuple[str, str, str]] | None:
    url = os.getenv("SHECRAFT_BACKEND_URL", "http://localhost:5000").rstrip("/")
    try:
        response = httpx.get(f"{url}/api/stones", timeout=5)
        response.raise_for_status()
        return [(row["gem"].strip(), row["color"].strip(), row["cut"].strip().title())
                for row in response.json() if str(row.get("stoneID", "")).startswith("B")
                and row.get("colorHex") and row.get("gem") and row.get("color") and row.get("cut")]
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None


def available_values(rows, field, month="", color=""):
    column = {"birth_month": 0, "stone_color": 1, "cut": 2}[field]
    return list(dict.fromkeys(row[column] for row in rows
                              if (column < 1 or not month or row[0].casefold() == month.casefold())
                              and (column < 2 or not color or row[1].casefold() == color.casefold())))

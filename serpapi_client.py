from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import urlopen


class SerpApiError(RuntimeError):
    """Raised when SerpApi cannot return a usable product result."""


@dataclass(frozen=True)
class ProductReference:
    retailer: str
    product_id: str | None
    query: str


def parse_price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    matches = re.findall(r"(?:\$|USD\s*)(\d{1,5}(?:,\d{3})?(?:\.\d{2})?)", str(value), re.IGNORECASE)
    values = [float(match.replace(",", "")) for match in matches]
    return min(values) if values else None


def product_price(value: Any) -> float | None:
    """Find a current offer price without mistaking a previous price for it."""
    if not isinstance(value, dict):
        return parse_price(value)
    for key in ("price", "extracted_price", "current_price", "sale_price", "final_price"):
        candidate = value.get(key)
        price = product_price(candidate) if isinstance(candidate, dict) else parse_price(candidate)
        if price is not None:
            return price
    for key in ("amount", "value"):
        price = parse_price(value.get(key))
        if price is not None:
            return price
    for key, nested in value.items():
        if key in {"price_was", "original_price", "list_price", "compare_at_price"}:
            continue
        if isinstance(nested, (dict, list)):
            price = product_price(nested) if isinstance(nested, dict) else next(
                (product_price(item) for item in nested if isinstance(item, dict)), None
            )
            if price is not None:
                return price
    return None


def product_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("product_result", "product_results", "product", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    products = payload.get("products")
    if isinstance(products, list):
        candidates.extend(item for item in products if isinstance(item, dict))
    return candidates


def retailer_for(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host.endswith("walmart.com"):
        return "walmart"
    if host.endswith("homedepot.com"):
        return "home_depot"
    raise SerpApiError("Only Walmart and Home Depot URLs are supported at this time.")


def product_reference(url: str) -> ProductReference:
    retailer = retailer_for(url)
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query_values = parse_qs(parsed.query)
    product_id = None
    if retailer == "walmart":
        if "ip" in parts:
            index = parts.index("ip")
            if len(parts) > index + 2:
                product_id = parts[index + 2]
        product_id = product_id or query_values.get("product_id", [None])[0]
        name_parts = parts[parts.index("ip") + 1:index + 2] if "ip" in parts else parts
    else:
        if "p" in parts:
            index = parts.index("p")
            if len(parts) > index + 2 and re.fullmatch(r"\d+", parts[index + 2]):
                product_id = parts[index + 2]
                name_parts = parts[index + 1:index + 2]
            else:
                name_parts = parts[index + 1:]
        else:
            name_parts = parts
        product_id = product_id or query_values.get("product_id", [None])[0]
    if name_parts and re.fullmatch(r"\d+", name_parts[-1]):
        name_parts.pop()
    query = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", " ".join(name_parts))).strip()
    return ProductReference(retailer, product_id, query)


class SerpApiClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("SERPAPI_API_KEY")
        self.timeout_seconds = max(30, int(os.environ.get("SERPAPI_TIMEOUT_SECONDS", "90")))
        if not self.api_key:
            raise SerpApiError("SERPAPI_API_KEY is not configured.")

    def request(self, engine: str, **parameters: str) -> dict[str, Any]:
        query = {"engine": engine, "api_key": self.api_key, **parameters}
        endpoint = "https://serpapi.com/search.json?" + "&".join(
            f"{quote_plus(str(key))}={quote_plus(str(value))}" for key, value in query.items()
        )
        try:
            with urlopen(endpoint, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as error:
            raise SerpApiError(
                f"SerpApi {engine} request timed out after {self.timeout_seconds} seconds. "
                "The request may still appear in your SerpApi usage dashboard."
            ) from error
        except Exception as error:
            raise SerpApiError(f"SerpApi {engine} request failed: {error}") from error
        if payload.get("error"):
            raise SerpApiError(f"SerpApi error: {payload['error']}")
        return payload

    def product(self, url: str) -> dict[str, object]:
        reference = product_reference(url)
        if reference.retailer == "walmart":
            return self.walmart_product(reference)
        return self.home_depot_product(reference)

    def walmart_product(self, reference: ProductReference) -> dict[str, object]:
        if not reference.product_id:
            raise SerpApiError("The Walmart URL does not contain a product ID.")
        payload = self.request("walmart_product", product_id=reference.product_id)
        result = payload.get("product_result") or {}
        price = product_price(result)
        if price is None:
            raise SerpApiError("Walmart returned the product but no price.")
        return {"title": result.get("title") or reference.query, "price": price, "currency": result.get("currency", "USD")}

    def home_depot_product(self, reference: ProductReference) -> dict[str, object]:
        product_id = reference.product_id
        if not product_id:
            search = self.request("home_depot", q=reference.query, country="us")
            for result in search.get("products", []):
                if isinstance(result, dict) and result.get("product_id"):
                    product_id = str(result["product_id"])
                    break
        if not product_id:
            raise SerpApiError("Home Depot search did not return a product ID.")
        payload = self.request("home_depot_product", product_id=product_id, country="us")
        candidates = product_candidates(payload)
        result = next((candidate for candidate in candidates if product_price(candidate) is not None), None)
        if result is None:
            keys = ", ".join(sorted(payload.keys())) or "no response fields"
            raise SerpApiError(f"Home Depot returned the product but no price. Response fields: {keys}.")
        price = product_price(result)
        return {"title": result.get("title") or reference.query, "price": price, "currency": "USD"}

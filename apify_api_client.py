from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

"""
Apify API Currently Implemented:
- Lowes
Planned:


Last Update: 10/9/2026
Written on: 10/9/2026
Written by: AJ Utz
"""

LOWES_ACTOR = "maplerope44~lowes-product-lookup"
ZIP_PATTERN = re.compile(r"\d{5}")


class ApifyError(RuntimeError):
    pass


def lowes_product_id(url: str) -> str:
    """Lowes URLs end with the numeric product ID: /pd/<name>/<id>."""
    parsed = urlparse(url)
    if not parsed.netloc.lower().removeprefix("www.").endswith("lowes.com"):
        raise ApifyError("The Apify provider currently only supports Lowes URLs.")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    for part in reversed(parts):
        if part.isdigit():
            return part
    raise ApifyError("The Lowes URL does not contain a product ID.")


def cents_to_dollars(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(value / 100, 2)


class ApifyClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("APIFY_API_TOKEN")
        # The actor can take a while when its cache is cold; Apify's sync endpoint caps at 300s.
        self.timeout_seconds = min(300, max(120, int(os.environ.get("APIFY_TIMEOUT_SECONDS", "180"))))
        self.response_history: list[dict[str, Any]] = []
        if not self.api_key:
            raise ApifyError("APIFY_API_TOKEN is not configured.")

    def run_actor(self, actor: str, actor_input: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        request = Request(
            endpoint,
            data=json.dumps(actor_input).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        #Error Net
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as error:
            raise ApifyError(f"Apify request timed out after {self.timeout_seconds} seconds.") from error
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace").strip()
            detail = f" Response: {body[:300]}" if body else ""
            raise ApifyError(f"Apify request failed: HTTP {error.code} {error.reason}.{detail}") from error
        except URLError as error:
            raise ApifyError(f"Apify request failed: {error.reason}") from error
        except Exception as error:
            raise ApifyError(f"Apify request failed: {error}") from error
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict):
            raise ApifyError("Apify returned no results for this product.")
        self.response_history.append(item)
        return item

    def product(self, url: str, store_id: str | None = None, zip_state: str | None = None) -> dict[str, object]:
        product_id = lowes_product_id(url)
        zip_code = (zip_state or "").strip()

        if not ZIP_PATTERN.fullmatch(zip_code):
            zip_code = os.environ.get("APIFY_LOWES_ZIP", "").strip()

        if not ZIP_PATTERN.fullmatch(zip_code):
            raise ApifyError("Apify's Lowes lookup needs a 5-digit ZIP code. Set APIFY_LOWES_ZIP or save the store with a ZIP.")
        
        payload = self.run_actor(LOWES_ACTOR, {"zip": zip_code, "productId": product_id})
        status = payload.get("status")

        if status in (202, 206):
            raise ApifyError("Apify's lookup is still processing; try again shortly.")
        
        if isinstance(status, int) and status >= 400:
            raise ApifyError(f"Apify's Lowes lookup returned status {status}.")
        
        stores = payload.get("stores")

        if not isinstance(stores, dict) or not stores:
            raise ApifyError("Apify returned no stores for this product.")
        
        if store_id:
            store = stores.get(str(store_id))
            if not isinstance(store, dict):
                raise ApifyError(f"Lowes store {store_id} was not returned near ZIP {zip_code}.")
        else:
            candidates = [value for value in stores.values() if isinstance(value, dict)]
            store = min(candidates, key=lambda item: item.get("distance") if isinstance(item.get("distance"), (int, float)) else float("inf"))

        products = store.get("products")
        item = products.get(product_id) if isinstance(products, dict) else None

        if not isinstance(item, dict) and isinstance(products, dict) and products:
            item = next(iter(products.values()))
        if not isinstance(item, dict):
            raise ApifyError("Apify returned the store but no product data.")
        
        regular = cents_to_dollars(item.get("priceCentsPerUnit"))
        sale = cents_to_dollars(item.get("salePriceCentsPerUnit"))
        price = min(value for value in (regular, sale) if value is not None) if (regular or sale) else None
        bulk_price = cents_to_dollars(item.get("bulkPriceCentsPerUnit"))
        bulk_quantity = item.get("bulkQuantityRequired")
        has_bulk = bulk_price is not None and isinstance(bulk_quantity, int) and bulk_quantity > 1

        # Lowes hides the real price (and stock) when blocking; the actor then returns the bulk price in the regular price field.
        if item.get("quantityAvailable") == 0 and sale is None and not has_bulk and regular is not None:
            return {
                "title": item.get("name") or item.get("description"),
                "price": None,
                "bulk_price": regular,
                "currency": "USD",
                **self._store_fields(store),
            }
        if price is None and not has_bulk:
            raise ApifyError("Lowes returned the product but no price.")
        product: dict[str, object] = {
            "title": item.get("name") or item.get("description"),
            "price": price,
            "currency": "USD",
        }
        if has_bulk and (price is None or bulk_price < price):
            product["bulk_price"], product["bulk_quantity"] = bulk_price, bulk_quantity
        product.update(self._store_fields(store))
        return product

    @staticmethod
    def _store_fields(store: dict[str, Any]) -> dict[str, object]:
        fields: dict[str, object] = {}
        if store.get("name"):
            fields["store_name"] = store["name"]
        location = ", ".join(str(part) for part in (store.get("city"), store.get("state"), store.get("zip")) if part)
        if location:
            fields["store_location"] = location
        return fields

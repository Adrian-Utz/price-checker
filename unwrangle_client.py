from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import urlopen

"""
Unwrangle API Currently Implemented:
- Walmart
- Home Depot
- Lowes
- Ace Hardware
- Sams Club
Planned:
- Amazon

Use Unwrangle to fetch product information, and keep it in a list.
Unwrangle currently does not have a monthly free version, only a free trial of 100 credits.
Credits used per API call (Standard|Enterprise):
Lowes: 10 | 2
Walmart: 2.5 | 2
Sams Club: 10
Ace Hardware: 10 | 2
Home Depot: 2.5 | 2
Amazon: 10

Written by: AJ Utz
Written on: 8/26/2026
Last Update: 9/10/2026
"""

class UnwrangleError(RuntimeError):
    """Raised when Unwrangle cannot return a usable product result."""

@dataclass(frozen=True)
class ProductReference:
    retailer: str
    product_id: str | None
    query: str


def parse_price(value: Any) -> float | None:
    #Check if the value is an int or a float
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        prices = [parse_price(item) for item in value]
        prices = [price for price in prices if price is not None]
        return min(prices) if prices else None
    plain_number = re.fullmatch(r"\s*\d{1,5}(?:,\d{3})*(?:\.\d+)?\s*", str(value))
    if plain_number:
        return float(plain_number.group(0).replace(",", ""))
    #Use regular expressions t find all price pattens in the string value
    text = str(value)
    matches = re.findall(r"(?:\$|USD\s*)(\d{1,5}(?:,\d{3})?(?:\.\d{2})?)", text, re.IGNORECASE)
    matches += re.findall(r"(\d{1,5}(?:,\d{3})?(?:\.\d{2})?)\s*USD\b", text, re.IGNORECASE)
    #convert matched strings to floats, replace commas with dots
    values = [float(match.replace(",", "")) for match in matches]
    return min(values) if values else None


def product_price(value: Any) -> float | None:
    """Find a current offer price without mistaking a previous price for it."""
    if not isinstance(value, dict): #Check if the input value is a dictionary
        #parse the price from the input value if it's not a dictionary
        return parse_price(value) 

    #Iterate over potential price keys and recursively search for the price
    for key in ("price", "extracted_price", "current_price", "sale_price", "final_price"):
        candidate = value.get(key) #Get the candidat price from the dictionary
        # Recursively find the price in nested objects and lists.
        price = product_price(candidate)
        if price is not None:
            return price

    #Iterate ove potentail amount/value keys and recursively search for the price
    for key in ("amount", "value"):
        price = product_price(value.get(key))
        if price is not None:
            return price

    # Iterate ove potential nested keys
    for key, nested in value.items():
        #skip certian keys
        if key in {"price_was", "original_price", "list_price", "compare_at_price"}:
            continue
        #if the nested value is a dictionary or list recursivley search fro the price
        if isinstance(nested, (dict, list)):
            price = product_price(nested)
            if price is not None: #if valid price is found
                return price

    #Last resort: some retailers omit a live "price" when an item has no current offer, fall back to a list/original price
    for key in ("list_price", "listing_price", "original_price", "price_was"):
        price = product_price(value.get(key))
        if price is not None:
            return price
    return None


def product_bulk_price(value: Any, regular_price: float | None = None) -> tuple[float, int] | None:
    """Find a bulk price and the minimum quantity needed to receive it."""
    if not isinstance(value, dict):
        return None
    for quantity_key in ("bulk_price_threshold", "bulk_price_quantity"):
        if "bulk_price" in value and quantity_key in value:
            price = product_price(value["bulk_price"])
            quantity = parse_quantity(value[quantity_key])
            if price is not None and quantity is not None and quantity > 1 and (regular_price is None or price < regular_price):
                return price, quantity
    price = None
    quantity = None
    for key, candidate in value.items():
        normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if isinstance(candidate, str):
            quantity = quantity or parse_quantity(candidate)
        if ("bulk" in normalized_key or "volume" in normalized_key) and ("price" in normalized_key or "cost" in normalized_key):
            price = product_price(candidate)
            quantity = quantity or parse_quantity(candidate)
        elif any(token in normalized_key for token in ("quantity", "qty", "minimum", "min")):
            quantity = parse_quantity(candidate)
    if price is not None and quantity is not None and quantity > 1 and (regular_price is None or price < regular_price):
        return price, quantity
    for nested in value.values():
        if isinstance(nested, dict):
            bulk = product_bulk_price(nested, regular_price)
            if bulk:
                return bulk
        elif isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    bulk = product_bulk_price(item, regular_price)
                    if bulk:
                        return bulk
    return None


def parse_quantity(value: Any) -> int | None:
    if isinstance(value, (int, float)) and int(value) == value:
        return int(value)
    text = str(value)
    for pattern in (r"\b(?:buy|for)\s+(\d+)", r"\b(?:qty|quantity)\s*:?\s*(\d+)", r"\bmin(?:imum)?(?:\s+order)?\s+quantity\s*:?\s*(\d+)"):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None

def retailer_for(url: str) -> str:
    """Parse the URL and extract the Host part. If it matches return the respective parameter."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host.endswith("walmart.com"):
        return "walmart"
    if host.endswith("homedepot.com"):
        return "home_depot"
    if host.endswith("lowes.com"):
        return "lowes"
    if host.endswith("acehardware.com"):
        return "ace_hardware"
    if host.endswith("samsclub.com"):
        return "sams_club"
    raise UnwrangleError("Only Walmart, Home Depot, Lowes, Ace Hardware, and Sams' Club URLs are supported at this time.")


def product_reference(url: str) -> ProductReference:
    retailer = retailer_for(url) #Determine retailer based on the URl
    parsed = urlparse(url) # Parse the URL and extrace path and query components
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query_values = parse_qs(parsed.query)
    product_id = None #Initilize product ID
    #Determine product ID and name parts based on the retailer
    if retailer == "walmart":
        if "ip" in parts: # Check for specific parts in the URL path
            index = parts.index("ip")
            if len(parts) > index + 2:
                product_id = parts[index + 2]
        product_id = product_id or query_values.get("product_id", [None])[0] or query_values.get("item_id", [None])[0]
        name_parts = parts[parts.index("ip") + 1:index + 2] if "ip" in parts else parts
    elif retailer == "ace_hardware":
        # https://www.acehardware.com/p/<sku_id> or /departments/.../<sku_id>
        if parts and re.fullmatch(r"\d+", parts[-1]):
            product_id = parts[-1]
        name_parts = parts[:-1] if product_id else parts
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
    #Remove any numeric suffix from the last part of name_parts
    if name_parts and re.fullmatch(r"\d+", name_parts[-1]):
        name_parts.pop()
    #Clean and normalize the query string
    query = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", " ".join(name_parts))).strip()
    # Return a ProductReference object with the determined retailer, product ID, and query
    return ProductReference(retailer, product_id, query)


class UnwrangleClient:
    def __init__(self, api_key: str | None = None) -> None:
        """
        Initilize the API key from environment variables or default.
        Set timout for requests.
        Check if the API key is configured
        """
        self.api_key = api_key or os.environ.get("UNWRANGLE_API_KEY")
        self.timeout_seconds = max(30, int(os.environ.get("UNWRANGLE_TIMEOUT_SECONDS", "90")))
        self.response_history: list[dict[str, Any]] = []
        if not self.api_key:
            raise UnwrangleError("UNWRANGLE_API_KEY is not configured.")

    def request(self, platform: str, **parameters: str) -> dict[str, Any]:
        query = {"platform": platform, "api_key": self.api_key, **parameters} # Create a query dictionary with platform and API key, merging with other parameters
        #Construct the endpoint URL with query paramters
        endpoint = "https://data.unwrangle.com/api/getter/?" + "&".join(
            f"{quote_plus(str(key))}={quote_plus(str(value))}" for key, value in query.items() if value is not None
        )
        attempts = 0
        while True:
            attempts += 1
            try:
                #Open the endpoint and read the responce with the specified timeout
                with urlopen(endpoint, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8")) #Decode the responce and parse it as JSON
                break
            except TimeoutError as error: #Timeout error
                raise UnwrangleError(
                    f"Unwrangle {platform} request timed out after {self.timeout_seconds} seconds. "
                    "The request may still appear in your Unwrangle usage dashboard."
                ) from error
            except HTTPError as error: #Unwrangle returned a non-2xx status; surface its body since it usually explains why
                body = error.read().decode("utf-8", errors="replace").strip()
                if error.code in (502, 503, 504) and attempts < 2: #Retry once on transient gateway errors before giving up
                    continue
                detail = f" Response: {body[:300]}" if body else ""
                raise UnwrangleError(f"Unwrangle {platform} request failed: HTTP {error.code} {error.reason}.{detail}") from error
            except URLError as error: #Network-level failure (DNS, connection refused, etc.)
                raise UnwrangleError(f"Unwrangle {platform} request failed: {error.reason}") from error
            except Exception as error: #Any other unexpected failure, including malformed JSON
                raise UnwrangleError(f"Unwrangle {platform} request failed: {error}") from error
            self.response_history.append(payload)
        #Check for errors in the responce payload
        if payload.get("error") or payload.get("success") is False:
            raise UnwrangleError(f"Unwrangle error: {payload.get('error') or payload.get('message') or 'unknown error'}")
        return payload

    def product(self, url: str, store_id: str | None = None) -> dict[str, object]:
        reference = product_reference(url) #Determine the retailer
        if reference.retailer == "walmart":
            return self.walmart_detail(reference)
        elif reference.retailer == "home_depot":
            return self.home_depot_detail(reference, url, store_id)
        elif reference.retailer == "lowes":
            return self.lowes_detail(reference, url, store_id)
        elif reference.retailer == "ace_hardware":
            return self.ace_hardware_detail(reference, url, store_id)
        elif reference.retailer == "sams_club":
            return self.sams_club_detail(reference, url) # Unwrangle doesn't have a store identifier for Sams Club
        raise UnwrangleError(f"Unsupported retailer: {reference.retailer}")

    @staticmethod
    def _detail_and_price(payload: dict[str, Any]) -> tuple[dict[str, Any], float | None, tuple[float, int] | None]:
        """Unwrangle nests product fields under "detail"; fall back to the top-level payload if that's missing or empty."""
        detail = payload.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        price = product_price(detail)
        if price is None:
            price = product_price(payload)
        if price is None:
            price = product_price(detail.get("list_price"))
        if price is None:
            price = product_price(payload.get("list_price"))
        return detail, price, product_bulk_price(detail, price) or product_bulk_price(payload, price)

    def walmart_detail(self, reference: ProductReference) -> dict[str, object]:
        if not reference.product_id: #Error if no item ID is available
            raise UnwrangleError("The Walmart URL does not contain an item ID.")
        payload = self.request("walmart_detail", item_id=reference.product_id) #Send the Request
        detail, price, bulk = self._detail_and_price(payload)
        if price is None: #If price is not found
            keys = ", ".join(sorted(detail.keys())) or ", ".join(sorted(payload.keys())) or "no fields"
            raise UnwrangleError(f"Walmart returned the product but no price. Fields: {keys}.")
        product = {
            "title": detail.get("name") or reference.query,
            "price": price,
            "currency": detail.get("currency", "USD"),
        }
        if bulk:
            product["bulk_price"], product["bulk_quantity"] = bulk
        return product

    def home_depot_detail(self, reference: ProductReference, url: str, store_no: str | None = None) -> dict[str, object]:
        parameters: dict[str, str] = {"url": url}
        if store_no:
            parameters["store_no"] = store_no
        payload = self.request("homedepot_detail", **parameters) #Send the Request
        detail, price, bulk = self._detail_and_price(payload)
        if price is None: #If price is not found
            keys = ", ".join(sorted(detail.keys())) or ", ".join(sorted(payload.keys())) or "no fields"
            raise UnwrangleError(f"Home Depot returned the product but no price. Fields: {keys}.")
        product = {
            "title": detail.get("name") or reference.query,
            "price": price,
            "currency": detail.get("currency", "USD"),
        }
        if bulk:
            product["bulk_price"], product["bulk_quantity"] = bulk
        store_name = detail.get("store_name")
        if store_name:
            product["store_name"] = store_name
        return product

    def lowes_detail(self, reference: ProductReference, url: str, store_no: str | None = None) -> dict[str, object]:
        parameters: dict[str, str] = {"url": url}
        if store_no:
            parameters["store_no"] = store_no
        payload = self.request("lowes_detail", **parameters) #Send the Request
        detail, price, bulk = self._detail_and_price(payload)
        if price is None: #If price is not found
            keys = ", ".join(sorted(detail.keys())) or ", ".join(sorted(payload.keys())) or "no fields"
            price_value = repr(detail.get("price"))[:200]
            list_price_value = repr(detail.get("list_price"))[:200]
            raise UnwrangleError(f"Lowes returned the product but no price. Fields: {keys}. Price value: {price_value} ({type(detail.get('price')).__name__}); list_price: {list_price_value}.")
        product = {
            "title": detail.get("name") or reference.query,
            "price": price,
            "currency": detail.get("currency", "USD"),
        }
        if bulk:
            product["bulk_price"], product["bulk_quantity"] = bulk
        store = detail.get("store")
        if isinstance(store, dict):
            if store.get("name"):
                product["store_name"] = store["name"]
            location_parts = [part for part in (store.get("city"), store.get("state"), store.get("zipcode")) if part]
            if location_parts:
                product["store_location"] = ", ".join(str(part) for part in location_parts)
        return product

    def ace_hardware_detail(self, reference: ProductReference, url: str, store_no: str | None = None) -> dict[str, object]:
        parameters: dict[str, str] = {"url": url}
        if store_no:
            parameters["store_no"] = store_no
        payload = self.request("acehardware_detail", **parameters) #Send the Request
        detail, price, bulk = self._detail_and_price(payload)
        if price is None: #If price is not found
            keys = ", ".join(sorted(detail.keys())) or ", ".join(sorted(payload.keys())) or "no fields"
            raise UnwrangleError(f"Ace Hardware returned the product but no price. Fields: {keys}.")
        product = {
            "title": detail.get("name") or reference.query,
            "price": price,
            "currency": detail.get("currency", "USD"),
        }
        if bulk:
            product["bulk_price"], product["bulk_quantity"] = bulk
        return product

    def sams_club_detail(self, reference: ProductReference, url: str) -> dict[str, object]:
        payload = self.request("samsclub_detail", url=url)
        detail, price, bulk = self._detail_and_price(payload) #Send the Request
        if price is None: #If price is not found
            keys = ", ".join(sorted(detail.keys())) or ", ".join(sorted(payload.keys())) or "no fields"
            raise UnwrangleError(f"Sams Club returned the product but no price. Fields: {keys}.")
        product = {
            "title": detail.get("name") or reference.query,
            "price": price,
            "currency": detail.get("currency", "USD"),
        }
        if bulk:
            product["bulk_price"], product["bulk_quantity"] = bulk
        return product

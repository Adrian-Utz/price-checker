from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import urlopen

"""
SerpApi API Currently Implemented:
- Walmart
- Home Depot
Planned:
- Amazon

Use SerpApi to fetch product information, and keep it in lists.
The free version of SerpApi allows for 250 searches per month, you can comfortable search 57ish items per week, or 8 items a day.

Last Update: 9/10/2026
Written on: 7/27/2026
Written by: AJ Utz
"""


class SerpApiError(RuntimeError):
    """Raised when SerpApi cannot return a usable product result."""


@dataclass(frozen=True)
class ProductReference:
    retailer: str
    product_id: str | None
    query: str


def parse_price(value: Any) -> float | None:
    #Check if the value is an int or a float
    if isinstance(value, (int, float)):
        return float(value)
    #Use regular expressions t find all price pattens in the string value
    matches = re.findall(r"(?:\$|USD\s*)(\d{1,5}(?:,\d{3})?(?:\.\d{2})?)", str(value), re.IGNORECASE)
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
        # Recursively find the price if the candidte is a dictionary
        price = product_price(candidate) if isinstance(candidate, dict) else parse_price(candidate)
        if price is not None:
            return price

    #Iterate ove potentail amount/value keys and recursively search for the price
    for key in ("amount", "value"):
        price = parse_price(value.get(key))
        if price is not None:
            return price

    # Iterate ove potential nested keys
    for key, nested in value.items():
        #skip certian keys
        if key in {"price_was", "original_price", "list_price", "compare_at_price"}:
            continue
        #if the nested value is a dictionary or list recursivley search fro the price
        if isinstance(nested, (dict, list)):
            price = product_price(nested) if isinstance(nested, dict) else next(
                (product_price(item) for item in nested if isinstance(item, dict)), None
            )
            if price is not None: #if valid price is found
                return price
    return None


def product_bulk_price(value: Any, regular_price: float | None = None) -> tuple[float, int] | None:
    """Find a bulk price and the minimum quantity needed to receive it."""
    if not isinstance(value, dict):
        return None
    for quantity_key in ("bulk_price_quantity", "bulk_price_threshold"):
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


def product_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = [] #Init list to store candidates

    #Iterate over a set of keys that may contain product information
    for key in ("product_result", "product_results", "product", "data"):
        value = payload.get(key) #get the value associated with the current key from the payload
        if isinstance(value, dict): #Check if the value is a Dictionary
            candidates.append(value) #If it is the add it to the candidates list
        elif isinstance(value, list): #check if the value is a list
            #If it is iterate over the list and add each dictionary item to the candidates list
            candidates.extend(item for item in value if isinstance(item, dict))
    products = payload.get("products")
    if isinstance(products, list):
        candidates.extend(item for item in products if isinstance(item, dict))
    return candidates


def store_name_from_payload(payload: dict[str, Any]) -> str | None:
    """Find the Store's name from the store number"""
    search_information = payload.get("search_information")
    if isinstance(search_information, dict):
        for key in ("store_name", "store"):
            value = search_information.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        location = search_information.get("location")
        if isinstance(location, dict):
            value = location.get("store_name")
            if isinstance(value, str) and value.strip():
                return value.strip()
    for opt_key in ("pickup_option", "delivery_option", "product_result"):
        opt = payload.get(opt_key)
        if isinstance(opt, dict):
            if opt_key == "product_result":
                p_opt = opt.get("pickup_option")
                if isinstance(p_opt, dict) and isinstance(p_opt.get("location"), str) and p_opt["location"].strip():
                    return p_opt["location"].strip()
            elif isinstance(opt.get("location"), str) and opt["location"].strip():
                return opt["location"].strip()
    pr_list = payload.get("product_results") or payload.get("product_result")
    if isinstance(pr_list, dict):
        ful = pr_list.get("fulfillment")
        if isinstance(ful, dict) and isinstance(ful.get("store"), str) and ful["store"].strip():
            return ful["store"].strip()
    return None


def store_location_from_payload(payload: dict[str, Any]) -> str | None:
    """Find the location data from the payload"""
    search_information = payload.get("search_information")
    if isinstance(search_information, dict):
        location = search_information.get("location")
        if isinstance(location, str) and location.strip():
            return location.strip()
        if isinstance(location, dict):
            city = location.get("city")
            state = location.get("state") or location.get("province") or location.get("province_code")
            zip_code = location.get("postal_code") or location.get("zip")
            parts = [p for p in (city, state, zip_code) if p]
            if parts:
                return ", ".join(str(p).strip() for p in parts)
        for key in ("store_location", "address", "store_address"):
            value = search_information.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for opt_key in ("delivery_option", "product_result"):
        opt = payload.get(opt_key)
        if isinstance(opt, dict):
            if opt_key == "product_result":
                d_opt = opt.get("delivery_option")
                if isinstance(d_opt, dict) and isinstance(d_opt.get("location"), str) and d_opt["location"].strip():
                    return d_opt["location"].strip()
            elif isinstance(opt.get("location"), str) and opt["location"].strip():
                return opt["location"].strip()
    sp = payload.get("search_parameters")
    if isinstance(sp, dict):
        for key in ("delivery_zip", "location", "zip"):
            value = sp.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def retailer_for(url: str) -> str:
    """Parse the URL and extract the Host part. If it matches either walmart or homedepot return the respective parameter."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host.endswith("walmart.com"):
        return "walmart"
    if host.endswith("homedepot.com"):
        return "home_depot"
    raise SerpApiError("Only Walmart and Home Depot URLs are supported at this time.")


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
    #Remove any numeric suffix from the last part of name_parts
    if name_parts and re.fullmatch(r"\d+", name_parts[-1]):
        name_parts.pop()
    #Clean and normalize the query string
    query = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", " ".join(name_parts))).strip()
    # Return a ProductReference object with the determined retailer, product ID, and query
    return ProductReference(retailer, product_id, query)


class SerpApiClient:
    def __init__(self, api_key: str | None = None) -> None:
        """
        Initialize the API key from environment variables or default.
        Set Timeout for requests.
        Check if the API key is configured.
        """
        self.api_key = api_key or os.environ.get("SERPAPI_API_KEY")
        self.timeout_seconds = max(30, int(os.environ.get("SERPAPI_TIMEOUT_SECONDS", "90")))
        self.response_history: list[dict[str, Any]] = []
        if not self.api_key:
            raise SerpApiError("SERPAPI_API_KEY is not configured.")

    def request(self, engine: str, **parameters: str) -> dict[str, Any]:
        query = {"engine": engine, "api_key": self.api_key, **parameters} # Create a query dictionary with engine and API key, merging with other parameters
        # Construct the endpoint URL with query parameters
        endpoint = "https://serpapi.com/search.json?" + "&".join(
            f"{quote_plus(str(key))}={quote_plus(str(value))}" for key, value in query.items()
        )
        try:
            # Open the endpoint and read the response with the specified timeout
            with urlopen(endpoint, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8")) #Decode the responce and parse it as JSON
        except TimeoutError as error: #Timeout error
            raise SerpApiError(
                f"SerpApi {engine} request timed out after {self.timeout_seconds} seconds. "
                "The request may still appear in your SerpApi usage dashboard."
            ) from error
        except Exception as error: #Request related error
            raise SerpApiError(f"SerpApi {engine} request failed: {error}") from error
        self.response_history.append(payload)
        #Check for errors in the resonce payload
        if payload.get("error"):
            raise SerpApiError(f"SerpApi error: {payload['error']}")
        return payload

    def product(self, url: str, store_id: str | None = None) -> dict[str, object]:
        reference = product_reference(url) #determine the retailer
        if reference.retailer == "walmart":
            return self.walmart_product(reference, store_id)
        return self.home_depot_product(reference, store_id)

    def walmart_product(self, reference: ProductReference, store_id: str | None = None) -> dict[str, object]:
        #check if the product ID is available
        if not reference.product_id:
            raise SerpApiError("The Walmart URL does not contain a product ID.")
        #Request the product details from Walmart
        parameters = {"product_id": reference.product_id, "q": reference.query or reference.product_id}
        if store_id:
            parameters["store_id"] = store_id
        payload = self.request("walmart_product", **parameters)
        #extract the payload result and price
        result = payload.get("product_result") or {}
        price = product_price(result)
        if price is None: #If price is not found
            raise SerpApiError("Walmart returned the product but no price.")
        #Return product details
        product = {"title": result.get("title") or reference.query, "price": price, "currency": result.get("currency", "USD")}
        bulk = product_bulk_price(result, price)
        if bulk:
            product["bulk_price"], product["bulk_quantity"] = bulk
        store_name = store_name_from_payload(payload)
        if store_name:
            product["store_name"] = store_name
        store_location = store_location_from_payload(payload)
        if store_location:
            product["store_location"] = store_location
        return product

    def home_depot_product(self, reference: ProductReference, store_id: str | None = None) -> dict[str, object]:
        search: dict[str, Any] = {}
        #Determine the product ID from the URl or search Results
        product_id = reference.product_id
        if not product_id:
            # Perform a search on Home Depot
            parameters = {"q": reference.query, "country": "us"}
            if store_id:
                parameters["store_id"] = store_id
            search = self.request("home_depot", **parameters)
            # Find the Product ID from the search results
            for result in search.get("products", []):
                if isinstance(result, dict) and result.get("product_id"):
                    product_id = str(result["product_id"])
                    break
        if not product_id: #Error if product ID is not found
            raise SerpApiError("Home Depot search did not return a product ID.")
        #Request the product details from Home Depot
        parameters = {"product_id": product_id, "q": reference.query or product_id, "country": "us"}
        if store_id:
            parameters["store_id"] = store_id
        payload = self.request("home_depot_product", **parameters)
        #Extract the product candidates and price
        candidates = product_candidates(payload)
        result = next((candidate for candidate in candidates if product_price(candidate) is not None), None)
        if result is None: #Error if Price is not found
            keys = ", ".join(sorted(payload.keys())) or "no response fields"
            raise SerpApiError(f"Home Depot returned the product but no price. Response fields: {keys}.")
        price = product_price(result) #Return product details
        store_name = store_name_from_payload(payload)
        if not store_name:
            store_name = store_name_from_payload(search)
        product = {"title": result.get("title") or reference.query, "price": price, "currency": "USD"}
        bulk = product_bulk_price(result)
        if bulk:
            product["bulk_price"], product["bulk_quantity"] = bulk
        if store_name:
            product["store_name"] = store_name
        store_location = store_location_from_payload(payload) or store_location_from_payload(search)
        if store_location:
            product["store_location"] = store_location
        return product

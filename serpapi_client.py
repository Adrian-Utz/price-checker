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
        #Check for errors in the resonce payload
        if payload.get("error"):
            raise SerpApiError(f"SerpApi error: {payload['error']}")
        return payload

    def product(self, url: str) -> dict[str, object]:
        reference = product_reference(url) #determine the retailer
        if reference.retailer == "walmart":
            return self.walmart_product(reference)
        return self.home_depot_product(reference)

    def walmart_product(self, reference: ProductReference) -> dict[str, object]:
        #check if the product ID is available
        if not reference.product_id:
            raise SerpApiError("The Walmart URL does not contain a product ID.")
        #Request the product details from Walmart
        payload = self.request("walmart_product", product_id=reference.product_id)
        #extract the payload result and price
        result = payload.get("product_result") or {}
        price = product_price(result)
        if price is None: #If price is not found
            raise SerpApiError("Walmart returned the product but no price.")
        #Return product details
        return {"title": result.get("title") or reference.query, "price": price, "currency": result.get("currency", "USD")}

    def home_depot_product(self, reference: ProductReference) -> dict[str, object]:
        #Determine the product ID from the URl or search Results
        product_id = reference.product_id
        if not product_id:
            # Perform a search on Home Depot
            search = self.request("home_depot", q=reference.query, country="us")
            # Find the Product ID from the search results
            for result in search.get("products", []):
                if isinstance(result, dict) and result.get("product_id"):
                    product_id = str(result["product_id"])
                    break
        if not product_id: #Error if product ID is not found
            raise SerpApiError("Home Depot search did not return a product ID.")
        #Request the product details from Home Depot
        payload = self.request("home_depot_product", product_id=product_id, country="us")
        #Extract the product candidates and price
        candidates = product_candidates(payload)
        result = next((candidate for candidate in candidates if product_price(candidate) is not None), None)
        if result is None: #Error if Price is not found
            keys = ", ".join(sorted(payload.keys())) or "no response fields"
            raise SerpApiError(f"Home Depot returned the product but no price. Response fields: {keys}.")
        price = product_price(result) #Return product details
        return {"title": result.get("title") or reference.query, "price": price, "currency": "USD"}

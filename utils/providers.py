import os
from collections.abc import Callable
from urllib.parse import urlparse

from api_clients.apify_api_client import ApifyClient
from api_clients.serpapi_client import SerpApiClient
from api_clients.unwrangle_client import UnwrangleClient

"""
Holds the provider selection and API-specific behavior.

Last Update: 10/19/2026
Written on: 10/12/2026
Written by: AJ Utz
"""

API_PROVIDERS = ("auto", "serpapi", "unwrangle", "apify")

def api_provider() -> str:
	value = os.environ.get("PRICE_CHECKER_API_PROVIDER", "auto").strip().lower()
	return value if value in API_PROVIDERS else "auto"


#Providers a single item can be pinned to. "auto" is left out because an empty mapping already means the global setting.
ITEM_PROVIDERS = ("serpapi", "unwrangle", "apify")

def providers_for_retailer(retailer: str) -> tuple[str, ...]:
	"""Return the providers that can look up an item from the given retailer name (as stored in products.source)."""
	if retailer == "Lowes":
		return ("unwrangle", "apify")
	if retailer in {"Ace Hardware", "Sams Club"}:
		return ("unwrangle",)
	if retailer in {"Walmart", "Home Depot"}:
		return ("serpapi", "unwrangle")
	return ()


def fetch_product(
	url: str,
	store_id: str | None = None,
	zip_state: str | None = None,
	zipcode: str | None = None,
	*,
	mark_serpapi_usage_stale: Callable[[], None],
	update_unwrangle_usage: Callable[[list[dict[str, object]]], None],
	record_dev_api_responses: Callable[[str, str, list[dict[str, object]], str | None], None],
	provider: str | None = None,
) -> dict[str, object]:
	"""Fetch which store ID Api to call. A per-item provider overrides the global provider setting."""
	host = urlparse(url).netloc.lower().removeprefix("www.")
	provider = provider if provider in ITEM_PROVIDERS else api_provider()
	client = None
	error = None
	# SerpApi only covers Walmart and Home Depot; Unwrangle covers every supported retailer.
	try:
		if provider == "serpapi":
			if "lowes" in host or "acehardware" in host or "samsclub" in host:
				raise RuntimeError("SerpApi does not support this retailer. Switch the API provider to Unwrangle or Auto.")
			client = SerpApiClient()
			return client.product(url, store_id=store_id)
		if provider == "apify":
			if "lowes" not in host:
				raise RuntimeError("Apify currently only supports Lowes. Switch the API provider to Unwrangle, SerpApi, or Auto.")
			client = ApifyClient()
			return client.product(url, store_id=store_id, zip_state=zip_state, zipcode=zipcode)
		if provider == "unwrangle":
			client = UnwrangleClient()
			return client.product(url, store_id=store_id, zip_state=zip_state, zipcode=zipcode)
		if "lowes" in host and apify_ready():
			client = ApifyClient()
			return client.product(url, store_id=store_id, zip_state=zip_state, zipcode=zipcode)
		if "lowes" in host or "acehardware" in host or "samsclub" in host:
			client = UnwrangleClient()
			return client.product(url, store_id=store_id, zip_state=zip_state, zipcode=zipcode)
		client = SerpApiClient()
		return client.product(url, store_id=store_id)
	except Exception as fetch_error:
		error = str(fetch_error)
		raise
	finally:
		if client is not None:
			if isinstance(client, SerpApiClient):
				mark_serpapi_usage_stale()
			elif isinstance(client, UnwrangleClient):
				update_unwrangle_usage(client.response_history)
			record_dev_api_responses(url, client.__class__.__name__, getattr(client, "response_history", []), error)


#Is SerpApi ready? API key is found.
def serpapi_ready() -> bool:
	return bool(os.environ.get("SERPAPI_API_KEY"))

#Is Unwrangle ready? API key is found.
def unwrangle_ready() -> bool:
	return bool(os.environ.get("UNWRANGLE_API_KEY"))

#Is Apify ready? API key is found.
def apify_ready() -> bool:
	return bool(os.environ.get("APIFY_API_TOKEN"))


def provider_ready(provider: str) -> bool:
	#Call this to check if the API porviders are ready.
	if provider == "serpapi":
		return serpapi_ready()
	if provider == "unwrangle":
		return unwrangle_ready()
	if provider == "apify":
		return apify_ready()
	return serpapi_ready() or unwrangle_ready() or apify_ready()
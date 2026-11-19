from __future__ import annotations

from api_clients.serpapi_client import (
	product_bulk_price as serpapi_bulk_price,
	product_candidates,
	product_price as serpapi_product_price,
)
from api_clients.unwrangle_client import (
	product_bulk_price as unwrangle_bulk_price,
	product_discount_bulk_price,
	product_price as unwrangle_product_price,
)
from utils.product_utils import product_query

"""Contains the Saved-Responce JSON lookup and replay logic

Written by: AJ Utz
Written on: 11/11/2026
Latest Update: 11/11/2026
"""

def _find_json_field(value: object, field_names: tuple[str, ...], path: str = "$") -> tuple[str | None, object]:
	if isinstance(value, dict):
		for name in field_names:
			if name in value and value[name] is not None:
				return f"{path}.{name}", value[name]
		for key, nested in value.items():
			found_path, found_value = _find_json_field(nested, field_names, f"{path}.{key}")
			if found_path:
				return found_path, found_value
	elif isinstance(value, list):
		for index, nested in enumerate(value):
			found_path, found_value = _find_json_field(nested, field_names, f"{path}[{index}]")
			if found_path:
				return found_path, found_value
	return None, None


def _find_discount_fields(value: object, bulk: tuple[float, int], price: float | None, path: str = "$") -> tuple[str, object, str, object] | None:
	if isinstance(value, dict):
		if product_discount_bulk_price(value, price) == bulk:
			return f"{path}.unit_price", value["unit_price"], f"{path}.min_quantity", value["min_quantity"]
		for key, nested in value.items():
			found = _find_discount_fields(nested, bulk, price, f"{path}.{key}")
			if found:
				return found
	elif isinstance(value, list):
		for index, nested in enumerate(value):
			found = _find_discount_fields(nested, bulk, price, f"{path}[{index}]")
			if found:
				return found
	return None


def replay_dev_response(provider: str, url: str, payload: dict[str, object]) -> dict[str, object]:
	"""Build the extraction debugger's view of a saved provider response."""
	if provider == "SerpApiClient":
		candidates = product_candidates(payload)
		candidate = next((item for item in candidates if serpapi_product_price(item) is not None), {})
		price = serpapi_product_price(candidate)
		bulk = serpapi_bulk_price(candidate, price) if price is not None else None
		title = candidate.get("title") or product_query(url)
		currency = candidate.get("currency") or "USD"
		debug_source = candidate
	else:
		detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
		price = unwrangle_product_price(detail)
		if price is None:
			price = unwrangle_product_price(payload)
		if price is None:
			price = unwrangle_product_price(detail.get("list_price"))
		if price is None:
			price = unwrangle_product_price(payload.get("list_price"))
		bulk = unwrangle_bulk_price(detail, price) or unwrangle_bulk_price(payload, price)
		title = detail.get("name") or detail.get("title") or product_query(url)
		currency = detail.get("currency") or payload.get("currency") or "USD"
		debug_source = payload
	bulk_price, bulk_quantity = bulk if bulk else (None, None)
	price_path, raw_price = _find_json_field(debug_source, ("price", "current_price", "sale_price", "final_price"))
	if raw_price is None:
		price_path, raw_price = _find_json_field(debug_source, ("list_price", "listing_price", "original_price"))
	bulk_price_path, raw_bulk_price = _find_json_field(debug_source, ("bulk_price", "volume_price"))
	quantity_path, raw_quantity = _find_json_field(debug_source, ("bulk_price_threshold", "bulk_price_quantity", "minimum_order_quantity", "quantity"))
	if provider != "SerpApiClient" and bulk:
		discount_fields = _find_discount_fields(debug_source, bulk, price)
		if discount_fields:
			bulk_price_path, raw_bulk_price, quantity_path, raw_quantity = discount_fields
	return {
		"product": {"title": title, "price": price, "currency": currency, "bulk_price": bulk_price, "bulk_quantity": bulk_quantity},
		"fields": {
			"title": {"path": _find_json_field(debug_source, ("name", "title"))[0], "value": title},
			"price": {"path": price_path, "raw": raw_price, "parsed": price},
			"currency": {"path": _find_json_field(debug_source, ("currency",))[0], "value": currency},
			"bulk_price": {"path": bulk_price_path, "raw": raw_bulk_price, "parsed": bulk_price},
			"bulk_quantity": {"path": quantity_path, "raw": raw_quantity, "parsed": bulk_quantity},
		},
		"error": "No regular price could be extracted from this response." if price is None else None,
	}

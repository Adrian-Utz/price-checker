from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from api_clients.unwrangle_client import product_reference as unwrangle_product_reference
from utils.migrations import LATEST_VERSION
from utils.providers import ITEM_PROVIDERS

"""
Helper methods for watchlist importing and exporting. 

Last Update: 10/19/2026
Written on: 10/10/2026
Written by: AJ Utz
"""

MAX_IMPORT_PRODUCTS = 1000
MAX_IMPORT_OBSERVATIONS = 20000 #Per product


def source_for(url: str) -> str:
	"""Search the Url for the source name."""
	host = urlparse(url).netloc.lower().removeprefix("www.")
	if "homedepot" in host:
		return "Home Depot"
	if "walmart" in host:
		return "Walmart"
	if "lowes" in host:
		return "Lowes"
	if "acehardware" in host:
		return "Ace Hardware"
	if "samsclub" in host:
		return "Sams Club"
	return host


def watchlist_export_rows(connection: sqlite3.Connection, user_id: int) -> list[dict[str, object]]:
	"""Read one user's watchlist products and price history into export-ready rows."""
	products = connection.execute("SELECT * FROM products WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
	rows = []
	for product in products:
		host = urlparse(product["url"]).netloc.lower().removeprefix("www.")
		# Prefer the ID the provider returned (Apify's "products" key); otherwise parse it from the URL.
		product_id = product["retailer_product_id"] or (
			unwrangle_product_reference(product["url"]).product_id
			if host.endswith(("walmart.com", "homedepot.com", "lowes.com", "acehardware.com", "samsclub.com"))
			else None
		)
		history = connection.execute(
			"SELECT price, bulk_price, currency, status, error, observed_at FROM observations WHERE product_id = ? ORDER BY observed_at",
			(product["id"],),
		).fetchall()
		rows.append({
			"id": product["id"],
			"product_id": product_id,
			"url": product["url"],
			"title": product["title"],
			"source": product["source"],
			"price": product["price"],
			"bulk_price": product["bulk_price"],
			"bulk_quantity": product["bulk_quantity"],
			"currency": product["currency"],
			"checked_at": product["checked_at"],
			"error": product["error"],
			"store_id": product["store_id"],
			"api_provider": product["api_provider"],
			"history": [dict(observation) for observation in history],
		})
	return rows


def validate_watchlist_database(path: Path) -> None:
	"""Raise ValueError unless the file is a usable Price Checker SQLite database."""
	with open(path, "rb") as handle:
		header = handle.read(16)
	if header != b"SQLite format 3\x00":
		raise ValueError("That file is not a SQLite database.")
	connection = sqlite3.connect(path)
	try:
		tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
		version = connection.execute("PRAGMA user_version").fetchone()[0]
	finally:
		connection.close()
	if "products" not in tables:
		raise ValueError("That database does not contain a 'products' table.")
	if version > LATEST_VERSION:
		raise ValueError("That database was made by a newer version of Price Checker.")


def parse_watchlist_json(text: str) -> list[dict[str, object]]:
	"""Parse a previously-exported, or your own personally built, watchlist JSON file."""
	try:
		payload = json.loads(text)
	except json.JSONDecodeError as error:
		raise ValueError(f"That file is not valid JSON: {error}") from error
	products = payload.get("products") if isinstance(payload, dict) else None
	if not isinstance(products, list):
		raise ValueError("That JSON file does not look like a Price Checker export (missing a 'products' list).")
	return products


def parse_watchlist_csv(text: str) -> list[dict[str, object]]:
	"""Parse a previously-exported, or your own personally built, watchlist CSV file."""
	reader = csv.DictReader(io.StringIO(text))
	if reader.fieldnames is None or "url" not in reader.fieldnames:
		raise ValueError("That CSV file does not look like a Price Checker export (missing a 'url' column).")
	products = []
	for row in reader:
		history_text = row.get("history") or "[]"
		try:
			row["history"] = json.loads(history_text)
		except json.JSONDecodeError as error:
			raise ValueError(f"That CSV file has an invalid 'history' column: {error}") from error
		products.append(row)
	return products


def read_watchlist_database(path: Path) -> list[dict[str, object]]:
	"""Read products and price history out of an uploaded SQLite file, read-only.

	The file is untrusted: it is opened read-only, only known columns are selected, and any
	user_id/ownership data in it is ignored. Older files that lack newer columns still work.
	"""
	validate_watchlist_database(path)
	connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
	connection.row_factory = sqlite3.Row
	try:
		def columns(table: str, wanted: tuple[str, ...]) -> list[str]:
			present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
			return [column for column in wanted if column in present]

		product_columns = columns("products", ("id", "url", "title", "source", "store_id", "price", "bulk_price", "bulk_quantity", "currency", "checked_at", "error", "retailer_product_id", "api_provider"))
		if "id" not in product_columns or "url" not in product_columns:
			raise ValueError("That database's 'products' table is missing required columns.")
		observation_columns = columns("observations", ("product_id", "price", "bulk_price", "currency", "status", "error", "observed_at"))
		has_history = {"product_id", "observed_at"} <= set(observation_columns)
		products = []
		for row in connection.execute(f"SELECT {', '.join(product_columns)} FROM products ORDER BY id"):
			product = dict(row)
			product["product_id"] = product.pop("retailer_product_id", None)
			product["history"] = [
				dict(observation) for observation in connection.execute(
					f"SELECT {', '.join(observation_columns)} FROM observations WHERE product_id = ? ORDER BY observed_at", (row["id"],)
				)
			] if has_history else []
			products.append(product)
		return products
	except sqlite3.DatabaseError as error:
		raise ValueError(f"That database could not be read: {error}") from error
	finally:
		connection.close()


def _number(value: object, kind: type = float) -> float | int | None:
	"""Coerce a JSON/CSV value to a finite number, or None. CSV cells arrive as strings."""
	if value is None or isinstance(value, bool) or value == "":
		return None
	try:
		number = float(value)
	except (TypeError, ValueError):
		return None
	if not math.isfinite(number):
		return None
	return int(number) if kind is int else number


def _text(value: object, limit: int = 2000) -> str | None:
	if value is None or value == "":
		return None
	return str(value)[:limit]


def _timestamp(value: object) -> str | None:
	"""Keep only real ISO-8601 timestamps; anything else would break charts and ordering."""
	if not isinstance(value, str):
		return None
	try:
		datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None
	return value


def merge_watchlist(connection: sqlite3.Connection, user_id: int, products: list[dict[str, object]], *, max_products: int, normalize_url: Callable[[str], str]) -> dict[str, int]:
	"""Merge parsed watchlist rows into one user's data without touching anything else.

	A product already tracked (same URL and store) keeps its row and only gains price history it
	doesn't have yet. New products are added until the user's product limit is reached.
	Every row is written with this user's id, whatever the file claims.
	"""
	if len(products) > MAX_IMPORT_PRODUCTS:
		raise ValueError(f"That file has more than {MAX_IMPORT_PRODUCTS} products.")
	result = {"added": 0, "merged": 0, "observations": 0, "skipped": 0, "over_limit": 0}
	existing = {
		(row["url"], row["store_id"] or ""): row["id"]
		for row in connection.execute("SELECT id, url, store_id FROM products WHERE user_id = ?", (user_id,))
	}
	total = len(existing)
	for product in products:
		if not isinstance(product, dict):
			result["skipped"] += 1
			continue
		try:
			url = normalize_url(str(product.get("url") or ""))
		except ValueError:
			result["skipped"] += 1
			continue
		store_id = _text(product.get("store_id"), 64)
		key = (url, store_id or "")
		product_id = existing.get(key)
		if product_id is None:
			if total >= max_products:
				result["over_limit"] += 1
				continue
			provider = product.get("api_provider")
			cursor = connection.execute(
				"INSERT INTO products (user_id, url, title, source, store_id, price, bulk_price, bulk_quantity, currency, checked_at, error, retailer_product_id, api_provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
				(user_id, url, _text(product.get("title")), _text(product.get("source"), 100) or source_for(url), store_id,
					_number(product.get("price")), _number(product.get("bulk_price")) or None, _number(product.get("bulk_quantity"), int) or None,
					_text(product.get("currency"), 8), _timestamp(product.get("checked_at")), _text(product.get("error")),
					_text(product.get("product_id"), 100), provider if provider in ITEM_PROVIDERS else None),
			)
			product_id = existing[key] = cursor.lastrowid
			total += 1
			result["added"] += 1
			known_times: set[str] = set()
		else:
			result["merged"] += 1
			known_times = {row[0] for row in connection.execute("SELECT observed_at FROM observations WHERE product_id = ?", (product_id,))}
		history = product.get("history")
		if not isinstance(history, list):
			continue
		for observation in history[:MAX_IMPORT_OBSERVATIONS]:
			if not isinstance(observation, dict):
				continue
			observed_at = _timestamp(observation.get("observed_at"))
			if observed_at is None or observed_at in known_times:
				continue
			price = _number(observation.get("price"))
			status = observation.get("status")
			if status not in ("success", "error"):
				status = "success" if price is not None else "error"
			connection.execute(
				"INSERT INTO observations (product_id, price, bulk_price, currency, status, error, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
				(product_id, price, _number(observation.get("bulk_price")), _text(observation.get("currency"), 8), status, _text(observation.get("error")), observed_at),
			)
			known_times.add(observed_at)
			result["observations"] += 1
	if result["added"] + result["merged"] == 0:
		raise ValueError("That file did not contain any importable products." if not result["over_limit"] else f"Your watchlist is already at its limit of {max_products} products.")
	return result
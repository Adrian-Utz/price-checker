from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from api_clients.unwrangle_client import product_reference as unwrangle_product_reference
from utils.providers import ITEM_PROVIDERS

"""
Helper methods for watchlist importing and exporting. 

Last Update: 10/19/2026
Written on: 10/10/2026
Written by: AJ Utz
"""

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


def watchlist_export_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
	"""Read watchlist products and price history into export-ready rows."""
	products = connection.execute("SELECT * FROM products ORDER BY id").fetchall()
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
	finally:
		connection.close()
	if "products" not in tables:
		raise ValueError("That database does not contain a 'products' table.")


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


def build_watchlist_database(path: Path, products: list[dict[str, object]]) -> None:
	"""Create a fresh watchlist SQLite database at path from parsed product/history rows."""
	connection = sqlite3.connect(path)
	try:
		# Create four tables to hold the user's desired information.
		connection.executescript("""
			CREATE TABLE products (
				id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT,
				source TEXT NOT NULL, store_id TEXT, price REAL, bulk_price REAL,
				bulk_quantity INTEGER, currency TEXT, checked_at TEXT,
				error TEXT, retailer_product_id TEXT, api_provider TEXT
			);
			CREATE TABLE observations (
				id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
				price REAL, bulk_price REAL, currency TEXT, title TEXT, status TEXT NOT NULL,
				error TEXT, observed_at TEXT NOT NULL,
				FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
			);
			CREATE TABLE scans (
				id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
				completed_at TEXT, status TEXT NOT NULL
			);
			CREATE TABLE stores (
				id INTEGER PRIMARY KEY, retailer TEXT NOT NULL, store_id TEXT NOT NULL,
				name TEXT NOT NULL, location TEXT NOT NULL, zip_state TEXT, zipcode TEXT,
				UNIQUE(retailer, store_id)
			);
		""")
		imported = 0
		for product in products:
			url = str(product.get("url") or "").strip()
			source = str(product.get("source") or "").strip() or source_for(url)
			if not url:
				continue
			cursor = connection.execute(
				"INSERT INTO products (url, title, source, store_id, price, bulk_price, bulk_quantity, currency, checked_at, error, retailer_product_id, api_provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
				(url, product.get("title"), source, product.get("store_id"), product.get("price"),
					product.get("bulk_price") or None, product.get("bulk_quantity") or None, product.get("currency"), product.get("checked_at"), product.get("error"),
					str(product.get("product_id")) if product.get("product_id") else None,
					product.get("api_provider") if product.get("api_provider") in ITEM_PROVIDERS else None),
			)
			product_id = cursor.lastrowid
			history = product.get("history")
			if not isinstance(history, list):
				continue
			for observation in history:
				if not isinstance(observation, dict):
					continue
				if not observation.get("observed_at"):
					continue
				connection.execute(
					"INSERT INTO observations (product_id, price, bulk_price, currency, status, error, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
					(product_id, observation.get("price"), observation.get("bulk_price"), observation.get("currency"),
						observation.get("status") or "ok", observation.get("error"), observation.get("observed_at")),
				)
			imported += 1
		if imported == 0:
			raise ValueError("That file did not contain any importable products.")
		connection.commit()
	finally:
		connection.close()
from __future__ import annotations

# Python 3.15+ defers these imports; older versions ignore this allowlist.
__lazy_modules__ = {
	"json",
	"csv",
	"io",
	"math",
	"random",
	"re",
	"shutil",
	"sqlite3",
	"time",
	"datetime",
	"urllib.parse",
	"serpapi_client",
	"unwrangle_client",
	"charting",
	"version",
	"check_for_update",
	"werkzeug.serving",
}

import json
import csv
import contextlib
import io
import math
import os
import random
import re
import secrets
import shutil
import sqlite3
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import unquote, urlparse

from flask import Flask, Response, abort, redirect, render_template, request, send_file, session, url_for
from serpapi_client import SerpApiClient, product_bulk_price as serpapi_bulk_price, product_candidates, product_price as serpapi_product_price
from unwrangle_client import UnwrangleClient, product_bulk_price as unwrangle_bulk_price, product_price as unwrangle_product_price, product_reference as unwrangle_product_reference
from charting import chart_observations_in_range, format_observed_at_utc, price_chart
from version import VERSION_NUMBER
import check_for_update
from werkzeug.serving import make_server

"""
Main entry point into the program. This is a web application with a python backend. Used to keep track of certian items that the user selects.
Made with Flask.

Last Update: 9/28/2026
Written on: 7/27/2026
Written by: AJ Utz
"""

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE, override=True)
DATABASE_PATH = Path(os.environ.get("PRICE_CHECKER_DB", ROOT / "price_checker.sqlite3"))
DEVELOPER_MODE = os.environ.get("PRICE_CHECKER_DEVELOPER_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
DEV_DATABASE_PATH = Path(os.environ.get("PRICE_CHECKER_DEV_DB", ROOT / "price_checker.dev.sqlite3"))
MAX_URLS = max(20, min(int(os.environ.get("PRICE_CHECKER_MAX_URLS", "100")), 100)) #Change this variable if you want to track more items.
CACHE_HOURS = max(1, int(os.environ.get("PRICE_CHECKER_CACHE_HOURS", "24")))
MIN_DELAY_SECONDS = max(1.0, float(os.environ.get("PRICE_CHECKER_MIN_DELAY", "4")))
MAX_FULL_CHART_WIDTH = 2400
DEFAULT_CHART_RANGE_DAYS = 30
API_USAGE_CACHE_SECONDS = 300
app = Flask(__name__)
app.secret_key = os.environ.get("PRICE_CHECKER_SECRET", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024 #Cap uploaded database files at 64 MB
server = None
shutdown_lock = threading.Lock()
shutdown_timer = None
update_info = {"available": False, "latest_version": None}
update_info_lock = threading.Lock()
api_usage_lock = threading.Lock()
api_usage = {
	"serpapi_remaining": None,
	"serpapi_monthly_limit": None,
	"serpapi_monthly_used": None,
	"unwrangle_remaining": None,
	"unwrangle_starting_credits": None,
}
serpapi_usage_checked_at = 0.0


def check_for_update_background() -> None:
	"""Query GitHub for a newer release in a background thread and cache the result for the UI."""
	try:
		available, _local, latest = check_for_update.is_update_available()
	except Exception:
		return
	with update_info_lock:
		update_info["available"] = available
		update_info["latest_version"] = latest


@contextlib.contextmanager
def connect():
	"""
	## Open a SQLite connection
	Allow commits (or roll backs) on exit like a plain sqlite3. Connection would,
	but also close the connection - sqlite3.Connection's own context manager never closes the file,
	which left stale open handles on DATABASE_PATH and blocked database import/replace on Windows.
	"""
	connection = sqlite3.connect(DATABASE_PATH)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA foreign_keys = ON")
	try:
		#Yield the connection to the caller
		yield connection
		#Commit chagees to the database
		connection.commit()
	except Exception:
		connection.rollback() #In case of exception, rollback
		raise
	finally:
		connection.close()


@contextlib.contextmanager
def connect_dev():
	"""Provides a context manager for connecting to a SQLite database in dev mode"""
	connection = sqlite3.connect(DEV_DATABASE_PATH)
	connection.row_factory = sqlite3.Row
	try:
		#Yield the connection to the caller
		yield connection
		#Commit chagees to the database
		connection.commit()
	except Exception:
		connection.rollback() #In case of exception, rollback
		raise
	finally:
		connection.close()


def init_dev_db() -> None:
	"""Initialize the dev database with a table named api_responses"""
	with connect_dev() as connection:
		#The table will store info about API responces (timestamps, URLs, providers, JSON data)
		connection.execute("""
			CREATE TABLE IF NOT EXISTS api_responses (
				id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL,
				url TEXT NOT NULL, provider TEXT NOT NULL,
				response_json TEXT, error TEXT
			)
		""")


def record_dev_api_responses(url: str, provider: str, responses: list[dict[str, object]], error: str | None = None) -> None:
	"""Record the entire json responce if instance is in dev mode"""
	if not DEVELOPER_MODE:
		return
	with connect_dev() as connection:
		for response in responses:
			connection.execute(
				"INSERT INTO api_responses (observed_at, url, provider, response_json, error) VALUES (?, ?, ?, ?, ?)",
				(utc_now().isoformat(), url, provider, json.dumps(response), error),
			)
		if error and not responses:
			connection.execute(
				"INSERT INTO api_responses (observed_at, url, provider, response_json, error) VALUES (?, ?, ?, ?, ?)",
				(utc_now().isoformat(), url, provider, None, error),
			)


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


def replay_dev_response(provider: str, url: str, payload: dict[str, object]) -> dict[str, object]:
	"""Show the user what is being pulled from the raw JSON"""
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


def init_db() -> None:
	"""
	## Initialize the database by creating necessary tables if they do not exist.
	This function establishes a connection to the database using the connect() function.
	It then executes SQL statements to create two tables:'products' and 'observations'.
	'products' contain info such as: ID, URL, title, source, price, currency, check date, and error status.
	'observations' records the observations of prices, curencies, titles, statuses, errors, observed dates, and references to products.
	"""
	with connect() as connection:
		connection.execute("PRAGMA foreign_keys = OFF")
		connection.executescript("""
			CREATE TABLE IF NOT EXISTS products (
				id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT,
				source TEXT NOT NULL, store_id TEXT, price REAL, bulk_price REAL,
				bulk_quantity INTEGER, currency TEXT, checked_at TEXT,
				error TEXT
			);
			CREATE TABLE IF NOT EXISTS observations (
				id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
				price REAL, bulk_price REAL, currency TEXT, title TEXT, status TEXT NOT NULL,
				error TEXT, observed_at TEXT NOT NULL,
				FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
			);
			CREATE TABLE IF NOT EXISTS scans (
				id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
				completed_at TEXT, status TEXT NOT NULL
			);
			CREATE TABLE IF NOT EXISTS stores (
				id INTEGER PRIMARY KEY, retailer TEXT NOT NULL, store_id TEXT NOT NULL,
				name TEXT NOT NULL, location TEXT NOT NULL,
				UNIQUE(retailer, store_id)
			);
		""")
		product_columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
		if "store_id" not in product_columns:
			connection.execute("ALTER TABLE products ADD COLUMN store_id TEXT")
		if "bulk_price" not in product_columns:
			connection.execute("ALTER TABLE products ADD COLUMN bulk_price REAL")
		if "bulk_quantity" not in product_columns:
			connection.execute("ALTER TABLE products ADD COLUMN bulk_quantity INTEGER")
		observation_columns = {row[1] for row in connection.execute("PRAGMA table_info(observations)")}
		if "bulk_price" not in observation_columns:
			connection.execute("ALTER TABLE observations ADD COLUMN bulk_price REAL")
		table_definition = connection.execute(
			"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'products'"
		).fetchone()[0]
		if "url TEXT NOT NULL UNIQUE" in table_definition:
			connection.execute("ALTER TABLE products RENAME TO products_old")
			connection.execute("""
				CREATE TABLE products (
					id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT,
					source TEXT NOT NULL, store_id TEXT, price REAL, bulk_price REAL,
					bulk_quantity INTEGER, currency TEXT, checked_at TEXT,
					error TEXT
				)
			""")
			connection.execute("""
				INSERT INTO products (id, url, title, source, store_id, price, currency, checked_at, error)
				SELECT id, url, title, source, store_id, price, currency, checked_at, error
				FROM products_old
			""")
			connection.execute("DROP TABLE products_old")
		connection.execute("PRAGMA foreign_keys = ON")


def utc_now() -> datetime:
	"""Time check"""
	return datetime.now(timezone.utc)


def local_calendar_date(timestamp: str):
	"""Time Stamp"""
	return datetime.fromisoformat(timestamp).astimezone().date()


def normalize_url(value: str) -> str:
	"""
	## Check and normilize the URL
	This function takes a string input and checks if it is a complete HTTP or HTTPS URL,
	and returns a normalized version of the URL without any fragment identifiers.
	"""
	parsed = urlparse(value.strip())
	if parsed.scheme not in {"http", "https"} or not parsed.netloc:
		raise ValueError("Enter a complete http or https URL.")
	return parsed._replace(fragment="").geturl()


def source_for(url: str) -> str:
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


def parse_price(text: str) -> float | None:
	"""Use regex to find all occurrences of dollar or USD followed by digits, optional commas and decimal points"""
	matches = re.findall(r"(?:\$|USD\s*)(\d{1,5}(?:,\d{3})?(?:\.\d{2})?)", text, re.IGNORECASE)
	#Convert each match to a float.
	values = [float(match.replace(",", "")) for match in matches]
	return min(values) if values else None


def ensure_success_status(status: int | None) -> None:
	#Check if the status code exists and is >= 400.
	if status is not None and status >= 400:
		raise RuntimeError(f"The retailer returned HTTP {status}; no product page was available.")


def product_query(url: str) -> str:
	#Split the URL into parts, unquote them, then filter out any empty strings
	path_parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
	#If forst part is "pd" remove from list
	if path_parts and path_parts[0].lower() == "pd":
		path_parts = path_parts[1:]
	#Remove any trailing numeric parts from the end of the path
	if path_parts and re.fullmatch(r"\d+", path_parts[-1]):
		path_parts.pop()
	#Extract the product name by replacing hyphens, underscores, and spaces with a space
	name = re.sub(r"[-_]+", " ", path_parts[-1] if path_parts else "")
	#remove any extra whitespace, strip leading/trailing spaces from name
	return re.sub(r"\s+", " ", name).strip()

"""Allow the user to select which API to use."""
API_PROVIDERS = ("auto", "serpapi", "unwrangle")

def api_provider() -> str:
	value = os.environ.get("PRICE_CHECKER_API_PROVIDER", "auto").strip().lower()
	return value if value in API_PROVIDERS else "auto"


def fetch_product(url: str, store_id: str | None = None) -> dict[str, object]:
	"""Fetch which store ID Api to call"""
	host = urlparse(url).netloc.lower().removeprefix("www.")
	provider = api_provider()
	client = None
	error = None
	# SerpApi only covers Walmart and Home Depot; Unwrangle covers every supported retailer.
	try:
		if provider == "serpapi":
			if "lowes" in host or "acehardware" in host or "samsclub" in host:
				raise RuntimeError("SerpApi does not support this retailer. Switch the API provider to Unwrangle or Auto.")
			client = SerpApiClient()
			return client.product(url, store_id=store_id)
		if provider == "unwrangle":
			client = UnwrangleClient()
			return client.product(url, store_id=store_id)
		if "lowes" in host or "acehardware" in host or "samsclub" in host:
			client = UnwrangleClient()
			return client.product(url, store_id=store_id)
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


def cache_is_fresh(checked_at: str | None) -> bool:
	#check if the checked_at timestamp exists
	if not checked_at:
		return False
	return datetime.fromisoformat(checked_at) > utc_now() - timedelta(hours=CACHE_HOURS)

#Is SerpApi ready? API key is found.
def serpapi_ready() -> bool:
	return bool(os.environ.get("SERPAPI_API_KEY"))

#Is Unwrangle ready? API key is found.
def unwrangle_ready() -> bool:
	return bool(os.environ.get("UNWRANGLE_API_KEY"))


def provider_ready(provider: str) -> bool:
	if provider == "serpapi":
		return serpapi_ready()
	if provider == "unwrangle":
		return unwrangle_ready()
	return serpapi_ready() or unwrangle_ready()


def normalize_usage_count(value: object) -> int | float | None:
	"""	If the count is  an int, return as an int. else return the float count"""
	if isinstance(value, bool) or value is None:
		return None
	try:
		#convert to float
		count = float(value)
	except (TypeError, ValueError, OverflowError):
		return None
	#make sure the count is finite and not negative
	if not math.isfinite(count) or count < 0:
		return None
	return int(count) if count.is_integer() else count


def update_unwrangle_usage(responses: list[dict[str, object]]) -> None:
	"""Check Unwrangle's responce for remaining credits, and update the amount remaining based on that number."""
	for payload in reversed(responses):
		if "remaining_credits" not in payload:
			continue
		remaining = normalize_usage_count(payload["remaining_credits"])
		if remaining is not None:
			with api_usage_lock:
				starting_credits = api_usage["unwrangle_starting_credits"]
				#If starting credits are less than the remaining credits, update the starting credits to the remaining amount.
				#This is to keep things accurate. 
				if starting_credits is None or remaining > starting_credits:
					api_usage["unwrangle_starting_credits"] = remaining
				api_usage["unwrangle_remaining"] = remaining
			return


def mark_serpapi_usage_stale() -> None:
	global serpapi_usage_checked_at
	with api_usage_lock:
		serpapi_usage_checked_at = 0.0


def invalidate_api_usage() -> None:
	"""Return the API usage variables to none"""
	global serpapi_usage_checked_at
	with api_usage_lock:
		serpapi_usage_checked_at = 0.0
		api_usage["serpapi_remaining"] = None
		api_usage["serpapi_monthly_limit"] = None
		api_usage["serpapi_monthly_used"] = None
		api_usage["unwrangle_remaining"] = None
		api_usage["unwrangle_starting_credits"] = None


def api_usage_summary() -> dict[str, int | float | None]:
	global serpapi_usage_checked_at
	if serpapi_ready() and not app.testing:
		now = time.monotonic()
		with api_usage_lock:
			should_refresh = now - serpapi_usage_checked_at >= API_USAGE_CACHE_SECONDS
			if should_refresh:
				serpapi_usage_checked_at = now
		if should_refresh:
			try:
				usage = SerpApiClient().account_usage()
			except Exception:
				pass
			else:
				with api_usage_lock:
					api_usage["serpapi_remaining"] = usage["remaining"]
					api_usage["serpapi_monthly_limit"] = usage["monthly_limit"]
					api_usage["serpapi_monthly_used"] = usage["monthly_used"]
	with api_usage_lock:
		usage = dict(api_usage)
	serpapi_limit = usage["serpapi_monthly_limit"]
	serpapi_used = usage["serpapi_monthly_used"]
	if isinstance(serpapi_limit, (int, float)) and serpapi_limit > 0 and isinstance(serpapi_used, (int, float)):
		usage["serpapi_usage_percent"] = min(100.0, max(0.0, serpapi_used / serpapi_limit * 100))
	else:
		usage["serpapi_usage_percent"] = 0.0
	starting_credits = usage["unwrangle_starting_credits"]
	remaining_credits = usage["unwrangle_remaining"]
	if isinstance(starting_credits, (int, float)) and starting_credits > 0 and isinstance(remaining_credits, (int, float)):
		usage["unwrangle_usage_percent"] = min(100.0, max(0.0, (starting_credits - remaining_credits) / starting_credits * 100))
	else:
		usage["unwrangle_usage_percent"] = 0.0
	return usage


def read_env_file_text() -> str:
	if not ENV_FILE.exists():
		return "SERPAPI_API_KEY=\nUNWRANGLE_API_KEY=\n"
	return ENV_FILE.read_text(encoding="utf-8")


def save_env_file_text(content: str) -> None:
	"""Check if .env content is valid, normilize, then save and load the key"""
	if "\x00" in content:
		raise ValueError("Invalid .env content.")
	#fixed `normilized` as it was causing blank lines.
	normalized = content.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
	ENV_FILE.write_text(normalized, encoding="utf-8")
	load_dotenv(ENV_FILE, override=True)
	invalidate_api_usage()


def update_env_setting(content: str, key: str, value: str) -> str:
	"""Updates or adds a setting to an environment config file."""
	lines = content.splitlines() #split the content into individual lines
	setting = f"{key}={value}" # Construct the line to be updated or added
	for index, line in enumerate(lines):
		#Check if the line starts with the key followes by a '=' 
		if line.split("=", 1)[0].strip() == key:
			lines[index] = setting #Update values
			return "\n".join(lines) + "\n" #Return content with a newline before and after.
	if not value:
		return content #Retrun original if the value is empty
	#Append the new setting at the end of the content with a new line
	return content.rstrip("\n") + "\n" + setting + "\n"


def watchlist_export_rows() -> list[dict[str, object]]:
	"""Read the sqlite3 file for information, then return a tuple with that data."""
	with connect() as connection:
		products = connection.execute("SELECT * FROM products ORDER BY id").fetchall()
		rows = []
		for product in products:
			host = urlparse(product["url"]).netloc.lower().removeprefix("www.")
			product_id = (
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
				"history": [dict(observation) for observation in history],
			})
	return rows


@app.before_request
def protect_post_forms() -> None:
	"""
	## Protects POST requests by checking for valid CSRF tokens
	This function runs before each request and ensures that the form token is valid
	It cancels any pending shutdown, checks if a CSRF token exists in the session, and aborts with a 400 error 
	if the token is invalid or missing when processing POST requests to endpoints other than 'shutdown'.
	"""
	cancel_pending_shutdown()

	#If the CSRF token is not in the session, generate a new one.
	#Fun fact: Passing 32 as the argument tells Python to generate 32 bytes of random data. 
	#This equates to 256 bits of entropy. Brute-forcing a 256-bit key would require more energy than exists in the observable universe, 
	#making guessing attacks a mathematical impossibility.
	if "csrf_token" not in session:
		session["csrf_token"] = secrets.token_urlsafe(32)

	#Check if the request method is POST and the endpoint is not 'shutdown'
	if request.method == "POST" and request.endpoint != "shutdown" and not app.testing:
		#Retrieve the submitten CSRF token from the form
		submitted = request.form.get("csrf_token", "")

		#Compare the submitted token with the session token using a secure comparison function
		if not secrets.compare_digest(submitted, session["csrf_token"]):
			abort(400, description="Invalid form token.")

"""Check if the page is closed then stop local server."""
def cancel_pending_shutdown() -> None:
	global shutdown_timer
	with shutdown_lock:
		if shutdown_timer is not None:
			shutdown_timer.cancel()
			shutdown_timer = None

def stop_server() -> None:
	if server is not None:
		server.shutdown()

def schedule_shutdown() -> None:
	global shutdown_timer
	cancel_pending_shutdown()
	with shutdown_lock:
		shutdown_timer = threading.Timer(1.5, stop_server)
		shutdown_timer.daemon = True
		shutdown_timer.start()


def run_scan() -> None:
	"""
	## Runs a full scan of all products in the database.
	This function initiates a scan, updates product details based on their latest observations,
	and records observations for each product. It handles retries and ensures that only one URL is fetched
	per second to avoid overwhelming the server.
	"""
	started = utc_now().isoformat()#Record the start time of the scan

	#insert a new row into the scans table with the start time and status
	with connect() as connection:
		scan_id = connection.execute("INSERT INTO scans (started_at, status) VALUES (?, ?)", (started, "running")).lastrowid
		products = connection.execute("SELECT * FROM products ORDER BY id").fetchall() #Retrieve all products from the database
	previous_fetch = 0.0 #initialize a variable to track the time between fetches

	#Iterate over each product in the list
	for product in products:
		#Developer mode always re-fetches so every response gets recorded to the dev database.
		if not DEVELOPER_MODE and product["price"] is not None and cache_is_fresh(product["checked_at"]): #Skip if product is not availiable or the cache is fresh
			continue
		#Wait to ensure a minimum delay between fetches to avoid overwhelming the server
		time.sleep(max(0, MIN_DELAY_SECONDS + random.uniform(0, 2) - (time.monotonic() - previous_fetch)))
		previous_fetch = time.monotonic()
		refresh_product(product)
	#Mark the scan as complete in the database
	with connect() as connection:
		connection.execute("UPDATE scans SET completed_at = ?, status = ? WHERE id = ?", (utc_now().isoformat(), "completed", scan_id))


def refresh_product(product: sqlite3.Row) -> None:
	"""Fetch the latest price for a single product row and record the result/observation."""
	observed_at = utc_now().isoformat() #get the observed at timestamp for the current product

	#Try to fetch the product details from the URl
	try:
		result = fetch_product(product["url"], store_id=product["store_id"])
		status, error = "success", None
	except Exception as fetch_error:  # A single URL must not cancel the scan.
		result, status, error = {}, "error", str(fetch_error)

	#Update the product details in the database with the new data and record the observation
	with connect() as connection:
		if product["store_id"] and (result.get("store_name") or result.get("store_location")):
			connection.execute(
				"UPDATE stores SET name = COALESCE(?, name), location = COALESCE(?, location) WHERE retailer = ? AND store_id = ? AND (name = ? OR location = ?)",
				(result.get("store_name"), result.get("store_location"), product["source"], product["store_id"], f"Store {product['store_id']}", "Location pending"),
			)
		connection.execute("UPDATE products SET title = COALESCE(?, title), price = ?, bulk_price = ?, bulk_quantity = ?, currency = ?, checked_at = ?, error = ? WHERE id = ?", (result.get("title"), result.get("price"), result.get("bulk_price"), result.get("bulk_quantity"), result.get("currency"), observed_at, error, product["id"]))
		connection.execute("INSERT INTO observations (product_id, price, bulk_price, currency, title, status, error, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (product["id"], result.get("price"), result.get("bulk_price"), result.get("currency"), result.get("title"), status, error, observed_at))


def scan_allowed() -> bool:
	"""Check if the user is allowed to query the API's to check prices"""
	if DEVELOPER_MODE:
		return True
	#Connect to the database using the connect function
	with connect() as connection:
		#Execute a SQL query to select the latest completed data
		# The quety orders the scans by their ID in descending order and limits the results to 1
		scan = connection.execute("SELECT completed_at FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()
	#Check if there is no scan or if the scan's completed date is not today
	return not scan or local_calendar_date(scan["completed_at"]) != datetime.now().astimezone().date()


@app.route("/", methods=["GET"])
def index():
	"""Run the program in the user's prefered web browser"""
	#Connect to the database using the connect function
	with connect() as connection:
		#Execute a SQL query to retrieve all products and order them by their ID
		product_rows = connection.execute("SELECT * FROM products ORDER BY id").fetchall()
		store_rows = connection.execute("SELECT * FROM stores ORDER BY retailer, name, store_id").fetchall()
		stores_by_retailer = {}
		for store in store_rows:
			stores_by_retailer.setdefault(store["retailer"], []).append(dict(store))
		products = [] #Empty list to store product views
		for product in product_rows:
			#Execute a SQL query to retrieve the history of observations for the current product
			history = connection.execute(
				"SELECT price, bulk_price, observed_at FROM observations WHERE product_id = ? AND status = 'success' AND price IS NOT NULL ORDER BY observed_at",
				(product["id"],),
			).fetchall()
			product_view = dict(product)#create a dictionary for the product view and populate it with porduct details
			product_view["chart_total"] = len(history)
			if history:
				product_view["chart_min_date"] = datetime.fromisoformat(history[0]["observed_at"]).date().isoformat()
				product_view["chart_end_date"] = datetime.fromisoformat(history[-1]["observed_at"]).date()
				product_view["chart_start_date"] = max(
					datetime.fromisoformat(history[0]["observed_at"]).date(),
					product_view["chart_end_date"] - timedelta(days=DEFAULT_CHART_RANGE_DAYS - 1),
				)
				product_view["chart"] = price_chart(chart_observations_in_range(
					history, product_view["chart_start_date"], product_view["chart_end_date"],
				))
				product_view["chart_start_date"] = product_view["chart_start_date"].isoformat()
				product_view["chart_end_date"] = product_view["chart_end_date"].isoformat()
			else:
				product_view["chart"] = None

			# Create a list of history rows with formatted observed_at UTC
			product_view["history_rows"] = [
				{"price": float(row["price"]), "observed_at_utc": format_observed_at_utc(row["observed_at"])}
				for row in reversed(history)
			]
			products.append(product_view)#Append the product view to the porduct list
		#Retrieve the latest completed scan
		scan = connection.execute("SELECT completed_at FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()
	#extract the last scan's completed date and format it to "YYYY-MM-DD HH:MM"
	last_scan = scan["completed_at"].replace("T", " ")[:16] if scan else None
	with update_info_lock:
		update_available = update_info["available"]
		latest_version = update_info["latest_version"]
	#Render the page.html template with the products, maximum URLs, scan allowed status, last scan, message, CSRF token, API readiness, version number, environment file text, and more.
	return render_template(
		"page.html", 
		products=products, 
		stores_by_retailer=stores_by_retailer, 
		max_urls=MAX_URLS, 
		can_scan=scan_allowed(), 
		last_scan=last_scan, 
		message=request.args.get("message"), 
		csrf_token=session["csrf_token"], 
		serpapi_ready=serpapi_ready(), 
		unwrangle_ready=unwrangle_ready(), 
		api_provider=api_provider(), 
		provider_is_ready=provider_ready(api_provider()), 
		developer_mode=DEVELOPER_MODE, 
		version_number=VERSION_NUMBER, 
		env_file_text=read_env_file_text(), 
		update_available=update_available, 
		latest_version=latest_version, 
		releases_url=check_for_update.get_latest_release_url(),
		api_usage=api_usage_summary(),
	)


@app.get("/dev/responses")
def dev_responses():
	if not DEVELOPER_MODE:
		abort(404)
	with connect_dev() as connection:
		rows = connection.execute(
			"SELECT id, observed_at, url, provider, response_json, error FROM api_responses ORDER BY id DESC LIMIT 100"
		).fetchall()
	records = [dict(row) for row in rows]
	selected_id = request.args.get("response_id", type=int)
	selected = next((item for item in records if item["id"] == selected_id), None)
	pretty_response = None
	if selected and selected["response_json"]:
		try:
			pretty_response = json.dumps(json.loads(selected["response_json"]), indent=2, ensure_ascii=False)
		except json.JSONDecodeError:
			pretty_response = selected["response_json"]
	return render_template(
		"dev_responses.html", records=records, selected=selected,
		pretty_response=pretty_response, replay_result=None, csrf_token=session["csrf_token"],
	)


@app.post("/dev/responses/<int:response_id>/replay")
def replay_dev_response_route(response_id: int):
	if not DEVELOPER_MODE:
		abort(404)
	with connect_dev() as connection:
		rows = connection.execute(
			"SELECT id, observed_at, url, provider, response_json, error FROM api_responses ORDER BY id DESC LIMIT 100"
		).fetchall()
		record = connection.execute(
			"SELECT id, observed_at, url, provider, response_json, error FROM api_responses WHERE id = ?",
			(response_id,),
		).fetchone()
	if record is None:
		abort(404)
	selected = dict(record)
	payload = None
	try:
		payload = json.loads(selected["response_json"] or "null")
		if not isinstance(payload, dict):
			raise ValueError("Saved response is empty or is not a JSON object.")
		replay_result = replay_dev_response(selected["provider"], selected["url"], payload)
	except (ValueError, TypeError) as error:
		replay_result = {"error": str(error), "product": None, "fields": {}}
	pretty_response = json.dumps(payload, indent=2, ensure_ascii=False) if isinstance(payload, dict) else selected["response_json"]
	return render_template(
		"dev_responses.html", records=[dict(row) for row in rows], selected=selected,
		pretty_response=pretty_response, replay_result=replay_result, csrf_token=session["csrf_token"],
	)


@app.get("/chart/<int:product_id>")
def chart_window(product_id: int):
	"""Return a selected time range for an interactive history chart."""
	with connect() as connection:
		history = connection.execute(
			"SELECT price, bulk_price, observed_at FROM observations WHERE product_id = ? AND status = 'success' AND price IS NOT NULL ORDER BY observed_at",
			(product_id,),
		).fetchall()
	if not history:
		abort(404, description="Price history was not found.")
	if request.args.get("all") == "1":
		chart = price_chart(history, width=min(MAX_FULL_CHART_WIDTH, max(360, len(history) * 36)))
		return Response(str(chart), mimetype="image/svg+xml")
	try:
		start_date = date.fromisoformat(request.args["start"])
		end_date = date.fromisoformat(request.args["end"])
	except (KeyError, ValueError):
		abort(400, description="Choose valid chart start and end dates.")
	if start_date > end_date:
		abort(400, description="Chart start date must not be after its end date.")
	chart = price_chart(chart_observations_in_range(history, start_date, end_date))
	if chart is None:
		abort(404, description="No price checks fall within that date range.")
	return Response(str(chart), mimetype="image/svg+xml")


@app.post("/add")
def add_url():
	"""Add a url to the database"""
	try:
		normalized = normalize_url(request.form.get("url", ""))#Normilize the URl
		with connect() as connection: #Connect to the database
			#Check if the number of products in the database exceeds the max limit
			if connection.execute("SELECT COUNT(*) FROM products").fetchone()[0] >= MAX_URLS:
				raise ValueError(f"The watchlist limit is {MAX_URLS} URLs.")
			duplicate_exists = connection.execute("SELECT 1 FROM products WHERE url = ? LIMIT 1", (normalized,)).fetchone()
			if duplicate_exists and request.form.get("allow_duplicate") != "on":
				return redirect(url_for("index", message="That URL is already tracked. Check 'Track another store' to add it again."))
			connection.execute("INSERT INTO products (url, source) VALUES (?, ?)", (normalized, source_for(normalized)))
		#Set message based on whether a duplicate was explicitly allowed
		message = "URL added as another store." if duplicate_exists else "URL added."
	except ValueError as error:
		message = str(error)
	#Redirect to the index page with the message
	return redirect(url_for("index", message=message))


@app.post("/store/<int:product_id>")
def set_store(product_id: int):
	"""Set the store number so you can search a specific store."""
	store_id = request.form.get("store_id", "").strip()
	with connect() as connection:
		product = connection.execute("SELECT source FROM products WHERE id = ?", (product_id,)).fetchone()
		if product is None:
			return redirect(url_for("index", message="Product was not found."))
		if store_id and not connection.execute(
			"SELECT 1 FROM stores WHERE store_id = ? AND retailer = ?", (store_id, product["source"])
		).fetchone():
			return redirect(url_for("index", message="Choose a saved store for this retailer."))
		connection.execute("UPDATE products SET store_id = ? WHERE id = ?", (store_id or None, product_id))
	return redirect(url_for("index", message="Store selection updated."))


@app.post("/stores/add")
def add_store():
	"""Add a store number to the list"""
	retailer = request.form.get("retailer", "").strip()
	store_id = request.form.get("store_id", "").strip()
	name = request.form.get("name", "").strip()
	location = request.form.get("location", "").strip()
	if retailer not in {"Walmart", "Home Depot", "Lowes", "Ace Hardware"}:
		return redirect(url_for("index", message="Choose a supported retailer."))
	if not store_id.isdigit():
		return redirect(url_for("index", message="Store ID must contain digits only."))
	name = name or f"Store {store_id}"
	# Show Location pending until the search. 
	location = location or "Location pending"
	# Then once the search is run, check the search responce for the store name.
	with connect() as connection:
		try:
			connection.execute(
				"INSERT INTO stores (retailer, store_id, name, location) VALUES (?, ?, ?, ?)",
				(retailer, store_id, name, location),
			)
		except sqlite3.IntegrityError:
			return redirect(url_for("index", message="That store is already saved for this retailer."))
	return redirect(url_for("index", message="Store saved."))


@app.post("/remove/<int:product_id>")
def remove_url(product_id: int):
	"""Endpoint to remove a product from the database based on product ID"""
	with connect() as connection:
		connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
	return redirect(url_for("index", message="URL removed."))


@app.post("/recheck/<int:product_id>")
def recheck_url(product_id: int):
	"""Re-fetch a single product's price, ignoring the daily scan limit and freshness cache."""
	with connect() as connection:
		product = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
	if product is None:
		return redirect(url_for("index", message="Product was not found."))
	if not DEVELOPER_MODE and not product["error"]:
		return redirect(url_for("index", message="Re-check is only available for items whose last check failed."))
	refresh_product(product)
	return redirect(url_for("index", message="Re-checked that item."))


@app.post("/scan")
def scan():
	"""Check if a scan is allowed for the user, if so run the scan"""
	if not scan_allowed():
		return redirect(url_for("index", message="Only one completed scan is allowed per day."))
	run_scan()
	return redirect(url_for("index", message="Scan complete."))


@app.get("/export/json")
def export_watchlist_json():
	"""Export SQL database as a json file"""
	content = json.dumps({"products": watchlist_export_rows()}, indent=2)
	return Response(
		content,
		mimetype="application/json",
		headers={"Content-Disposition": "attachment; filename=price-checker-watchlist.json"},
	)


@app.get("/export/csv")
def export_watchlist_csv():
	"""Export SQL database as a csv file"""
	output = io.StringIO(newline="")
	fieldnames = ["id", "product_id", "url", "title", "source", "price", "currency", "checked_at", "error", "history"]
	writer = csv.DictWriter(output, fieldnames=fieldnames)
	writer.writeheader()
	for product in watchlist_export_rows():
		row = {field: product[field] for field in fieldnames if field != "history"}
		row["history"] = json.dumps(product["history"], separators=(",", ":"))
		writer.writerow(row)
	return Response(
		output.getvalue(),
		mimetype="text/csv",
		headers={"Content-Disposition": "attachment; filename=price-checker-watchlist.csv"},
	)


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
	"""Parse a previously-exported, or your own personaly built, watchlist JSON file into a list of product rows."""
	try:
		payload = json.loads(text)
	except json.JSONDecodeError as error:
		raise ValueError(f"That file is not valid JSON: {error}") from error
	products = payload.get("products") if isinstance(payload, dict) else None
	if not isinstance(products, list):
		raise ValueError("That JSON file does not look like a Price Checker export (missing a 'products' list).")
	return products


def parse_watchlist_csv(text: str) -> list[dict[str, object]]:
	"""Parse a previously-exported, or your own personaly built, watchlist CSV file into a list of product rows."""
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
		# Create four tables to hold the users desired information
		connection.executescript("""
			CREATE TABLE products (
				id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT,
				source TEXT NOT NULL, store_id TEXT, price REAL, bulk_price REAL,
				bulk_quantity INTEGER, currency TEXT, checked_at TEXT,
				error TEXT
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
				name TEXT NOT NULL, location TEXT NOT NULL,
				UNIQUE(retailer, store_id)
			);
		""")
		imported = 0
		for product in products:
			url = str(product.get("url") or "").strip()
			source = str(product.get("source") or "").strip() or source_for(url)
			if not url:
				continue #Skip rows missing the one truly required field
			cursor = connection.execute(
				"INSERT INTO products (url, title, source, store_id, price, bulk_price, bulk_quantity, currency, checked_at, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
				(url, product.get("title"), source, product.get("store_id"), product.get("price"),
					product.get("bulk_price"), product.get("bulk_quantity"), product.get("currency"), product.get("checked_at"), product.get("error")),
			)
			product_id = cursor.lastrowid
			history = product.get("history")
			if not isinstance(history, list):
				continue
			for observation in history:
				if not isinstance(observation, dict):
					continue
				if not observation.get("observed_at"):
					continue #observed_at is NOT NULL; skip malformed entries rather than fail the whole import
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


@app.get("/export/db")
def export_watchlist_db():
	"""Export the raw SQLite database file so it can be imported again later, on this machine or another."""
	with connect() as connection:
		connection.execute("PRAGMA wal_checkpoint(FULL)") #Flush any pending writes into the main file before copying it
	return send_file(DATABASE_PATH, as_attachment=True, download_name="price-checker-watchlist.sqlite3", mimetype="application/vnd.sqlite3")


@app.post("/import/db")
def import_watchlist_db():
	"""Replace the active database with an uploaded SQLite/CSV/JSON file, keeping a backup of the previous one."""
	uploaded = request.files.get("database_file")
	if uploaded is None or not uploaded.filename:
		return redirect(url_for("index", message="Choose a database file to import."))
	filename = uploaded.filename.lower()
	temp_path = DATABASE_PATH.with_suffix(".upload.tmp")
	try:
		if filename.endswith((".sqlite3", ".sqlite", ".db")):
			uploaded.save(temp_path)
			validate_watchlist_database(temp_path)
		elif filename.endswith(".json"):
			build_watchlist_database(temp_path, parse_watchlist_json(uploaded.read().decode("utf-8")))
		elif filename.endswith(".csv"):
			build_watchlist_database(temp_path, parse_watchlist_csv(uploaded.read().decode("utf-8")))
		else:
			return redirect(url_for("index", message="Choose a .sqlite3, .sqlite, .db, .csv, or .json file."))
		if DATABASE_PATH.exists():
			shutil.copyfile(DATABASE_PATH, DATABASE_PATH.with_suffix(".backup.sqlite3"))
		os.replace(temp_path, DATABASE_PATH)
	except (ValueError, OSError) as error:
		temp_path.unlink(missing_ok=True)
		return redirect(url_for("index", message=f"Could not import database: {error}"))
	init_db()
	return redirect(url_for("index", message="Database imported. The previous database was backed up."))


@app.post("/shutdown")
def shutdown():
	"""Detect when the user closed the tab and shutdown the rest of the program"""
	if request.remote_addr not in {"127.0.0.1", "::1", "localhost", None}:
		abort(403)
	schedule_shutdown()
	return ("", 204)


@app.post("/settings/provider")
def set_api_provider():
	"""Let the user choose which API provider fetch_product() should prefer."""
	provider = request.form.get("provider", "").strip().lower()
	if provider not in API_PROVIDERS:
		return redirect(url_for("index", message="Choose a valid API provider."))
	content = update_env_setting(read_env_file_text(), "PRICE_CHECKER_API_PROVIDER", provider)
	save_env_file_text(content)
	return redirect(url_for("index", message=f"API provider set to {provider}."))


@app.post("/settings/env")
def save_env_file():
	"""Update the user's .env"""
	try:
		content = request.form.get("env_content", "")
		save_env_file_text(content)
		message = ".env updated successfully."
	except ValueError as error:
		message = str(error)
	return redirect(url_for("index", message=message))


def open_app_browser(host: str, port: int) -> None:
	"""Open a web browser for a specified host and port"""
	if os.environ.get("PRICE_CHECKER_OPEN_BROWSER", "1").lower() in {"0", "false", "no"}:
		return
	url = app_url(host, port)
	threading.Timer(0.8, webbrowser.open, args=(url,), kwargs={"new": 2}).start()


def app_url(host: str, port: int) -> str:
	configured_scheme = os.environ.get("PRICE_CHECKER_URL_SCHEME")
	if configured_scheme:
		scheme = configured_scheme.strip().lower()
	else:
		# Local Flask development does not serve TLS by default, so localhost URLs stay on HTTP.
		scheme = "http" if host in {"127.0.0.1", "localhost"} else "https"
	return f"{scheme}://{host}:{port}"


init_db()
if DEVELOPER_MODE:
	init_dev_db()

if __name__ == "__main__":
	host = "127.0.0.1"
	port = int(os.environ.get("PORT", "5000"))
	server = make_server(host, port, app)
	threading.Thread(target=check_for_update_background, daemon=True).start()
	open_app_browser(host, port)
	server.serve_forever()
	server.server_close()

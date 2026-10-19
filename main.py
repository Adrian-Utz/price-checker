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
	"apify_api_client",
	"providers",
	"charting",
	"watchlist_io",
	"scanner",
	"version",
	"check_for_update",
	"werkzeug.serving",
}

import json
import csv
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
from werkzeug.serving import make_server
from flask import Flask, Response, abort, redirect, render_template, request, send_file, session, url_for

from version import VERSION_NUMBER
from api_clients.serpapi_client import SerpApiClient, product_bulk_price as serpapi_bulk_price, product_candidates, product_price as serpapi_product_price
from api_clients.unwrangle_client import product_bulk_price as unwrangle_bulk_price, product_price as unwrangle_product_price
from utils.charting import chart_observations_in_range, format_observed_at_utc, price_chart
import utils.check_for_update as check_for_update
from utils.watchlist_io import (
	build_watchlist_database,
	parse_watchlist_csv,
	parse_watchlist_json,
	source_for,
	validate_watchlist_database,
	watchlist_export_rows,
)
from utils.database import (
	DATABASE_PATH,
	DEV_DATABASE_PATH,
	connect,
	connect_dev,
	init_db,
	init_dev_db,
)
from utils.scanner import (
	refresh_product as _refresh_product,
	run_scan as _run_scan,
	scan_allowed as _scan_allowed,
)
from utils.providers import (
	API_PROVIDERS,
	ITEM_PROVIDERS,
	apify_ready,
	api_provider,
	fetch_product as _fetch_product,
	provider_ready,
	providers_for_retailer,
	serpapi_ready,
	unwrangle_ready,
)

"""
Main entry point into the program. This is a web application with a python backend. Used to keep track of certian items that the user selects.
Made with Flask.

Last Update: 10/19/2026
Written on: 7/27/2026
Written by: AJ Utz
"""

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE, override=True)
DEVELOPER_MODE = os.environ.get("PRICE_CHECKER_DEVELOPER_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
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

# Sharing variable for group testing with Live Share
auto_server_shutdown = True #This variable turns off the automatic server shutdown whenever a user closes a tab
edit_env = True #This varaible turns off the ability to edit the .env file 


def check_for_update_background() -> None:
	"""Query GitHub for a newer release in a background thread and cache the result for the UI."""
	try:
		available, _local, latest = check_for_update.is_update_available()
	except Exception:
		return
	with update_info_lock:
		update_info["available"] = available
		update_info["latest_version"] = latest


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


def fetch_product(
	url: str,
	store_id: str | None = None,
	zip_state: str | None = None,
	zipcode: str | None = None,
	provider: str | None = None,
) -> dict[str, object]:
	return _fetch_product(
		url,
		store_id=store_id,
		zip_state=zip_state,
		zipcode=zipcode,
		provider=provider,
		mark_serpapi_usage_stale=mark_serpapi_usage_stale,
		update_unwrangle_usage=update_unwrangle_usage,
		record_dev_api_responses=record_dev_api_responses,
	)


def cache_is_fresh(checked_at: str | None) -> bool:
	#check if the checked_at timestamp exists
	if not checked_at:
		return False
	return datetime.fromisoformat(checked_at) > utc_now() - timedelta(hours=CACHE_HOURS)


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
		return "SERPAPI_API_KEY=\nUNWRANGLE_API_KEY=\nAPIFY_API_TOKEN=\n"
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
	if auto_server_shutdown and server is not None:
		server.shutdown()

def schedule_shutdown() -> None:
	global shutdown_timer
	if auto_server_shutdown == False:
		cancel_pending_shutdown()
		return
	cancel_pending_shutdown()
	with shutdown_lock:
		shutdown_timer = threading.Timer(1.5, stop_server)
		shutdown_timer.daemon = True
		shutdown_timer.start()


def run_scan() -> None:
	_run_scan(
		connect=connect,
		utc_now=utc_now,
		developer_mode=DEVELOPER_MODE,
		cache_is_fresh=cache_is_fresh,
		min_delay_seconds=MIN_DELAY_SECONDS,
		refresh_product=refresh_product,
	)


def refresh_product(product: sqlite3.Row) -> None:
	_refresh_product(
		product,
		connect=connect,
		utc_now=utc_now,
		fetch_product=fetch_product,
	)


def scan_allowed() -> bool:
	return _scan_allowed(
		connect=connect,
		developer_mode=DEVELOPER_MODE,
		local_calendar_date=local_calendar_date,
	)


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
		apify_ready=apify_ready(),
		api_provider=api_provider(), 
		provider_is_ready=provider_ready(api_provider()), 
		developer_mode=DEVELOPER_MODE, 
		version_number=VERSION_NUMBER, 
		env_file_text=read_env_file_text() if edit_env else "",
		edit_env=edit_env,
		update_available=update_available, 
		latest_version=latest_version, 
		releases_url=check_for_update.get_latest_release_url(),
		api_usage=api_usage_summary(),
		providers_for_retailer=providers_for_retailer,
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
	"""Replay and show what data is getting pulled from the responce. No API credits are used"""
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


@app.post("/item-provider/<int:product_id>")
def set_item_provider(product_id: int):
	"""Pin one item to a specific API provider, or clear the pin to follow the global provider."""
	provider = request.form.get("provider", "").strip().lower()
	with connect() as connection:
		product = connection.execute("SELECT source FROM products WHERE id = ?", (product_id,)).fetchone()
		if product is None:
			return redirect(url_for("index", message="Product was not found."))
		if provider and (provider not in ITEM_PROVIDERS or provider not in providers_for_retailer(product["source"])):
			return redirect(url_for("index", message=f"That API provider does not support {product['source']}."))
		connection.execute("UPDATE products SET api_provider = ? WHERE id = ?", (provider or None, product_id))
	message = f"This item will always use {provider}." if provider else "This item now follows the global API provider."
	return redirect(url_for("index", message=message))


@app.post("/stores/add")
def add_store():
	"""Add a store number to the list"""
	retailer = request.form.get("retailer", "").strip()
	store_id = request.form.get("store_id", "").strip()
	name = request.form.get("name", "").strip()
	location = request.form.get("location", "").strip()
	zip_state = request.form.get("zip_state", "").strip().upper()
	zipcode = request.form.get("zipcode", "").strip()
	if retailer not in {"Walmart", "Home Depot", "Lowes", "Ace Hardware"}:
		return redirect(url_for("index", message="Choose a supported retailer."))
	if not store_id.isdigit():
		return redirect(url_for("index", message="Store ID must contain digits only."))
	if retailer == "Lowes":
		if not re.fullmatch(r"\d{5}", zipcode):
			return redirect(url_for("index", message="Lowes stores need a 5-digit ZIP code."))
		if not re.fullmatch(r"[A-Z]{2}", zip_state):
			return redirect(url_for("index", message="Lowes stores need a 2-letter state code."))
	name = name or f"Store {store_id}"
	# Show Location pending until the search. 
	location = location or "Location pending"
	# Then once the search is run, check the search responce for the store name.
	with connect() as connection:
		try:
			connection.execute(
				"INSERT INTO stores (retailer, store_id, name, location, zip_state, zipcode) VALUES (?, ?, ?, ?, ?, ?)",
				(retailer, store_id, name, location, zip_state or None, zipcode or None),
			)
		except sqlite3.IntegrityError:
			if retailer == "Lowes":
				connection.execute(
					"UPDATE stores SET zip_state = ?, zipcode = ? WHERE retailer = ? AND store_id = ?",
					(zip_state, zipcode, retailer, store_id),
				)
				return redirect(url_for("index", message="Lowes store ZIP code/state updated."))
			return redirect(url_for("index", message="That store is already saved for this retailer."))
	return redirect(url_for("index", message="Store saved."))


@app.post("/stores/<int:store_record_id>/remove")
def remove_store(store_record_id: int):
	"""Remove a saved store and reset products assigned to it to all stores."""
	with connect() as connection:
		store = connection.execute(
			"SELECT retailer, store_id FROM stores WHERE id = ?", (store_record_id,)
		).fetchone()
		if store is None:
			return redirect(url_for("index", message="Saved store was not found."))
		connection.execute(
			"UPDATE products SET store_id = NULL WHERE source = ? AND store_id = ?",
			(store["retailer"], store["store_id"]),
		)
		connection.execute("DELETE FROM stores WHERE id = ?", (store_record_id,))
	return redirect(url_for("index", message="Saved store removed; assigned products now use all stores."))


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


@app.post("/recheck-selected")
def recheck_selected():
	"""Re-fetch only the checked products, ignoring the daily scan limit and freshness cache."""
	selected_ids = sorted({int(value) for value in request.form.getlist("product_ids") if value.isdigit()})
	if not selected_ids:
		return redirect(url_for("index", message="Select at least one item to re-check."))
	placeholders = ",".join("?" * len(selected_ids))
	with connect() as connection:
		products = connection.execute(f"SELECT * FROM products WHERE id IN ({placeholders}) ORDER BY id", selected_ids).fetchall()
	previous_fetch = 0.0
	for product in products:
		#Keep the same pacing as a full scan so a large selection doesn't hammer the APIs
		if previous_fetch:
			time.sleep(max(0, MIN_DELAY_SECONDS + random.uniform(0, 2) - (time.monotonic() - previous_fetch)))
		previous_fetch = time.monotonic()
		refresh_product(product)
	return redirect(url_for("index", message=f"Re-checked {len(products)} selected item{'s' if len(products) != 1 else ''}."))


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
	with connect() as connection:
		products = watchlist_export_rows(connection)
	content = json.dumps({"products": products}, indent=2)
	return Response(
		content,
		mimetype="application/json",
		headers={"Content-Disposition": "attachment; filename=price-checker-watchlist.json"},
	)


@app.get("/export/csv")
def export_watchlist_csv():
	"""Export SQL database as a csv file"""
	with connect() as connection:
		products = watchlist_export_rows(connection)
	output = io.StringIO(newline="")
	fieldnames = ["id", "product_id", "url", "title", "source", "price", "bulk_price", "bulk_quantity", "currency", "checked_at", "error", "api_provider", "history"]
	writer = csv.DictWriter(output, fieldnames=fieldnames)
	writer.writeheader()
	for product in products:
		row = {field: product[field] for field in fieldnames if field != "history"}
		row["history"] = json.dumps(product["history"], separators=(",", ":"))
		writer.writerow(row)
	return Response(
		output.getvalue(),
		mimetype="text/csv",
		headers={"Content-Disposition": "attachment; filename=price-checker-watchlist.csv"},
	)


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
	if not edit_env:
		abort(403, description="Editing the .env file is disabled.")
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

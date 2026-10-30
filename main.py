from __future__ import annotations

# Python 3.15+ defers these imports; older versions ignore this allowlist.
__lazy_modules__ = {
	"json",
	"csv",
	"io",
	"math",
	"random",
	"re",
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
}

import json
import csv
import io
import math
import os
import random
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import unquote, urlparse, urlsplit
from flask import Flask, Response, abort, g, redirect, render_template, request, send_file, session, url_for

from version import VERSION_NUMBER
from api_clients.serpapi_client import SerpApiClient, product_bulk_price as serpapi_bulk_price, product_candidates, product_price as serpapi_product_price
from api_clients.unwrangle_client import product_bulk_price as unwrangle_bulk_price, product_price as unwrangle_product_price
from utils.charting import chart_observations_in_range, format_observed_at_utc, price_chart
import utils.check_for_update as check_for_update
from utils.watchlist_io import (
	merge_watchlist,
	parse_watchlist_csv,
	parse_watchlist_json,
	read_watchlist_database,
	source_for,
	watchlist_export_rows,
)
from utils.database import (
	DATABASE_PATH,
	DEV_DATABASE_PATH,
	admin_exists,
	connect,
	connect_dev,
	init_db,
	init_dev_db,
)
from utils.config import ConfigError, create_secret_file, load_settings, startup_errors
from utils.migrations import LOCAL_USERNAME, UNUSABLE_PASSWORD_HASH
from utils.serving import ensure_tls_files, serve, start_scheduler
from utils.users import claim_local_data, create_user, get_user, local_user_id, real_user_count
from utils import auth
from utils.limits import BudgetExceeded, calls_today, reserve_api_call
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
try:
	SETTINGS = load_settings()
except ConfigError as error:
	raise SystemExit(f"Configuration error: {error}")
DEVELOPER_MODE = SETTINGS.allow_dev_tools
MAX_URLS = max(20, min(int(os.environ.get("PRICE_CHECKER_MAX_URLS", "100")), 100)) #Change this variable if you want to track more items.
CACHE_HOURS = max(1, int(os.environ.get("PRICE_CHECKER_CACHE_HOURS", "24")))
MIN_DELAY_SECONDS = max(1.0, float(os.environ.get("PRICE_CHECKER_MIN_DELAY", "4")))
MAX_FULL_CHART_WIDTH = 2400
DEFAULT_CHART_RANGE_DAYS = 30
API_USAGE_CACHE_SECONDS = 300
REGISTER_IP_LIMIT = 10 #Sign-up attempts allowed per address per hour
REGISTER_WINDOW_SECONDS = 3600
app = Flask(__name__)
app.secret_key = SETTINGS.secret or secrets.token_hex(32) #Only local mode may fall back to a per-run secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SETTINGS.secure_cookies
# The __Host- prefix makes browsers refuse the cookie unless it is Secure, host-only and path "/".
app.config["SESSION_COOKIE_NAME"] = "__Host-session" if SETTINGS.secure_cookies else "session"
app.config["CSRF_ENABLED"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=max(SETTINGS.remember_days, 1))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024 #Cap uploaded database files at 64 MB
server = None
shutdown_lock = threading.Lock()
shutdown_timer = None
update_info = {"available": False, "latest_version": None}
update_info_lock = threading.Lock()
api_usage_lock = threading.Lock()
user_scan_locks: dict[int, threading.Lock] = {}
user_scan_locks_guard = threading.Lock()
api_usage = {
	"serpapi_remaining": None,
	"serpapi_monthly_limit": None,
	"serpapi_monthly_used": None,
	"unwrangle_remaining": None,
	"unwrangle_starting_credits": None,
}
serpapi_usage_checked_at = 0.0

# Sharing variable for group testing with Live Share
auto_server_shutdown = SETTINGS.auto_shutdown #This variable turns off the automatic server shutdown whenever a user closes a tab
edit_env = SETTINGS.allow_env_editor #This varaible turns off the ability to edit the .env file 


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
	"""Locate the requested json value. Used in the dev responce replayer."""
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


def host_allowed(host: str) -> bool:
	"""Match the Host header (port stripped) against PRICE_CHECKER_ALLOWED_HOSTS entries: exact names or '*.domain'."""
	name = host.lower()
	name = name[: name.rindex("]") + 1] if name.startswith("[") and "]" in name else name.split(":", 1)[0]
	for allowed in SETTINGS.allowed_hosts:
		allowed = allowed[: allowed.rindex("]") + 1] if allowed.startswith("[") and "]" in allowed else allowed.split(":", 1)[0]
		if name == allowed or (allowed.startswith("*.") and name.endswith(allowed[1:]) and len(name) > len(allowed) - 1):
			return True
	return False


@app.before_request
def check_host_header() -> None:
	"""Reject unexpected Host headers (DNS rebinding, host-header poisoning). LAN mode checks only when hosts are configured."""
	if SETTINGS.allowed_hosts and request.endpoint != "healthz" and not host_allowed(request.host):
		abort(400, description="Unexpected host name.")
	g.csp_nonce = secrets.token_urlsafe(16)


@app.context_processor
def inject_security_context() -> dict:
	return {
		"csp_nonce": g.get("csp_nonce", ""),
		"auto_shutdown": SETTINGS.is_local and auto_server_shutdown,
	}


@app.after_request
def add_security_headers(response):
	nonce = g.get("csp_nonce", "")
	response.headers["Content-Security-Policy"] = (
		f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
		"img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
	)
	response.headers["X-Content-Type-Options"] = "nosniff"
	response.headers["X-Frame-Options"] = "DENY"
	response.headers["Referrer-Policy"] = "same-origin" #Invite tokens travel in URLs
	response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
	if SETTINGS.secure_cookies and request.is_secure:
		response.headers["Strict-Transport-Security"] = "max-age=31536000"
	if not SETTINGS.is_local and request.endpoint != "static":
		response.headers["Cache-Control"] = "no-store"
	return response


def api_budget_view(user_id: int) -> dict | None:
	if not SETTINGS.user_daily_api_calls:
		return None
	with connect() as connection:
		return {"used": calls_today(connection, user_id), "limit": SETTINGS.user_daily_api_calls}


def recheck_limited():
	"""Per-user cap on re-check requests per hour. Returns a redirect when the user is over it, else None."""
	limit = SETTINGS.rechecks_per_hour
	if not limit:
		return None
	identity = str(current_user_id())
	with connect() as connection:
		wait = auth.retry_after_seconds(connection, identity, "recheck", limit, 3600)
		if wait:
			minutes = max(1, -(-wait // 60))
			return redirect(url_for("index", message=f"Too many re-checks. Try again in about {minutes} minute{'s' if minutes != 1 else ''}."))
		auth.record_attempt(connection, identity, "recheck")
	return None


def same_origin(request_origin: str) -> bool:
	return urlsplit(request_origin).netloc.lower() == request.host.lower()


@app.before_request
def protect_post_forms() -> None:
	"""
	## Protects every POST by checking for a valid CSRF token
	Cancels any pending shutdown, makes sure the session has a token, then requires POSTs to carry it
	(form field or X-CSRF-Token header) and to come from this site (Origin / Sec-Fetch-Site).
	"""
	cancel_pending_shutdown()

	#Passing 32 to token_urlsafe generates 32 random bytes (256 bits of entropy).
	if "csrf_token" not in session:
		session["csrf_token"] = secrets.token_urlsafe(32)

	if request.method != "POST" or not app.config["CSRF_ENABLED"]:
		return
	if request.headers.get("Sec-Fetch-Site") == "cross-site":
		abort(400, description="Cross-site request blocked.")
	if request.endpoint == "shutdown":
		return #sendBeacon can't carry a token; the endpoint is local-only and cross-site is rejected above
	origin = request.headers.get("Origin")
	if origin and origin != "null" and not same_origin(origin):
		abort(400, description="Cross-origin request blocked.")
	submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
	if not secrets.compare_digest(submitted, session["csrf_token"]):
		abort(400, description="Invalid form token.")


PUBLIC_ENDPOINTS = {"login", "register", "static", "healthz"}


@app.get("/healthz")
def healthz():
	"""Liveness/readiness probe for Docker and reverse proxies. Reveals nothing beyond up/down."""
	try:
		with connect() as connection:
			connection.execute("SELECT 1").fetchone()
	except sqlite3.Error:
		return ("database unavailable", 503, {"Content-Type": "text/plain"})
	return ("ok", 200, {"Content-Type": "text/plain"})


@app.before_request
def require_login():
	"""Outside local mode every page except sign-in and registration needs a logged-in session."""
	if SETTINGS.is_local or request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
		return None
	if signed_in_user() is None:
		abort(401)
	return None


@app.errorhandler(401)
def not_signed_in(error):
	next_url = request.full_path.rstrip("?") if request.method == "GET" else None
	return redirect(url_for("login", next=next_url) if next_url and next_url != "/" else url_for("login"))

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


def run_scan(user_id: int) -> bool:
	return _run_scan(
		user_id=user_id,
		connect=connect,
		utc_now=utc_now,
		developer_mode=DEVELOPER_MODE,
		cache_is_fresh=cache_is_fresh,
		min_delay_seconds=MIN_DELAY_SECONDS,
		refresh_product=refresh_product,
	)


def refresh_product(product: sqlite3.Row) -> None:
	# Reserve the credit before fetching: a failed fetch would otherwise overwrite the stored price with an error.
	with connect() as connection:
		reserve_api_call(
			connection, product["user_id"],
			user_limit=SETTINGS.user_daily_api_calls, global_limit=SETTINGS.global_daily_api_calls,
		)
	_refresh_product(
		product,
		connect=connect,
		utc_now=utc_now,
		fetch_product=fetch_product,
	)


def scan_allowed(user_id: int) -> bool:
	return _scan_allowed(
		user_id=user_id,
		connect=connect,
		developer_mode=DEVELOPER_MODE,
		local_calendar_date=local_calendar_date,
	)


def scan_lock_for(user_id: int) -> threading.Lock:
	"""One lock per user: a user can't run two scans (and spend API credits twice) at once, but users don't block each other."""
	with user_scan_locks_guard:
		return user_scan_locks.setdefault(user_id, threading.Lock())


def session_expired() -> bool:
	"""Remembered sessions last a fixed number of days; ordinary ones end after a period of inactivity."""
	now = time.time()
	if session.get("remember"):
		return now - session.get("issued", 0) > SETTINGS.remember_days * 86400
	return now - session.get("last_seen", 0) > SETTINGS.session_idle_minutes * 60


def signed_in_user() -> sqlite3.Row | None:
	"""The logged-in user, or None. A session dies on timeout, password reset, or when the account is disabled."""
	if "current_user" in g:
		return g.current_user
	with connect() as connection:
		if SETTINGS.is_local:
			user = get_user(connection, local_user_id(connection))
		else:
			session_user_id = session.get("user_id")
			user = get_user(connection, session_user_id) if isinstance(session_user_id, int) else None
			if user is not None and (session_expired() or session.get("sv") != user["session_version"]):
				user = None
			if user is None and "user_id" in session:
				end_session()
			elif user is not None:
				session["last_seen"] = int(time.time())
	if user is None or user["disabled"]:
		return None
	g.current_user = user
	return user


def current_user() -> sqlite3.Row:
	"""Return the signed-in user. Local mode always acts as the implicit local user; other modes need a session login."""
	user = signed_in_user()
	if user is None:
		abort(401)
	return user


def current_user_id() -> int:
	return current_user()["id"]


def is_admin() -> bool:
	return current_user()["role"] == "admin"


def require_admin() -> None:
	if not is_admin():
		abort(403, description="Only an administrator can do that.")


def owned_product(connection: sqlite3.Connection, product_id: int) -> sqlite3.Row:
	"""Fetch a product only if the current user owns it. Missing and not-yours look identical (404)."""
	product = connection.execute(
		"SELECT * FROM products WHERE id = ? AND user_id = ?", (product_id, current_user_id())
	).fetchone()
	if product is None:
		abort(404, description="Product was not found.")
	return product


def require_accounts() -> None:
	"""Login, registration and user management only exist when accounts are enabled."""
	if SETTINGS.is_local:
		abort(404)


def client_ip() -> str:
	return request.remote_addr or "unknown"


def safe_next_url(target: str | None) -> str:
	"""Only follow same-site relative paths after login, never an external address."""
	if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
		parsed = urlparse(target)
		if not parsed.scheme and not parsed.netloc:
			return target
	return url_for("index")


def end_session() -> None:
	session.clear()
	session["csrf_token"] = secrets.token_urlsafe(32)


def start_session(user: sqlite3.Row, remember: bool) -> None:
	"""Begin a fresh session (and a new CSRF token) so nothing from before the login carries over."""
	remember = remember and SETTINGS.remember_days > 0
	end_session()
	now = int(time.time())
	session["user_id"] = user["id"]
	session["sv"] = user["session_version"]
	session["issued"] = now
	session["last_seen"] = now
	session["remember"] = remember
	session.permanent = remember


def rate_limited_response(template: str, retry_after: int, **context):
	response = Response(render_template(template, csrf_token=session["csrf_token"], error="Too many attempts. Try again later.", **context), status=429)
	if SETTINGS.retry_after_header:
		response.headers["Retry-After"] = str(retry_after)
	return response


@app.route("/login", methods=["GET", "POST"])
def login():
	require_accounts()
	next_url = request.values.get("next", "")
	context = {"next_url": next_url, "message": request.args.get("message"), "registration": SETTINGS.registration, "remember_enabled": SETTINGS.remember_days > 0}
	if request.method == "GET":
		if signed_in_user() is not None:
			return redirect(safe_next_url(next_url))
		return render_template("login.html", csrf_token=session["csrf_token"], error=None, **context)
	ip = client_ip()
	with connect() as connection:
		wait = auth.retry_after_seconds(connection, ip, "login", SETTINGS.login_ip_limit)
	if wait:
		return rate_limited_response("login.html", wait, **context)
	with connect() as connection:
		user = auth.authenticate(
			connection, request.form.get("username", ""), request.form.get("password", ""),
			max_failures=SETTINGS.login_max_failures, lockout_minutes=SETTINGS.login_lockout_minutes,
		)
		if user is None:
			auth.record_attempt(connection, ip, "login")
	if user is None:
		error = "Invalid username or password, or the account is temporarily locked."
		return render_template("login.html", csrf_token=session["csrf_token"], error=error, **context), 401
	start_session(user, request.form.get("remember") == "on")
	return redirect(safe_next_url(next_url))


@app.post("/logout")
def logout():
	require_accounts()
	end_session()
	return redirect(url_for("login", message="You have been signed out."))


@app.route("/register", methods=["GET", "POST"])
def register():
	require_accounts()
	if SETTINGS.registration == "admin":
		abort(404)
	needs_invite = SETTINGS.registration == "invite"
	token = request.values.get("invite", "") if needs_invite else ""
	context = {"invite": token, "needs_invite": needs_invite}
	if request.method == "GET":
		with connect() as connection:
			invite_ok = not needs_invite or auth.find_invite(connection, token) is not None
		error = None if invite_ok else "This invite link is invalid or has expired."
		return render_template("register.html", csrf_token=session["csrf_token"], error=error, form_enabled=invite_ok, **context), (200 if invite_ok else 404)
	ip = client_ip()
	with connect() as connection:
		wait = auth.retry_after_seconds(connection, ip, "register", REGISTER_IP_LIMIT, REGISTER_WINDOW_SECONDS)
		if not wait:
			auth.record_attempt(connection, ip, "register")
	if wait:
		return rate_limited_response("register.html", wait, form_enabled=True, **context)
	username = request.form.get("username", "").strip()
	password = request.form.get("password", "")
	error = None
	if password != request.form.get("confirm_password", ""):
		error = "The passwords don't match."
	else:
		try:
			with connect() as connection:
				if needs_invite:
					auth.accept_invite(connection, token, username, password)
				else:
					create_user(connection, username, password, "user")
		except ValueError as problem:
			error = str(problem)
	if error:
		return render_template("register.html", csrf_token=session["csrf_token"], error=error, form_enabled=True, **context), 400
	return redirect(url_for("login", message="Account created. You can sign in now."))


@app.get("/account")
def account():
	require_accounts()
	user = current_user()
	return render_template("account.html", csrf_token=session["csrf_token"], username=user["username"], is_admin=is_admin(), message=request.args.get("message"), error=None)


@app.post("/account/password")
def change_own_password():
	require_accounts()
	user = current_user()
	new_password = request.form.get("new_password", "")
	error = None
	if new_password != request.form.get("confirm_password", ""):
		error = "The new passwords don't match."
	else:
		try:
			with connect() as connection:
				verified = auth.authenticate(
					connection, user["username"], request.form.get("current_password", ""),
					max_failures=SETTINGS.login_max_failures, lockout_minutes=SETTINGS.login_lockout_minutes,
				)
				if verified is None:
					error = "The current password is incorrect."
				else:
					auth.set_password(connection, user["id"], new_password)
					updated = get_user(connection, user["id"])
		except ValueError as problem:
			error = str(problem)
	if error:
		return render_template("account.html", csrf_token=session["csrf_token"], username=user["username"], is_admin=is_admin(), message=None, error=error), 400
	session["sv"] = updated["session_version"] #Keep this session, sign every other one out
	return redirect(url_for("account", message="Password changed. Other devices were signed out."))


def render_admin_users(message: str | None = None, error: str | None = None, invite_url: str | None = None, status: int = 200):
	with connect() as connection:
		users = auth.list_users(connection)
		invites = auth.list_invites(connection) if SETTINGS.registration == "invite" else []
	return render_template(
		"admin_users.html", csrf_token=session["csrf_token"], users=users, invites=invites, invite_url=invite_url,
		registration=SETTINGS.registration, current_user_id=current_user_id(), message=message, error=error,
		max_urls=MAX_URLS, now=auth.utc_text(),
	), status


def admin_action(action, success_message: str):
	"""Run an admin change; a refused change (bad input, self-edit) is reported instead of crashing."""
	require_accounts()
	require_admin()
	try:
		with connect() as connection:
			action(connection)
	except ValueError as problem:
		return render_admin_users(error=str(problem), status=400)
	return redirect(url_for("admin_users", message=success_message))


@app.get("/admin/users")
def admin_users():
	require_accounts()
	require_admin()
	return render_admin_users(message=request.args.get("message"))


@app.post("/admin/users/create")
def admin_create_user():
	return admin_action(
		lambda connection: create_user(
			connection, request.form.get("username", ""), request.form.get("password", ""),
			request.form.get("role", "user"),
		),
		"Account created.",
	)


@app.post("/admin/users/<int:user_id>/role")
def admin_set_role(user_id: int):
	return admin_action(
		lambda connection: auth.set_role(connection, current_user_id(), user_id, request.form.get("role", "")),
		"Role updated.",
	)


@app.post("/admin/users/<int:user_id>/status")
def admin_set_status(user_id: int):
	disable = request.form.get("action") == "disable"
	return admin_action(
		lambda connection: auth.set_disabled(connection, current_user_id(), user_id, disable),
		"Account disabled and signed out." if disable else "Account enabled.",
	)


@app.post("/admin/users/<int:user_id>/password")
def admin_reset_password(user_id: int):
	return admin_action(
		lambda connection: auth.set_password(connection, user_id, request.form.get("password", "")),
		"Password reset. The user was signed out everywhere.",
	)


@app.post("/admin/invites/create")
def admin_create_invite():
	require_accounts()
	require_admin()
	if SETTINGS.registration != "invite":
		abort(404)
	try:
		hours = int(request.form.get("hours", "72"))
		with connect() as connection:
			token = auth.create_invite(connection, current_user_id(), hours)
	except ValueError as problem:
		return render_admin_users(error=str(problem), status=400)
	return render_admin_users(invite_url=url_for("register", invite=token, _external=True))


@app.post("/admin/invites/<int:invite_id>/revoke")
def admin_revoke_invite(invite_id: int):
	return admin_action(lambda connection: auth.revoke_invite(connection, invite_id), "Invite revoked.")


@app.route("/", methods=["GET"])
def index():
	"""Run the program in the user's prefered web browser"""
	user_id = current_user_id()
	#Connect to the database using the connect function
	with connect() as connection:
		#Execute a SQL query to retrieve this user's products and order them by their ID
		product_rows = connection.execute("SELECT * FROM products WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
		store_rows = connection.execute("SELECT * FROM stores WHERE user_id = ? ORDER BY retailer, name, store_id", (user_id,)).fetchall()
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
		#Retrieve this user's latest completed scan
		scan = connection.execute(
			"SELECT completed_at FROM scans WHERE user_id = ? AND status = 'completed' ORDER BY id DESC LIMIT 1", (user_id,)
		).fetchone()
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
		can_scan=scan_allowed(user_id), 
		last_scan=last_scan, 
		message=request.args.get("message"), 
		csrf_token=session["csrf_token"], 
		is_admin=is_admin(),
		current_username=current_user()["username"],
		accounts_enabled=not SETTINGS.is_local,
		api_budget=api_budget_view(user_id),
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
		api_usage=api_usage_summary() if is_admin() else None,
		allow_database_files=SETTINGS.is_local,
		providers_for_retailer=providers_for_retailer,
	)


@app.get("/dev/responses")
def dev_responses():
	if not DEVELOPER_MODE or not SETTINGS.is_local:
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
	if not DEVELOPER_MODE or not SETTINGS.is_local:
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
		owned_product(connection, product_id)
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
	user_id = current_user_id()
	try:
		normalized = normalize_url(request.form.get("url", ""))#Normilize the URl
		with connect() as connection: #Connect to the database
			#Check if this user's number of products exceeds the max limit
			if connection.execute("SELECT COUNT(*) FROM products WHERE user_id = ?", (user_id,)).fetchone()[0] >= MAX_URLS:
				raise ValueError(f"The watchlist limit is {MAX_URLS} URLs.")
			duplicate_exists = connection.execute("SELECT 1 FROM products WHERE user_id = ? AND url = ? LIMIT 1", (user_id, normalized)).fetchone()
			if duplicate_exists and request.form.get("allow_duplicate") != "on":
				return redirect(url_for("index", message="That URL is already tracked. Check 'Track another store' to add it again."))
			connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, ?, ?)", (user_id, normalized, source_for(normalized)))
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
		product = owned_product(connection, product_id)
		if store_id and not connection.execute(
			"SELECT 1 FROM stores WHERE user_id = ? AND store_id = ? AND retailer = ?", (current_user_id(), store_id, product["source"])
		).fetchone():
			return redirect(url_for("index", message="Choose a saved store for this retailer."))
		connection.execute("UPDATE products SET store_id = ? WHERE id = ? AND user_id = ?", (store_id or None, product_id, current_user_id()))
	return redirect(url_for("index", message="Store selection updated."))


@app.post("/item-provider/<int:product_id>")
def set_item_provider(product_id: int):
	"""Pin one item to a specific API provider, or clear the pin to follow the global provider."""
	provider = request.form.get("provider", "").strip().lower()
	with connect() as connection:
		product = owned_product(connection, product_id)
		if provider and (provider not in ITEM_PROVIDERS or provider not in providers_for_retailer(product["source"])):
			return redirect(url_for("index", message=f"That API provider does not support {product['source']}."))
		connection.execute("UPDATE products SET api_provider = ? WHERE id = ? AND user_id = ?", (provider or None, product_id, current_user_id()))
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
	user_id = current_user_id()
	with connect() as connection:
		try:
			connection.execute(
				"INSERT INTO stores (user_id, retailer, store_id, name, location, zip_state, zipcode) VALUES (?, ?, ?, ?, ?, ?, ?)",
				(user_id, retailer, store_id, name, location, zip_state or None, zipcode or None),
			)
		except sqlite3.IntegrityError:
			if retailer == "Lowes":
				connection.execute(
					"UPDATE stores SET zip_state = ?, zipcode = ? WHERE user_id = ? AND retailer = ? AND store_id = ?",
					(zip_state, zipcode, user_id, retailer, store_id),
				)
				return redirect(url_for("index", message="Lowes store ZIP code/state updated."))
			return redirect(url_for("index", message="That store is already saved for this retailer."))
	return redirect(url_for("index", message="Store saved."))


@app.post("/stores/<int:store_record_id>/remove")
def remove_store(store_record_id: int):
	"""Remove a saved store and reset products assigned to it to all stores."""
	user_id = current_user_id()
	with connect() as connection:
		store = connection.execute(
			"SELECT retailer, store_id FROM stores WHERE id = ? AND user_id = ?", (store_record_id, user_id)
		).fetchone()
		if store is None:
			abort(404, description="Saved store was not found.")
		connection.execute(
			"UPDATE products SET store_id = NULL WHERE user_id = ? AND source = ? AND store_id = ?",
			(user_id, store["retailer"], store["store_id"]),
		)
		connection.execute("DELETE FROM stores WHERE id = ? AND user_id = ?", (store_record_id, user_id))
	return redirect(url_for("index", message="Saved store removed; assigned products now use all stores."))


@app.post("/remove/<int:product_id>")
def remove_url(product_id: int):
	"""Endpoint to remove a product from the database based on product ID"""
	with connect() as connection:
		owned_product(connection, product_id)
		connection.execute("DELETE FROM products WHERE id = ? AND user_id = ?", (product_id, current_user_id()))
	return redirect(url_for("index", message="URL removed."))


@app.post("/recheck/<int:product_id>")
def recheck_url(product_id: int):
	"""Re-fetch a single product's price, ignoring the daily scan limit and freshness cache."""
	with connect() as connection:
		product = owned_product(connection, product_id)

	if not DEVELOPER_MODE and not product["error"]:
		return redirect(url_for("index", message="Re-check is only available for items whose last check failed."))

	limited = recheck_limited()
	if limited:
		return limited
	try:
		refresh_product(product)
	except BudgetExceeded as error:
		return redirect(url_for("index", message=str(error)))

	return redirect(url_for("index", message="Re-checked that item."))


@app.post("/recheck-selected")
def recheck_selected():
	"""Re-fetch only the checked products, ignoring the daily scan limit and freshness cache."""
	user_id = current_user_id()
	selected_ids = sorted({int(value) for value in request.form.getlist("product_ids") if value.isdigit()})

	if not selected_ids:
		return redirect(url_for("index", message="Select at least one item to re-check."))

	placeholders = ",".join("?" * len(selected_ids))

	limited = recheck_limited()
	if limited:
		return limited
	lock = scan_lock_for(user_id)
	if not lock.acquire(blocking=False):
		return redirect(url_for("index", message="A check is already running for your account."))
	try:
		with connect() as connection:
			products = connection.execute(
				f"SELECT * FROM products WHERE user_id = ? AND id IN ({placeholders}) ORDER BY id", [user_id, *selected_ids]
			).fetchall()

		previous_fetch = 0.0
		checked = 0
		budget_message = None

		for product in products:
			#Keep the same pacing as a full scan so a large selection doesn't hammer the APIs
			if previous_fetch:
				time.sleep(max(0, MIN_DELAY_SECONDS + random.uniform(0, 2) - (time.monotonic() - previous_fetch)))
			previous_fetch = time.monotonic()
			try:
				refresh_product(product)
			except BudgetExceeded as error:
				budget_message = str(error)
				break
			checked += 1
	finally:
		lock.release()
	message = f"Re-checked {checked} selected item{'s' if checked != 1 else ''}."
	if budget_message:
		message = f"{message} {budget_message}"
	return redirect(url_for("index", message=message))


@app.post("/scan")
def scan():
	"""Check if a scan is allowed for the user, if so run the scan"""
	user_id = current_user_id()
	lock = scan_lock_for(user_id)
	if not lock.acquire(blocking=False):
		return redirect(url_for("index", message="A scan is already running for your account."))
	try:
		if not scan_allowed(user_id):
			return redirect(url_for("index", message="Only one completed scan is allowed per day."))
		finished = run_scan(user_id)
	finally:
		lock.release()

	if finished is False:
		return redirect(url_for("index", message="Scan stopped: the daily API budget ran out. It resets at midnight UTC."))
	return redirect(url_for("index", message="Scan complete."))


@app.get("/export/json")
def export_watchlist_json():
	"""Export the current user's watchlist as a json file"""
	with connect() as connection:
		products = watchlist_export_rows(connection, current_user_id())

	content = json.dumps({"products": products}, indent=2)

	return Response(
		content,
		mimetype="application/json",
		headers={"Content-Disposition": "attachment; filename=price-checker-watchlist.json"},
	)


@app.get("/export/csv")
def export_watchlist_csv():
	"""Export the current user's watchlist as a csv file"""
	with connect() as connection:
		products = watchlist_export_rows(connection, current_user_id())

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
	require_whole_database_access()
	with connect() as connection:
		connection.execute("PRAGMA wal_checkpoint(FULL)") #Flush any pending writes into the main file before copying it
	return send_file(DATABASE_PATH, as_attachment=True, download_name="price-checker-watchlist.sqlite3", mimetype="application/vnd.sqlite3")


@app.post("/import")
def import_watchlist():
	"""Merge an uploaded SQLite/CSV/JSON watchlist into the current user's data. Nothing is replaced or deleted."""
	user_id = current_user_id()
	uploaded = request.files.get("database_file")

	if uploaded is None or not uploaded.filename:
		return redirect(url_for("index", message="Choose a file to import."))

	filename = uploaded.filename.lower()

	try:
		if filename.endswith((".sqlite3", ".sqlite", ".db")):
			with tempfile.TemporaryDirectory(prefix="price-checker-import-") as scratch:
				upload_path = Path(scratch) / "upload.sqlite3"
				uploaded.save(upload_path)
				products = read_watchlist_database(upload_path)
		elif filename.endswith(".json"):
			products = parse_watchlist_json(uploaded.read().decode("utf-8-sig"))
		elif filename.endswith(".csv"):
			products = parse_watchlist_csv(uploaded.read().decode("utf-8-sig"))
		else:
			return redirect(url_for("index", message="Choose a .sqlite3, .sqlite, .db, .csv, or .json file."))
		with connect() as connection:
			result = merge_watchlist(connection, user_id, products, max_products=MAX_URLS, normalize_url=normalize_url)
	except (ValueError, OSError) as error:
		return redirect(url_for("index", message=f"Could not import: {error}"))

	message = f"Imported {result['added']} new and merged {result['merged']} existing products ({result['observations']} price records added)."
	if result["over_limit"]:
		message += f" {result['over_limit']} skipped because the watchlist limit is {MAX_URLS}."
	if result["skipped"]:
		message += f" {result['skipped']} skipped because they had no valid URL."
	return redirect(url_for("index", message=message))


@app.post("/shutdown")
def shutdown():
	"""Detect when the user closed the tab and shutdown the rest of the program"""
	if not SETTINGS.is_local:
		abort(404)
	if request.remote_addr not in {"127.0.0.1", "::1", "localhost", None}:
		abort(403)
	schedule_shutdown()
	return ("", 204)


def require_whole_database_access() -> None:
	"""The raw database file holds every user's data and password hashes, so it is only offered in local mode,
	where the one implicit user owns it all. Other modes use the per-user JSON/CSV export."""
	current_user_id()
	if not SETTINGS.is_local:
		abort(403, description="Whole-database export is disabled when accounts are enabled.")


@app.post("/settings/provider")
def set_api_provider():
	"""Let the admin choose which API provider fetch_product() should prefer."""
	require_admin()
	provider = request.form.get("provider", "").strip().lower()
	if provider not in API_PROVIDERS:
		return redirect(url_for("index", message="Choose a valid API provider."))
	content = update_env_setting(read_env_file_text(), "PRICE_CHECKER_API_PROVIDER", provider)
	save_env_file_text(content)
	return redirect(url_for("index", message=f"API provider set to {provider}."))


@app.post("/settings/env")
def save_env_file():
	"""Update the user's .env"""
	require_admin()
	if not edit_env or not SETTINGS.is_local:
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
	if not SETTINGS.is_local or os.environ.get("PRICE_CHECKER_OPEN_BROWSER", "1").lower() in {"0", "false", "no"}:
		return
	url = app_url(host, port)
	threading.Timer(0.8, webbrowser.open, args=(url,), kwargs={"new": 2}).start()


def app_url(host: str, port: int) -> str:
	configured_scheme = os.environ.get("PRICE_CHECKER_URL_SCHEME")
	if configured_scheme:
		scheme = configured_scheme.strip().lower()
	else:
		scheme = SETTINGS.scheme
	return f"{scheme}://{host}:{port}"


def enforce_startup_requirements() -> None:
	"""Exit instead of serving LAN or public mode without a persistent secret and an admin account."""
	errors = startup_errors(SETTINGS, admin_exists())
	if errors:
		raise SystemExit(f"Cannot start in {SETTINGS.mode} mode:\n- " + "\n- ".join(errors))


def read_new_password(prompt: str = "New password") -> str:
	"""Prompt twice without echo. PRICE_CHECKER_ADMIN_PASSWORD allows scripted setup, but never accept passwords as arguments."""
	import getpass
	scripted = os.environ.get("PRICE_CHECKER_ADMIN_PASSWORD")
	if scripted:
		return scripted
	password = getpass.getpass(f"{prompt}: ")
	if password != getpass.getpass("Repeat password: "):
		raise SystemExit("The passwords don't match.")
	return password


def cli_create_admin(arguments: list[str]) -> None:
	username = (arguments[0] if arguments else os.environ.get("PRICE_CHECKER_ADMIN_USERNAME") or input("Admin username: ")).strip()
	password = read_new_password("Admin password")
	try:
		with connect() as connection:
			first_account = real_user_count(connection) == 0
			user_id = create_user(connection, username, password, "admin")
			if first_account:
				claim_local_data(connection, user_id) #An existing single-user watchlist moves to the first account
	except ValueError as problem:
		raise SystemExit(str(problem))
	print(f"Admin '{username}' created." + (" Existing watchlist data was assigned to this account." if first_account else ""))


def cli_reset_password(arguments: list[str]) -> None:
	username = (arguments[0] if arguments else input("Username: ")).strip()
	with connect() as connection:
		row = connection.execute("SELECT id FROM users WHERE username = ? AND username != 'local'", (username,)).fetchone()
		if row is None:
			raise SystemExit(f"No account named '{username}'.")
		try:
			auth.set_password(connection, row["id"], read_new_password())
		except ValueError as problem:
			raise SystemExit(str(problem))
	print(f"Password reset for '{username}'. The account is unlocked and signed out everywhere.")


def register_server(running_server) -> None:
	"""Keep the werkzeug server so tab-close shutdown (local mode) can stop it."""
	global server
	server = running_server


def run_scheduled_scans() -> None:
	"""Scan every enabled account once, skipping anyone already scanned today. Each scan keeps the per-user lock and API budget."""
	if not provider_ready(api_provider()):
		print("Scheduled scan skipped: the API provider is not set up.")
		return
	with connect() as connection:
		if SETTINGS.is_local:
			user_ids = [local_user_id(connection)]
		else:
			user_ids = [row["id"] for row in connection.execute(
				"SELECT id FROM users WHERE disabled = 0 AND username != ? AND password_hash != ? ORDER BY id",
				(LOCAL_USERNAME, UNUSABLE_PASSWORD_HASH),
			)]
	for user_id in user_ids:
		lock = scan_lock_for(user_id)
		if not lock.acquire(blocking=False):
			continue
		try:
			if scan_allowed(user_id):
				run_scan(user_id)
		except Exception as error: #One account's failure shouldn't skip everyone else
			print(f"Scheduled scan failed for user {user_id}: {error}")
		finally:
			lock.release()


def start_background_tasks() -> None:
	"""Update check, the optional scheduled scan and (local mode) the browser. Started once in the serving process."""
	threading.Thread(target=check_for_update_background, daemon=True).start()
	start_scheduler(SETTINGS.scheduled_scan_hour, run_scheduled_scans)
	if SETTINGS.open_browser:
		open_app_browser(SETTINGS.host, SETTINGS.port)


def run_cli_command(command: str, arguments: list[str]) -> None:
	if command == "init-secret":
		path = create_secret_file(SETTINGS.data_dir)
		print(f"Secret ready at {path}")
	elif command == "make-cert":
		if SETTINGS.tls != "selfsigned":
			raise SystemExit("Set PRICE_CHECKER_MODE=lan and PRICE_CHECKER_TLS=selfsigned first.")
		ensure_tls_files(SETTINGS, force=True)
		print(f"Certificate written to {SETTINGS.tls_cert}")
	elif command == "create-admin":
		cli_create_admin(arguments)
	elif command == "reset-password":
		cli_reset_password(arguments)
	else:
		raise SystemExit(f"Unknown command '{command}'. Available: init-secret, create-admin, reset-password, make-cert")


init_db()
if DEVELOPER_MODE:
	init_dev_db()


if SETTINGS.trusted_proxies:
	from werkzeug.middleware.proxy_fix import ProxyFix
	app.wsgi_app = ProxyFix(app.wsgi_app, x_for=SETTINGS.trusted_proxies, x_proto=SETTINGS.trusted_proxies, x_host=SETTINGS.trusted_proxies)

if __name__ != "__main__":
	enforce_startup_requirements() #Covers production servers that import main:app

if __name__ == "__main__":
	import sys
	if len(sys.argv) > 1:
		run_cli_command(sys.argv[1], sys.argv[2:])
		raise SystemExit(0)

	enforce_startup_requirements()
	serve(app, SETTINGS, on_ready=start_background_tasks, on_server=register_server)

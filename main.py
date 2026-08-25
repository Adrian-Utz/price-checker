from __future__ import annotations

import json
import csv
import io
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import unquote, urlparse

from flask import Flask, Response, abort, redirect, render_template, request, session, url_for
from markupsafe import Markup
from serpapi_client import SerpApiClient
from werkzeug.serving import make_server

from version import VERSION_NUMBER
import check_for_update

"""
Main entry point into the program. This is a web application with a python backend. Used to keep track of certian items that the user selects.


Last Update: 8/24/2026
Written on: 7/27/2026
Written by: AJ Utz
"""


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)
DATABASE_PATH = Path(os.environ.get("PRICE_CHECKER_DB", ROOT / "price_checker.sqlite3"))
MAX_URLS = max(20, min(int(os.environ.get("PRICE_CHECKER_MAX_URLS", "100")), 100))
CACHE_HOURS = max(1, int(os.environ.get("PRICE_CHECKER_CACHE_HOURS", "24")))
MIN_DELAY_SECONDS = max(1.0, float(os.environ.get("PRICE_CHECKER_MIN_DELAY", "4")))
app = Flask(__name__)
app.secret_key = os.environ.get("PRICE_CHECKER_SECRET", secrets.token_hex(32))
server = None
shutdown_lock = threading.Lock()
shutdown_timer = None
update_info = {"available": False, "latest_version": None}
update_info_lock = threading.Lock()


def check_for_update_background() -> None:
	"""Query GitHub for a newer release in a background thread and cache the result for the UI."""
	try:
		available, _local, latest = check_for_update.is_update_available()
	except Exception:
		return
	with update_info_lock:
		update_info["available"] = available
		update_info["latest_version"] = latest


def connect() -> sqlite3.Connection:
	connection = sqlite3.connect(DATABASE_PATH)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA foreign_keys = ON")
	return connection


def init_db() -> None:
	"""
	Initialize the database by creating necessary tables if they do not exist.
	This function establishes a connection to the database using the connect() function.
	It then executes SQL statements to create two tables:'products' and 'observations'.
	'products' contain info such as: ID, URL, title, source, price, currency, check date, and error status.
	'observations' records the observations of prices, curencies, titles, statuses, errors, observed dates, and references to products.
	"""
	with connect() as connection:
		connection.executescript("""
			CREATE TABLE IF NOT EXISTS products (
				id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT,
				source TEXT NOT NULL, price REAL, currency TEXT, checked_at TEXT,
				error TEXT
			);
			CREATE TABLE IF NOT EXISTS observations (
				id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
				price REAL, currency TEXT, title TEXT, status TEXT NOT NULL,
				error TEXT, observed_at TEXT NOT NULL,
				FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
			);
			CREATE TABLE IF NOT EXISTS scans (
				id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
				completed_at TEXT, status TEXT NOT NULL
			);
		""")


def utc_now() -> datetime:
	"""Time check"""
	return datetime.now(timezone.utc)


def local_calendar_date(timestamp: str):
	"""Time Stamp"""
	return datetime.fromisoformat(timestamp).astimezone().date()


def normalize_url(value: str) -> str:
	"""
	Check and normilize the URl
	This function takes a string input and checks if it is a complete HTTP or HTTPS URL,
	and returns a normalized version of the URL without any fragment identifiers.
	"""
	parsed = urlparse(value.strip())
	if parsed.scheme not in {"http", "https"} or not parsed.netloc:
		raise ValueError("Enter a complete http or https URL.")
	return parsed._replace(fragment="").geturl()


def source_for(url: str) -> str:
	host = urlparse(url).netloc.lower().removeprefix("www.")
	for name in ("homedepot", "walmart"):
		if name in host:
			return name.replace("homedepot", "home depot").title()
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


def fetch_product(url: str) -> dict[str, object]:
	return SerpApiClient().product(url)


def cache_is_fresh(checked_at: str | None) -> bool:
	#check if the checked_at timestamp exists
	if not checked_at:
		return False
	return datetime.fromisoformat(checked_at) > utc_now() - timedelta(hours=CACHE_HOURS)


def serpapi_ready() -> bool:
	return bool(os.environ.get("SERPAPI_API_KEY"))


def read_env_file_text() -> str:
	if not ENV_FILE.exists():
		return "SERPAPI_API_KEY=\n"
	return ENV_FILE.read_text(encoding="utf-8")


def save_env_file_text(content: str) -> None:
	"""Check if .env content is valid, normilize, then save and load the key"""
	if "\x00" in content:
		raise ValueError("Invalid .env content.")
	normalized = content if content.endswith("\n") else f"{content}\n"
	ENV_FILE.write_text(normalized, encoding="utf-8")
	load_dotenv(ENV_FILE, override=True)


def format_observed_at_utc(timestamp: str) -> str:
	return timestamp.replace("T", " ")[:16] + " UTC"


def watchlist_export_rows() -> list[dict[str, object]]:
	"""Read the sqlite3 file for information, then return a tuple with that data."""
	with connect() as connection:
		products = connection.execute("SELECT * FROM products ORDER BY id").fetchall()
		rows = []
		for product in products:
			history = connection.execute(
				"SELECT price, currency, status, error, observed_at FROM observations WHERE product_id = ? ORDER BY observed_at",
				(product["id"],),
			).fetchall()
			rows.append({
				"id": product["id"],
				"url": product["url"],
				"title": product["title"],
				"source": product["source"],
				"price": product["price"],
				"currency": product["currency"],
				"checked_at": product["checked_at"],
				"error": product["error"],
				"history": [dict(observation) for observation in history],
			})
	return rows


def price_chart(observations: list[sqlite3.Row]) -> Markup | None:
	"""
	Generates a SVG line chart represienting the historical prices of products.
	This function takes a list of SQLite Row objects containing observation data.
	Filters out the rows where the price is 'None' and creates a list of points with their observed_at timestamp and corresponding price.
	If there is no valid points, it returns None.
	"""
	#extract observed_at timestamps and prices from the list
	points = [(row["observed_at"], float(row["price"])) for row in observations if row["price"] is not None]
	if not points:
		return None
	
	#define dimension of the chart
	width, height = 300, 100
	left, right, top, bottom = 8, 8, 10, 18

	#calculate the plot area within the chart dimensions
	plot_width, plot_height = width - left - right, height - top - bottom

	#Scaling
	prices = [price for _, price in points]

	#Determine the min and max prices to scale the y-axis
	minimum, maximum = min(prices), max(prices)
	spread = maximum - minimum or max(minimum * 0.1, 1)

	#Calculate the coords for each pint on the chart
	coordinates = []
	for index, (observed_at, price) in enumerate(points):
		x = left + (plot_width * index / max(len(points) - 1, 1))
		y = top + plot_height - ((price - minimum) / spread * plot_height)
		coordinates.append((x, y, observed_at, price))

	#Generate the SVG polyline for the chart line
	line = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in coordinates)

	# Generate the SVG marks for each point
	marks = []
	for x, y, observed_at, price in coordinates:
		date = format_observed_at_utc(observed_at)
		marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" tabindex="0"><title>${price:,.2f} on {date}</title></circle>')

	# Return the SVG markup for the price chart
	return Markup(
		f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Price history with {len(points)} observations">'
		f'<polyline points="{line}" fill="none" stroke="#d06b35" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
		f'{"".join(marks)}<text x="8" y="96" fill="#6c756b" font-size="9">${minimum:,.2f}</text>'
		f'<text x="292" y="96" fill="#6c756b" font-size="9" text-anchor="end">${maximum:,.2f}</text></svg>'
	)


@app.before_request
def protect_post_forms() -> None:
	"""
	Protects POST requests by checking for valid CSRF tokens
	This function runs before each request and ensures that the form token is valid
	It cancels any pending shutdown, checks if a CSRF token exists in the session, and aborts with a 400 error 
	if the token is invalid or missing when processing POST requests to endpoints other than 'shutdown'.
	"""
	cancel_pending_shutdown()

	#If the CSRF token is not in the session, generate a new one.
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
	Runs a full scan of all products in the database.
	This function initiates a scan, updates product details based on their latest observations,
	and records observations for each product. It handles retries and ensures that only one URL is fetched
	per second to avoid overwhelming the server.
	"""
	#Record the start time of the scan
	started = utc_now().isoformat()

	#insert a new row into the scans table with the start time and status
	with connect() as connection:
		scan_id = connection.execute("INSERT INTO scans (started_at, status) VALUES (?, ?)", (started, "running")).lastrowid
		products = connection.execute("SELECT * FROM products ORDER BY id").fetchall() #Retrieve all products from the database
	previous_fetch = 0.0 #initialize a variable to track the time between fetches

	#Iterate over each product in the list
	for product in products:
		if product["price"] is not None and cache_is_fresh(product["checked_at"]): #Skip if product is not availiable or the cache is fresh
			continue
		#Wait to ensure a minimum delay between fetches to avoid overwhelming the server
		time.sleep(max(0, MIN_DELAY_SECONDS + random.uniform(0, 2) - (time.monotonic() - previous_fetch)))
		previous_fetch = time.monotonic()
		observed_at = utc_now().isoformat() #get the observed at timestamp for the current products

		#Try to fetch the product details from the URl
		try:
			result = fetch_product(product["url"])
			status, error = "success", None
		except Exception as fetch_error:  # A single URL must not cancel the scan.
			result, status, error = {}, "error", str(fetch_error)

		#Update the product details in the database with the new data and record the observation
		with connect() as connection:
			connection.execute("UPDATE products SET title = COALESCE(?, title), price = ?, currency = ?, checked_at = ?, error = ? WHERE id = ?", (result.get("title"), result.get("price"), result.get("currency"), observed_at, error, product["id"]))
			connection.execute("INSERT INTO observations (product_id, price, currency, title, status, error, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (product["id"], result.get("price"), result.get("currency"), result.get("title"), status, error, observed_at))
	#Mark the scan as complete in the database
	with connect() as connection:
		connection.execute("UPDATE scans SET completed_at = ?, status = ? WHERE id = ?", (utc_now().isoformat(), "completed", scan_id))


def scan_allowed() -> bool:
	#Connect to the database using the connect function
	with connect() as connection:
		#Execute a SQL query to select the latest completed data
		# The quety orders the scans by their ID in descending order and limits the results to 1
		scan = connection.execute("SELECT completed_at FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()
	#Check if there is no scan or if the scan's completed date is not today
	return not scan or local_calendar_date(scan["completed_at"]) != datetime.now().astimezone().date()


@app.route("/", methods=["GET"])
def index():
	#Connect to the database using the connect function
	with connect() as connection:
		#Execute a SQL query to retrieve all products and order them by their ID
		product_rows = connection.execute("SELECT * FROM products ORDER BY id").fetchall()
		products = [] #Empty list to store product views
		for product in product_rows:
			#Execute a SQL query to retrieve the history of observations for the current product
			history = connection.execute(
				"SELECT price, observed_at FROM observations WHERE product_id = ? AND status = 'success' AND price IS NOT NULL ORDER BY observed_at",
				(product["id"],),
			).fetchall()
			product_view = dict(product)#create a dictionary for the product view and populate it with porduct details
			product_view["chart"] = price_chart(history)#Generate a price chart for the product using the price_chart function

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
	#Render the page.html template with the products, maximum URLs, scan allowed status, last scan, message, CSRF token, SERP API readiness, version number, and environment file text
	return render_template("page.html", products=products, max_urls=MAX_URLS, can_scan=scan_allowed(), last_scan=last_scan, message=request.args.get("message"), csrf_token=session["csrf_token"], serpapi_ready=serpapi_ready(), version_number=VERSION_NUMBER, env_file_text=read_env_file_text(), update_available=update_available, latest_version=latest_version, releases_url=check_for_update.get_latest_release_url())


@app.post("/add")
def add_url():
	try:
		normalized = normalize_url(request.form.get("url", ""))#Normilize the URl
		with connect() as connection: #Connect to the database
			#Check if the number of products in the database exceeds the max limit
			if connection.execute("SELECT COUNT(*) FROM products").fetchone()[0] >= MAX_URLS:
				raise ValueError(f"The watchlist limit is {MAX_URLS} URLs.")
			# Insert the normalized URL into the porduct table if it doesn't exist
			connection.execute("INSERT OR IGNORE INTO products (url, source) VALUES (?, ?)", (normalized, source_for(normalized)))
		#Set message based on the number of changes in the database
		message = "URL added." if connection.total_changes else "That URL is already tracked."
	except ValueError as error:
		message = str(error)
	#Redirect to the index page with the message
	return redirect(url_for("index", message=message))


@app.post("/remove/<int:product_id>")
def remove_url(product_id: int):
	"""Endpoint to remove a product from the database based on product ID"""
	with connect() as connection:
		connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
	return redirect(url_for("index", message="URL removed."))


@app.post("/scan")
def scan():
	"""Check if a scan is allowed for the user"""
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
	fieldnames = ["id", "url", "title", "source", "price", "currency", "checked_at", "error", "history"]
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


@app.post("/shutdown")
def shutdown():
	"""Detect when the user closed the tab and shutdown the rest of the program"""
	if request.remote_addr not in {"127.0.0.1", "::1", "localhost", None}:
		abort(403)
	schedule_shutdown()
	return ("", 204)


@app.post("/settings/env")
def save_env_file():
	"""Update the user's .env"""
	try:
		save_env_file_text(request.form.get("env_content", ""))
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

if __name__ == "__main__":
	host = "127.0.0.1"
	port = int(os.environ.get("PORT", "5000"))
	server = make_server(host, port, app)
	threading.Thread(target=check_for_update_background, daemon=True).start()
	open_app_browser(host, port)
	server.serve_forever()
	server.server_close()

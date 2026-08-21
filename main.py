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

from flask import Flask, Response, abort, redirect, render_template_string, request, session, url_for
from markupsafe import Markup
from serpapi_client import SerpApiClient
from werkzeug.serving import make_server

from version import VERSION_NUMBER

"""
Main entry point into the program. This is a web application with a python backend. Used to keep track of certian items that the user selects.


Last Update: 8/21/2026
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


PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Price Checker</title>
  <style>
	:root {
		background: #f3f0e8;
		color: #202820;
		color-scheme: light;
		font-family: Georgia, serif;
	}

	body {
		margin: 0 auto;
		max-width: 1080px;
		padding: 32px 20px 64px;
	}

	header {
		align-items: end;
		border-bottom: 3px solid #d06b35;
		display: flex;
		gap: 20px;
		justify-content: space-between;
		margin-bottom: 28px;
		padding-bottom: 18px;
	}

	.title-line {
		align-items: baseline;
		display: flex;
		gap: 12px;
	}

	h1 {
		font-size: clamp(1rem, 6vw, 3rem);
		line-height: .95;
		margin: 0;
		max-width: 600px;
		white-space: nowrap;
	}

	.version {
		color: #6c756b;
		font: 700 .82rem system-ui, sans-serif;
		letter-spacing: .06em;
		white-space: nowrap;
	}

	.lede {
		color: #596257;
		font: 1rem/1.5 system-ui, sans-serif;
		max-width: 650px;
	}

	.eyebrow {
		color: #596257;
		font: 700 .75rem system-ui, sans-serif;
		letter-spacing: .1em;
		margin: 0 0 8px;
		text-transform: uppercase;
	}

	.status {
		background: #e2f0e3;
		color: #226b4d;
		font: 700 .82rem system-ui, sans-serif;
		padding: 8px 11px;
		white-space: nowrap;
	}

	.status.warning {
		background: #f6e6c8;
		color: #8a4b24;
	}

	.header-actions {
		align-items: center;
		display: flex;
		gap: 10px;
	}

	.env-button {
		background: #226b4d;
		padding: 8px 12px;
	}

	section {
		background: #fffdf8;
		border: 1px solid #d8d4c9;
		margin: 18px 0;
		padding: 22px;
	}

	section h2 { margin-top: 0; }

	form {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
	}

	input[type=url] {
		border: 1px solid #9ca597;
		flex: 1 1 480px;
		font-size: 1rem;
		min-width: 0;
		padding: 12px;
	}

	button {
		background: #d06b35;
		border: 0;
		color: white;
		cursor: pointer;
		font-weight: 700;
		padding: 12px 18px;
	}

	button[disabled] { background: #9ca597; cursor: not-allowed; }

	.notice {
		background: #f6e6c8;
		border-left: 5px solid #d06b35;
		font: .92rem/1.4 system-ui, sans-serif;
		padding: 12px 15px;
	}

	.help {
		color: #596257;
		font: .88rem/1.45 system-ui, sans-serif;
		margin-bottom: 0;
	}

	table {
		border-collapse: collapse;
		font: .92rem system-ui, sans-serif;
		width: 100%;
	}

	th, td {
		border-bottom: 1px solid #dedbd2;
		padding: 12px 8px;
		text-align: left;
		vertical-align: top;
	}

	th {
		color: #596257;
		font-size: .78rem;
		letter-spacing: .06em;
		text-transform: uppercase;
	}

	.muted { color: #6c756b; }
	.error { background: #fbe9e5; color: #a23c2d; margin-top: 8px; padding: 8px; }
	.price { color: #226b4d; font-size: 1.1rem; font-weight: 700; }
	.product-link { color: #202820; font-weight: 700; }
	.product-url { display: block; font-size: .78rem; margin-top: 4px; max-width: 460px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.row-actions { align-items: center; display: flex; flex-wrap: wrap; gap: 8px; }
	.history-button { background: #226b4d; padding: 6px 10px; }
	.history-button[disabled] { background: #9ca597; }
	.history-modal { border: 1px solid #d8d4c9; box-shadow: 0 18px 60px rgb(32 40 32 / 25%); max-width: min(760px, calc(100vw - 32px)); padding: 0; width: 720px; }
	.history-modal::backdrop { background: rgb(32 40 32 / 45%); }
	.modal-header { align-items: start; border-bottom: 1px solid #d8d4c9; display: flex; gap: 20px; justify-content: space-between; padding: 18px 20px 12px; }
	.modal-header h3 { margin: 0; }
	.modal-close { background: transparent; border: 1px solid #9ca597; color: #202820; padding: 6px 10px; }
	.modal-content { padding: 12px 20px 20px; }
	.env-textarea { border: 1px solid #9ca597; font: .88rem/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; min-height: 180px; padding: 10px; resize: vertical; width: 100%; box-sizing: border-box; }
	.modal-actions { display: flex; gap: 10px; justify-content: end; margin-top: 12px; }
	.chart { background: #f7f8f2; border: 1px solid #e2e4d9; box-sizing: border-box; margin-top: 8px; padding: 10px; width: 100%; }
	.chart svg { display: block; height: auto; width: 100%; }
	.chart-empty { color: #6c756b; font-size: .78rem; margin: 8px 0 2px; }
	.history-list { font: .88rem/1.45 system-ui, sans-serif; margin: 12px 0 0; padding-left: 20px; }
	.history-list li { margin: 4px 0; }
	.history-empty { color: #6c756b; font-size: .82rem; margin: 12px 0 0; }
	.export-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
	.export-button { background: #226b4d; color: white; display: inline-block; padding: 10px 14px; text-decoration: none; }
	.remove { background: transparent; border: 1px solid #a23c2d; color: #a23c2d; padding: 6px 10px; }

	@media (max-width: 680px) {
		header { align-items: start; flex-direction: column; }
		section { padding: 14px; }
		table, tbody, tr, td { display: block; }
		thead { display: none; }
		tr { border-bottom: 1px solid #dedbd2; padding: 12px 0; }
		td { border: 0; padding: 4px 0; }
		td:last-child { margin-top: 8px; }
	}
  </style>
</head>
<body>
		<header>
		<div>
			<p class="eyebrow">Personal price watchlist</p>
			<div class="title-line">
				<h1>Price Checker</h1>
				<span class="version">v{{ version_number }}</span>
			</div>
			<p class="lede">A quiet, once-a-day watchlist for prices that matter.</p>
		</div>
			<div class="header-actions">
				<button class="env-button" type="button" onclick="document.getElementById('env-editor').showModal()">Edit .env</button>
				<span class="status {{ '' if serpapi_ready else 'warning' }}">
					{{ 'API ready' if serpapi_ready else 'API key needed' }}
				</span>
			</div>
	</header>
		<dialog class="history-modal" id="env-editor">
			<div class="modal-header">
				<div>
					<p class="eyebrow">Settings</p>
					<h3>Edit .env</h3>
				</div>
				<form method="dialog">
					<button class="modal-close" aria-label="Close env editor">Close</button>
				</form>
			</div>
			<div class="modal-content">
				<form method="post" action="{{ url_for('save_env_file') }}">
					<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
					<textarea class="env-textarea" name="env_content" spellcheck="false">{{ env_file_text }}</textarea>
					<div class="modal-actions">
						<button type="submit">Save .env</button>
					</div>
				</form>
			</div>
		</dialog>
  <div class="notice">Use responsibly. Retailer pages can change, impose rate limits, or prohibit automated access. This tool spaces requests, uses a daily scan limit, and never bypasses CAPTCHAs or access controls.</div>
  {% if message %}<p class="notice">{{ message }}</p>{% endif %}
	{% if not serpapi_ready %}<p class="notice"><strong>One setup step:</strong> set <code>SERPAPI_API_KEY</code> in the same terminal used to start this app, then restart it.</p>{% endif %}
	<section>
		<h2>Track a product</h2>
		<form method="post" action="{{ url_for('add_url') }}">
			<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
			<input name="url" type="url" placeholder="Paste a Walmart or Home Depot product URL" aria-label="Product URL" required>
			<button>Add URL</button>
		</form>
		<p class="help">Supported now: Walmart and Home Depot product pages. You can track up to {{ max_urls }} URLs.</p>
	</section>
	<section>
		<h2>Daily check</h2>
		<form method="post" action="{{ url_for('scan') }}">
			<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
			<button {% if not can_scan or not products or not serpapi_ready %}disabled{% endif %}>
				{{ 'Check prices' if can_scan else 'Checked for today' }}
			</button>
			<span class="muted">
				{% if last_scan %}Last check: {{ last_scan }}{% else %}No check yet. Add a URL, then check prices.{% endif %}
			</span>
		</form>
		<p class="help">Each URL is checked through SerpApi, then saved locally so you can review the result later.</p>
		<div class="export-actions">
			<a class="export-button" href="{{ url_for('export_watchlist_json') }}">Export JSON</a>
			<a class="export-button" href="{{ url_for('export_watchlist_csv') }}">Export CSV</a>
		</div>
	</section>
	<section><h2>Your watchlist <span class="muted">({{ products|length }})</span></h2>{% if products %}<table><thead><tr><th>Product</th><th>Retailer</th><th>Price</th><th>Last checked</th><th></th></tr></thead><tbody>{% for product in products %}<tr><td><a class="product-link" href="{{ product.url }}" rel="noreferrer">{{ product.title or 'Product waiting for first check' }}</a><span class="product-url muted">{{ product.url }}</span>{% if product.error %}<div class="error"><strong>Could not get price:</strong> {{ product.error }}</div>{% endif %}</td><td>{{ product.source }}</td><td class="{{ 'price' if product.price is not none else 'muted' }}">{{ ('$%.2f' % product.price) if product.price is not none else 'Waiting for check' }}</td><td class="muted">{{ product.checked_at or 'Not checked yet' }}</td><td><div class="row-actions"><button class="history-button" type="button" {% if not product.chart %}disabled{% else %}onclick="document.getElementById('history-{{ product.id }}').showModal()"{% endif %}>{{ 'View history graph' if product.chart else 'No history yet' }}</button><form method="post" action="{{ url_for('remove_url', product_id=product.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="remove" title="Remove URL">Remove from Search</button></form></div><dialog class="history-modal" id="history-{{ product.id }}"><div class="modal-header"><div><p class="eyebrow">Price history</p><h3>{{ product.title or product.url }}</h3></div><form method="dialog"><button class="modal-close" aria-label="Close price history">Close</button></form></div><div class="modal-content">{{ product.chart|safe if product.chart else '<p class="chart-empty">Price history appears after a successful check.</p>' }}{% if product.history_rows %}<ol class="history-list">{% for item in product.history_rows %}<li>${{ '%.2f'|format(item.price) }} on {{ item.observed_at_utc }}</li>{% endfor %}</ol>{% else %}<p class="history-empty">No past prices yet.</p>{% endif %}</div></dialog></td></tr>{% endfor %}</tbody></table>{% else %}<p class="muted">Your watchlist is empty. Paste a product URL above to get started.</p>{% endif %}</section>
</body></html>
<script>
window.addEventListener("pagehide", function (event) {
	if (!event.persisted) {
		navigator.sendBeacon("{{ url_for('shutdown') }}");
	}
});
</script>
"""


def connect() -> sqlite3.Connection:
	connection = sqlite3.connect(DATABASE_PATH)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA foreign_keys = ON")
	return connection


def init_db() -> None:
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
	points = [(row["observed_at"], float(row["price"])) for row in observations if row["price"] is not None]
	if not points:
		return None
	width, height = 300, 100
	left, right, top, bottom = 8, 8, 10, 18
	plot_width, plot_height = width - left - right, height - top - bottom
	prices = [price for _, price in points]
	minimum, maximum = min(prices), max(prices)
	spread = maximum - minimum or max(minimum * 0.1, 1)
	coordinates = []
	for index, (observed_at, price) in enumerate(points):
		x = left + (plot_width * index / max(len(points) - 1, 1))
		y = top + plot_height - ((price - minimum) / spread * plot_height)
		coordinates.append((x, y, observed_at, price))
	line = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in coordinates)
	marks = []
	for x, y, observed_at, price in coordinates:
		date = format_observed_at_utc(observed_at)
		marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" tabindex="0"><title>${price:,.2f} on {date}</title></circle>')
	return Markup(
		f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Price history with {len(points)} observations">'
		f'<polyline points="{line}" fill="none" stroke="#d06b35" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
		f'{"".join(marks)}<text x="8" y="96" fill="#6c756b" font-size="9">${minimum:,.2f}</text>'
		f'<text x="292" y="96" fill="#6c756b" font-size="9" text-anchor="end">${maximum:,.2f}</text></svg>'
	)


@app.before_request
def protect_post_forms() -> None:
	cancel_pending_shutdown()
	if "csrf_token" not in session:
		session["csrf_token"] = secrets.token_urlsafe(32)
	if request.method == "POST" and request.endpoint != "shutdown" and not app.testing:
		submitted = request.form.get("csrf_token", "")
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
	started = utc_now().isoformat()
	with connect() as connection:
		scan_id = connection.execute("INSERT INTO scans (started_at, status) VALUES (?, ?)", (started, "running")).lastrowid
		products = connection.execute("SELECT * FROM products ORDER BY id").fetchall()
	previous_fetch = 0.0
	for product in products:
		if product["price"] is not None and cache_is_fresh(product["checked_at"]):
			continue
		time.sleep(max(0, MIN_DELAY_SECONDS + random.uniform(0, 2) - (time.monotonic() - previous_fetch)))
		previous_fetch = time.monotonic()
		observed_at = utc_now().isoformat()
		try:
			result = fetch_product(product["url"])
			status, error = "success", None
		except Exception as fetch_error:  # A single URL must not cancel the scan.
			result, status, error = {}, "error", str(fetch_error)
		with connect() as connection:
			connection.execute("UPDATE products SET title = COALESCE(?, title), price = ?, currency = ?, checked_at = ?, error = ? WHERE id = ?", (result.get("title"), result.get("price"), result.get("currency"), observed_at, error, product["id"]))
			connection.execute("INSERT INTO observations (product_id, price, currency, title, status, error, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (product["id"], result.get("price"), result.get("currency"), result.get("title"), status, error, observed_at))
	with connect() as connection:
		connection.execute("UPDATE scans SET completed_at = ?, status = ? WHERE id = ?", (utc_now().isoformat(), "completed", scan_id))


def scan_allowed() -> bool:
	with connect() as connection:
		scan = connection.execute("SELECT completed_at FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()
	return not scan or local_calendar_date(scan["completed_at"]) != datetime.now().astimezone().date()


@app.route("/", methods=["GET"])
def index():
	with connect() as connection:
		product_rows = connection.execute("SELECT * FROM products ORDER BY id").fetchall()
		products = []
		for product in product_rows:
			history = connection.execute(
				"SELECT price, observed_at FROM observations WHERE product_id = ? AND status = 'success' AND price IS NOT NULL ORDER BY observed_at",
				(product["id"],),
			).fetchall()
			product_view = dict(product)
			product_view["chart"] = price_chart(history)
			product_view["history_rows"] = [
				{"price": float(row["price"]), "observed_at_utc": format_observed_at_utc(row["observed_at"])}
				for row in reversed(history)
			]
			products.append(product_view)
		scan = connection.execute("SELECT completed_at FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()
	last_scan = scan["completed_at"].replace("T", " ")[:16] if scan else None
	return render_template_string(PAGE, products=products, max_urls=MAX_URLS, can_scan=scan_allowed(), last_scan=last_scan, message=request.args.get("message"), csrf_token=session["csrf_token"], serpapi_ready=serpapi_ready(), version_number=VERSION_NUMBER, env_file_text=read_env_file_text())


@app.post("/add")
def add_url():
	try:
		normalized = normalize_url(request.form.get("url", ""))
		with connect() as connection:
			if connection.execute("SELECT COUNT(*) FROM products").fetchone()[0] >= MAX_URLS:
				raise ValueError(f"The watchlist limit is {MAX_URLS} URLs.")
			connection.execute("INSERT OR IGNORE INTO products (url, source) VALUES (?, ?)", (normalized, source_for(normalized)))
		message = "URL added." if connection.total_changes else "That URL is already tracked."
	except ValueError as error:
		message = str(error)
	return redirect(url_for("index", message=message))


@app.post("/remove/<int:product_id>")
def remove_url(product_id: int):
	with connect() as connection:
		connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
	return redirect(url_for("index", message="URL removed."))


@app.post("/scan")
def scan():
	if not scan_allowed():
		return redirect(url_for("index", message="Only one completed scan is allowed per day."))
	run_scan()
	return redirect(url_for("index", message="Scan complete."))


@app.get("/export/json")
def export_watchlist_json():
	content = json.dumps({"products": watchlist_export_rows()}, indent=2)
	return Response(
		content,
		mimetype="application/json",
		headers={"Content-Disposition": "attachment; filename=price-checker-watchlist.json"},
	)


@app.get("/export/csv")
def export_watchlist_csv():
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
	if request.remote_addr not in {"127.0.0.1", "::1", "localhost", None}:
		abort(403)
	schedule_shutdown()
	return ("", 204)


@app.post("/settings/env")
def save_env_file():
	try:
		save_env_file_text(request.form.get("env_content", ""))
		message = ".env updated successfully."
	except ValueError as error:
		message = str(error)
	return redirect(url_for("index", message=message))


def open_app_browser(host: str, port: int) -> None:
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
	open_app_browser(host, port)
	server.serve_forever()
	server.server_close()

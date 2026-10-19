import random
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date, datetime


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
ProductRefresh = Callable[[sqlite3.Row], None]

"""
Here are the functions that form the scan workflow.

Last Update: 10/19/2026
Written on: 10/12/2026
Written by: AJ Utz
"""

def run_scan(
	*,
	connect: ConnectionFactory,
	utc_now: Callable[[], datetime],
	developer_mode: bool,
	cache_is_fresh: Callable[[str | None], bool],
	min_delay_seconds: float,
	refresh_product: ProductRefresh,
) -> None:
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
		if not developer_mode and product["price"] is not None and cache_is_fresh(product["checked_at"]): #Skip if product is not availiable or the cache is fresh
			continue
		#Wait to ensure a minimum delay between fetches to avoid overwhelming the server
		time.sleep(max(0, min_delay_seconds + random.uniform(0, 2) - (time.monotonic() - previous_fetch)))
		previous_fetch = time.monotonic()
		refresh_product(product)
	#Mark the scan as complete in the database
	with connect() as connection:
		connection.execute("UPDATE scans SET completed_at = ?, status = ? WHERE id = ?", (utc_now().isoformat(), "completed", scan_id))


def refresh_product(
	product: sqlite3.Row,
	*,
	connect: ConnectionFactory,
	utc_now: Callable[[], datetime],
	fetch_product: Callable[..., dict[str, object]],
) -> None:
	"""Fetch the latest price for a single product row and record the result/observation."""
	observed_at = utc_now().isoformat() #get the observed at timestamp for the current product

	#Try to fetch the product details from the URl
	try:
		zip_state = None
		zipcode = None
		if product["source"] == "Lowes" and product["store_id"]:
			with connect() as connection:
				store = connection.execute(
					"SELECT zip_state, zipcode FROM stores WHERE retailer = ? AND store_id = ?",
					(product["source"], product["store_id"]),
				).fetchone()
			if store:
				zip_state, zipcode = store["zip_state"], store["zipcode"]
		result = fetch_product(
			product["url"],
			store_id=product["store_id"],
			zip_state=zip_state,
			zipcode=zipcode,
			provider=product["api_provider"] if "api_provider" in product.keys() else None,
		)
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
		connection.execute("UPDATE products SET title = COALESCE(?, title), price = ?, bulk_price = ?, bulk_quantity = ?, currency = ?, checked_at = ?, error = ?, retailer_product_id = COALESCE(?, retailer_product_id) WHERE id = ?", (result.get("title"), result.get("price"), result.get("bulk_price"), result.get("bulk_quantity"), result.get("currency"), observed_at, error, result.get("product_id"), product["id"]))
		connection.execute("INSERT INTO observations (product_id, price, bulk_price, currency, title, status, error, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (product["id"], result.get("price"), result.get("bulk_price"), result.get("currency"), result.get("title"), status, error, observed_at))


def scan_allowed(
	*,
	connect: ConnectionFactory,
	developer_mode: bool,
	local_calendar_date: Callable[[str], date],
) -> bool:
	"""Check if the user is allowed to query the API's to check prices"""
	if developer_mode:
		return True
	#Connect to the database using the connect function
	with connect() as connection:
		#Execute a SQL query to select the latest completed data
		# The quety orders the scans by their ID in descending order and limits the results to 1
		scan = connection.execute("SELECT completed_at FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()
	#Check if there is no scan or if the scan's completed date is not today
	return not scan or local_calendar_date(scan["completed_at"]) != datetime.now().astimezone().date()

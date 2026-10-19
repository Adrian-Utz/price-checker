import contextlib
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
# Loaded here too because main.py imports this module before it loads .env itself.
load_dotenv(ROOT / ".env", override=True)
DATABASE_PATH = Path(os.environ.get("PRICE_CHECKER_DB", ROOT / "price_checker.sqlite3"))
DEV_DATABASE_PATH = Path(os.environ.get("PRICE_CHECKER_DEV_DB", ROOT / "price_checker.dev.sqlite3"))

"""
Contains the functions for connections management and schema setup.

Last Update: 10/19/2026
Written on: 10/12/2026
Written by: AJ Utz
"""

@contextlib.contextmanager
def connect():
	"""
	## Open a SQLite connection
	Allow commits (or roll backs) on exit like a plain sqlite3 Connection would,
	but also close the connection - sqlite3 Connection's own context manager never closes the file,
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
				error TEXT, retailer_product_id TEXT, api_provider TEXT
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
				name TEXT NOT NULL, location TEXT NOT NULL, zip_state TEXT, zipcode TEXT,
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
		if "retailer_product_id" not in product_columns:
			connection.execute("ALTER TABLE products ADD COLUMN retailer_product_id TEXT")
		if "api_provider" not in product_columns:
			connection.execute("ALTER TABLE products ADD COLUMN api_provider TEXT")
		observation_columns = {row[1] for row in connection.execute("PRAGMA table_info(observations)")}
		if "bulk_price" not in observation_columns:
			connection.execute("ALTER TABLE observations ADD COLUMN bulk_price REAL")
		store_columns = {row[1] for row in connection.execute("PRAGMA table_info(stores)")}
		if "zip_state" not in store_columns:
			connection.execute("ALTER TABLE stores ADD COLUMN zip_state TEXT")
		if "zipcode" not in store_columns:
			connection.execute("ALTER TABLE stores ADD COLUMN zipcode TEXT")
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
					error TEXT, retailer_product_id TEXT, api_provider TEXT
				)
			""")
			connection.execute("""
				INSERT INTO products (id, url, title, source, store_id, price, currency, checked_at, error)
				SELECT id, url, title, source, store_id, price, currency, checked_at, error
				FROM products_old
			""")
			connection.execute("DROP TABLE products_old")
		connection.execute("PRAGMA foreign_keys = ON")


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


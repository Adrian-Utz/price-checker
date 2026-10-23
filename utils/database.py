import contextlib
import os
import sqlite3
from pathlib import Path

from utils.config import load_env_file, load_settings
from utils.migrations import UNUSABLE_PASSWORD_HASH, migrate

"""
Contains the functions for connections management and schema setup.

Last Update: 10/19/2026
Written on: 10/12/2026
Written by: AJ Utz
"""

# Loaded here too because main.py imports this module before it loads .env itself.
load_env_file()
SETTINGS = load_settings()
DATABASE_PATH = SETTINGS.database_path
DEV_DATABASE_PATH = SETTINGS.dev_database_path

@contextlib.contextmanager
def connect():
	"""
	## Open a SQLite connection
	Allow commits (or roll backs) on exit like a plain sqlite3 Connection would,
	but also close the connection - sqlite3 Connection's own context manager never closes the file,
	which left stale open handles on DATABASE_PATH and blocked database import/replace on Windows.
	"""
	DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
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
		migrate(connection, backup_dir=DATABASE_PATH.parent / "backups")


def admin_exists() -> bool:
	"""Report whether an enabled admin account that can log in exists (the placeholder local owner can't)."""
	with connect() as connection:
		return connection.execute(
			"SELECT 1 FROM users WHERE role = 'admin' AND disabled = 0 AND password_hash != ? LIMIT 1",
			(UNUSABLE_PASSWORD_HASH,),
		).fetchone() is not None


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


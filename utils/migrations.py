from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

"""
Versioned schema migrations, tracked with SQLite's PRAGMA user_version.

To change the schema, append a new function to MIGRATIONS. Never edit or reorder a released migration.
Each migration runs in its own transaction and bumps user_version only if it succeeds.

Last Update: 10/20/2026
Written on: 10/20/2026
Written by: AJ Utz
"""


class MigrationError(RuntimeError):
	"""Raised when the database cannot be migrated safely."""


def _baseline(connection: sqlite3.Connection) -> None:
	"""Create the original tables, or bring a database from before versioning up to date. Safe to re-run."""
	# Create necissary tables if they don't exist
	for statement in (
		"""CREATE TABLE IF NOT EXISTS products (
			id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT,
			source TEXT NOT NULL, store_id TEXT, price REAL, bulk_price REAL,
			bulk_quantity INTEGER, currency TEXT, checked_at TEXT,
			error TEXT, retailer_product_id TEXT, api_provider TEXT
		)""",
		"""CREATE TABLE IF NOT EXISTS observations (
			id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
			price REAL, bulk_price REAL, currency TEXT, title TEXT, status TEXT NOT NULL,
			error TEXT, observed_at TEXT NOT NULL,
			FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
		)""",
		"""CREATE TABLE IF NOT EXISTS scans (
			id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
			completed_at TEXT, status TEXT NOT NULL
		)""",
		"""CREATE TABLE IF NOT EXISTS stores (
			id INTEGER PRIMARY KEY, retailer TEXT NOT NULL, store_id TEXT NOT NULL,
			name TEXT NOT NULL, location TEXT NOT NULL, zip_state TEXT, zipcode TEXT,
			UNIQUE(retailer, store_id)
		)""",
	):
		connection.execute(statement)

	#Get all columns in the 'products'table
	product_columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}

	#Add columns to 'products' if they don't exist
	for column, definition in (
		("store_id", "TEXT"), ("bulk_price", "REAL"), ("bulk_quantity", "INTEGER"),
		("retailer_product_id", "TEXT"), ("api_provider", "TEXT"),
	):
		if column not in product_columns:
			connection.execute(f"ALTER TABLE products ADD COLUMN {column} {definition}")

	# Get all columns in the 'observations' table
	observation_columns = {row[1] for row in connection.execute("PRAGMA table_info(observations)")}

	# Add 'bulk_price' column to 'observations' table if it does not exist
	if "bulk_price" not in observation_columns:
		connection.execute("ALTER TABLE observations ADD COLUMN bulk_price REAL")

	# Get all columns in the 'stores' table
	store_columns = {row[1] for row in connection.execute("PRAGMA table_info(stores)")}

	# Add 'zip_state' and 'zipcode' columns to 'stores' table if they do not exist
	for column in ("zip_state", "zipcode"):
		if column not in store_columns:
			connection.execute(f"ALTER TABLE stores ADD COLUMN {column} TEXT")

	# Check if 'url' column in 'products' table is TEXT NOT NULL UNIQUE
	table_definition = connection.execute(
		"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'products'"
	).fetchone()[0]

	if "url TEXT NOT NULL UNIQUE" in table_definition:
		# Rename 'products' table if it exists
		connection.execute("ALTER TABLE products RENAME TO products_old")
		 # Create a new 'products' table with the required columns
		connection.execute("""
			CREATE TABLE products (
				id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT,
				source TEXT NOT NULL, store_id TEXT, price REAL, bulk_price REAL,
				bulk_quantity INTEGER, currency TEXT, checked_at TEXT,
				error TEXT, retailer_product_id TEXT, api_provider TEXT
			)
		""")
		# Insert data from the old 'products' table into the new table
		connection.execute("""
			INSERT INTO products (id, url, title, source, store_id, price, currency, checked_at, error)
			SELECT id, url, title, source, store_id, price, currency, checked_at, error
			FROM products_old
		""")
		# Drop the old 'products' table
		connection.execute("DROP TABLE products_old")


def _users(connection: sqlite3.Connection) -> None:
	"""Create the 'users' table if it does not exist"""
	connection.execute("""
		CREATE TABLE IF NOT EXISTS users (
			id INTEGER PRIMARY KEY,
			username TEXT NOT NULL COLLATE NOCASE UNIQUE,
			password_hash TEXT NOT NULL,
			role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
			disabled INTEGER NOT NULL DEFAULT 0,
			failed_logins INTEGER NOT NULL DEFAULT 0,
			locked_until TEXT,
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
		)
	""")


LOCAL_USERNAME = "local"
# Never matches a real password hash, so the placeholder local owner can't be logged into.
UNUSABLE_PASSWORD_HASH = "!"


def _ownership(connection: sqlite3.Connection) -> None:
	"""Give products, stores and scans an owner. Existing rows go to the placeholder 'local' user,
	which the first admin account adopts (see utils.users.claim_local_data)."""

	# Insert or ignore a placeholder user named 'local' with a usuable password hash
	connection.execute(
		"INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
		(LOCAL_USERNAME, UNUSABLE_PASSWORD_HASH),
	)
	#Get the ID of the 'local' user
	owner = connection.execute("SELECT id FROM users WHERE username = ?", (LOCAL_USERNAME,)).fetchone()[0]

	#Iterate over the tables 'products' and 'scans'
	for table in ("products", "scans"):
		# Get the existing columns in the table
		columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
		#Add a 'user_id' column to the table if it doen't exist
		if "user_id" not in columns:
			connection.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
		#update existing rows to assign the 'local' user ID
		connection.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (owner,))
	#Check if 'user_id' exists in the 'stores' table
	store_columns = {row[1] for row in connection.execute("PRAGMA table_info(stores)")}
	if "user_id" not in store_columns:
		# Store numbers are now unique per user, which SQLite can only change by rebuilding the table.
		connection.execute("""
			CREATE TABLE stores_new (
				id INTEGER PRIMARY KEY,
				user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
				retailer TEXT NOT NULL, store_id TEXT NOT NULL,
				name TEXT NOT NULL, location TEXT NOT NULL, zip_state TEXT, zipcode TEXT,
				UNIQUE(user_id, retailer, store_id)
			)
		""")
		# insert data from the old 'stores' table into the new table
		connection.execute("""
			INSERT INTO stores_new (id, user_id, retailer, store_id, name, location, zip_state, zipcode)
			SELECT id, ?, retailer, store_id, name, location, zip_state, zipcode FROM stores
		""", (owner,))
		#Drop the old 'stores' table
		connection.execute("DROP TABLE stores")
		# Rename the new 'stores_new' table to 'stores'
		connection.execute("ALTER TABLE stores_new RENAME TO stores")
	#create indexes
	connection.execute("CREATE INDEX IF NOT EXISTS idx_products_user ON products(user_id)")
	connection.execute("CREATE INDEX IF NOT EXISTS idx_scans_user ON scans(user_id, status)")
	connection.execute("CREATE INDEX IF NOT EXISTS idx_observations_product ON observations(product_id)")


def _auth(connection: sqlite3.Connection) -> None:
	"""Session invalidation, invite links and login rate limiting."""
	#get all columns in the users table
	columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
	#add session_version if not in the users table
	if "session_version" not in columns:
		connection.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
	#ditto for last_login_at
	if "last_login_at" not in columns:
		connection.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
	#Create the invites table if it doesn't exist
	connection.execute("""
		CREATE TABLE IF NOT EXISTS invites (
			id INTEGER PRIMARY KEY,
			token_hash TEXT NOT NULL UNIQUE,
			created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			created_at TEXT NOT NULL,
			expires_at TEXT NOT NULL,
			used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
			used_at TEXT,
			revoked INTEGER NOT NULL DEFAULT 0
		)
	""")
	#ditto for login_attempts table if it doesn't exist
	connection.execute("""
		CREATE TABLE IF NOT EXISTS login_attempts (
			id INTEGER PRIMARY KEY,
			ip TEXT NOT NULL,
			kind TEXT NOT NULL,
			attempted_at REAL NOT NULL
		)
	""")
	#Create an index on the ip and kind columns or the login_attemps table
	connection.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(ip, kind, attempted_at)")


def _api_spend(connection: sqlite3.Connection) -> None:
	"""Paid API calls made per user per UTC day, for daily budgets."""
	connection.execute("""
		CREATE TABLE IF NOT EXISTS api_spend (
			user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			day TEXT NOT NULL,
			calls INTEGER NOT NULL DEFAULT 0,
			PRIMARY KEY (user_id, day)
		)
	""")


MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection], None]]] = [
	("baseline schema", _baseline),
	("users table", _users),
	("per-user ownership", _ownership),
	("authentication", _auth),
	("API spend tracking", _api_spend),
]
LATEST_VERSION = len(MIGRATIONS)


def current_version(connection: sqlite3.Connection) -> int:
	return connection.execute("PRAGMA user_version").fetchone()[0]


def _backup(connection: sqlite3.Connection, backup_path: Path) -> None:
	target = sqlite3.connect(backup_path)
	try:
		connection.backup(target)
	finally:
		target.close()


def migrate(connection: sqlite3.Connection, backup_dir: Path | None = None) -> int:
	"""Apply pending migrations in order and return the resulting version.

	When an existing database is about to change, a copy is saved in backup_dir first.
	Databases newer than this code are refused instead of being downgraded.
	"""
	version = current_version(connection)

	if version > LATEST_VERSION:
		raise MigrationError(
			f"The database is at schema version {version}, but this app only understands up to {LATEST_VERSION}. "
			"Upgrade the app instead of using an older copy."
		)

	if version == LATEST_VERSION:
		return version

	connection.commit()
	has_data = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()

	if backup_dir is not None and has_data:
		backup_dir.mkdir(parents=True, exist_ok=True)
		_backup(connection, backup_dir / f"pre-migration-v{version}.sqlite3")

	# Table rebuilds need foreign keys off, and that pragma is ignored inside a transaction.
	connection.execute("PRAGMA foreign_keys = OFF")
	try:
		for target_version in range(version + 1, LATEST_VERSION + 1):
			name, step = MIGRATIONS[target_version - 1]
			try:
				connection.execute("BEGIN")
				step(connection)
				connection.execute(f"PRAGMA user_version = {target_version}")
				connection.commit()
			except Exception as error:
				connection.rollback()
				raise MigrationError(f"Migration {target_version} ({name}) failed: {error}") from error
	finally:
		connection.execute("PRAGMA foreign_keys = ON")
	return LATEST_VERSION

from __future__ import annotations

import re
import sqlite3
from utils.migrations import LOCAL_USERNAME
from werkzeug.security import generate_password_hash


"""
User account helpers: creating accounts, looking them up, and the placeholder 'local' owner.

Last Update: 10/21/2026
Written on: 10/21/2026
Written by: AJ Utz
"""

ROLES = ("admin", "user")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{3,32}")


def local_user_id(connection: sqlite3.Connection) -> int:
	"""The implicit account that owns data in local mode, and any data from before accounts existed."""
	row = connection.execute("SELECT id FROM users WHERE username = ?", (LOCAL_USERNAME,)).fetchone()
	if row is None:
		raise RuntimeError("The local user is missing; the database has not been migrated.")
	return row[0]


def validate_password(username: str, password: str) -> None:
	"""Length is the only rule (no forced complexity); a password that is just the username is refused."""
	if len(password) < MIN_PASSWORD_LENGTH:
		raise ValueError(f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters.")
	if len(password) > MAX_PASSWORD_LENGTH:
		raise ValueError(f"Passwords can be at most {MAX_PASSWORD_LENGTH} characters.")
	if password.lower() == username.lower():
		raise ValueError("The password can't be the same as the username.")


def validate_new_account(username: str, password: str, role: str) -> None:
	if not USERNAME_PATTERN.fullmatch(username):
		raise ValueError("Usernames must be 3-32 characters: letters, numbers, dot, dash or underscore.")
	if username.lower() == LOCAL_USERNAME:
		raise ValueError(f"'{LOCAL_USERNAME}' is a reserved username.")
	validate_password(username, password)
	if role not in ROLES:
		raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")


def create_user(connection: sqlite3.Connection, username: str, password: str, role: str = "user") -> int:
	"""Create an account with a scrypt-hashed password and return its id."""
	username = username.strip()
	validate_new_account(username, password, role)
	try:
		cursor = connection.execute(
			"INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
			(username, generate_password_hash(password, method="scrypt"), role),
		)
	except sqlite3.IntegrityError:
		raise ValueError("That username is already taken.") from None
	return cursor.lastrowid


def get_user(connection: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
	return connection.execute(
		"SELECT id, username, role, disabled, session_version FROM users WHERE id = ?", (user_id,)
	).fetchone()


def real_user_count(connection: sqlite3.Connection) -> int:
	"""Accounts that can log in (everything except the placeholder local owner)."""
	return connection.execute("SELECT COUNT(*) FROM users WHERE username != ?", (LOCAL_USERNAME,)).fetchone()[0]


def claim_local_data(connection: sqlite3.Connection, user_id: int) -> None:
	"""Move everything owned by the placeholder local user to a real account.

	Meant for the first admin created when switching a local install to LAN or public mode.
	The target account must not already have stores that collide with the local ones.
	"""
	local_id = local_user_id(connection)
	if user_id == local_id:
		return
	for table in ("products", "stores", "scans"):
		connection.execute(f"UPDATE {table} SET user_id = ? WHERE user_id = ?", (user_id, local_id))

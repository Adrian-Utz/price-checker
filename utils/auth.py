from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from utils.migrations import LOCAL_USERNAME, UNUSABLE_PASSWORD_HASH
from utils.users import ROLES, create_user, validate_password
from werkzeug.security import check_password_hash, generate_password_hash


"""
Authentication helpers: password checks with account lockout, per-IP rate limiting,
password changes, role/disable management, and invite links.
Login, logout and session handling live in main.py.

Last Update: 10/23/2026
Written on: 10/20/2026
Written by: AJ Utz
"""

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
IP_WINDOW_SECONDS = 15 * 60
INVITE_MAX_HOURS = 24 * 30
_dummy_hash: str | None = None


def utc_text(moment: datetime | None = None) -> str:
	return (moment or datetime.now(timezone.utc)).strftime(TIME_FORMAT)


def _burn_password_check(password: str) -> None:
	"""Spend the same time as a real check so unknown or locked accounts can't be told apart by timing."""
	global _dummy_hash
	if _dummy_hash is None:
		_dummy_hash = generate_password_hash(secrets.token_hex(16), method="scrypt")
	check_password_hash(_dummy_hash, password)


def authenticate(connection: sqlite3.Connection, username: str, password: str, *, max_failures: int, lockout_minutes: int) -> sqlite3.Row | None:
	"""Return the user row for a correct password, otherwise None.

	Every failure looks the same to the caller. A locked account isn't checked at all, so guessing
	can't continue during the lockout.
	"""
	user = connection.execute(
		"SELECT id, username, role, disabled, session_version, password_hash, locked_until FROM users WHERE username = ? AND username != ?",
		(username.strip(), LOCAL_USERNAME),
	).fetchone()
	if user is None or user["password_hash"] == UNUSABLE_PASSWORD_HASH or user["disabled"]:
		_burn_password_check(password)
		return None
	if user["locked_until"] and user["locked_until"] > utc_text():
		_burn_password_check(password)
		return None
	if check_password_hash(user["password_hash"], password):
		connection.execute(
			"UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
			(utc_text(), user["id"]),
		)
		return user
	connection.execute("UPDATE users SET failed_logins = failed_logins + 1 WHERE id = ?", (user["id"],))
	failures = connection.execute("SELECT failed_logins FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
	if failures >= max_failures:
		locked_until = utc_text(datetime.now(timezone.utc) + timedelta(minutes=lockout_minutes))
		connection.execute("UPDATE users SET failed_logins = 0, locked_until = ? WHERE id = ?", (locked_until, user["id"]))
	return None


def retry_after_seconds(connection: sqlite3.Connection, ip: str, kind: str, limit: int, window: int = IP_WINDOW_SECONDS) -> int:
	"""Seconds until this address may try again, or 0 when it is under the limit."""
	now = time.time()
	attempts = [
		row[0] for row in connection.execute(
			"SELECT attempted_at FROM login_attempts WHERE ip = ? AND kind = ? AND attempted_at > ? ORDER BY attempted_at",
			(ip, kind, now - window),
		)
	]
	if len(attempts) < limit:
		return 0
	return max(1, math.ceil(attempts[len(attempts) - limit] + window - now))


def record_attempt(connection: sqlite3.Connection, ip: str, kind: str) -> None:
	now = time.time()
	connection.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (now - 24 * 3600,))
	connection.execute("INSERT INTO login_attempts (ip, kind, attempted_at) VALUES (?, ?, ?)", (ip, kind, now))


def set_password(connection: sqlite3.Connection, user_id: int, password: str) -> None:
	"""Replace a password, unlock the account, and sign out every existing session."""
	user = connection.execute("SELECT username FROM users WHERE id = ? AND username != ?", (user_id, LOCAL_USERNAME)).fetchone()
	if user is None:
		raise ValueError("That account does not exist.")
	validate_password(user["username"], password)
	connection.execute(
		"UPDATE users SET password_hash = ?, session_version = session_version + 1, failed_logins = 0, locked_until = NULL WHERE id = ?",
		(generate_password_hash(password, method="scrypt"), user_id),
	)


def _managed_user(connection: sqlite3.Connection, actor_id: int, target_id: int) -> sqlite3.Row:
	if actor_id == target_id:
		raise ValueError("You can't change your own role or status. Ask another administrator.")
	target = connection.execute("SELECT id, username FROM users WHERE id = ? AND username != ?", (target_id, LOCAL_USERNAME)).fetchone()
	if target is None:
		raise ValueError("That account does not exist.")
	return target


def set_role(connection: sqlite3.Connection, actor_id: int, target_id: int, role: str) -> None:
	if role not in ROLES:
		raise ValueError("Unknown role.")
	_managed_user(connection, actor_id, target_id)
	connection.execute("UPDATE users SET role = ? WHERE id = ?", (role, target_id))


def set_disabled(connection: sqlite3.Connection, actor_id: int, target_id: int, disabled: bool) -> None:
	_managed_user(connection, actor_id, target_id)
	connection.execute(
		"UPDATE users SET disabled = ?, session_version = session_version + 1, failed_logins = 0, locked_until = NULL WHERE id = ?",
		(int(disabled), target_id),
	)


def list_users(connection: sqlite3.Connection) -> list[sqlite3.Row]:
	return connection.execute(
		"""
		SELECT u.id, u.username, u.role, u.disabled, u.created_at, u.last_login_at, u.locked_until,
			(SELECT COUNT(*) FROM products WHERE user_id = u.id) AS product_count
		FROM users u WHERE u.username != ? ORDER BY u.username COLLATE NOCASE
		""",
		(LOCAL_USERNAME,),
	).fetchall()


def _hash_token(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def create_invite(connection: sqlite3.Connection, created_by: int, hours: int) -> str:
	"""Create a single-use invite link token. Only its hash is stored, so it can't be shown again."""
	if not 1 <= hours <= INVITE_MAX_HOURS:
		raise ValueError(f"Invites last between 1 hour and {INVITE_MAX_HOURS} hours.")
	token = secrets.token_urlsafe(24)
	now = datetime.now(timezone.utc)
	connection.execute(
		"INSERT INTO invites (token_hash, created_by, created_at, expires_at) VALUES (?, ?, ?, ?)",
		(_hash_token(token), created_by, utc_text(now), utc_text(now + timedelta(hours=hours))),
	)
	return token


def find_invite(connection: sqlite3.Connection, token: str) -> sqlite3.Row | None:
	"""The invite for a token if it is unused, not revoked and not expired."""
	if not token:
		return None
	return connection.execute(
		"SELECT id FROM invites WHERE token_hash = ? AND used_at IS NULL AND revoked = 0 AND expires_at > ?",
		(_hash_token(token), utc_text()),
	).fetchone()


def accept_invite(connection: sqlite3.Connection, token: str, username: str, password: str) -> int:
	"""Create a regular user from an invite. The invite is consumed in the same transaction, so a link works once."""
	invite = find_invite(connection, token)
	if invite is None:
		raise ValueError("This invite link is invalid or has expired.")
	user_id = create_user(connection, username, password, "user")
	claimed = connection.execute(
		"UPDATE invites SET used_by = ?, used_at = ? WHERE id = ? AND used_at IS NULL",
		(user_id, utc_text(), invite["id"]),
	)
	if claimed.rowcount != 1:
		raise ValueError("This invite link is invalid or has expired.")
	return user_id


def list_invites(connection: sqlite3.Connection) -> list[sqlite3.Row]:
	return connection.execute(
		"""
		SELECT i.id, i.created_at, i.expires_at, u.username AS created_by_name
		FROM invites i JOIN users u ON u.id = i.created_by
		WHERE i.used_at IS NULL AND i.revoked = 0 AND i.expires_at > ?
		ORDER BY i.id DESC
		""",
		(utc_text(),),
	).fetchall()


def revoke_invite(connection: sqlite3.Connection, invite_id: int) -> None:
	connection.execute("UPDATE invites SET revoked = 1 WHERE id = ? AND used_at IS NULL", (invite_id,))

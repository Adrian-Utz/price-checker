from __future__ import annotations

import sqlite3

"""
Shared watchlists. Products and saved stores belong to a list, and every user assigned to a list sees the same items,
so an edit made by one member (store, provider) is seen by all of them. Only administrators change list membership.
"""

MAX_LIST_NAME_LENGTH = 60

#Lists a user can see; bind the user id once.
MEMBER_LISTS_SQL = "SELECT list_id FROM list_members WHERE user_id = ?"


def clean_name(name: str) -> str:
	name = " ".join(name.split())
	if not name or len(name) > MAX_LIST_NAME_LENGTH:
		raise ValueError(f"A list name must be 1-{MAX_LIST_NAME_LENGTH} characters.")
	return name


def create_list(connection: sqlite3.Connection, name: str) -> int:
	try:
		return connection.execute("INSERT INTO lists (name) VALUES (?)", (clean_name(name),)).lastrowid
	except sqlite3.IntegrityError:
		raise ValueError("A list with that name already exists.") from None


def delete_list(connection: sqlite3.Connection, list_id: int) -> None:
	"""Refuses a list that still has items, so removing a list can never silently delete price history."""
	if connection.execute("SELECT 1 FROM products WHERE list_id = ? LIMIT 1", (list_id,)).fetchone():
		raise ValueError("Remove or import the items out of this list before deleting it.")
	if connection.execute("DELETE FROM lists WHERE id = ?", (list_id,)).rowcount == 0:
		raise ValueError("That list does not exist.")


def add_member(connection: sqlite3.Connection, list_id: int, user_id: int) -> None:
	if not connection.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone():
		raise ValueError("That list does not exist.")
	if not connection.execute("SELECT 1 FROM users WHERE id = ? AND password_hash != '!'", (user_id,)).fetchone():
		raise ValueError("That user does not exist.")
	connection.execute("INSERT OR IGNORE INTO list_members (list_id, user_id) VALUES (?, ?)", (list_id, user_id))


def remove_member(connection: sqlite3.Connection, list_id: int, user_id: int) -> None:
	connection.execute("DELETE FROM list_members WHERE list_id = ? AND user_id = ?", (list_id, user_id))


def all_lists(connection: sqlite3.Connection) -> list[dict]:
	"""Every list with its members and item count, for the administrator's page."""
	lists = []
	for row in connection.execute("SELECT id, name FROM lists ORDER BY name COLLATE NOCASE"):
		members = connection.execute(
			"SELECT u.id, u.username FROM list_members m JOIN users u ON u.id = m.user_id WHERE m.list_id = ? ORDER BY u.username COLLATE NOCASE",
			(row["id"],),
		).fetchall()
		count = connection.execute("SELECT COUNT(*) FROM products WHERE list_id = ?", (row["id"],)).fetchone()[0]
		lists.append({"id": row["id"], "name": row["name"], "members": [dict(m) for m in members], "product_count": count})
	return lists


def lists_for_user(connection: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
	return connection.execute(
		f"SELECT id, name FROM lists WHERE id IN ({MEMBER_LISTS_SQL}) ORDER BY name COLLATE NOCASE", (user_id,)
	).fetchall()


def ensure_personal_list(connection: sqlite3.Connection, user_id: int, name: str | None = None) -> int:
	"""The user's first list, creating one named after them when they have none."""
	existing = connection.execute(
		"SELECT list_id FROM list_members WHERE user_id = ? ORDER BY list_id LIMIT 1", (user_id,)
	).fetchone()
	if existing:
		return existing[0]
	base = name or connection.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()[0]
	candidate, number = base, 1
	while connection.execute("SELECT 1 FROM lists WHERE name = ?", (candidate,)).fetchone():
		number += 1
		candidate = f"{base} ({number})"
	list_id = connection.execute("INSERT INTO lists (name) VALUES (?)", (candidate,)).lastrowid
	connection.execute("INSERT INTO list_members (list_id, user_id) VALUES (?, ?)", (list_id, user_id))
	return list_id

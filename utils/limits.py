from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


"""
Per-user and server-wide daily budgets for paid API calls. One call is one credit.
Spend is counted in the database, so it survives restarts and is shared by every worker process.

Last Update: 10/27/2026
Written on: 10/27/2026
Written by: AJ Utz
"""

KEEP_DAYS = 90


class BudgetExceeded(Exception):
	"""Raised before an API call is made when a daily budget has no credits left."""

	def __init__(self, scope: str, limit: int):
		self.scope = scope
		self.limit = limit
		super().__init__(f"The {scope} limit of {limit} API calls per day has been reached. It resets at midnight UTC.")


def today() -> str:
	return datetime.now(timezone.utc).date().isoformat()


def calls_today(connection: sqlite3.Connection, user_id: int, day: str | None = None) -> int:
	row = connection.execute("SELECT calls FROM api_spend WHERE user_id = ? AND day = ?", (user_id, day or today())).fetchone()
	return row[0] if row else 0


def reserve_api_call(connection: sqlite3.Connection, user_id: int, *, user_limit: int, global_limit: int) -> None:
	"""Count one API call against the user, or raise BudgetExceeded without counting it.

	A limit of 0 means unlimited. The whole check-and-increment runs under a write lock,
	so two simultaneous scans can't both slip past the last credit.
	"""
	day = today()
	connection.execute("BEGIN IMMEDIATE")
	if user_limit and calls_today(connection, user_id, day) >= user_limit:
		raise BudgetExceeded("per-user daily", user_limit)
	if global_limit:
		total = connection.execute("SELECT COALESCE(SUM(calls), 0) FROM api_spend WHERE day = ?", (day,)).fetchone()[0]
		if total >= global_limit:
			raise BudgetExceeded("server-wide daily", global_limit)
	connection.execute(
		"INSERT INTO api_spend (user_id, day, calls) VALUES (?, ?, 1) ON CONFLICT(user_id, day) DO UPDATE SET calls = calls + 1",
		(user_id, day),
	)
	cutoff = datetime.now(timezone.utc).date() - timedelta(days=KEEP_DAYS)
	connection.execute("DELETE FROM api_spend WHERE day < ?", (cutoff.isoformat(),))

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def parse_schedule(frequency: str, start_at: str, now: datetime) -> tuple[str, str]:
	if frequency not in {"once", "daily", "weekly", "monthly"}:
		raise ValueError("Choose a valid frequency.")
	try:
		start = datetime.strptime(start_at, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
	except ValueError:
		raise ValueError("Enter a valid start date and time in UTC.") from None
	if start <= now:
		raise ValueError("The start date and time must be in the future.")
	return frequency, start.strftime("%Y-%m-%dT%H:%M")


def due(schedule: sqlite3.Row, now: datetime) -> bool:
	start = datetime.strptime(schedule["start_at"], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
	minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
	if minute < start or minute.time() != start.time():
		return False
	frequency = schedule["frequency"]
	return (
		(frequency == "once" and minute == start)
		or frequency == "daily"
		or (frequency == "weekly" and minute.weekday() == start.weekday())
		or (frequency == "monthly" and minute.day == start.day)
	)

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def parse_schedule(frequency: str, start_at: str, now: datetime) -> tuple[str, str]:
	"""
	Parses the frequency and start_at fields to ensure they are valid
	:param frequency: the frequency of the scan
	:param start_at: the start date and time of the schedule in UTC format
	:param now: the current datetime object
	returns a tuple containing the frequency and the formatted start_at datetime string
	raises a ValueErrorif the frequency is invalid, or the start_at date and time is invalid or in the past
	"""
	if frequency not in {"once", "daily", "weekly", "monthly"}:
		raise ValueError("Choose a valid frequency.")
	try:
		#Parse the start_at string into a datetime object with UTC timezone
		start = datetime.strptime(start_at, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
	except ValueError:
		#Raises a ValueError if the start_at format is incorrect
		raise ValueError("Enter a valid start date and time in UTC.") from None
	#Ensure the start time is in the future
	if start <= now:
		raise ValueError("The start date and time must be in the future.")
	#Return the frequency and formatted start_at datetime string
	return frequency, start.strftime("%Y-%m-%dT%H:%M")


def due(schedule: sqlite3.Row, now: datetime) -> bool:
	"""
	Determines if the schedule is due to run based on the current time.
	:param schedule: a sqlite3.Row object representing the schedule
	:param now: The current datetime object
	returns a boolean indicating if the schedule is due to run
	"""
	#Parse the start_at string from the schedule into a datetime object with UTC timezone
	start = datetime.strptime(schedule["start_at"], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
	#Get the current minute and time
	minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
	#Check if the curent minute is less than the start minute or if it's not the same time
	if minute < start or minute.time() != start.time():
		return False
	#Determine if the schedule is due based on its frequency
	frequency = schedule["frequency"]
	return (
		(frequency == "once" and minute == start) #One time
		or frequency == "daily" #Daily
		or (frequency == "weekly" and minute.weekday() == start.weekday()) #Weekly
		or (frequency == "monthly" and minute.day == start.day)#Monthly
	)

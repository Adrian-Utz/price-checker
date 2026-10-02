from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime

from markupsafe import Markup

"""
This file contains the functions needed for line chart creation.

Last Update: 10/1/2026
Written on: 10/1/2026
Written by: AJ Utz
"""
MAX_CHART_POINTS = 12


def format_observed_at_utc(timestamp: str) -> str:
	return timestamp.replace("T", " ")[:16] + " UTC"


def price_chart(observations: list[sqlite3.Row], width: int = 360) -> Markup | None:
	"""Generate an SVG line chart for regular and bulk price observations."""
	points = [(row["observed_at"], float(row["price"]), float(row["bulk_price"]) if "bulk_price" in row.keys() and row["bulk_price"] is not None else None)
		for row in observations if row["price"] is not None]
	if not points:
		return None
	bulk_points = [(index, observed_at, bulk_price) for index, (observed_at, _price, bulk_price) in enumerate(points) if bulk_price is not None]

	height = 180
	left, right, top, bottom = 52, 12, 12, 18
	plot_width, plot_height = width - left - right, height - top - bottom
	prices = [price for _, price, _ in points]
	prices.extend(price for _, _, price in bulk_points)

	minimum, maximum = min(prices), max(prices)
	target_interval = max((maximum - minimum) / 4, 0.5)
	intervals = (0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
	tick_interval = next((interval for interval in intervals if interval >= target_interval), None)
	if tick_interval is None:
		tick_interval = math.ceil(target_interval / 5000) * 5000
	lower_bound = math.floor(minimum / tick_interval) * tick_interval
	upper_bound = math.ceil(maximum / tick_interval) * tick_interval
	if lower_bound == upper_bound:
		lower_bound -= tick_interval
		upper_bound += tick_interval
	tick_count = math.ceil((upper_bound - lower_bound) / tick_interval)
	upper_bound = lower_bound + tick_count * tick_interval
	spread = upper_bound - lower_bound

	coordinates = []
	for index, (observed_at, price, _bulk_price) in enumerate(points):
		x = left + (plot_width * index / max(len(points) - 1, 1))
		y = top + plot_height - ((price - lower_bound) / spread * plot_height)
		coordinates.append((x, y, observed_at, price))
	bulk_coordinates = []
	for index, observed_at, price in bulk_points:
		x = left + (plot_width * index / max(len(points) - 1, 1))
		y = top + plot_height - ((price - lower_bound) / spread * plot_height)
		bulk_coordinates.append((x, y, observed_at, price))

	line = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in coordinates)
	marks = []
	for x, y, observed_at, price in coordinates:
		observed_date = format_observed_at_utc(observed_at)
		marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" tabindex="0"><title>${price:,.2f} on {observed_date}</title></circle>')
	bulk_line = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in bulk_coordinates)
	bulk_marks = []
	for x, y, observed_at, price in bulk_coordinates:
		observed_date = format_observed_at_utc(observed_at)
		bulk_marks.append(f'<circle class="bulk-price-point" cx="{x:.1f}" cy="{y:.1f}" r="4" tabindex="0"><title>Bulk price ${price:,.2f} on {observed_date}</title></circle>')
	bulk_series = (
		f'<polyline class="bulk-price-line" points="{bulk_line}" fill="none" stroke="var(--lg-bulk-line, #d46b36)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
		if bulk_coordinates else ""
	)
	legend = (
		f'<g class="chart-legend" font-size="8"><line x1="{left + 8}" y1="8" x2="{left + 20}" y2="8" stroke="var(--lg-main-line)" stroke-width="2"/><text x="{left + 23}" y="11" fill="#6c756b">Price</text>'
		f'<line x1="{left + 62}" y1="8" x2="{left + 74}" y2="8" stroke="var(--lg-bulk-line, #d46b36)" stroke-width="2"/><text x="{left + 77}" y="11" fill="#6c756b">Bulk price</text></g>'
		if bulk_coordinates else ""
	)

	axis_y = top + plot_height
	y_axis_ticks = []
	for index in range(tick_count + 1):
		price = lower_bound + index * tick_interval
		y = axis_y - (index / tick_count * plot_height)
		y_axis_ticks.append(
			f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e2e4d9" stroke-width="1"/>'
			f'<text x="{left - 6}" y="{y + 3:.1f}" fill="#6c756b" font-size="9" text-anchor="end">${price:,.2f}</text>'
		)

	date_indexes = sorted({0, len(coordinates) // 2, len(coordinates) - 1})
	x_axis_labels = []
	for index in date_indexes:
		x, _, observed_at, _ = coordinates[index]
		observed_date = format_observed_at_utc(observed_at).split(" ")[0]
		if index == 0:
			anchor = "start"
		elif index == len(coordinates) - 1:
			anchor = "end"
		else:
			anchor = "middle"
		x_axis_labels.append(
			f'<text x="{x:.1f}" y="{height - 4}" fill="#6c756b" font-size="9" text-anchor="{anchor}">{observed_date}</text>'
		)

	return Markup(
		f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Price history with {len(points)} observations">'
		f'{"".join(y_axis_ticks)}<line x1="{left}" y1="{top}" x2="{left}" y2="{axis_y}" stroke="#9ca597" stroke-width="1"/>'
		f'<line x1="{left}" y1="{axis_y}" x2="{width - right}" y2="{axis_y}" stroke="#9ca597" stroke-width="1"/>'
		f'<polyline points="{line}" fill="none" stroke="var(--lg-main-line)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
		f'{bulk_series}{"".join(marks)}{"".join(bulk_marks)}{"".join(x_axis_labels)}{legend}</svg>'
	)


def chart_observations_in_range(observations: list[sqlite3.Row], start_date: date, end_date: date) -> list[sqlite3.Row]:
	"""Keep observations inside a date range, then retain its largest price movements."""
	if not observations:
		return []
	ranged = [
		row for row in observations
		if start_date <= datetime.fromisoformat(row["observed_at"]).date() <= end_date
	]
	if len(ranged) <= MAX_CHART_POINTS:
		return ranged

	movements = [
		(abs(float(ranged[index]["price"]) - float(ranged[index - 1]["price"])), index)
		for index in range(1, len(ranged) - 1)
		if float(ranged[index]["price"]) != float(ranged[index - 1]["price"])
	]
	bulk_movements = [
		(abs(float(ranged[index]["bulk_price"]) - float(ranged[index - 1]["bulk_price"])), index)
		for index in range(1, len(ranged) - 1)
		if "bulk_price" in ranged[index].keys() and "bulk_price" in ranged[index - 1].keys()
		and ranged[index]["bulk_price"] is not None and ranged[index - 1]["bulk_price"] is not None
		and float(ranged[index]["bulk_price"]) != float(ranged[index - 1]["bulk_price"])
	]
	indexes = {0, len(ranged) - 1}
	for _change, index in sorted(movements + bulk_movements, reverse=True)[:MAX_CHART_POINTS - 2]:
		indexes.add(index)
	return [ranged[index] for index in sorted(indexes)]
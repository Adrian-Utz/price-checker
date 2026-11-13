import re
from urllib.parse import unquote, urlparse

"""Helper method for URL normilization, product- name extraction, price parsing, and HTTP status checking

Written by: AJ Utz
Written on: 11/11/2026
Latest update: 11/11/2026
"""

def normalize_url(value: str) -> str:
	"""
	Check that the URL is complete and uses HTTP or HTTPS, then remove its fragment.
	"""
	parsed = urlparse(value.strip())
	if parsed.scheme not in {"http", "https"} or not parsed.netloc:
		raise ValueError("Enter a complete http or https URL.")
	return parsed._replace(fragment="").geturl()


def parse_price(text: str) -> float | None:
	"""Return the lowest dollar or USD amount found in text."""
	matches = re.findall(r"(?:\$|USD\s*)(\d{1,5}(?:,\d{3})?(?:\.\d{2})?)", text, re.IGNORECASE)
	values = [float(match.replace(",", "")) for match in matches]
	return min(values) if values else None


def ensure_success_status(status: int | None) -> None:
	"""Make sure the status passes and is not Greater than 400"""
	if status is not None and status >= 400:
		raise RuntimeError(f"The retailer returned HTTP {status}; no product page was available.")


def product_query(url: str) -> str:
	path_parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
	if path_parts and path_parts[0].lower() == "pd":
		path_parts = path_parts[1:]
	if path_parts and re.fullmatch(r"\d+", path_parts[-1]):
		path_parts.pop()
	name = re.sub(r"[-_]+", " ", path_parts[-1] if path_parts else "")
	return re.sub(r"\s+", " ", name).strip()

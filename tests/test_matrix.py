"""Route-wide checks. These walk app.url_map, so a new route that forgets login, admin or ownership checks fails here."""
import dataclasses
import json
import time

import pytest
from flask.testing import FlaskClient

import main
from utils.config import ConfigError, load_settings, startup_errors
from utils.database import connect
from utils.migrations import LATEST_VERSION, migrate
from utils.users import create_user

PASSWORD = "cave johnsons correct horse battery"
HOST = "prices.example.com"
OPEN_ENDPOINTS = {"login", "register", "static", "healthz"}


@pytest.fixture(params=["lan", "public"])
def hosted(request, monkeypatch):
	"""App in LAN or public mode with an admin, alice and bob. Returns a function that signs a client in."""
	extra = {"allowed_hosts": (HOST,), "trusted_proxies": 1} if request.param == "public" else {}
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(
		main.SETTINGS, mode=request.param, auth_required=True, auto_shutdown=False, allow_env_editor=False, allow_dev_tools=False, **extra))
	monkeypatch.setattr(main, "auto_server_shutdown", False)
	monkeypatch.setattr(main, "edit_env", False)
	main.app.testing = True
	with connect() as connection:
		ids = {name: create_user(connection, name, PASSWORD, role) for name, role in (("root", "admin"), ("alice", "user"), ("bob", "user"))}
	base_url = f"https://{HOST}" if request.param == "public" else "http://localhost"

	class HostedClient(FlaskClient):
		def open(self, *args, **kwargs):
			kwargs.setdefault("base_url", base_url)
			return super().open(*args, **kwargs)

	def client_for(name=None):
		client = HostedClient(main.app, main.app.response_class, use_cookies=True)
		if name:
			with client.session_transaction(base_url=base_url) as session:
				session.update(user_id=ids[name], sv=0, last_seen=int(time.time()))
		return client

	client_for.ids = ids
	client_for.mode = request.param
	return client_for


def routes():
	for rule in main.app.url_map.iter_rules():
		for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
			yield rule, method


def fill(rule, value=1):
	return main.app.url_map.bind("localhost").build(rule.endpoint, {arg: value for arg in rule.arguments}, method="GET" if "GET" in rule.methods else "POST")


def send(client, rule, method, value=1):
	return client.open(fill(rule, value), method=method, data={})


def test_the_sweep_actually_finds_routes():
	assert len(list(routes())) > 30


# Auth required in lan and public, local unchanged

def test_every_route_requires_login_outside_local_mode(hosted):
	for rule, method in routes():
		if rule.endpoint in OPEN_ENDPOINTS:
			continue
		response = send(hosted(), rule, method)
		label = f"{method} {rule.rule}"
		assert response.status_code in (302, 400, 401, 403, 404), label
		if response.status_code == 302:
			assert "/login" in response.headers["Location"], label


def test_open_endpoints_work_without_login(hosted):
	assert hosted().get("/healthz").status_code == 200
	assert hosted().get("/login").status_code == 200


def test_anonymous_requests_never_leak_watchlist_data(hosted):
	with connect() as connection:
		connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, 'https://example.com/secret-item', 'T')", (hosted.ids["alice"],))
	for path in ("/", "/export/json", "/export/csv", "/chart/1", "/account", "/admin/users"):
		response = hosted().get(path, follow_redirects=True)
		assert "secret-item" not in response.get_data(as_text=True), path


def test_local_mode_needs_no_login_and_has_no_account_pages():
	client = main.app.test_client()
	assert client.get("/").status_code == 200
	assert client.get("/login").status_code == 404
	assert client.get("/admin/users").status_code == 404
	assert client.get("/export/json").status_code == 200


# Cross-user isolation on every route

def test_other_users_ids_are_404_on_every_id_route(hosted, monkeypatch):
	monkeypatch.setattr(main, "refresh_product", lambda product: None)
	with connect() as connection:
		product = connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, 'https://example.com/bobs', 'T')", (hosted.ids["bob"],)).lastrowid
		store = connection.execute("INSERT INTO stores (user_id, retailer, store_id, name, location) VALUES (?, 'Walmart', '9', 'S', 'L')", (hosted.ids["bob"],)).lastrowid
		connection.execute("INSERT INTO observations (product_id, price, status, observed_at) VALUES (?, 5, 'success', '2026-08-20T12:00:00+00:00')", (product,))
	checked = 0
	for rule, method in routes():
		if rule.rule.startswith(("/admin", "/dev")) or not rule.arguments or rule.endpoint in OPEN_ENDPOINTS:
			continue
		target = product if "product_id" in rule.arguments else store
		assert rule.arguments <= {"product_id", "store_record_id"}, f"New id route {rule.rule} needs an isolation check"
		assert send(hosted("alice"), rule, method, target).status_code == 404, f"{method} {rule.rule}"
		checked += 1
	assert checked >= 6
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM products WHERE user_id = ?", (hosted.ids["bob"],)).fetchone()[0] == 1
		assert connection.execute("SELECT COUNT(*) FROM stores WHERE user_id = ?", (hosted.ids["bob"],)).fetchone()[0] == 1


def test_exports_contain_only_the_signed_in_users_products(hosted):
	with connect() as connection:
		for name in ("alice", "bob"):
			connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, ?, 'T')", (hosted.ids[name], f"https://example.com/{name}-item"))
	for path in ("/export/json", "/export/csv"):
		body = hosted("alice").get(path).get_data(as_text=True)
		assert "alice-item" in body and "bob-item" not in body, path


def test_import_lands_only_in_the_signed_in_account(hosted):
	payload = json.dumps({"products": [{"url": "https://www.walmart.com/ip/imported/1", "source": "Walmart", "history": []}]})
	import io
	response = hosted("alice").post("/import", data={"database_file": (io.BytesIO(payload.encode()), "list.json")}, content_type="multipart/form-data")
	assert response.status_code in (200, 302)
	with connect() as connection:
		owners = {row[0] for row in connection.execute("SELECT user_id FROM products WHERE url LIKE '%imported%'")}
	assert owners == {hosted.ids["alice"]}


def test_global_api_settings_are_admin_only(hosted):
	response = hosted("alice").post("/settings/provider", data={"provider": "serpapi"})
	assert response.status_code == 403
	assert "API usage" not in hosted("alice").get("/").get_data(as_text=True)


# Admin-only routes

def test_every_admin_route_refuses_regular_users(hosted):
	seen = 0
	for rule, method in routes():
		if not rule.rule.startswith("/admin"):
			continue
		response = send(hosted("alice"), rule, method)
		assert response.status_code in (403, 404), f"{method} {rule.rule}"
		seen += 1
	assert seen >= 6


def test_admin_pages_open_for_admins(hosted):
	assert hosted("root").get("/admin/users").status_code == 200


# Routes that must be disabled outside local mode

@pytest.mark.parametrize("method, path", [
	("post", "/shutdown"),
	("post", "/settings/env"),
	("get", "/export/db"),
])
def test_local_only_routes_are_disabled_even_for_admins(hosted, method, path):
	response = getattr(hosted("root"), method)(path, data={})
	assert response.status_code in (403, 404)


def test_no_tab_close_beacon_or_env_editor_in_the_page(hosted):
	body = hosted("root").get("/").get_data(as_text=True)
	assert "sendBeacon" not in body
	assert "env-editor" not in body


# TLS configuration

BASE = {"PRICE_CHECKER_SECRET": "s" * 40}


def lan_env(tmp_path, **extra):
	return {**BASE, "PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_DATA_DIR": str(tmp_path), **extra}


def test_tls_off_serves_http(tmp_path):
	settings = load_settings(lan_env(tmp_path))
	assert settings.scheme == "http" and not settings.secure_cookies
	assert startup_errors(settings, admin_exists=True) == []


def test_selfsigned_tls_needs_no_files(tmp_path):
	settings = load_settings(lan_env(tmp_path, PRICE_CHECKER_TLS="selfsigned"))
	assert settings.scheme == "https" and settings.secure_cookies
	assert startup_errors(settings, admin_exists=True) == []


def test_selfsigned_uses_supplied_files_or_generates_its_own(tmp_path):
	generated = load_settings(lan_env(tmp_path, PRICE_CHECKER_TLS="selfsigned"))
	assert generated.tls_cert == tmp_path / "certs" / "selfsigned.pem"
	supplied = load_settings(lan_env(tmp_path, PRICE_CHECKER_TLS="selfsigned", PRICE_CHECKER_TLS_CERT="a.pem", PRICE_CHECKER_TLS_KEY="b.pem"))
	assert str(supplied.tls_cert) == "a.pem"


@pytest.mark.parametrize("present", ["cert", "key"])
def test_custom_tls_with_only_one_file_is_refused(tmp_path, present):
	file = tmp_path / "one.pem"
	file.write_text("x")
	setting = "PRICE_CHECKER_TLS_CERT" if present == "cert" else "PRICE_CHECKER_TLS_KEY"
	errors = startup_errors(load_settings(lan_env(tmp_path, PRICE_CHECKER_TLS="custom", **{setting: str(file)})), admin_exists=True)
	assert len(errors) == 1


def test_tls_setting_is_ignored_outside_lan_mode(tmp_path):
	for mode in ("local", "public"):
		env = {**BASE, "PRICE_CHECKER_MODE": mode, "PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_TLS": "custom", "PRICE_CHECKER_ALLOWED_HOSTS": HOST}
		assert load_settings(env).tls == "off"


# Migrations

def test_migrating_twice_changes_nothing(tmp_path):
	import sqlite3
	connection = sqlite3.connect(tmp_path / "m.sqlite3")
	connection.row_factory = sqlite3.Row
	assert migrate(connection) == LATEST_VERSION
	before = connection.execute("SELECT group_concat(name) FROM sqlite_master ORDER BY name").fetchone()[0]
	assert migrate(connection) == LATEST_VERSION
	assert connection.execute("SELECT group_concat(name) FROM sqlite_master ORDER BY name").fetchone()[0] == before

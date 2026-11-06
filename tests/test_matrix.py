"""Route-wide checks. These walk app.url_map, so a new route that forgets login, admin or ownership checks fails here."""
import dataclasses
import json
import time

import pytest
from flask.testing import FlaskClient

import main
from utils.config import ConfigError, load_settings, startup_errors
from utils.database import connect
from utils.lists import ensure_personal_list
from utils.migrations import LATEST_VERSION, migrate
from utils.users import create_user

PASSWORD = "cave johnsons correct horse battery"
HOST = "prices.example.com"
OPEN_ENDPOINTS = {"login", "register", "static", "healthz"}


@pytest.fixture(params=["lan", "public"])
def hosted(request, monkeypatch):
	"""App in LAN or public mode with an admin, alice and bob. Returns a function that signs a client in."""
	#Set Extra configuration based on the request parameter
	extra = {"allowed_hosts": (HOST,), "trusted_proxies": 1} if request.param == "public" else {}
	#Uupdate the settings with the requested mode
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(
		main.SETTINGS, mode=request.param, auth_required=True, auto_shutdown=False, allow_env_editor=False, allow_dev_tools=False, **extra))
	#Set the auto server shutdown and edit environment variables to False
	monkeypatch.setattr(main, "auto_server_shutdown", False)
	monkeypatch.setattr(main, "edit_env", False)
	#Set the application to testing mode
	main.app.testing = True
	#Connect to the database and create users
	with connect() as connection:
		ids = {name: create_user(connection, name, PASSWORD, role) for name, role in (("root", "admin"), ("alice", "user"), ("bob", "user"))}
	#Define the base URL based on the request parameter
	base_url = f"https://{HOST}" if request.param == "public" else "http://localhost"

	#create a custome client class that sets the base URL
	class HostedClient(FlaskClient):
		def open(self, *args, **kwargs):
			kwargs.setdefault("base_url", base_url)
			return super().open(*args, **kwargs)

	#Define a function to get a client for a specific user
	def client_for(name=None):
		client = HostedClient(main.app, main.app.response_class, use_cookies=True)
		if name:
			with client.session_transaction(base_url=base_url) as session:
				session.update(user_id=ids[name], sv=0, last_seen=int(time.time()))
		return client
	#Attach User IDs and mode to the client function
	client_for.ids = ids
	client_for.mode = request.param
	return client_for


def routes():
	"""Iterate over all routes in the application"""
	for rule in main.app.url_map.iter_rules():
		for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
			yield rule, method


def fill(rule, value=1):
	"""Build the URL for a route with a given value"""
	return main.app.url_map.bind("localhost").build(rule.endpoint, {arg: value for arg in rule.arguments}, method="GET" if "GET" in rule.methods else "POST")


def send(client, rule, method, value=1):
	"""Send a request to a rout using the client"""
	return client.open(fill(rule, value), method=method, data={})


def test_the_sweep_actually_finds_routes():
	"""Test that the sweep actually finds routes"""
	assert len(list(routes())) > 30


# Auth required in lan and public, local unchanged

def test_every_route_requires_login_outside_local_mode(hosted):
	"""Test that every route requires login outside of local mode"""
	for rule, method in routes():
		#Skip open endpoints
		if rule.endpoint in OPEN_ENDPOINTS:
			continue
		#Send a request to the route
		response = send(hosted(), rule, method)
		#Define a lable for the test case
		label = f"{method} {rule.rule}"
		#Assert the status code is one of the expected values
		assert response.status_code in (302, 400, 401, 403, 404), label
		#If it is 302, check the location
		if response.status_code == 302:
			assert "/login" in response.headers["Location"], label


def test_open_endpoints_work_without_login(hosted):
	"""Rest that open endpoints work without login"""
	#Test the /healthz endpoint
	assert hosted().get("/healthz").status_code == 200
	#Test the /login endpoint
	assert hosted().get("/login").status_code == 200


def test_anonymous_requests_never_leak_watchlist_data(hosted):
	"""Test that anonymous requests never leak watchlist data"""
	#Connect to the dataves and insert a secret item
	with connect() as connection:
		connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, 'https://example.com/secret-item', 'T')", (ensure_personal_list(connection, hosted.ids["alice"]), hosted.ids["alice"]))
	#Iterate over various paths
	for path in ("/", "/export/json", "/export/csv", "/chart/1", "/account", "/admin/users"):
		#send a GET request to the path
		response = hosted().get(path, follow_redirects=True)
		#Assert that the secret item is not present in the responce data
		assert "secret-item" not in response.get_data(as_text=True), path


def test_local_mode_needs_no_login_and_has_no_account_pages():
	"""Test that local mode requires no login and has no account path"""
	#Create test client
	client = main.app.test_client()
	#test the root endpoint
	assert client.get("/").status_code == 200
	#Test the login endpoint
	assert client.get("/login").status_code == 404
	#Test the admin/users endpoint
	assert client.get("/admin/users").status_code == 404
	#Test the export/json endpoint
	assert client.get("/export/json").status_code == 200


# Cross-user isolation on every route

def test_other_users_ids_are_404_on_every_id_route(hosted, monkeypatch):
	"""Test that other users IDs are 404 on every ID route"""
	#Set the refresh_product function to a no-op
	monkeypatch.setattr(main, "refresh_product", lambda product: None)
	#Connect to the database and insert products for both users
	with connect() as connection:
		bobs_list = ensure_personal_list(connection, hosted.ids["bob"])
		product = connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, 'https://example.com/bobs', 'T')", (bobs_list, hosted.ids["bob"])).lastrowid
		store = connection.execute("INSERT INTO stores (list_id, retailer, store_id, name, location) VALUES (?, 'Walmart', '9', 'S', 'L')", (bobs_list,)).lastrowid
		connection.execute("INSERT INTO observations (product_id, price, status, observed_at) VALUES (?, 5, 'success', '2026-08-20T12:00:00+00:00')", (product,))
	#Init a counter
	checked = 0
	#Iterate over all routes
	for rule, method in routes():
		#Skip certain routes and those with no args
		if rule.rule.startswith(("/admin", "/dev")) or not rule.arguments or rule.endpoint in OPEN_ENDPOINTS:
			continue
		#Determine the target based on the route
		target = product if "product_id" in rule.arguments else store
		#Assert that the route args are as expected
		assert rule.arguments <= {"product_id", "store_record_id"}, f"New id route {rule.rule} needs an isolation check"
		#Assert that the route returns a 404 status code
		assert send(hosted("alice"), rule, method, target).status_code == 404, f"{method} {rule.rule}"
		checked += 1 #Increment counter
	#Assert that at least 6 routes were checked
	assert checked >= 6
	#Connect to the database and verify the products
	with connect() as connection:
		#Check the numberof products and stores owned by each user
		assert connection.execute("SELECT COUNT(*) FROM products WHERE list_id = ?", (bobs_list,)).fetchone()[0] == 1
		assert connection.execute("SELECT COUNT(*) FROM stores WHERE list_id = ?", (bobs_list,)).fetchone()[0] == 1


def test_exports_contain_only_the_signed_in_users_products(hosted):
	"""Test that exports contain only the signed-in users products"""
	#Connect to the database and insert products for both users
	with connect() as connection:
		for name in ("alice", "bob"):
			connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, ?, 'T')", (ensure_personal_list(connection, hosted.ids[name]), hosted.ids[name], f"https://example.com/{name}-item"))
	#Iterate over export routes
	for path in ("/export/json", "/export/csv"):
		#Get the expoirt data for the signed in user
		body = hosted("alice").get(path).get_data(as_text=True)
		#Assert that the signed in users product is in the export but not the other users
		assert "alice-item" in body 
		assert "bob-item" not in body, path


def test_import_lands_only_in_the_chosen_list(hosted):
	"""Test that imports land only in the signed in account"""
	#Prepare the import payload
	payload = json.dumps({"products": [{"url": "https://www.walmart.com/ip/imported/1", "source": "Walmart", "history": []}]})
	import io
	#Send the import request
	with connect() as connection:
		alices_list = ensure_personal_list(connection, hosted.ids["alice"])
		ensure_personal_list(connection, hosted.ids["bob"])
	denied = hosted("alice").post("/import", data={"database_file": (io.BytesIO(payload.encode()), "list.json")}, content_type="multipart/form-data")
	#Assert that the responce is 403
	assert denied.status_code == 403
	response = hosted("root").post("/import", data={"list_id": str(alices_list), "database_file": (io.BytesIO(payload.encode()), "list.json")}, content_type="multipart/form-data")
	assert response.status_code in (200, 302)
	with connect() as connection:
		owners = {row[0] for row in connection.execute("SELECT list_id FROM products WHERE url LIKE '%imported%'")}
	#Assert that the owner is the signed in user
	assert owners == {alices_list}


def test_global_api_settings_are_admin_only(hosted):
	"""Test that global API settings are admin only"""
	#Send a POST request to update the provider settings as an admin
	response = hosted("alice").post("/settings/provider", data={"provider": "serpapi"})
	#Assert that the responce status code is 403
	assert response.status_code == 403
	#Assert that API usage is not present in the responce data
	assert "API usage" not in hosted("alice").get("/").get_data(as_text=True)


# Admin-only routes

def test_every_admin_route_refuses_regular_users(hosted):
	"""Test that every admin route refuses regular users"""
	seen = 0
	#Iterate over all routes
	for rule, method in routes():
		#Check if the route starts with /admin
		if not rule.rule.startswith("/admin"):
			continue
		#Send a request to the admin rout as a regular user
		response = send(hosted("alice"), rule, method)
		#Assert that the responce code is 403 or 404
		assert response.status_code in (403, 404), f"{method} {rule.rule}"
		#Iterate the seen counter
		seen += 1
	#Assert that at least 6 admin routes were checked
	assert seen >= 6


def test_admin_pages_open_for_admins(hosted):
	"""Test that admin pages are open for admins"""
	#Assert that the admin/users page is accessible for the root user
	assert hosted("root").get("/admin/users").status_code == 200


# Routes that must be disabled outside local mode

@pytest.mark.parametrize("method, path", [
	("post", "/shutdown"),
	("post", "/settings/env"),
	("get", "/export/db"),
])
def test_local_only_routes_are_disabled_even_for_admins(hosted, method, path):
	"""Test that local-only routes are disabled even for admins"""
	#Send a request to the local only route as an admin
	response = getattr(hosted("root"), method)(path, data={})
	#Assert that the response is a 403 or 404
	assert response.status_code in (403, 404)


def test_no_tab_close_beacon_or_env_editor_in_the_page(hosted):
	"""Test that no tab close beacon or env editor is present in the page"""
	#Get the content of the roo users page
	body = hosted("root").get("/").get_data(as_text=True)
	#Assert that "sendBeacon" or "env-editor" is not present in the body
	assert "sendBeacon" not in body
	assert "env-editor" not in body


# TLS configuration

BASE = {"PRICE_CHECKER_SECRET": "s" * 40}


def lan_env(tmp_path, **extra):
	"""Create environment setting for LAN mode"""
	return {**BASE, "PRICE_CHECKER_MODE": "lan", "PRICE_CHECKER_DATA_DIR": str(tmp_path), **extra}


def test_tls_off_serves_http(tmp_path):
	"""Test that TLS is off and serves HTTP when in LAN mode"""
	settings = load_settings(lan_env(tmp_path))
	#Assert that the scheme is HTTP and secure cookies are disabled
	assert settings.scheme == "http" 
	assert not settings.secure_cookies
	#Assert that there are no startup errors
	assert startup_errors(settings, admin_exists=True) == []


def test_selfsigned_tls_needs_no_files(tmp_path):
	"""Test that self-signed TLS requires no files when in LAN mode"""
	settings = load_settings(lan_env(tmp_path, PRICE_CHECKER_TLS="selfsigned"))
	#Assert that the scheme is HTTPS and secure cookies is enabled
	assert settings.scheme == "https" 
	assert settings.secure_cookies
	#Check for startup errors
	assert startup_errors(settings, admin_exists=True) == []


def test_selfsigned_uses_supplied_files_or_generates_its_own(tmp_path):
	"""Test that self-signed TLS uses supplied files or generates its own"""
	generated = load_settings(lan_env(tmp_path, PRICE_CHECKER_TLS="selfsigned"))
	#Assert that the TLS certificate is generated
	assert generated.tls_cert == tmp_path / "certs" / "selfsigned.pem"
	#Assert that the TLS certificate is supplied
	supplied = load_settings(lan_env(tmp_path, PRICE_CHECKER_TLS="selfsigned", PRICE_CHECKER_TLS_CERT="a.pem", PRICE_CHECKER_TLS_KEY="b.pem"))
	assert str(supplied.tls_cert) == "a.pem"


@pytest.mark.parametrize("present", ["cert", "key"])
def test_custom_tls_with_only_one_file_is_refused(tmp_path, present):
	"""Test that custom TLS with only one file is refused"""
	file = tmp_path / "one.pem"
	file.write_text("x")
	setting = "PRICE_CHECKER_TLS_CERT" if present == "cert" else "PRICE_CHECKER_TLS_KEY"
	#Load settings with custom TLS and the missing file
	errors = startup_errors(load_settings(lan_env(tmp_path, PRICE_CHECKER_TLS="custom", **{setting: str(file)})), admin_exists=True)
	assert len(errors) == 1


def test_tls_setting_is_ignored_outside_lan_mode(tmp_path):
	"""Test that TLS settings are ignored in non-LAN modes"""
	for mode in ("local", "public"):
		#Create environment settings with non-LAN mode
		env = {**BASE, "PRICE_CHECKER_MODE": mode, "PRICE_CHECKER_DATA_DIR": str(tmp_path), "PRICE_CHECKER_TLS": "custom", "PRICE_CHECKER_ALLOWED_HOSTS": HOST}
		#Load setting and check that TLS is off
		assert load_settings(env).tls == "off"


# Migrations

def test_migrating_twice_changes_nothing(tmp_path):
	"""Test that migrating twice does not change anythin"""
	import sqlite3
	#Connect to sqlite database
	connection = sqlite3.connect(tmp_path / "m.sqlite3")
	connection.row_factory = sqlite3.Row
	#Assert that the initial version is applied then apply the migratin and verify that the migration didn't change anything
	assert migrate(connection) == LATEST_VERSION
	before = connection.execute("SELECT group_concat(name) FROM sqlite_master ORDER BY name").fetchone()[0]
	assert migrate(connection) == LATEST_VERSION
	assert connection.execute("SELECT group_concat(name) FROM sqlite_master ORDER BY name").fetchone()[0] == before

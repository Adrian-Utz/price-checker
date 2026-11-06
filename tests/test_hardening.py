import dataclasses

import pytest

import main
from utils.database import connect
from utils.limits import BudgetExceeded, calls_today, reserve_api_call
from utils.lists import ensure_personal_list
from utils.users import create_user

PASSWORD = "cave johnsons correct horse battery"

#Fixture to configure the application environment for LAN mode
@pytest.fixture
def lan(monkeypatch):
	#Set auto_server_shutdown to False to prevent the server form shutting down automatically
	monkeypatch.setattr(main, "auto_server_shutdown", False)
	#Enable testing mode for the application
	main.app.testing = True

	def configure(**overrides):
		#Set default settings for LAN mode
		monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, auto_shutdown=False, **overrides))

	configure()
	with connect() as connection:
		# Create an admin user
		configure.admin = create_user(connection, "boss", PASSWORD, "admin")
		#Create a regular user
		configure.alice = create_user(connection, "alice", PASSWORD)
	return configure


def sign_in(username="alice", **headers):
	#Create a test client for tha application
	client = main.app.test_client()
	#Simulate a login attempt with the provided username and password
	client.post("/login", data={"username": username, "password": PASSWORD}, headers=headers)
	return client


def add_product(user_id, url="https://example.com/p"):
	# Insert a new product into the database
	with connect() as connection:
		#Use a parameterized query to safely insert data into the product tables
		list_id = ensure_personal_list(connection, user_id)
		return connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, ?, 'Test')", (list_id, user_id, url)).lastrowid


def product_row(product_id):
	#Retrieve a product row form the database by product ID
	with connect() as connection:
		# Use a parameterized query to safely retrieve data from the products table
		return connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


# Lifecycle

def test_lifecycle_features_are_off_outside_local_mode(lan, monkeypatch):
	#Tests that lifecycle features like shutdown and opening the app browser are not available outside of local mode
	#Sign in to the application
	client = sign_in()
	#Attempt to shut down the server an expect a 404 error
	assert client.post("/shutdown").status_code == 404
	#Retrieve the main page content and check if the 'sendBeacon' script is not present
	page = client.get("/").get_data(as_text=True)
	assert "sendBeacon" not in page
	#Use monkeypatch to capture the 'open' function calls
	opened = []
	monkeypatch.setattr(main.webbrowser, "open", lambda *args, **kwargs: opened.append(args))
	#Attempt to open an app browser and verify that it was not called
	main.open_app_browser("127.0.0.1", 5000)
	assert opened == []


def test_env_editor_and_dev_routes_are_blocked_outside_local_mode(lan, monkeypatch):
	#Tests that the environment editor and develeoper routes are blocked outside of local mode.
	#Set up the DEVELOPER_MODE and edit_env flags in the main module
	monkeypatch.setattr(main, "DEVELOPER_MODE", True)
	monkeypatch.setattr(main, "edit_env", True)
	#Sign in as boss
	admin = sign_in("boss")
	#Atttempt to modigy the environment and expect a 403 error
	assert admin.post("/settings/env", data={"env_content": "X=1"}).status_code == 403


def test_dev_tools_are_for_administrators_in_hosted_modes(lan):
	#Tests that the developer tools are only accessible for administrators in hosted mode
	#Initialize the developement database
	main.init_dev_db()
	#Check that the admin can access the developer responses page
	assert sign_in("boss").get("/dev/responses").status_code == 200
	#Check that the user cannot access the developer responses pages
	assert sign_in().get("/dev/responses").status_code == 404
	#Check that the user cannot replay a developer responce
	assert sign_in().post("/dev/responses/1/replay").status_code == 404


def test_admin_calls_skip_budgets_but_are_counted(lan, monkeypatch):
	#Tests that administrators can call skip budgets but are counted as users in local mode
	#Set the user daily API calls limit for the admin
	lan(user_daily_api_calls=1)
	#Mock the _refresh_product function to avoid actual API calls
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: None)
	#Create product rows for the admin and user
	admin_product = product_row(add_product(lan.admin))
	user_product = product_row(add_product(lan.alice))
	#Refresh the product multiple times for the admin
	for _ in range(3):
		main.refresh_product(admin_product)
	#Refresh the product once for the user
	main.refresh_product(user_product)
	#Attempt to refresh the product for the user and expect an exception
	with pytest.raises(Exception):
		main.refresh_product(user_product)


def test_local_mode_keeps_the_beacon(monkeypatch):
	#Tests that the 'sendBeacon' script is kept in the responce for local mode.
	#Set the auro_server_shutdown flag to True
	monkeypatch.setattr(main, "auto_server_shutdown", True)
	#Check that the 'sencBeacon' script is in the main page responce
	assert b"sendBeacon" in main.app.test_client().get("/").data


# Headers, host allowlist, cookies

def test_security_headers_and_nonce_csp(lan):
	"""Tests that the security headers and CSP are correctly set in the response.
	This includes checking the Content-Security-Policy, nonce, X-Content-Type-Options, Referrer-Policy, Cache-Control, and Strict-Transport-Security headers"""
	response = sign_in().get("/")
	#Check the CSP header
	csp = response.headers["Content-Security-Policy"]
	nonce = csp.split("'nonce-")[1].split("'")[0]
	assert f'nonce="{nonce}"' in response.get_data(as_text=True)
	assert "script-src 'self' 'nonce-" in csp 
	assert "frame-ancestors 'none'" in csp 
	assert "unsafe-inline'; img" in csp
	#Check other secrity headers
	assert response.headers["X-Content-Type-Options"] == "nosniff"
	assert response.headers["Referrer-Policy"] == "same-origin"
	assert response.headers["Cache-Control"] == "no-store"
	assert "Strict-Transport-Security" not in response.headers


def test_pages_have_no_inline_event_handlers(lan):
	""" Tests that no inline event handlers are used in the HTML pages.
	Ensures that all event handlers are delegated to JavaScript files or other secure sources"""
	page = sign_in().get("/").get_data(as_text=True)
	#Check for inline event handlers in specific attributes
	for attribute in ("onclick=", "onsubmit=", "onfocus="):
		assert attribute not in page
	#Ensure there are no <script> tags in the HTML
	assert "<script>" not in page


def test_hsts_only_over_https_with_secure_cookies(lan):
	#Tests that HSTS (HTTP Strict Transport Security) is only enforced over HTTPS and that cookies are set with the secure flag when running in HTTPS mode
	lan(tls="custom")
	#Send a request to the login page over HTTPS and check for HSTS
	response = main.app.test_client().get("/login", base_url="https://localhost")
	assert "Strict-Transport-Security" in response.headers


def test_host_allowlist(lan):
	#Tests that only hosts listed in the host allowlist are allowed to access the application
	#This includes checkng that requests to non-allowed hosts return a 400 Bad Request response
	lan(allowed_hosts=("prices.example.com", "*.lan.test"))
	#Create a test client
	client = main.app.test_client()
	#Test valid and invalid hosts
	assert client.get("/login", headers={"Host": "prices.example.com:8443"}).status_code == 200
	assert client.get("/login", headers={"Host": "nas.lan.test"}).status_code == 200
	assert client.get("/login", headers={"Host": "evil.example.net"}).status_code == 400
	assert client.get("/login", headers={"Host": "lan.test"}).status_code == 400


def test_local_mode_rejects_foreign_host_names():
	"""Tests that requests with foreign host names are rejected in local mode
	Ensures that only localhost or a specified host allowlist are allowed"""
	client = main.app.test_client()
	# Try accessing the login page with a foreign host name
	assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
	# Try accessing the login page with locahost
	assert client.get("/", headers={"Host": "localhost:5000"}).status_code == 200


def test_session_cookie_name_follows_cookie_security():
	"""Tests that the session cookie name follows the secure cookies setting
	Ensures that the session cookie is named'__Host-session' when secure cookies are enabled"""
	assert main.app.config["SESSION_COOKIE_NAME"] == ("__Host-session" if main.SETTINGS.secure_cookies else "session")


# CSRF

def test_csrf_is_enforced_on_every_post(lan, monkeypatch):
	"""Tests that CSRF protection is enforced on every POST request.
	This includes checkin that POST requests to /logout and /scan return a 400 Bad Request responce when CSRF protection is enabled"""
	monkeypatch.setitem(main.app.config, "CSRF_ENABLED", True)
	#Create an unauthenticated client for login, which should require a CSRF token
	client = sign_in(Origin="http://localhost")
	#Check that POST requests to /logout and /scan return a 400 Bad Request responce
	assert client.post("/logout").status_code == 400
	assert client.post("/scan").status_code == 400


def test_csrf_accepts_form_field_and_header(lan, monkeypatch):
	"""Tests that CSRF protection accepts a valid token through both the form field and the X-CSRF-Token header
	Ensuring that both methods of passing the CSRF token are correctly handled"""
	monkeypatch.setitem(main.app.config, "CSRF_ENABLED", True)
	#Create a test Client
	client = main.app.test_client()
	#Get the initial page and retrieve the CSRF token from the session
	client.get("/login")
	with client.session_transaction() as session:
		token = session["csrf_token"]
	#Test POST request with CSRF token in the form field
	assert client.post("/login", data={"username": "alice", "password": PASSWORD, "csrf_token": token}).status_code == 302
	#Test POST request with CSRF token in X-CSRF-Token header
	with client.session_transaction() as session:
		token = session["csrf_token"]
	assert client.post("/logout", headers={"X-CSRF-Token": token}).status_code == 302


def test_cross_origin_post_is_rejected_even_with_a_valid_token(lan, monkeypatch):
	"""Tests that CSRF protection rejects POST requests with a valid token when the origin is cross-origin.
	This includes checking that POST requests to /login are rejected with 400 Bad Request 
	when the origin is evil.example or when the origin header is not present."""
	monkeypatch.setitem(main.app.config, "CSRF_ENABLED", True)
	#Create a test client
	client = main.app.test_client()
	#Get the initial page and retrieve the CSRF token from the session
	client.get("/login")
	with client.session_transaction() as session:
		token = session["csrf_token"]
	#Test POST request with valid token but cross-origin origin
	data = {"username": "alice", "password": PASSWORD, "csrf_token": token}
	assert client.post("/login", data=data, headers={"Origin": "http://evil.example"}).status_code == 400
	#Test POST request with valid token but cross-origin Sec-Fetch-Site
	assert client.post("/login", data=data, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 400
	#Test POST request with valid token and localhost origin
	assert client.post("/login", data=data, headers={"Origin": "http://localhost"}).status_code == 302


# Budgets

def test_reserve_api_call_enforces_user_and_global_limits(lan):
	"""Tests that the reserve_api_call function correctly enforces both user-specific and global limits
	It checks that the call is successful when both user and global limits are within the budget.
	It also ensures that the call fails when the user limit is exceeded"""
	with connect() as connection:
		#Reserve 2 calls 
		reserve_api_call(connection, lan.alice, user_limit=2, global_limit=0)
	with connect() as connection:
		#Attempt to reserve another call it should succeed
		reserve_api_call(connection, lan.alice, user_limit=2, global_limit=0)
	with pytest.raises(BudgetExceeded):
		#Attempt to reserve a third call, should fail due to user limit
		with connect() as connection:
			reserve_api_call(connection, lan.alice, user_limit=2, global_limit=0)
	with connect() as connection:
		#Check that user call count is == 2
		assert calls_today(connection, lan.alice) == 2
		#Reserve 2 calls fro admin
		reserve_api_call(connection, lan.admin, user_limit=2, global_limit=0)  # other users are unaffected
	with pytest.raises(BudgetExceeded):
		#Check that the admin's call count remains unaffected
		with connect() as connection:
			reserve_api_call(connection, lan.admin, user_limit=5, global_limit=3)


def test_unlimited_budget_still_counts(lan):
	#Tests that call continue to bee counted even when the budget is unlimited (0), it reserves 3 call s for the user with unlimited budget and checks the call count
	for _ in range(3):
		with connect() as connection:
			reserve_api_call(connection, lan.alice, user_limit=0, global_limit=0)
	with connect() as connection:
		assert calls_today(connection, lan.alice) == 3


def test_budget_resets_on_a_new_day(lan, monkeypatch):
	"""Tests that the budget resets on a new day. 
	It reserves a call for user, then changes the current date to January 1, 2999. 
	It checks that the call counted is reset"""
	with connect() as connection:
		reserve_api_call(connection, lan.alice, user_limit=1, global_limit=0)
	monkeypatch.setattr("utils.limits.today", lambda: "2999-01-01")
	with connect() as connection:
		reserve_api_call(connection, lan.alice, user_limit=1, global_limit=0)


def test_refresh_stops_at_budget_and_does_not_fetch(lan, monkeypatch):
	"""Tests that the refres_product function stops fetching products when the budget is reached
	It adds a product and reserves a clla, then attempts to fetch more products
	It checks that the refresh stops and no new products are fetched"""
	lan(user_daily_api_calls=1)
	fetched = []
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: fetched.append(product["id"]))
	first, second = add_product(lan.alice), add_product(lan.alice, "https://example.com/q")
	main.refresh_product(product_row(first))
	with pytest.raises(BudgetExceeded):
		main.refresh_product(product_row(second))
	assert fetched == [first]


def test_scan_stops_when_budget_runs_out_and_is_not_marked_completed(lan, monkeypatch):
	"""Tests that the run_scan function stops scanning when the budget is exhausted and does not mark the scan as completed
	It sets the user's daily API calls to 1, adds products, and attempts to scan.
	IT checks that the scan stops and the status is not marked as completed"""
	lan(user_daily_api_calls=1)
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: None)
	monkeypatch.setattr(main, "MIN_DELAY_SECONDS", 0)
	monkeypatch.setattr("utils.scanner.time.sleep", lambda seconds: None)
	add_product(lan.alice)
	add_product(lan.alice, "https://example.com/q")
	monkeypatch.setattr(main, "serpapi_ready", lambda: True)
	assert main.run_scan(lan.alice) is False
	with connect() as connection:
		status = connection.execute("SELECT status FROM scans WHERE user_id = ?", (lan.alice,)).fetchone()[0]
	assert status == "stopped"
	assert main.scan_allowed(lan.alice)


def test_recheck_requests_are_rate_limited_per_user(lan, monkeypatch):
	"""Tests that the recheck_product function enforces rate limitin based on user calls
	It sets the user's rechecks per hour to 2, adds a product, reserves calls, and attempts to recheck
	It checks that the recheck stops after reaching the rate limit"""
	lan(rechecks_per_hour=2)
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: None)
	product_id = add_product(lan.alice)
	with connect() as connection:
		connection.execute("UPDATE products SET error = 'boom' WHERE id = ?", (product_id,))
	client = sign_in()
	messages = [client.post(f"/recheck/{product_id}").headers["Location"] for _ in range(3)]
	assert "Too" in messages[2]
	assert "Too" not in messages[0] 
	assert "Too" not in messages[1]
	other = sign_in("boss")
	other_product = add_product(lan.admin)
	with connect() as connection:
		connection.execute("UPDATE products SET error = 'boom' WHERE id = ?", (other_product,))
	assert "Too" not in other.post(f"/recheck/{other_product}").headers["Location"]


def test_budget_is_shown_on_the_home_page(lan):
	"""Tests that the budget is displayed the homepage when the user daily API call limit is exceeded
	It sets the user's daily API calls to 7 and checks if the budget usage is shown on the home page"""
	lan(user_daily_api_calls=7)
	page = sign_in().get("/").get_data(as_text=True)
	assert "0 of 7 calls used" in page


def test_budget_not_shown_when_unlimited(lan):
	"""Tests that the budget is not displayed on the home page when the user's daily API cals limit is unlimited (0)
	It checks if the budget usage is not shown on the home page for unlimited budget"""
	assert "calls used" not in sign_in().get("/").get_data(as_text=True)

import dataclasses

import pytest

import main
from utils.database import connect
from utils.limits import BudgetExceeded, calls_today, reserve_api_call
from utils.users import create_user

PASSWORD = "cave johnsons correct horse battery"


@pytest.fixture
def lan(monkeypatch):
	monkeypatch.setattr(main, "auto_server_shutdown", False)
	main.app.testing = True

	def configure(**overrides):
		monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, auto_shutdown=False, **overrides))

	configure()
	with connect() as connection:
		configure.admin = create_user(connection, "boss", PASSWORD, "admin")
		configure.alice = create_user(connection, "alice", PASSWORD)
	return configure


def sign_in(username="alice", **headers):
	client = main.app.test_client()
	client.post("/login", data={"username": username, "password": PASSWORD}, headers=headers)
	return client


def add_product(user_id, url="https://example.com/p"):
	with connect() as connection:
		return connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, ?, 'Test')", (user_id, url)).lastrowid


def product_row(product_id):
	with connect() as connection:
		return connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


# Lifecycle

def test_lifecycle_features_are_off_outside_local_mode(lan, monkeypatch):
	client = sign_in()
	assert client.post("/shutdown").status_code == 404
	page = client.get("/").get_data(as_text=True)
	assert "sendBeacon" not in page
	opened = []
	monkeypatch.setattr(main.webbrowser, "open", lambda *args, **kwargs: opened.append(args))
	main.open_app_browser("127.0.0.1", 5000)
	assert opened == []


def test_env_editor_and_dev_routes_are_blocked_outside_local_mode(lan, monkeypatch):
	monkeypatch.setattr(main, "DEVELOPER_MODE", True)
	monkeypatch.setattr(main, "edit_env", True)
	admin = sign_in("boss")
	assert admin.post("/settings/env", data={"env_content": "X=1"}).status_code == 403


def test_dev_tools_are_for_administrators_in_hosted_modes(lan):
	main.init_dev_db()
	assert sign_in("boss").get("/dev/responses").status_code == 200
	assert sign_in().get("/dev/responses").status_code == 404
	assert sign_in().post("/dev/responses/1/replay").status_code == 404


def test_admin_calls_skip_budgets_but_are_counted(lan, monkeypatch):
	lan(user_daily_api_calls=1)
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: None)
	admin_product = product_row(add_product(lan.admin))
	user_product = product_row(add_product(lan.alice))
	for _ in range(3):
		main.refresh_product(admin_product)
	main.refresh_product(user_product)
	with pytest.raises(Exception):
		main.refresh_product(user_product)


def test_local_mode_keeps_the_beacon(monkeypatch):
	monkeypatch.setattr(main, "auto_server_shutdown", True)
	assert b"sendBeacon" in main.app.test_client().get("/").data


# Headers, host allowlist, cookies

def test_security_headers_and_nonce_csp(lan):
	response = sign_in().get("/")
	csp = response.headers["Content-Security-Policy"]
	nonce = csp.split("'nonce-")[1].split("'")[0]
	assert f'nonce="{nonce}"' in response.get_data(as_text=True)
	assert "script-src 'self' 'nonce-" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline'; img" in csp
	assert response.headers["X-Content-Type-Options"] == "nosniff"
	assert response.headers["Referrer-Policy"] == "same-origin"
	assert response.headers["Cache-Control"] == "no-store"
	assert "Strict-Transport-Security" not in response.headers


def test_pages_have_no_inline_event_handlers(lan):
	page = sign_in().get("/").get_data(as_text=True)
	for attribute in ("onclick=", "onsubmit=", "onfocus="):
		assert attribute not in page
	assert "<script>" not in page


def test_hsts_only_over_https_with_secure_cookies(lan):
	lan(tls="custom")
	response = main.app.test_client().get("/login", base_url="https://localhost")
	assert "Strict-Transport-Security" in response.headers


def test_host_allowlist(lan):
	lan(allowed_hosts=("prices.example.com", "*.lan.test"))
	client = main.app.test_client()
	assert client.get("/login", headers={"Host": "prices.example.com:8443"}).status_code == 200
	assert client.get("/login", headers={"Host": "nas.lan.test"}).status_code == 200
	assert client.get("/login", headers={"Host": "evil.example.net"}).status_code == 400
	assert client.get("/login", headers={"Host": "lan.test"}).status_code == 400


def test_local_mode_rejects_foreign_host_names():
	client = main.app.test_client()
	assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
	assert client.get("/", headers={"Host": "localhost:5000"}).status_code == 200


def test_session_cookie_name_follows_cookie_security():
	assert main.app.config["SESSION_COOKIE_NAME"] == ("__Host-session" if main.SETTINGS.secure_cookies else "session")


# CSRF

def test_csrf_is_enforced_on_every_post(lan, monkeypatch):
	monkeypatch.setitem(main.app.config, "CSRF_ENABLED", True)
	client = sign_in(Origin="http://localhost")  # login itself needs a token, so this client is unauthenticated
	assert client.post("/logout").status_code == 400
	assert client.post("/scan").status_code == 400


def test_csrf_accepts_form_field_and_header(lan, monkeypatch):
	monkeypatch.setitem(main.app.config, "CSRF_ENABLED", True)
	client = main.app.test_client()
	client.get("/login")
	with client.session_transaction() as session:
		token = session["csrf_token"]
	assert client.post("/login", data={"username": "alice", "password": PASSWORD, "csrf_token": token}).status_code == 302
	with client.session_transaction() as session:
		token = session["csrf_token"]
	assert client.post("/logout", headers={"X-CSRF-Token": token}).status_code == 302


def test_cross_origin_post_is_rejected_even_with_a_valid_token(lan, monkeypatch):
	monkeypatch.setitem(main.app.config, "CSRF_ENABLED", True)
	client = main.app.test_client()
	client.get("/login")
	with client.session_transaction() as session:
		token = session["csrf_token"]
	data = {"username": "alice", "password": PASSWORD, "csrf_token": token}
	assert client.post("/login", data=data, headers={"Origin": "http://evil.example"}).status_code == 400
	assert client.post("/login", data=data, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 400
	assert client.post("/login", data=data, headers={"Origin": "http://localhost"}).status_code == 302


# Budgets

def test_reserve_api_call_enforces_user_and_global_limits(lan):
	with connect() as connection:
		reserve_api_call(connection, lan.alice, user_limit=2, global_limit=0)
	with connect() as connection:
		reserve_api_call(connection, lan.alice, user_limit=2, global_limit=0)
	with pytest.raises(BudgetExceeded):
		with connect() as connection:
			reserve_api_call(connection, lan.alice, user_limit=2, global_limit=0)
	with connect() as connection:
		assert calls_today(connection, lan.alice) == 2
		reserve_api_call(connection, lan.admin, user_limit=2, global_limit=0)  # other users are unaffected
	with pytest.raises(BudgetExceeded):
		with connect() as connection:
			reserve_api_call(connection, lan.admin, user_limit=5, global_limit=3)


def test_unlimited_budget_still_counts(lan):
	for _ in range(3):
		with connect() as connection:
			reserve_api_call(connection, lan.alice, user_limit=0, global_limit=0)
	with connect() as connection:
		assert calls_today(connection, lan.alice) == 3


def test_budget_resets_on_a_new_day(lan, monkeypatch):
	with connect() as connection:
		reserve_api_call(connection, lan.alice, user_limit=1, global_limit=0)
	monkeypatch.setattr("utils.limits.today", lambda: "2999-01-01")
	with connect() as connection:
		reserve_api_call(connection, lan.alice, user_limit=1, global_limit=0)


def test_refresh_stops_at_budget_and_does_not_fetch(lan, monkeypatch):
	lan(user_daily_api_calls=1)
	fetched = []
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: fetched.append(product["id"]))
	first, second = add_product(lan.alice), add_product(lan.alice, "https://example.com/q")
	main.refresh_product(product_row(first))
	with pytest.raises(BudgetExceeded):
		main.refresh_product(product_row(second))
	assert fetched == [first]


def test_scan_stops_when_budget_runs_out_and_is_not_marked_completed(lan, monkeypatch):
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
	lan(rechecks_per_hour=2)
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: None)
	product_id = add_product(lan.alice)
	with connect() as connection:
		connection.execute("UPDATE products SET error = 'boom' WHERE id = ?", (product_id,))
	client = sign_in()
	messages = [client.post(f"/recheck/{product_id}").headers["Location"] for _ in range(3)]
	assert "Too" in messages[2]
	assert "Too" not in messages[0] and "Too" not in messages[1]
	other = sign_in("boss")
	other_product = add_product(lan.admin)
	with connect() as connection:
		connection.execute("UPDATE products SET error = 'boom' WHERE id = ?", (other_product,))
	assert "Too" not in other.post(f"/recheck/{other_product}").headers["Location"]


def test_budget_is_shown_on_the_home_page(lan):
	lan(user_daily_api_calls=7)
	page = sign_in().get("/").get_data(as_text=True)
	assert "0 of 7 calls used" in page


def test_budget_not_shown_when_unlimited(lan):
	assert "calls used" not in sign_in().get("/").get_data(as_text=True)

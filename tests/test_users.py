import dataclasses
import sqlite3
import time
import pytest

import main
from utils.database import admin_exists, connect
from utils.lists import ensure_personal_list
from utils.migrations import LATEST_VERSION, MIGRATIONS, migrate
from utils.users import claim_local_data, create_user, local_user_id

URL = "https://www.walmart.com/ip/example/123456"


@pytest.fixture
def accounts(monkeypatch):
	"""LAN-mode app with two regular users and an admin; returns a function that logs a client in as one."""
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, auto_shutdown=False))
	monkeypatch.setattr(main, "auto_server_shutdown", False)
	main.app.testing = True
	with connect() as connection:
		ids = {
			"alice": create_user(connection, "alice", "correct horse battery"),
			"bob": create_user(connection, "bob", "correct horse battery"),
			"root": create_user(connection, "root-admin", "correct horse battery", role="admin"),
		}

	def client_for(name):
		client = main.app.test_client()
		with client.session_transaction() as session:
			session["user_id"] = ids[name]
			session["sv"] = 0
			session["last_seen"] = int(time.time())
		return client

	client_for.ids = ids
	return client_for


def add_product(user_id, url=URL, source="Walmart"):
	with connect() as connection:
		list_id = ensure_personal_list(connection, user_id)
		return connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, ?, ?)", (list_id, user_id, url, source)).lastrowid


def local_id():
	with connect() as connection:
		return local_user_id(connection)


def test_migration_assigns_existing_rows_to_local_user(tmp_path):
	connection = sqlite3.connect(tmp_path / "old.sqlite3")
	connection.execute("BEGIN")
	for _, step in MIGRATIONS[:2]:
		step(connection)
	connection.execute("PRAGMA user_version = 2")
	connection.execute("INSERT INTO products (url, source) VALUES (?, ?)", (URL, "Walmart"))
	connection.execute("INSERT INTO scans (started_at, status) VALUES ('2026-01-01', 'completed')")
	connection.execute("INSERT INTO stores (retailer, store_id, name, location) VALUES ('Walmart', '1', 'S', 'L')")
	connection.commit()
	assert migrate(connection) == LATEST_VERSION
	owner = local_user_id(connection)
	for table in ("products", "scans"):
		assert connection.execute(f"SELECT DISTINCT user_id FROM {table}").fetchall() == [(owner,)]
	personal = ensure_personal_list(connection, owner)
	for table in ("products", "stores"):
		assert connection.execute(f"SELECT DISTINCT list_id FROM {table}").fetchall() == [(personal,)]


def test_store_numbers_are_unique_per_list_not_globally(accounts):
	ids = accounts.ids
	with connect() as connection:
		lists = {user: ensure_personal_list(connection, ids[user]) for user in ("alice", "bob")}
		for user in ("alice", "bob"):
			connection.execute("INSERT INTO stores (list_id, retailer, store_id, name, location) VALUES (?, 'Walmart', '4638', 'S', 'L')", (lists[user],))
		with pytest.raises(sqlite3.IntegrityError):
			connection.execute("INSERT INTO stores (list_id, retailer, store_id, name, location) VALUES (?, 'Walmart', '4638', 'S', 'L')", (lists["alice"],))


def test_local_placeholder_is_not_an_admin_account():
	assert admin_exists() is False
	with connect() as connection:
		create_user(connection, "owner", "correct horse battery", role="admin")
	assert admin_exists() is True


def test_claim_local_data_moves_everything_to_the_new_admin():
	product_id = add_product(local_id())
	with connect() as connection:
		admin_id = create_user(connection, "owner", "correct horse battery", role="admin")
		claim_local_data(connection, admin_id)
		assert connection.execute("SELECT user_id FROM products WHERE id = ?", (product_id,)).fetchone()[0] == admin_id


def test_reserved_and_weak_accounts_are_rejected():
	with connect() as connection:
		with pytest.raises(ValueError):
			create_user(connection, "local", "correct horse battery")
		with pytest.raises(ValueError):
			create_user(connection, "shortpw", "short")
		create_user(connection, "Dupe", "correct horse battery")
		with pytest.raises(ValueError):
			create_user(connection, "dupe", "correct horse battery")
		assert connection.execute("SELECT password_hash FROM users WHERE username = 'Dupe'").fetchone()[0].startswith("scrypt:")


def test_users_only_see_their_own_watchlist(accounts):
	add_product(accounts.ids["alice"], "https://www.walmart.com/ip/alice-item/1")
	add_product(accounts.ids["bob"], "https://www.walmart.com/ip/bob-item/2")
	page = accounts("alice").get("/").get_data(as_text=True)
	assert "alice-item" in page 
	assert "bob-item" not in page


@pytest.mark.parametrize("method, path", [
	("post", "/remove/{id}"),
	("post", "/recheck/{id}"),
	("post", "/store/{id}"),
	("post", "/item-provider/{id}"),
	("get", "/chart/{id}"),
])
def test_other_users_product_ids_return_404(accounts, monkeypatch, method, path):
	monkeypatch.setattr(main, "refresh_product", lambda product: None)
	bobs_product = add_product(accounts.ids["bob"])
	with connect() as connection:
		connection.execute("INSERT INTO observations (product_id, price, status, observed_at) VALUES (?, 5, 'success', '2026-08-20T12:00:00+00:00')", (bobs_product,))
	response = getattr(accounts("alice"), method)(path.format(id=bobs_product), data={"store_id": "", "provider": ""})
	assert response.status_code == 404
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM products WHERE id = ?", (bobs_product,)).fetchone()[0] == 1
	owner_response = getattr(accounts("bob"), method)(path.format(id=bobs_product), data={"store_id": ""})
	assert owner_response.status_code != 404


def test_other_users_stores_cannot_be_removed(accounts):
	with connect() as connection:
		bobs_list = ensure_personal_list(connection, accounts.ids["bob"])
		store_id = connection.execute("INSERT INTO stores (list_id, retailer, store_id, name, location) VALUES (?, 'Walmart', '9', 'S', 'L')", (bobs_list,)).lastrowid
	assert accounts("alice").post(f"/stores/{store_id}/remove").status_code == 404
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 1


def test_recheck_selected_ignores_other_users_products(accounts, monkeypatch):
	bobs_product = add_product(accounts.ids["bob"])
	refreshed = []
	monkeypatch.setattr(main, "refresh_product", lambda product: refreshed.append(product["id"]))
	accounts("alice").post("/recheck-selected", data={"product_ids": [str(bobs_product)]})
	assert refreshed == []


def test_product_limit_is_per_user(accounts, monkeypatch):
	monkeypatch.setattr(main, "MAX_URLS", 1)
	alice, bob = accounts("root"), accounts("bob")
	alice.post("/add", data={"url": URL})
	alice.post("/add", data={"url": "https://www.walmart.com/ip/other/2"})
	with connect() as connection:
		connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, ?, 'Walmart')", (accounts.ids["bob"], URL))
	assert bob.post("/add", data={"url": URL}).status_code == 403
	with connect() as connection:
		counts = dict(connection.execute("SELECT user_id, COUNT(*) FROM products GROUP BY user_id").fetchall())
	assert counts == {accounts.ids["root"]: 1, accounts.ids["bob"]: 1}


def test_scan_only_touches_own_products_and_daily_limit_is_per_user(accounts, monkeypatch):
	monkeypatch.setattr(main, "MIN_DELAY_SECONDS", 0.0)
	monkeypatch.setattr(main, "DEVELOPER_MODE", False)
	add_product(accounts.ids["alice"], "https://www.walmart.com/ip/alice-item/1")
	add_product(accounts.ids["bob"], "https://www.walmart.com/ip/bob-item/2")
	calls = []
	monkeypatch.setattr(main, "fetch_product", lambda url, **kwargs: (calls.append(url), {"price": 1.0, "currency": "USD"})[1])
	alice = accounts("alice")
	assert "Scan complete" in alice.get(alice.post("/scan").location).get_data(as_text=True)
	assert calls == ["https://www.walmart.com/ip/alice-item/1"]
	assert main.scan_allowed(accounts.ids["alice"]) is False
	assert main.scan_allowed(accounts.ids["bob"]) is True
	assert "Only one completed scan" in alice.get(alice.post("/scan").location).get_data(as_text=True)


def test_a_user_cannot_start_two_scans_at_once(accounts):
	alice = accounts("alice")
	lock = main.scan_lock_for(accounts.ids["alice"])
	assert lock.acquire(blocking=False)
	try:
		response = alice.post("/scan")
		assert "already running" in alice.get(response.location).get_data(as_text=True)
		bobs_lock = main.scan_lock_for(accounts.ids["bob"])
		assert bobs_lock.acquire(blocking=False)
		bobs_lock.release()
	finally:
		lock.release()


def test_exports_contain_only_the_current_users_items(accounts):
	add_product(accounts.ids["alice"], "https://www.walmart.com/ip/alice-item/1")
	add_product(accounts.ids["bob"], "https://www.walmart.com/ip/bob-item/2")
	body = accounts("alice").get("/export/json").get_data(as_text=True)
	assert "alice-item" in body 
	assert "bob-item" not in body
	assert "bob-item" not in accounts("alice").get("/export/csv").get_data(as_text=True)


def test_whole_database_export_is_blocked_with_accounts(accounts):
	assert accounts("root").get("/export/db").status_code == 403
	assert accounts("root").post("/import/db").status_code == 404 #The replace-everything import no longer exists


def test_api_settings_and_usage_are_admin_only(accounts, monkeypatch):
	monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
	assert accounts("alice").post("/settings/provider", data={"provider": "auto"}).status_code == 403
	assert accounts("alice").post("/settings/env", data={"env_content": "X=1"}).status_code == 403
	user_page = accounts("alice").get("/").data
	assert b"api-usage" not in user_page 
	assert b"Global API provider" not in user_page
	admin_page = accounts("root").get("/").data
	assert b"api-usage" in admin_page 
	assert b"Global API provider" in admin_page


def test_requests_without_a_login_are_sent_to_the_login_page(accounts):
	response = main.app.test_client().get("/")
	assert response.status_code == 302 
	assert "/login" in response.headers["Location"]
	client = accounts("alice")
	with connect() as connection:
		connection.execute("UPDATE users SET disabled = 1 WHERE id = ?", (accounts.ids["alice"],))
	assert client.get("/").status_code == 302


def test_local_mode_needs_no_login_and_acts_as_admin():
	assert main.app.test_client().get("/").status_code == 200
	with main.app.test_request_context("/"):
		assert main.current_user()["username"] == "local" 
		assert main.is_admin()

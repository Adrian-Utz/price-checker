import io
import json
import sqlite3

import pytest

import main
from utils.database import connect
from utils.users import create_user, local_user_id
from utils.watchlist_io import merge_watchlist, read_watchlist_database

URL = "https://www.walmart.com/ip/widget/111"


@pytest.fixture
def two_users(monkeypatch):
	import dataclasses
	import time
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, auto_shutdown=False))
	main.app.testing = True
	with connect() as connection:
		ids = {"alice": create_user(connection, "alice", "correct horse battery"), "bob": create_user(connection, "bob", "correct horse battery")}

	def client_for(name):
		client = main.app.test_client()
		with client.session_transaction() as session:
			session.update(user_id=ids[name], sv=0, last_seen=int(time.time()))
		return client

	client_for.ids = ids
	return client_for


def upload(client, name, content):
	data = content if isinstance(content, bytes) else content.encode()
	return client.post("/import", data={"database_file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def payload(*products):
	return json.dumps({"products": list(products)})


def rows(sql, *args):
	with connect() as connection:
		return connection.execute(sql, args).fetchall()


def test_import_merges_instead_of_replacing(two_users):
	alice = two_users("alice")
	upload(alice, "a.json", payload({"url": URL, "title": "Widget", "history": [{"price": 9.5, "observed_at": "2026-01-01T00:00:00+00:00", "status": "success"}]}))
	upload(alice, "b.json", payload({"url": "https://www.walmart.com/ip/other/222"}))
	assert [r["url"] for r in rows("SELECT url FROM products WHERE user_id = ? ORDER BY id", two_users.ids["alice"])] == [URL, "https://www.walmart.com/ip/other/222"]
	assert rows("SELECT COUNT(*) FROM observations")[0][0] == 1


def test_reimporting_the_same_file_adds_nothing_twice(two_users):
	alice = two_users("alice")
	body = payload({"url": URL, "history": [{"price": 9.5, "observed_at": "2026-01-01T00:00:00+00:00"}]})
	upload(alice, "a.json", body)
	response = upload(alice, "a.json", body)
	assert "0 new and merged 1" in alice.get(response.location).get_data(as_text=True)
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 1 and rows("SELECT COUNT(*) FROM observations")[0][0] == 1


def test_merging_existing_product_only_adds_missing_history(two_users):
	alice = two_users("alice")
	upload(alice, "a.json", payload({"url": URL, "history": [{"price": 9.5, "observed_at": "2026-01-01T00:00:00+00:00"}]}))
	upload(alice, "b.json", payload({"url": URL, "title": "Ignored", "history": [
		{"price": 9.5, "observed_at": "2026-01-01T00:00:00+00:00"},
		{"price": 8.0, "observed_at": "2026-01-02T00:00:00+00:00"},
	]}))
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 1
	assert [r["price"] for r in rows("SELECT price FROM observations ORDER BY observed_at")] == [9.5, 8.0]


def test_import_never_touches_other_users_data(two_users):
	with connect() as connection:
		connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, ?, 'Walmart')", (two_users.ids["bob"], URL))
	upload(two_users("alice"), "a.json", payload({"url": URL, "history": [{"price": 1, "observed_at": "2026-01-01T00:00:00+00:00"}]}))
	assert rows("SELECT COUNT(*) FROM products WHERE user_id = ?", two_users.ids["bob"])[0][0] == 1
	assert rows("SELECT COUNT(*) FROM observations o JOIN products p ON p.id = o.product_id WHERE p.user_id = ?", two_users.ids["bob"])[0][0] == 0
	assert rows("SELECT COUNT(*) FROM products WHERE user_id = ?", two_users.ids["alice"])[0][0] == 1


def test_ownership_fields_in_the_file_are_ignored(two_users):
	upload(two_users("alice"), "a.json", payload({"url": URL, "user_id": two_users.ids["bob"], "id": 1}))
	assert rows("SELECT user_id FROM products")[0][0] == two_users.ids["alice"]


def test_import_respects_the_per_user_product_limit(two_users, monkeypatch):
	monkeypatch.setattr(main, "MAX_URLS", 2)
	alice = two_users("alice")
	response = upload(alice, "a.json", payload(*[{"url": f"https://www.walmart.com/ip/p/{i}"} for i in range(5)]))
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 2
	assert "3 skipped because the watchlist limit is 2" in alice.get(response.location).get_data(as_text=True)
	full = upload(alice, "b.json", payload({"url": "https://www.walmart.com/ip/new/9"}))
	assert "already at its limit" in alice.get(full.location).get_data(as_text=True)


def test_bad_rows_are_skipped_and_values_are_sanitised(two_users):
	upload(two_users("alice"), "a.json", payload(
		{"url": "javascript:alert(1)"},
		{"url": ""},
		"not a product",
		{"url": URL, "price": "12.50", "bulk_quantity": "6", "bulk_price": "", "api_provider": "evil", "history": [
			{"price": "nan", "observed_at": "2026-01-01T00:00:00+00:00"},
			{"price": 5, "observed_at": "not a date"},
			{"price": "7.25", "observed_at": "2026-01-02T00:00:00Z", "status": "weird"},
		]},
	))
	product = rows("SELECT * FROM products")[0]
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 1
	assert product["price"] == 12.5 and product["bulk_quantity"] == 6 and product["bulk_price"] is None and product["api_provider"] is None
	history = rows("SELECT price, status FROM observations ORDER BY observed_at")
	assert [(r["price"], r["status"]) for r in history] == [(None, "error"), (7.25, "success")]


def test_csv_import(two_users):
	csv_text = 'url,title,price,history\r\n' + f'{URL},Widget,9.99,"[{{""price"": 9.99, ""observed_at"": ""2026-01-01T00:00:00+00:00""}}]"\r\n'
	upload(two_users("alice"), "a.csv", csv_text)
	assert rows("SELECT price FROM products")[0][0] == 9.99 and rows("SELECT COUNT(*) FROM observations")[0][0] == 1


def test_sqlite_upload_is_read_only_and_ownership_is_ignored(two_users, tmp_path):
	foreign = tmp_path / "foreign.sqlite3"
	database = sqlite3.connect(foreign)
	database.executescript(f"""
		CREATE TABLE products (id INTEGER PRIMARY KEY, url TEXT, source TEXT, user_id INTEGER, secret TEXT);
		CREATE TABLE observations (id INTEGER PRIMARY KEY, product_id INTEGER, price REAL, observed_at TEXT, status TEXT);
		INSERT INTO products VALUES (1, '{URL}', 'Walmart', {two_users.ids['bob']}, 'x');
		INSERT INTO observations VALUES (1, 1, 4.5, '2026-01-01T00:00:00+00:00', 'success');
	""")
	database.commit()
	database.close()
	before = foreign.read_bytes()
	assert read_watchlist_database(foreign)[0]["history"][0]["price"] == 4.5
	assert foreign.read_bytes() == before
	upload(two_users("alice"), "foreign.sqlite3", before)
	assert rows("SELECT user_id FROM products")[0][0] == two_users.ids["alice"]
	assert rows("SELECT COUNT(*) FROM users WHERE username = 'x'")[0][0] == 0


def test_invalid_uploads_are_rejected_without_changes(two_users):
	alice = two_users("alice")
	for name, content in (("a.json", "{nope"), ("a.json", '{"x": 1}'), ("a.csv", "name\r\nfoo"), ("a.sqlite3", b"not a database"), ("a.txt", "hi")):
		response = upload(alice, name, content)
		assert response.status_code == 302
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 0
	assert rows("SELECT COUNT(*) FROM users")[0][0] == 3 #local + alice + bob


def test_import_requires_a_login(two_users):
	assert upload(main.app.test_client(), "a.json", payload({"url": URL})).status_code == 302
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 0


def test_oversized_json_import_is_refused(two_users):
	with connect() as connection:
		with pytest.raises(ValueError):
			merge_watchlist(connection, two_users.ids["alice"], [{"url": URL}] * 1001, max_products=100, normalize_url=main.normalize_url)


def test_local_mode_import_merges_into_the_local_user():
	with connect() as connection:
		connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, 'https://www.walmart.com/ip/mine/1', 'Walmart')", (local_user_id(connection),))
	upload(main.app.test_client(), "a.json", payload({"url": URL}))
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 2


def test_export_then_import_roundtrips_between_users(two_users):
	alice, bob = two_users("alice"), two_users("bob")
	upload(alice, "a.json", payload({"url": URL, "title": "Widget", "history": [{"price": 3.0, "observed_at": "2026-01-01T00:00:00+00:00", "status": "success"}]}))
	exported = alice.get("/export/json").get_data()
	upload(bob, "alice.json", exported)
	assert rows("SELECT title FROM products WHERE user_id = ?", two_users.ids["bob"])[0][0] == "Widget"
	assert rows("SELECT COUNT(*) FROM observations o JOIN products p ON p.id = o.product_id WHERE p.user_id = ?", two_users.ids["bob"])[0][0] == 1

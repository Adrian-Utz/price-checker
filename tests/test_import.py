import io
import json
import sqlite3

import pytest

import main
from utils.database import connect
from utils.lists import create_list, ensure_personal_list
from utils.users import create_user, local_user_id
from utils.watchlist_io import merge_watchlist, read_watchlist_database

URL = "https://www.walmart.com/ip/widget/111"

# Fixture to create two users and setup the client for testing
@pytest.fixture
def two_users(monkeypatch):
	import dataclasses
	import time
	#Set up the app settings for LAN mode and authentication
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, auto_shutdown=False))
	#Set the app to testing mode
	main.app.testing = True
	#Connect to the database and create users
	with connect() as connection:
		ids = {"alice": create_user(connection, "alice", "correct horse battery", role="admin"), "bob": create_user(connection, "bob", "correct horse battery")}

	# Function to create a client for a specific user
	def client_for(name):
		#Create a test client for the app
		client = main.app.test_client()
		#Start a session for the user
		with client.session_transaction() as session:
			session.update(user_id=ids[name], sv=0, last_seen=int(time.time()))
		return client
	#Attach user IDs to the client function for easy access
	client_for.ids = ids
	return client_for


def upload(client, name, content, **form):
	#Helper function to upload data to  the server
	#Convert content to bytes if it isn't already
	data = content if isinstance(content, bytes) else content.encode()
	#POST the data to the server
	return client.post("/import", data={"database_file": (io.BytesIO(data), name), **form}, content_type="multipart/form-data")


def payload(*products):
	#Creates a payload for uploading data
	#Convert products to a JSON payload
	return json.dumps({"products": list(products)})


def rows(sql, *args):
	#Execute SQL queries and return rows
	#connect to the database and execute the query
	with connect() as connection:
		return connection.execute(sql, args).fetchall()


def test_regular_users_cannot_import_or_add_links(two_users):
	bob = two_users("bob")
	assert upload(bob, "a.json", payload({"url": URL})).status_code == 403
	assert bob.post("/add", data={"url": URL}).status_code == 403
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 0
	with connect() as connection:
		bobs_list = ensure_personal_list(connection, two_users.ids["bob"])
		seeded = connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, ?, 'Walmart')", (bobs_list, two_users.ids["bob"], URL)).lastrowid
	assert bob.post(f"/remove/{seeded}").status_code == 403
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 1
	page = bob.get("/").get_data(as_text=True)
	assert "Import watchlist" not in page 
	assert "Remove from Search" not in page
	assert two_users("alice").post(f"/remove/{seeded}").status_code == 404


def test_admin_can_import_into_a_chosen_list(two_users):
	with connect() as connection:
		shared = create_list(connection, "Shared")
	response = two_users("alice").post(
		"/import", data={"list_id": str(shared), "database_file": (io.BytesIO(payload({"url": URL}).encode()), "a.json")},
		content_type="multipart/form-data",
	)
	assert response.status_code == 302
	assert [r["list_id"] for r in rows("SELECT list_id FROM products")] == [shared]
	bad = two_users("alice").post("/import", data={"list_id": "9999", "database_file": (io.BytesIO(b"{}"), "a.json")}, content_type="multipart/form-data")
	assert "list" in two_users("alice").get(bad.location).get_data(as_text=True).lower()


def test_import_merges_instead_of_replacing(two_users):
	#Create an instance of the user
	alice = two_users("alice")
	#Upload a JSON file with specific product details and price history
	upload(alice, "a.json", payload({"url": URL, "title": "Widget", "history": [{"price": 9.5, "observed_at": "2026-01-01T00:00:00+00:00", "status": "success"}]}))
	#Upload anothe JSON file with a different product URL
	upload(alice, "b.json", payload({"url": "https://www.walmart.com/ip/other/222"}))
	# Assert that the URLs of the products in the database match the expected list
	assert [r["url"] for r in rows("SELECT url FROM products WHERE user_id = ? ORDER BY id", two_users.ids["alice"])] == [URL, "https://www.walmart.com/ip/other/222"]
	# Assert that the total number of observations matches the expected count
	assert rows("SELECT COUNT(*) FROM observations")[0][0] == 1


def test_reimporting_the_same_file_adds_nothing_twice(two_users):
	#Creat an instance of the user
	alice = two_users("alice")
	#Define the payload with a specific URL and price history
	body = payload({"url": URL, "history": [{"price": 9.5, "observed_at": "2026-01-01T00:00:00+00:00"}]})
	#Upload the first instance of the product
	upload(alice, "a.json", body)
	#upload the same product again, which should not add anything new
	response = upload(alice, "a.json", body)
	#Assert that the reponce contains the message indication 0 new and merged 1
	assert "0 new and merged 1" in alice.get(response.location).get_data(as_text=True)
	#Assert that the total number of products and observations remains unchanged
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 1 
	assert rows("SELECT COUNT(*) FROM observations")[0][0] == 1


def test_merging_existing_product_only_adds_missing_history(two_users):
	# Create and instance
	alice = two_users("alice")
	#upload the first product
	upload(alice, "a.json", payload({"url": URL, "history": [{"price": 9.5, "observed_at": "2026-01-01T00:00:00+00:00"}]}))
	#Upload the second product with different price and observation
	upload(alice, "b.json", payload({"url": URL, "title": "Ignored", "history": [
		{"price": 9.5, "observed_at": "2026-01-01T00:00:00+00:00"},
		{"price": 8.0, "observed_at": "2026-01-02T00:00:00+00:00"},
	]}))
	#Assert that the total number of products remains unchanged
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 1
	#Assert that the prices of the observations match the expected list
	assert [r["price"] for r in rows("SELECT price FROM observations ORDER BY observed_at")] == [9.5, 8.0]


def test_import_never_touches_other_users_data(two_users):
	#Connect to the database and insert a product for user
	with connect() as connection:
		bobs_list = ensure_personal_list(connection, two_users.ids["bob"])
		connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, ?, 'Walmart')", (bobs_list, two_users.ids["bob"], URL))
	#Create an instance for the second user
	with connect() as connection:
		#Upload a product for user 2 with the same URL as user 1
		alices_list = ensure_personal_list(connection, two_users.ids["alice"])
	upload(two_users("alice"), "a.json", payload({"url": URL, "history": [{"price": 1, "observed_at": "2026-01-01T00:00:00+00:00"}]}), list_id=str(alices_list))
	#Assert that the number of products for user 1 remains unchanged
	assert rows("SELECT COUNT(*) FROM products WHERE user_id = ?", two_users.ids["bob"])[0][0] == 1
	#Assert that the number of observations for user 1 remians unchanged
	assert rows("SELECT COUNT(*) FROM observations o JOIN products p ON p.id = o.product_id WHERE p.user_id = ?", two_users.ids["bob"])[0][0] == 0
	#Assert that the number of products for user 2 remains unchanged
	assert rows("SELECT COUNT(*) FROM products WHERE user_id = ?", two_users.ids["alice"])[0][0] == 1


def test_ownership_fields_in_the_file_are_ignored(two_users):
	#Upload a product for user 'alice' with a specific URL and user_id
	upload(two_users("alice"), "a.json", payload({"url": URL, "user_id": two_users.ids["bob"], "id": 1}))
	#Assert that the user_id of the product in the database matches the user_id in the payload
	assert rows("SELECT user_id FROM products")[0][0] == two_users.ids["alice"]


def test_import_respects_the_per_user_product_limit(two_users, monkeypatch):
	#Set the maximum number of URLs allowed per user to 2
	monkeypatch.setattr(main, "MAX_URLS", 2)
	#Create an instance of the user
	alice = two_users("alice")
	#Upload multiple products for the user, which should limit to the maximum
	response = upload(alice, "a.json", payload(*[{"url": f"https://www.walmart.com/ip/p/{i}"} for i in range(5)]))
	#Assert that the number of products in the database matches the maximum allowed
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 2
	#Assert that the responce contains the message indicating the number of skipped products
	assert "3 skipped because the watchlist limit is 2" in alice.get(response.location).get_data(as_text=True)
	#Upload another product for the user, which should not allow any more
	full = upload(alice, "b.json", payload({"url": "https://www.walmart.com/ip/new/9"}))
	#Assert that the responce contains the message indicating the product is at it's limit.
	assert "already at its limit" in alice.get(full.location).get_data(as_text=True)


def test_bad_rows_are_skipped_and_values_are_sanitised(two_users):
	#Upload a JSON file with various invalid and incomplete rows
	upload(two_users("alice"), "a.json", payload(
		{"url": "javascript:alert(1)"}, #invalid URL
		{"url": ""}, #Empty URL
		"not a product", #Non-product string
		{"url": URL, "price": "12.50", "bulk_quantity": "6", "bulk_price": "", "api_provider": "evil", "history": [
			{"price": "nan", "observed_at": "2026-01-01T00:00:00+00:00"}, #Invalid price
			{"price": 5, "observed_at": "not a date"}, #Invalid Date
			{"price": "7.25", "observed_at": "2026-01-02T00:00:00Z", "status": "weird"}, #invalid status
		]},
	))
	#Retrieve the product row form the database
	product = rows("SELECT * FROM products")[0]
	#Assert that the product has the expected values after sanitization
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 1
	assert product["price"] == 12.5 
	assert product["bulk_quantity"] == 6 
	assert product["bulk_price"] is None 
	assert product["api_provider"] is None
	# Retrieve the history of observations from the database
	history = rows("SELECT price, status FROM observations ORDER BY observed_at")
	assert [(r["price"], r["status"]) for r in history] == [(None, "error"), (7.25, "success")]


def test_csv_import(two_users):
	#create a CSV string with valid product data
	csv_text = 'url,title,price,history\r\n' + f'{URL},Widget,9.99,"[{{""price"": 9.99, ""observed_at"": ""2026-01-01T00:00:00+00:00""}}]"\r\n'
	#Upload the CSV file with the valid product data
	upload(two_users("alice"), "a.csv", csv_text)
	#Assert that the product price in the database matches the expected value
	assert rows("SELECT price FROM products")[0][0] == 9.99 
	#Assert that the number of observations in the database is 1
	assert rows("SELECT COUNT(*) FROM observations")[0][0] == 1


def test_sqlite_upload_is_read_only_and_ownership_is_ignored(two_users, tmp_path):
	#Create a temporary SQLite database file
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
	#Read the figinal bytes of the SQLite database file
	before = foreign.read_bytes()
	#Assert that the historical price in the database is sanitized
	assert read_watchlist_database(foreign)[0]["history"][0]["price"] == 4.5
	#Assert that the SQLite database file has not changed
	assert foreign.read_bytes() == before
	#Upload the modified SQLite database file
	upload(two_users("alice"), "foreign.sqlite3", before)
	#Assert that the user ID in the products table is still the same user
	assert rows("SELECT user_id FROM products")[0][0] == two_users.ids["alice"]
	#Assert that there are no users with the username 'x' in the database
	assert rows("SELECT COUNT(*) FROM users WHERE username = 'x'")[0][0] == 0


def test_invalid_uploads_are_rejected_without_changes(two_users):
	#New user instance
	alice = two_users("alice")
	#Iterate over a list of invalid upload files and their contents
	for name, content in (("a.json", "{nope"), ("a.json", '{"x": 1}'), ("a.csv", "name\r\nfoo"), ("a.sqlite3", b"not a database"), ("a.txt", "hi")):
		#upload the invalid file
		response = upload(alice, name, content)
		#Assert that the responce status code is 302
		assert response.status_code == 302
	#Assert thet there are no products in the database, and that there are 3 users in the database
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 0
	assert rows("SELECT COUNT(*) FROM users")[0][0] == 3 #local + alice + bob


def test_import_requires_a_login(two_users):
	#Attempt to upload a JSON file without logging in andassert the responce code
	assert upload(main.app.test_client(), "a.json", payload({"url": URL})).status_code == 302
	#Assert that there are no products in the database
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 0


def test_oversized_json_import_is_refused(two_users):
	#Create a connection to the database
	with connect() as connection:
		#Attempt to merge a large list of products into the database and assert a ValueError is raised
		with pytest.raises(ValueError):
			merge_watchlist(connection, two_users.ids["alice"], [{"url": URL}] * 1001, max_products=100, normalize_url=main.normalize_url)


def test_local_mode_import_merges_into_the_local_user():
	#Create a connection to the datavase and insert a product into the 'products' table
	with connect() as connection:
		connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, 'https://www.walmart.com/ip/mine/1', 'Walmart')", (local_user_id(connection),))
	#Upload a JSON file to the local user and assert the number of products is 2
	upload(main.app.test_client(), "a.json", payload({"url": URL}))
	assert rows("SELECT COUNT(*) FROM products")[0][0] == 2


def test_export_then_import_roundtrips_between_users(two_users):
	#Create two users
	alice = two_users("alice")
	with connect() as connection:
		alices_list = ensure_personal_list(connection, two_users.ids["alice"])
	#Upload a JSON file to alice user with product data
	upload(alice, "a.json", payload({"url": URL, "title": "Widget", "history": [{"price": 3.0, "observed_at": "2026-01-01T00:00:00+00:00", "status": "success"}]}), list_id=str(alices_list))
	#Export the JSON data from alice user
	exported = alice.get("/export/json").get_data()
	with connect() as connection:
		bobs_list = ensure_personal_list(connection, two_users.ids["bob"])
	#Upload the exported JSON data in to bobs user 
	alice.post("/import", data={"list_id": str(bobs_list), "database_file": (io.BytesIO(exported), "alice.json")}, content_type="multipart/form-data")
	# Assert that the title of the product in bobs user is Widget
	assert rows("SELECT title FROM products WHERE list_id = ?", bobs_list)[0][0] == "Widget"
	#Assert that there is 1 observation in bobs user
	assert rows("SELECT COUNT(*) FROM observations o JOIN products p ON p.id = o.product_id WHERE p.list_id = ?", bobs_list)[0][0] == 1

import dataclasses
import time

import pytest

import main
from utils import lists
from utils.database import connect
from utils.users import create_user

URL = "https://www.walmart.com/ip/shared-widget/1"


@pytest.fixture
def team(monkeypatch):
	"""LAN app with an admin and two regular users; returns a client factory with ids and a shared list."""
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, auto_shutdown=False))
	monkeypatch.setattr(main, "auto_server_shutdown", False)
	main.app.testing = True
	with connect() as connection:
		ids = {
			"root": create_user(connection, "root-admin", "correct horse battery", role="admin"),
			"alice": create_user(connection, "alice", "correct horse battery"),
			"bob": create_user(connection, "bob", "correct horse battery"),
			"carol": create_user(connection, "carol", "correct horse battery"),
		}
		shared = lists.create_list(connection, "Kitchen")
		lists.add_member(connection, shared, ids["alice"])
		lists.add_member(connection, shared, ids["bob"])
		product = connection.execute(
			"INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, ?, 'Walmart')", (shared, ids["root"], URL)
		).lastrowid

	def client_for(name):
		client = main.app.test_client()
		with client.session_transaction() as session:
			session.update(user_id=ids[name], sv=0, last_seen=int(time.time()))
		return client

	client_for.ids, client_for.list_id, client_for.product = ids, shared, product
	return client_for


def test_members_see_the_same_items_and_outsiders_do_not(team):
	assert "shared-widget" in team("alice").get("/").get_data(as_text=True)
	assert "shared-widget" in team("bob").get("/").get_data(as_text=True)
	assert "shared-widget" not in team("carol").get("/").get_data(as_text=True)
	assert team("carol").post(f"/item-provider/{team.product}", data={"provider": ""}).status_code == 404


def test_one_members_provider_change_is_seen_by_the_others(team):
	assert team("alice").post(f"/item-provider/{team.product}", data={"provider": "serpapi"}).status_code in (302, 200)
	with connect() as connection:
		assert connection.execute("SELECT api_provider FROM products WHERE id = ?", (team.product,)).fetchone()[0] == "serpapi"
	assert "API: serpapi" in team("bob").get("/").get_data(as_text=True)


def test_saved_stores_are_shared_within_a_list(team):
	team("alice").post("/stores/add", data={"retailer": "Walmart", "store_id": "4638", "list_id": str(team.list_id)})
	assert "Store 4638" in team("bob").get("/").get_data(as_text=True) or "4638" in team("bob").get("/").get_data(as_text=True)
	with connect() as connection:
		stores = connection.execute("SELECT list_id FROM stores").fetchall()
	assert [row[0] for row in stores] == [team.list_id]
	assert team("bob").post(f"/store/{team.product}", data={"store_id": "4638"}).status_code in (302, 200)
	with connect() as connection:
		assert connection.execute("SELECT store_id FROM products WHERE id = ?", (team.product,)).fetchone()[0] == "4638"


def test_scan_charges_the_member_who_runs_it(team, monkeypatch):
	monkeypatch.setattr(main, "MIN_DELAY_SECONDS", 0.0)
	monkeypatch.setattr(main, "DEVELOPER_MODE", False)
	monkeypatch.setattr(main, "fetch_product", lambda url, **kwargs: {"price": 2.0, "currency": "USD"})
	team("bob").post("/scan")
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM scans WHERE user_id = ?", (team.ids["bob"],)).fetchone()[0] == 1
		assert connection.execute("SELECT COUNT(*) FROM scans WHERE user_id = ?", (team.ids["alice"],)).fetchone()[0] == 0


def test_only_admins_manage_lists(team):
	assert team("alice").get("/admin/lists").status_code == 403
	assert team("alice").post("/admin/lists/create", data={"name": "Mine"}).status_code == 403
	root = team("root")
	assert root.get("/admin/lists").status_code == 200
	root.post("/admin/lists/create", data={"name": "Garage"})
	root.post(f"/admin/lists/{team.list_id}/members/add", data={"user_id": str(team.ids["carol"])})
	assert "shared-widget" in team("carol").get("/").get_data(as_text=True)
	root.post(f"/admin/lists/{team.list_id}/members/{team.ids['carol']}/remove")
	assert "shared-widget" not in team("carol").get("/").get_data(as_text=True)


def test_a_list_with_items_cannot_be_deleted(team):
	response = team("root").post(f"/admin/lists/{team.list_id}/delete")
	assert response.status_code == 400
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM lists WHERE id = ?", (team.list_id,)).fetchone()[0] == 1


def test_list_names_must_be_unique_and_members_must_be_real_users(team):
	with connect() as connection:
		with pytest.raises(ValueError):
			lists.create_list(connection, "kitchen")
		with pytest.raises(ValueError):
			lists.add_member(connection, team.list_id, 9999)


def test_list_filter_shows_one_list_or_all(team):
	with connect() as connection:
		other = lists.create_list(connection, "Garage")
		lists.add_member(connection, other, team.ids["alice"])
		connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, 'https://www.walmart.com/ip/garage-thing/2', 'Walmart')", (other, team.ids["root"]))
	alice = team("alice")
	both = alice.get("/").get_data(as_text=True)
	assert "shared-widget" in both 
	assert "garage-thing" in both 
	assert "All lists" in both
	kitchen = alice.get(f"/?list={team.list_id}").get_data(as_text=True)
	assert "shared-widget" in kitchen 
	assert "garage-thing" not in kitchen
	garage = alice.get(f"/?list={other}").get_data(as_text=True)
	assert "garage-thing" in garage 
	assert "shared-widget" not in garage
	assert "shared-widget" in alice.get("/?list=abc").get_data(as_text=True)


def test_list_filter_cannot_reach_lists_the_user_is_not_on(team):
	with connect() as connection:
		secret = lists.create_list(connection, "Secret")
		connection.execute("INSERT INTO products (list_id, user_id, url, source) VALUES (?, ?, 'https://www.walmart.com/ip/secret-thing/3', 'Walmart')", (secret, team.ids["root"]))
	body = team("alice").get(f"/?list={secret}").get_data(as_text=True)
	assert "secret-thing" not in body 
	assert "Secret" not in body

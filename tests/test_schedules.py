import dataclasses
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import main
from utils import schedules
from utils.database import connect
from utils.lists import create_list
from utils.users import claim_local_data, create_user, local_user_id


UTC = timezone.utc
START = datetime(2030, 1, 31, 12, 30, tzinfo=UTC)


@pytest.mark.parametrize(("frequency", "when", "expected"), [
	("once", START, True),
	("once", START + timedelta(days=1), False),
	("daily", START + timedelta(days=1), True),
	("weekly", START + timedelta(days=7), True),
	("weekly", START + timedelta(days=1), False),
	("monthly", datetime(2030, 2, 28, 12, 30, tzinfo=UTC), False),
	("monthly", datetime(2030, 3, 31, 12, 30, tzinfo=UTC), True),
	("daily", START - timedelta(days=1), False),
	("daily", START + timedelta(minutes=1), False),
])
def test_due(frequency, when, expected):
	assert schedules.due({"frequency": frequency, "start_at": "2030-01-31T12:30"}, when) is expected


def test_schedule_page_permissions_and_validation(monkeypatch):
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True))
	with connect() as connection:
		admin = create_user(connection, "root", "correct horse battery", "admin")
		user = create_user(connection, "alice", "correct horse battery")
		list_id = create_list(connection, "Kitchen")
	def client_for(user_id):
		client = main.app.test_client()
		with client.session_transaction() as session:
			session.update(user_id=user_id, sv=0, last_seen=time.time())
		return client
	admin_client, user_client = client_for(admin), client_for(user)
	assert user_client.get("/admin/schedules").status_code == 403
	assert user_client.post("/admin/schedules/create").status_code == 403
	assert user_client.post("/admin/schedules/1/delete").status_code == 403
	assert admin_client.get("/admin/schedules").status_code == 200
	assert admin_client.post("/admin/schedules/create", data={"list_id": list_id, "frequency": "daily", "start_at": "bad"}).status_code == 400
	assert admin_client.post("/admin/schedules/create", data={"list_id": 9999, "frequency": "daily", "start_at": "2099-01-01T12:00"}).status_code == 400
	assert admin_client.post("/admin/schedules/create", data={"list_id": list_id, "frequency": "weekly", "start_at": "2099-01-01T12:00", "force": "1"}).status_code == 302
	with connect() as connection:
		row = connection.execute("SELECT * FROM scan_schedules").fetchone()
		assert (row["created_by"], row["list_id"], row["force"]) == (admin, list_id, 1)
	assert admin_client.post(f"/admin/schedules/{row['id']}/disable").status_code == 302
	assert admin_client.post(f"/admin/schedules/{row['id']}/enable").status_code == 302
	assert admin_client.post(f"/admin/schedules/{row['id']}/delete").status_code == 302
	assert admin_client.post(f"/admin/schedules/{row['id']}/delete").status_code == 404


def test_csrf_required_for_schedule_changes(monkeypatch):
	monkeypatch.setitem(main.app.config, "CSRF_ENABLED", True)
	assert main.app.test_client().post("/admin/schedules/create").status_code == 400


def test_local_owner_can_open_schedule_page():
	client = main.app.test_client()
	assert "Schedules" in client.get("/").get_data(as_text=True)
	assert client.get("/admin/schedules").status_code == 200


def test_dispatch_claims_only_current_minute_once(monkeypatch):
	with connect() as connection:
		owner = local_user_id(connection)
		list_id = create_list(connection, "Unassigned")
		connection.execute("INSERT INTO scan_schedules (list_id, created_by, start_at, frequency) VALUES (?, ?, ?, 'daily')", (list_id, owner, "2030-01-31T12:30"))
	pending = queue.Queue()
	monkeypatch.setattr(main, "scheduled_list_queue", pending)
	main.dispatch_list_schedules(START + timedelta(minutes=1))
	assert pending.empty()
	main.dispatch_list_schedules(START)
	main.dispatch_list_schedules(START + timedelta(seconds=30))
	entry, occurrence = pending.get_nowait()
	assert (entry["list_id"], occurrence) == (list_id, "2030-01-31T12:30")
	assert pending.empty()
	main.dispatch_list_schedules(START + timedelta(days=1))
	assert pending.qsize() == 1


def test_scheduled_list_respects_cache_and_budget_or_forces(monkeypatch):
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, user_daily_api_calls=1, global_daily_api_calls=1))
	monkeypatch.setattr(main, "provider_ready", lambda provider: True)
	monkeypatch.setattr(main, "MIN_DELAY_SECONDS", 0)
	monkeypatch.setattr("utils.scanner.time.sleep", lambda _: None)
	fetched = []
	monkeypatch.setattr(main, "fetch_product", lambda url, **kwargs: fetched.append(url) or {"price": 7})
	pending = queue.Queue()
	monkeypatch.setattr(main, "scheduled_list_queue", pending)
	with connect() as connection:
		owner = local_user_id(connection)
		list_id = create_list(connection, "Unassigned")
		connection.execute("INSERT INTO products (user_id, list_id, url, source, price, checked_at) VALUES (?, ?, 'https://example.com/fresh', 'Test', 1, ?)", (owner, list_id, main.utc_now().isoformat()))
		connection.execute("INSERT INTO products (user_id, list_id, url, source) VALUES (?, ?, 'https://example.com/stale', 'Test')", (owner, list_id))
		connection.execute("INSERT INTO products (user_id, list_id, url, source) VALUES (?, ?, 'https://example.com/other', 'Test')", (owner, list_id))
		connection.execute("INSERT INTO scan_schedules (list_id, created_by, start_at, frequency) VALUES (?, ?, ?, 'daily')", (list_id, owner, "2030-01-31T12:30"))
	main.dispatch_list_schedules(START)
	row, occurrence = pending.get_nowait()
	main.run_list_schedule(row, occurrence)
	assert fetched == ["https://example.com/stale"]
	with connect() as connection:
		assert connection.execute("SELECT last_status FROM scan_schedules").fetchone()[0] == "stopped"
		connection.execute("UPDATE scan_schedules SET force = 1")
		row = connection.execute("SELECT * FROM scan_schedules").fetchone()
	main.run_list_schedule(row, "2030-01-31T12:30")
	assert fetched[1:] == ["https://example.com/fresh", "https://example.com/stale", "https://example.com/other"]
	with connect() as connection:
		assert connection.execute("SELECT calls FROM api_spend WHERE user_id = ?", (owner,)).fetchone()[0] == 4
		assert connection.execute("SELECT last_status FROM scan_schedules").fetchone()[0] == "completed"


def test_local_schedules_follow_first_admin():
	with connect() as connection:
		owner = local_user_id(connection)
		list_id = create_list(connection, "Scheduled")
		connection.execute("INSERT INTO scan_schedules (list_id, created_by, start_at, frequency) VALUES (?, ?, ?, 'daily')", (list_id, owner, "2099-01-01T12:00"))
		admin = create_user(connection, "root", "correct horse battery", "admin")
		claim_local_data(connection, admin)
		assert connection.execute("SELECT created_by FROM scan_schedules").fetchone()[0] == admin


def test_sub_admin_schedule_runs(monkeypatch):
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True))
	monkeypatch.setattr(main, "provider_ready", lambda provider: True)
	calls = []
	monkeypatch.setattr(main, "scan_products", lambda *args, **kwargs: calls.append(True) or True)
	with connect() as connection:
		sub = create_user(connection, "helper", "correct horse battery", "sub-admin")
		list_id = create_list(connection, "Scheduled")
		connection.execute("INSERT INTO scan_schedules (list_id, created_by, start_at, frequency, last_occurrence) VALUES (?, ?, '2030-01-31T12:30', 'daily', '2030-01-31T12:30')", (list_id, sub))
		row = connection.execute("SELECT * FROM scan_schedules").fetchone()
	main.run_list_schedule(row, "2030-01-31T12:30")
	assert calls == [True]
	with connect() as connection:
		assert connection.execute("SELECT last_status FROM scan_schedules").fetchone()[0] == "completed"


def test_list_schedule_force_is_independent_of_account_override(monkeypatch):
	monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, user_daily_api_calls=1, global_daily_api_calls=1))
	monkeypatch.setattr(main, "provider_ready", lambda provider: True)
	monkeypatch.setattr("utils.scanner.time.sleep", lambda _: None)
	fetched = []
	monkeypatch.setattr(main, "_refresh_product", lambda product, **kwargs: fetched.append(product["id"]))
	with connect() as connection:
		sub = create_user(connection, "helper", "correct horse battery", "sub-admin")
		list_id = create_list(connection, "Scheduled")
		product = connection.execute("INSERT INTO products (user_id, list_id, url, source, price, checked_at) VALUES (?, ?, 'https://example.com/p', 'Test', 1, ?)", (sub, list_id, main.utc_now().isoformat())).lastrowid
		connection.execute("INSERT INTO api_spend (user_id, day, calls) VALUES (?, date('now'), 1)", (sub,))
		connection.execute("INSERT INTO scan_schedules (list_id, created_by, start_at, frequency, last_occurrence) VALUES (?, ?, '2030-01-31T12:30', 'daily', '2030-01-31T12:30')", (list_id, sub))
		row = connection.execute("SELECT * FROM scan_schedules").fetchone()
	main.run_list_schedule(row, "2030-01-31T12:30")
	assert fetched == []
	with connect() as connection:
		connection.execute("UPDATE users SET override_limits = 0 WHERE id = ?", (sub,))
		connection.execute("UPDATE scan_schedules SET force = 1")
		row = connection.execute("SELECT * FROM scan_schedules").fetchone()
	main.run_list_schedule(row, "2030-01-31T12:30")
	assert fetched == [product]


def test_scheduled_scan_waits_for_list_and_disabled_queue_does_not_run(monkeypatch):
	monkeypatch.setattr(main, "provider_ready", lambda provider: True)
	calls = []
	monkeypatch.setattr(main, "scan_products", lambda *args, **kwargs: calls.append(True) or True)
	with connect() as connection:
		owner = local_user_id(connection)
		list_id = create_list(connection, "Busy")
		connection.execute("INSERT INTO scan_schedules (list_id, created_by, start_at, frequency, last_occurrence) VALUES (?, ?, ?, 'daily', ?)", (list_id, owner, "2030-01-31T12:30", "2030-01-31T12:30"))
		row = connection.execute("SELECT * FROM scan_schedules").fetchone()
	lock = main.list_scan_lock_for(list_id)
	with lock:
		worker = threading.Thread(target=main.run_list_schedule, args=(row, "2030-01-31T12:30"))
		worker.start()
		time.sleep(0.05)
		assert calls == []
	worker.join(timeout=2)
	assert not worker.is_alive()
	assert calls == [True]
	with connect() as connection:
		connection.execute("UPDATE scan_schedules SET generation = generation + 1, enabled = 1")
	main.run_list_schedule(row, "2030-01-31T12:30")
	assert calls == [True]

import dataclasses
import re
import time

import pytest

import main
from utils import auth
from utils.database import admin_exists, connect
from utils.users import create_user, local_user_id, validate_password

PASSWORD = "correct horse battery"


@pytest.fixture
def lan(monkeypatch):
	"""LAN-mode app with an admin and a regular user; settings can be overridden per test."""
	monkeypatch.setattr(main, "auto_server_shutdown", False)
	main.app.testing = True

	def configure(**overrides):
		monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, auto_shutdown=False, **overrides))

	configure()
	with connect() as connection:
		lan.ids = {
			"admin": create_user(connection, "boss", PASSWORD, "admin"),
			"alice": create_user(connection, "alice", PASSWORD),
		}
	configure.ids = lan.ids
	return configure


def sign_in(username="alice", password=PASSWORD, **extra):
	client = main.app.test_client()
	response = client.post("/login", data={"username": username, "password": password, **extra})
	return client, response


def user_row(name):
	with connect() as connection:
		return connection.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def test_login_logout_roundtrip(lan):
	client, response = sign_in()
	assert response.status_code == 302 and response.headers["Location"].endswith("/")
	assert client.get("/").status_code == 200
	client.post("/logout")
	assert client.get("/").status_code == 302


def test_wrong_password_and_unknown_user_look_identical(lan):
	_, wrong = sign_in(password="not the password")
	_, unknown = sign_in(username="nobody")
	assert wrong.status_code == unknown.status_code == 401
	strip_token = lambda response: re.sub(rb'nonce="[^"]*"', b"", re.sub(rb'name="csrf_token" value="[^"]*"', b"", response.get_data()))
	assert strip_token(wrong) == strip_token(unknown)


def test_local_placeholder_account_can_never_log_in(lan):
	assert sign_in(username="local", password="!")[1].status_code == 401


def test_session_is_regenerated_on_login(lan):
	client = main.app.test_client()
	with client.session_transaction() as session:
		session["junk"] = "from before the login"
		session["csrf_token"] = "old-token"
	client.post("/login", data={"username": "alice", "password": PASSWORD})
	with client.session_transaction() as session:
		assert "junk" not in session and session["csrf_token"] != "old-token"
		assert session["user_id"] == lan.ids["alice"]


def test_account_locks_after_repeated_failures_and_recovers(lan):
	lan(login_max_failures=3, login_ip_limit=1000)
	for _ in range(3):
		sign_in(password="wrong password!!")
	assert sign_in()[1].status_code == 401 #Correct password is refused while locked
	assert user_row("alice")["locked_until"]
	with connect() as connection:
		connection.execute("UPDATE users SET locked_until = '2000-01-01T00:00:00Z' WHERE username = 'alice'")
	assert sign_in()[1].status_code == 302
	assert user_row("alice")["failed_logins"] == 0


def test_ip_rate_limit_returns_429_with_retry_after(lan):
	lan(login_ip_limit=5)
	for _ in range(5):
		sign_in(username="nobody")
	response = sign_in()[1] #Even a correct login is blocked once the address is over the limit
	assert response.status_code == 429
	assert int(response.headers["Retry-After"]) > 0


def test_retry_after_header_is_optional(lan):
	lan(login_ip_limit=5, retry_after_header=False)
	for _ in range(5):
		sign_in(username="nobody")
	response = sign_in()[1]
	assert response.status_code == 429 and "Retry-After" not in response.headers


def test_idle_sessions_time_out(lan):
	client, _ = sign_in()
	with client.session_transaction() as session:
		session["last_seen"] = int(time.time()) - 3 * 3600
	assert client.get("/").status_code == 302
	with client.session_transaction() as session:
		assert "user_id" not in session


def test_remember_me_survives_idle_time_but_not_its_lifetime(lan):
	client, _ = sign_in(remember="on")
	with client.session_transaction() as session:
		assert session.permanent
		session["last_seen"] = int(time.time()) - 3 * 3600
	assert client.get("/").status_code == 200
	with client.session_transaction() as session:
		session["issued"] = int(time.time()) - 31 * 86400
	assert client.get("/").status_code == 302


def test_remember_me_can_be_turned_off(lan):
	lan(remember_days=0)
	client, _ = sign_in(remember="on")
	with client.session_transaction() as session:
		assert not session["remember"]


def test_next_parameter_cannot_redirect_off_site(lan):
	for target in ("//evil.example", "https://evil.example", "/\\evil.example"):
		_, response = sign_in(next=target)
		assert response.headers["Location"].endswith("/") and "evil" not in response.headers["Location"]
	_, response = sign_in(next="/account")
	assert response.headers["Location"].endswith("/account")


def test_login_page_is_not_available_in_local_mode():
	assert main.app.test_client().get("/login").status_code == 404


def test_changing_password_signs_out_other_sessions(lan):
	first, _ = sign_in()
	second, _ = sign_in()
	response = first.post("/account/password", data={"current_password": PASSWORD, "new_password": "a brand new passphrase", "confirm_password": "a brand new passphrase"})
	assert response.status_code == 302
	assert first.get("/").status_code == 200 #This session stays signed in
	assert second.get("/").status_code == 302
	assert sign_in(password="a brand new passphrase")[1].status_code == 302


def test_changing_password_needs_the_current_one_and_a_valid_new_one(lan):
	client, _ = sign_in()
	bad_current = client.post("/account/password", data={"current_password": "wrong wrong wrong", "new_password": "another good passphrase", "confirm_password": "another good passphrase"})
	too_short = client.post("/account/password", data={"current_password": PASSWORD, "new_password": "short", "confirm_password": "short"})
	mismatch = client.post("/account/password", data={"current_password": PASSWORD, "new_password": "another good passphrase", "confirm_password": "different one entirely"})
	assert bad_current.status_code == too_short.status_code == mismatch.status_code == 400
	assert sign_in()[1].status_code == 302


def test_password_policy_is_length_only():
	validate_password("someone", "alllowercaseletters")
	for bad in ("short", "someone" * 1, "x" * 300):
		with pytest.raises(ValueError):
			validate_password("someone", bad)
	with pytest.raises(ValueError):
		validate_password("longusername1", "LongUserName1")


def test_registration_is_closed_by_default(lan):
	assert main.app.test_client().get("/register").status_code == 404
	assert main.app.test_client().post("/register", data={"username": "mallory", "password": PASSWORD, "confirm_password": PASSWORD}).status_code == 404


def test_open_registration_creates_a_regular_user(lan):
	lan(registration="open", login_ip_limit=1000)
	client = main.app.test_client()
	response = client.post("/register", data={"username": "newbie", "password": PASSWORD, "confirm_password": PASSWORD})
	assert response.status_code == 302
	assert user_row("newbie")["role"] == "user"
	mismatch = client.post("/register", data={"username": "other", "password": PASSWORD, "confirm_password": "nope nope nope"})
	assert mismatch.status_code == 400


def test_open_registration_is_rate_limited(lan):
	lan(registration="open")
	client = main.app.test_client()
	statuses = [client.post("/register", data={"username": f"spam{i}", "password": PASSWORD, "confirm_password": PASSWORD}).status_code for i in range(12)]
	assert statuses[:10] == [302] * 10 and statuses[10:] == [429, 429]


def create_invite_token(hours=72):
	with connect() as connection:
		return auth.create_invite(connection, lan_admin_id(), hours)


def lan_admin_id():
	return user_row("boss")["id"]


def test_invite_links_work_once(lan):
	lan(registration="invite", login_ip_limit=1000)
	token = create_invite_token()
	client = main.app.test_client()
	assert client.get(f"/register?invite={token}").status_code == 200
	data = {"invite": token, "username": "guest1", "password": PASSWORD, "confirm_password": PASSWORD}
	assert client.post("/register", data=data).status_code == 302
	assert user_row("guest1")["role"] == "user"
	data["username"] = "guest2"
	assert client.post("/register", data=data).status_code == 400
	assert user_row("guest2") is None
	assert client.get(f"/register?invite={token}").status_code == 404


def test_invalid_expired_and_revoked_invites_are_refused(lan):
	lan(registration="invite")
	client = main.app.test_client()
	assert client.get("/register").status_code == 404
	assert client.get("/register?invite=made-up").status_code == 404
	expired = create_invite_token()
	revoked = create_invite_token()
	with connect() as connection:
		connection.execute("UPDATE invites SET expires_at = '2000-01-01T00:00:00Z' WHERE id = 1")
		auth.revoke_invite(connection, 2)
	assert client.get(f"/register?invite={expired}").status_code == 404
	assert client.get(f"/register?invite={revoked}").status_code == 404


def test_a_failed_signup_does_not_burn_the_invite(lan):
	lan(registration="invite", login_ip_limit=1000)
	token = create_invite_token()
	client = main.app.test_client()
	client.post("/register", data={"invite": token, "username": "x", "password": PASSWORD, "confirm_password": PASSWORD})
	assert client.get(f"/register?invite={token}").status_code == 200


def test_invites_are_stored_hashed(lan):
	token = create_invite_token()
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM invites WHERE token_hash = ?", (token,)).fetchone()[0] == 0


def test_admin_pages_are_admin_only(lan):
	alice, _ = sign_in()
	assert alice.get("/admin/users").status_code == 403
	assert alice.post("/admin/users/create", data={"username": "sneaky", "password": PASSWORD, "role": "admin"}).status_code == 403
	assert alice.post(f"/admin/users/{lan.ids['alice']}/role", data={"role": "admin"}).status_code == 403
	assert user_row("sneaky") is None and user_row("alice")["role"] == "user"


def test_admin_creates_users_and_changes_roles(lan):
	admin, _ = sign_in("boss")
	assert admin.get("/admin/users").status_code == 200
	assert admin.post("/admin/users/create", data={"username": "carol", "password": PASSWORD, "role": "user"}).status_code == 302
	carol = user_row("carol")
	assert admin.post(f"/admin/users/{carol['id']}/role", data={"role": "admin"}).status_code == 302
	assert user_row("carol")["role"] == "admin"
	assert admin.post("/admin/users/create", data={"username": "carol", "password": PASSWORD}).status_code == 400
	assert admin.post("/admin/users/create", data={"username": "dave", "password": "short"}).status_code == 400


def test_admin_cannot_demote_or_disable_themselves(lan):
	admin, _ = sign_in("boss")
	assert admin.post(f"/admin/users/{lan.ids['admin']}/role", data={"role": "user"}).status_code == 400
	assert admin.post(f"/admin/users/{lan.ids['admin']}/status", data={"action": "disable"}).status_code == 400
	assert user_row("boss")["role"] == "admin" and not user_row("boss")["disabled"]


def test_disabling_a_user_signs_them_out_and_blocks_login(lan):
	admin, _ = sign_in("boss")
	alice, _ = sign_in()
	assert admin.post(f"/admin/users/{lan.ids['alice']}/status", data={"action": "disable"}).status_code == 302
	assert alice.get("/").status_code == 302
	assert sign_in()[1].status_code == 401
	admin.post(f"/admin/users/{lan.ids['alice']}/status", data={"action": "enable"})
	assert sign_in()[1].status_code == 302


def test_admin_password_reset_unlocks_and_signs_out(lan):
	lan(login_max_failures=3, login_ip_limit=1000)
	alice, _ = sign_in()
	for _ in range(3):
		sign_in(password="wrong password!!")
	admin, _ = sign_in("boss")
	assert admin.post(f"/admin/users/{lan.ids['alice']}/password", data={"password": "reset by the admin"}).status_code == 302
	assert alice.get("/").status_code == 302
	assert sign_in(password="reset by the admin")[1].status_code == 302


def test_admin_creates_and_revokes_invites_only_in_invite_mode(lan):
	admin, _ = sign_in("boss")
	assert admin.post("/admin/invites/create", data={"hours": "24"}).status_code == 404
	lan(registration="invite")
	admin, _ = sign_in("boss")
	response = admin.post("/admin/invites/create", data={"hours": "24"})
	assert response.status_code == 200 and b"/register?invite=" in response.data
	assert admin.post("/admin/invites/create", data={"hours": "0"}).status_code == 400
	assert admin.post("/admin/invites/1/revoke").status_code == 302
	with connect() as connection:
		assert connection.execute("SELECT revoked FROM invites WHERE id = 1").fetchone()[0] == 1


def test_user_management_does_not_exist_in_local_mode():
	client = main.app.test_client()
	assert client.get("/admin/users").status_code == 404
	assert client.get("/account").status_code == 404


def test_create_admin_command_adopts_existing_local_data(monkeypatch):
	with connect() as connection:
		connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, 'https://www.walmart.com/ip/x/1', 'Walmart')", (local_user_id(connection),))
	monkeypatch.setenv("PRICE_CHECKER_ADMIN_PASSWORD", PASSWORD)
	assert not admin_exists()
	main.cli_create_admin(["firstadmin"])
	assert admin_exists()
	with connect() as connection:
		owner = connection.execute("SELECT u.username FROM products p JOIN users u ON u.id = p.user_id").fetchone()[0]
	assert owner == "firstadmin"
	main.cli_create_admin(["secondadmin"]) #Only the first account adopts the old data
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM products p JOIN users u ON u.id = p.user_id WHERE u.username = 'secondadmin'").fetchone()[0] == 0


def test_create_admin_command_rejects_bad_input(monkeypatch):
	monkeypatch.setenv("PRICE_CHECKER_ADMIN_PASSWORD", "short")
	with pytest.raises(SystemExit):
		main.cli_create_admin(["someadmin"])
	monkeypatch.setenv("PRICE_CHECKER_ADMIN_PASSWORD", PASSWORD)
	with pytest.raises(SystemExit):
		main.cli_create_admin(["local"])
	assert not admin_exists()


def test_reset_password_command(lan, monkeypatch):
	monkeypatch.setenv("PRICE_CHECKER_ADMIN_PASSWORD", "reset from the console")
	with connect() as connection:
		connection.execute("UPDATE users SET locked_until = '2999-01-01T00:00:00Z' WHERE username = 'alice'")
	main.cli_reset_password(["alice"])
	assert sign_in(password="reset from the console")[1].status_code == 302
	with pytest.raises(SystemExit):
		main.cli_reset_password(["nobody"])
	with pytest.raises(SystemExit):
		main.cli_reset_password(["local"])


def test_unknown_commands_are_rejected():
	with pytest.raises(SystemExit):
		main.run_cli_command("bogus", [])

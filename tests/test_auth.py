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
		#Replace the main.SETTINGS dictionary with a new instance of SETTINGS, setting the mode to lan, and adding or updateing other settings as needed
		monkeypatch.setattr(main, "SETTINGS", dataclasses.replace(main.SETTINGS, mode="lan", auth_required=True, auto_shutdown=False, **overrides))

	configure()
	with connect() as connection:
		# Create two users: one admin and one reegular user
		lan.ids = {
			"admin": create_user(connection, "boss", PASSWORD, "admin"),
			"alice": create_user(connection, "alice", PASSWORD),
		}
	#Update the configure function with the created user ids
	configure.ids = lan.ids
	return configure


def sign_in(username="alice", password=PASSWORD, **extra):
	#Create a test client using the main applications test client
	client = main.app.test_client()
	#Perform a POST request to the login endpoint with the provided username and password. Include any extra data passed as keyword args
	response = client.post("/login", data={"username": username, "password": password, **extra})
	return client, response #Return client and responce


def user_row(name):
	#Connect to the database and execute a query to find a user by username
	with connect() as connection:
		return connection.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def test_login_logout_roundtrip(lan):
	#Sign in as a user and store the clien and responce
	client, response = sign_in()
	#Assert that the responce status code is 302 (redirect) and the location header ends with "/"
	assert response.status_code == 302 
	assert response.headers["Location"].endswith("/")
	#Assert that the home page status code is 200 (OK)
	assert client.get("/").status_code == 200
	#Log the user out
	client.post("/logout")
	#Assert that the responce status code is 302 (redirect)
	assert client.get("/").status_code == 302


def test_wrong_password_and_unknown_user_look_identical(lan):
	#Sign in with a wrong password and store the response
	_, wrong = sign_in(password="not the password")
	#Sign in with as an unknown user and store the response
	_, unknown = sign_in(username="nobody")
	# Assert that both reponses have the same status code (401 Unauthorized)
	assert wrong.status_code == unknown.status_code == 401
	#Define a lambda function to strip tokens from the responce data
	strip_token = lambda response: re.sub(rb'nonce="[^"]*"', b"", re.sub(rb'name="csrf_token" value="[^"]*"', b"", response.get_data()))
	# Assert that the stripped down token data for both responces is identical
	assert strip_token(wrong) == strip_token(unknown)


def test_local_placeholder_account_can_never_log_in(lan):
	#Sign in with the local placeholder account and expect a 401 unauthorized status code
	assert sign_in(username="local", password="!")[1].status_code == 401


def test_session_is_regenerated_on_login(lan):
	#Create a test clien and start a session transaction
	client = main.app.test_client()
	with client.session_transaction() as session:
		#Add some initial dataa to the session
		session["junk"] = "from before the login"
		session["csrf_token"] = "old-token"
	#perform a POST request to the login endpoint
	client.post("/login", data={"username": "alice", "password": PASSWORD})
	#Start another session transaction to check the session data
	with client.session_transaction() as session:
		#Assert that the session datat has been cleared and the CSRF token has changed
		assert "junk" not in session 
		assert session["csrf_token"] != "old-token"
		# Assert that the user ID is set in the session
		assert session["user_id"] == lan.ids["alice"]


def test_account_locks_after_repeated_failures_and_recovers(lan):
	#Set up login restrictions and maximum failures for the local user
	lan(login_max_failures=3, login_ip_limit=1000)
	#Attempt to log in 3 times with the wrong password
	for _ in range(3):
		sign_in(password="wrong password!!")
	#Attempt to log in with the wrong password and expect a 401
	assert sign_in()[1].status_code == 401
	#Assert that the account is locked
	assert user_row("alice")["locked_until"]
	#Reset the locked_until date to a past date
	with connect() as connection:
		connection.execute("UPDATE users SET locked_until = '2000-01-01T00:00:00Z' WHERE username = 'alice'")
	# Attempt to log in again and expect a successful login
	assert sign_in()[1].status_code == 302
	#Assert taht the fialed_logins is reset
	assert user_row("alice")["failed_logins"] == 0


def test_ip_rate_limit_returns_429_with_retry_after(lan):
	# set up login restrictions and IP limit for the local user
	lan(login_ip_limit=5)
	#Attempt to log in 5 times from the same IP address
	for _ in range(5):
		sign_in(username="nobody")
	#Attempt to log in again and expect a 429 (Too many Requests) status code with a Retry-After header
	response = sign_in()[1] #Even a correct login is blocked once the address is over the limit
	assert response.status_code == 429
	assert int(response.headers["Retry-After"]) > 0


def test_retry_after_header_is_optional(lan):
	#Set up login restrictions and IP limit for the local user without the Retry-After header
	lan(login_ip_limit=5, retry_after_header=False)
	#Attempt to log in five times from the same IP
	for _ in range(5):
		sign_in(username="nobody")
	#Attempt to log in again and expect a 429 (Too many Requests) status code without a Retry-After header
	response = sign_in()[1]
	assert response.status_code == 429 
	assert "Retry-After" not in response.headers


def test_idle_sessions_time_out(lan):
	#Sign in and start a session transaction
	client, _ = sign_in()
	with client.session_transaction() as session:
		#Set the last seen timestamp to be three hours ago
		session["last_seen"] = int(time.time()) - 3 * 3600
	#Assert that tattemptin to access the home page results in a 302 redirect
	assert client.get("/").status_code == 302
	#Start another session transaction to check the session data
	with client.session_transaction() as session:
		assert "user_id" not in session


def test_remember_me_survives_idle_time_but_not_its_lifetime(lan):
	#Sign in with remember me enables and start a session transaction
	client, _ = sign_in(remember="on")
	with client.session_transaction() as session:
		#assert that remember me is enabled and last seen timestamp is three hours ago
		assert session.permanent
		session["last_seen"] = int(time.time()) - 3 * 3600
	#Assert that attempting to access the home page results in a 200 OK status
	assert client.get("/").status_code == 200
	#Start another session transaction to check the session data
	with client.session_transaction() as session:
		session["issued"] = int(time.time()) - 31 * 86400
	assert client.get("/").status_code == 302


def test_remember_me_can_be_turned_off(lan):
	#Set remember days to 0 and sign in with remember me enabled
	lan(remember_days=0)
	client, _ = sign_in(remember="on")
	#Start a session transaction to check the remember flag
	with client.session_transaction() as session:
		#Assert that remrmver me is not enabled
		assert not session["remember"]


def test_next_parameter_cannot_redirect_off_site(lan):
	#Test various next parameters that should not redirect to external sites
	for target in ("//evil.example", "https://evil.example", "/\\evil.example"):
		_, response = sign_in(next=target)
		#Assert that the redirect is to the home page and does not contian "evil"
		assert response.headers["Location"].endswith("/") 
		assert "evil" not in response.headers["Location"]
	#Test a valid redirect to a specific account page
	_, response = sign_in(next="/account")
	#Assert that the redirect is to the home page and contians "account"
	assert response.headers["Location"].endswith("/account")


def test_login_page_is_not_available_in_local_mode():
	# Test that the login page is not available in local mode
	assert main.app.test_client().get("/login").status_code == 404


def test_changing_password_signs_out_other_sessions(lan):
	#Sign in with a use and another user
	first, _ = sign_in()
	second, _ = sign_in()
	#Change the password for the first user and assert a 302 redirect
	response = first.post("/account/password", data={"current_password": PASSWORD, "new_password": "a brand new passphrase", "confirm_password": "a brand new passphrase"})
	assert response.status_code == 302
	#Assert that the first session remains signed in 
	assert first.get("/").status_code == 200 
	#Assert that the second session is signed out
	assert second.get("/").status_code == 302
	#Attempt to sign in with the new password and asser a 302 redirect
	assert sign_in(password="a brand new passphrase")[1].status_code == 302


def test_changing_password_needs_the_current_one_and_a_valid_new_one(lan):
	#Sign in and attempt to change the password with a wrong current password
	client, _ = sign_in()
	bad_current = client.post("/account/password", data={"current_password": "wrong wrong wrong", "new_password": "another good passphrase", "confirm_password": "another good passphrase"})
	too_short = client.post("/account/password", data={"current_password": PASSWORD, "new_password": "short", "confirm_password": "short"})
	mismatch = client.post("/account/password", data={"current_password": PASSWORD, "new_password": "another good passphrase", "confirm_password": "different one entirely"})
	#Assert that all attempts result in a 400 BadRequest status
	assert bad_current.status_code == too_short.status_code == mismatch.status_code == 400
	#Assert that loggin in with the old password still succeeds
	assert sign_in()[1].status_code == 302


def test_password_policy_is_length_only():
	#Validate a password that meets the length requirement
	validate_password("someone", "alllowercaseletters")
	#Test passwords that are too short
	for bad in ("short", "someone" * 1, "x" * 300):
		with pytest.raises(ValueError):
			validate_password("someone", bad)
	#Test passwords that exceed the maximum length
	with pytest.raises(ValueError):
		validate_password("longusername1", "LongUserName1")


def test_registration_is_closed_by_default(lan):
	#Assert that the registration page is not available by default
	assert main.app.test_client().get("/register").status_code == 404
	# Assert that attempting to register a user is not allowed by default
	assert main.app.test_client().post("/register", data={"username": "mallory", "password": PASSWORD, "confirm_password": PASSWORD}).status_code == 404


def test_open_registration_creates_a_regular_user(lan):
	#Set Registration to open and login IP limit to 1000
	lan(registration="open", login_ip_limit=1000)
	#Create a new user throught registration 
	client = main.app.test_client()
	response = client.post("/register", data={"username": "newbie", "password": PASSWORD, "confirm_password": PASSWORD})
	#Assert that the registration is successful and the use is a regular user
	assert response.status_code == 302
	assert user_row("newbie")["role"] == "user"
	#Attempt to register a user with a username that already exists
	mismatch = client.post("/register", data={"username": "other", "password": PASSWORD, "confirm_password": "nope nope nope"})
	#assert that the registration fails
	assert mismatch.status_code == 400


def test_open_registration_is_rate_limited(lan):
	#Set registration to open and login IP limit to 1000
	lan(registration="open")
	#Create multiple users through registration and test rate limiting
	client = main.app.test_client()
	statuses = [client.post("/register", data={"username": f"spam{i}", "password": PASSWORD, "confirm_password": PASSWORD}).status_code for i in range(12)]
	#Assert that the first 10 registrations are sucessful and the next 2 are rate limited
	assert statuses[:10] == [302] * 10 
	assert statuses[10:] == [429, 429]


def create_invite_token(hours=72):
	# Create and return an invite token for the boss user
	with connect() as connection:
		return auth.create_invite(connection, lan_admin_id(), hours)


def lan_admin_id():
	#Retrieve the ID of the boss user
	return user_row("boss")["id"]


def test_invite_links_work_once(lan):
	#Set the registration to invite and login IP limit to 1000
	lan(registration="invite", login_ip_limit=1000)
	#Create an invite token for the boss user
	token = create_invite_token()
	#Access the registration page with the invite token and register a new user
	client = main.app.test_client()
	assert client.get(f"/register?invite={token}").status_code == 200
	data = {"invite": token, "username": "guest1", "password": PASSWORD, "confirm_password": PASSWORD}
	assert client.post("/register", data=data).status_code == 302
	assert user_row("guest1")["role"] == "user"
	#Attempt to register the same user again with a different username
	data["username"] = "guest2"
	assert client.post("/register", data=data).status_code == 400
	#Assert that the user is not created and the invite token is no longer valid
	assert user_row("guest2") is None
	assert client.get(f"/register?invite={token}").status_code == 404


def test_invalid_expired_and_revoked_invites_are_refused(lan):
	#Set registration to invite and create a client
	lan(registration="invite")
	client = main.app.test_client()
	#Assert that accessing the registration page or with an invalid invite token results in a 404
	assert client.get("/register").status_code == 404
	assert client.get("/register?invite=made-up").status_code == 404
	#Create expired and revoked invite tokens
	expired = create_invite_token()
	revoked = create_invite_token()
	#Update the database to set the expired tokens expired_at to 2000-01-01T00:00:00Z and revoke the revoked token
	with connect() as connection:
		connection.execute("UPDATE invites SET expires_at = '2000-01-01T00:00:00Z' WHERE id = 1")
		auth.revoke_invite(connection, 2)
	#Assert that accessing the registration page with an expired or revoked token results in a 404
	assert client.get(f"/register?invite={expired}").status_code == 404
	assert client.get(f"/register?invite={revoked}").status_code == 404


def test_a_failed_signup_does_not_burn_the_invite(lan):
	#Set registration to invite and create a client
	lan(registration="invite", login_ip_limit=1000)
	token = create_invite_token()
	client = main.app.test_client()
	#Attempt a failed signup with the invite token
	client.post("/register", data={"invite": token, "username": "x", "password": PASSWORD, "confirm_password": PASSWORD})
	#Assert that the registrration page with the invite token still exists
	assert client.get(f"/register?invite={token}").status_code == 200


def test_invites_are_stored_hashed(lan):
	#Create an invite token and verify that it is stored in the database as a hash
	token = create_invite_token()
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM invites WHERE token_hash = ?", (token,)).fetchone()[0] == 0


def test_admin_pages_are_admin_only(lan):
	#Sign in a Alice and attempt to access admin pages, expecting 403 Forbidden
	alice, _ = sign_in()
	assert alice.get("/admin/users").status_code == 403
	assert alice.post("/admin/users/create", data={"username": "sneaky", "password": PASSWORD, "role": "admin"}).status_code == 403
	assert alice.post(f"/admin/users/{lan.ids['alice']}/role", data={"role": "admin"}).status_code == 403
	#Assert that Alice is not able to create or modify users or change their roles
	assert user_row("sneaky") is None 
	assert user_row("alice")["role"] == "user"


def test_admin_creates_users_and_changes_roles(lan):
	#Sign in as the boss and create a new user named Carol
	admin, _ = sign_in("boss")
	assert admin.get("/admin/users").status_code == 200
	assert admin.post("/admin/users/create", data={"username": "carol", "password": PASSWORD, "role": "user"}).status_code == 302
	#Retrieve carols user row to confirm the role has been updated
	carol = user_row("carol")
	assert admin.post(f"/admin/users/{carol['id']}/role", data={"role": "admin"}).status_code == 302
	assert user_row("carol")["role"] == "admin"
	#Attempt to create a duplicate upser named carol and confirm it fails
	assert admin.post("/admin/users/create", data={"username": "carol", "password": PASSWORD}).status_code == 400
	#Attempt to create a user with a short password and confirm it fails
	assert admin.post("/admin/users/create", data={"username": "dave", "password": "short"}).status_code == 400


def test_admin_cannot_demote_or_disable_themselves(lan):
	#Sign in as the boss and attempt to demote or disable themselves, expecting 400 Bad request
	admin, _ = sign_in("boss")
	assert admin.post(f"/admin/users/{lan.ids['admin']}/role", data={"role": "user"}).status_code == 400
	assert admin.post(f"/admin/users/{lan.ids['admin']}/status", data={"action": "disable"}).status_code == 400
	#Verify that the boss's role and disabled status remain unchanged
	assert user_row("boss")["role"] == "admin" 
	assert not user_row("boss")["disabled"]


def test_disabling_a_user_signs_them_out_and_blocks_login(lan):
	#Sign in as the boss and disable alice, sign her out and attempt to log in, expecting 401 Unauthorized
	admin, _ = sign_in("boss")
	alice, _ = sign_in()
	assert admin.post(f"/admin/users/{lan.ids['alice']}/status", data={"action": "disable"}).status_code == 302
	assert alice.get("/").status_code == 302
	assert sign_in()[1].status_code == 401
	#Enabpe alice and sign in again expecting 302 Redirect
	admin.post(f"/admin/users/{lan.ids['alice']}/status", data={"action": "enable"})
	assert sign_in()[1].status_code == 302


def test_admin_password_reset_unlocks_and_signs_out(lan):
	#Simulate a login failure
	lan(login_max_failures=3, login_ip_limit=1000)
	#Sign in as Alice
	alice, _ = sign_in()
	#Attempt to sign in three times with wrong passwords
	for _ in range(3):
		sign_in(password="wrong password!!")
	#Sign in as boss
	admin, _ = sign_in("boss")
	#Reset alice's password
	assert admin.post(f"/admin/users/{lan.ids['alice']}/password", data={"password": "reset by the admin"}).status_code == 302
	#Ensure alice is redirected to the login page after passwork reset
	assert alice.get("/").status_code == 302
	#Ensure the admin's login is redirected after password reset
	assert sign_in(password="reset by the admin")[1].status_code == 302


def test_admin_creates_and_revokes_invites_only_in_invite_mode(lan):
	#sign in as boss
	admin, _ = sign_in("boss")
	#Attempt to create an invite in local mode
	assert admin.post("/admin/invites/create", data={"hours": "24"}).status_code == 404
	#Set registration mode to invite
	lan(registration="invite")
	#Boss sign in
	admin, _ = sign_in("boss")
	#Attempt to create an invite creation is successful
	response = admin.post("/admin/invites/create", data={"hours": "24"})
	#Verif that the invite creation is successful
	assert response.status_code == 200 
	assert b"/register?invite=" in response.data
	#Attempt to create an invite with 0 hrs
	assert admin.post("/admin/invites/create", data={"hours": "0"}).status_code == 400
	# Revoke the invite
	assert admin.post("/admin/invites/1/revoke").status_code == 302
	#Query the invites table to ensure the invite is revoked
	with connect() as connection:
		assert connection.execute("SELECT revoked FROM invites WHERE id = 1").fetchone()[0] == 1


def test_user_management_does_not_exist_in_local_mode():
	#create a test client
	client = main.app.test_client()
	#Attempt to access user managemnet pages in local mode
	assert client.get("/admin/users").status_code == 404
	assert client.get("/account").status_code == 404


def test_create_admin_command_adopts_existing_local_data(monkeypatch):
	#insert a product into the database
	with connect() as connection:
		connection.execute("INSERT INTO products (user_id, url, source) VALUES (?, 'https://www.walmart.com/ip/x/1', 'Walmart')", (local_user_id(connection),))
	#Set the environment variable for the admin password
	monkeypatch.setenv("PRICE_CHECKER_ADMIN_PASSWORD", PASSWORD)
	# Verify that the admin does not exist
	assert not admin_exists()
	# Create an admin using the command line interface
	main.cli_create_admin(["firstadmin"])
	#Verify that the admin exists
	assert admin_exists()
	#Query the database to ensure the admin is created with the correct username
	with connect() as connection:
		owner = connection.execute("SELECT u.username FROM products p JOIN users u ON u.id = p.user_id").fetchone()[0]
	#Verify that the owner is 'firstadmin'
	assert owner == "firstadmin"
	#Create another admin using he CLI
	main.cli_create_admin(["secondadmin"]) #Only the first account adopts the old data
	with connect() as connection:
		assert connection.execute("SELECT COUNT(*) FROM products p JOIN users u ON u.id = p.user_id WHERE u.username = 'secondadmin'").fetchone()[0] == 0


def test_create_admin_command_rejects_bad_input(monkeypatch):
	#Set the environment variables for the admin password to a short password
	monkeypatch.setenv("PRICE_CHECKER_ADMIN_PASSWORD", "short")
	#Verify that creating an admin with a short password raises a SystemExit
	with pytest.raises(SystemExit):
		main.cli_create_admin(["someadmin"])
	# Reset the environment variable to the correct Password
	monkeypatch.setenv("PRICE_CHECKER_ADMIN_PASSWORD", PASSWORD)
	#Verify that creating an admin with 'local' raises a SystemExit
	with pytest.raises(SystemExit):
		main.cli_create_admin(["local"])
	#Verify that the admin does not exist
	assert not admin_exists()


def test_reset_password_command(lan, monkeypatch):
	#Set the environment variable for the admi password
	monkeypatch.setenv("PRICE_CHECKER_ADMIN_PASSWORD", "reset from the console")
	#Lock Alice's account
	with connect() as connection:
		connection.execute("UPDATE users SET locked_until = '2999-01-01T00:00:00Z' WHERE username = 'alice'")
	#Reset Alice's password using the CLI
	main.cli_reset_password(["alice"])
	#Verify that alice can log in with the new password
	assert sign_in(password="reset from the console")[1].status_code == 302
	#Verify that resetting the password for a non-existent user raises a SystemExit
	with pytest.raises(SystemExit):
		main.cli_reset_password(["nobody"])
	#Verify that resetting the password for 'local' raises a System Exit
	with pytest.raises(SystemExit):
		main.cli_reset_password(["local"])


def test_unknown_commands_are_rejected():
	#Attempt to run an unknown command and verify that it raises a SystemExit
	with pytest.raises(SystemExit):
		main.run_cli_command("bogus", [])
